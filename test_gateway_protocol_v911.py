"""Protocol upgrade coverage using only synthetic HTTP/WebSocket fixtures."""
import base64
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import socket
import struct
import threading
import time
from urllib.request import urlopen

import pytest

import agent_manager_core as core
import web2api_service as gateway
import web2api_websocket as ws


def sse(*events):
    return b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode() for event in events)


def final(response_id="resp-one", output=None):
    return {"type": "response.completed", "response": {"id": response_id, "object": "response", "status": "completed",
        "output": output or [], "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}}}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "load_settings", lambda: {"accounts": [], "web2api": {"accountIds": [], "providerIds": ["provider"]}})
    monkeypatch.setattr(core, "load_service_secret", lambda name, **_kwargs: "public-secret" if name == "web2api" else "internal-secret")
    monkeypatch.setattr(core, "service_secret_configured", lambda _name: True)
    monkeypatch.setattr(core, "_codex_client_version", lambda: "test")
    monkeypatch.setattr(core, "resolve_model_route", lambda model, **_kwargs: {"id": "gpt-test", "sourceKind": "provider", "sourceRecordId": "provider"})
    obj = gateway.Web2APIManager()
    obj.subagent_keys_cache_at = time.monotonic()
    yield obj
    obj.usage_stats.flush(force=True)
    if obj.usage_stats.flush_timer:
        obj.usage_stats.flush_timer.cancel()


@pytest.fixture
def backend(manager, monkeypatch):
    requests = []
    release = threading.Event()
    started = threading.Event()
    mode = {"pause": False, "truncate": False}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload, dict(self.headers)))
            if self.path.endswith("/input_tokens"):
                body = b'{"object":"response.input_tokens","input_tokens":123}'
                content_type = "application/json"
            elif self.path.endswith("/compact"):
                body = b'{"object":"response.compaction","output":[{"type":"compaction","encrypted_content":"opaque"}]}'
                content_type = "application/json"
            else:
                body = sse({"type": "response.created", "response": {"id": f"resp-{len(requests)}"}},
                           {"type": "response.output_text.delta", "delta": "hello"})
                if not mode["truncate"]:
                    body += sse(final(f"resp-{len(requests)}"))
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if not mode["pause"]:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode["pause"]:
                # Start a response and leave its network read pending until the
                # client's socket closure interrupts the gateway's upstream.
                self.wfile.write(sse({"type": "response.created", "response": {"id": "paused"}},
                                     {"type": "response.output_text.delta", "delta": "first"}))
                self.wfile.flush()
                started.set()
                release.wait(5)
                return
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    base_url = f"http://127.0.0.1:{upstream.server_port}/v1"
    provider = {"id": "provider", "name": "Fixture", "baseUrl": base_url}
    secret = {"value": "provider-secret"}
    monkeypatch.setattr(core, "provider_by_id", lambda _id: provider)
    monkeypatch.setattr(core, "load_provider_key", lambda *_args, **_kwargs: secret["value"])
    monkeypatch.setattr(core, "_provider_runtime_base_url", lambda value: value["baseUrl"])
    monkeypatch.setattr(core, "_open_same_origin_request", lambda request, timeout: urlopen(request, timeout=timeout))
    server = gateway.GatewayServer(("127.0.0.1", 0), manager)
    gateway_worker = threading.Thread(target=server.serve_forever, daemon=True)
    gateway_worker.start()
    yield {"manager": manager, "server": server, "requests": requests, "provider": provider,
           "secret": secret, "mode": mode, "started": started}
    release.set()
    server.shutdown()
    server.server_close()
    upstream.shutdown()
    upstream.server_close()


def post(backend, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", backend["server"].server_port, timeout=3)
    conn.request("POST", path, json.dumps(payload), {"Authorization": "Bearer public-secret", "Content-Type": "application/json"})
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


class Client:
    def __init__(self, backend, *, authorization="public-secret", extra=""):
        self.sock = socket.create_connection(backend["server"].server_address, timeout=3)
        self.stream = self.sock.makefile("rb")
        port = backend["server"].server_port
        request = (f"GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Authorization: Bearer {authorization}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n{extra}\r\n")
        self.sock.sendall(request.encode())
        self.status = int(self.stream.readline().split()[1])
        self.headers = {}
        while True:
            line = self.stream.readline()
            if line == b"\r\n":
                break
            key, _, value = line.decode().partition(":")
            self.headers[key.lower()] = value.strip()

    def send(self, value, *, opcode=1, final=True, masked=True):
        payload = json.dumps(value).encode() if isinstance(value, dict) else value
        frame = ws._encoded_frame(opcode, payload)
        first = frame[0] if final else frame[0] & 127
        if not masked:
            self.sock.sendall(bytes([first]) + frame[1:])
            return
        header_size = 2 if len(payload) < 126 else 4 if len(payload) < 65536 else 10
        mask = b"test"
        frame = bytes([first, frame[1] | 128]) + frame[2:header_size] + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(frame)

    def receive(self):
        first, size = self.stream.read(2)
        if size == 126:
            size = struct.unpack("!H", self.stream.read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.stream.read(8))[0]
        return first & 15, self.stream.read(size)

    def terminal(self):
        events = []
        for _ in range(20):
            opcode, raw = self.receive()
            assert opcode == 1
            event = json.loads(raw)
            events.append(event)
            if event["type"] in {"response.completed", "response.failed", "response.incomplete", "error"}:
                return events
        raise AssertionError("no terminal event")

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.stream.close()
        self.sock.close()


def test_responses_preserve_instruction_items_and_continuation():
    instructions = [{"role": "developer", "content": [{"type": "input_text", "text": "rules"}]}]
    result = gateway._responses_input({"model": "gpt-test", "previous_response_id": "r", "instructions": instructions})
    assert result["instructions"] == instructions
    assert "input" not in result
    raw = {"model": "gpt-test", "input": [{"type": "reasoning", "encrypted_content": "opaque"}, {"type": "compaction_trigger"}]}
    assert gateway._responses_input(raw)["input"] == raw["input"]


def test_chat_preserves_reasoning_schema_tools_and_usage():
    payload = {"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "high",
               "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": {"type": "object"}}}}
    converted = gateway._chat_input(payload)
    assert converted["reasoning"] == {"effort": "high"}
    assert converted["text"]["format"] == {"type": "json_schema", "name": "answer", "strict": True, "schema": {"type": "object"}}
    output = [{"type": "reasoning", "encrypted_content": "never-expose", "summary": [{"type": "summary_text", "text": "brief reason"}]},
              {"type": "function_call", "id": "item1", "call_id": "call1", "name": "tool", "arguments": '{"x":1}'}]
    response = final(output=output)["response"]
    chat = gateway._chat_completion(response, "gpt-test")
    assert chat["choices"][0]["message"]["reasoning_content"] == "brief reason"
    assert chat["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    assert "never-expose" not in json.dumps(chat)
    stream = gateway._chat_sse(sse(final(output=output)), "gpt-test", include_usage=True)
    events = gateway._sse_events(stream)
    assert any(event.get("usage", {}).get("total_tokens") == 14 and event["choices"] == [] for event in events)
    calls = [call for event in events for choice in event["choices"] for call in choice["delta"].get("tool_calls", [])]
    assert calls[0]["function"]["arguments"] == '{"x":1}'
    assert "never-expose" not in stream.decode()


def test_done_event_completes_function_arguments_once():
    item = {"type": "function_call", "id": "item1", "call_id": "call1", "name": "tool", "arguments": ""}
    completed_item = {**item, "arguments": '{"x":1}'}
    body = sse({"type": "response.output_item.added", "item": item},
               {"type": "response.function_call_arguments.delta", "item_id": "item1", "delta": '{"x":'},
               {"type": "response.output_item.done", "item": completed_item}, final(output=[completed_item]))
    events = gateway._sse_events(gateway._chat_sse(body, "gpt-test"))
    calls = [call for event in events for choice in event["choices"] for call in choice["delta"].get("tool_calls", [])]
    assert "".join(call["function"].get("arguments", "") for call in calls) == '{"x":1}'
    assert len([call for call in calls if call.get("id")]) == 1


def test_auxiliary_provider_http_roundtrip_has_no_token_count_generation_charge(backend):
    status, body = post(backend, "/v1/responses/input_tokens", {"model": "alias", "input": "hello"})
    assert status == 200 and body["input_tokens"] == 123
    assert backend["manager"].usage_stats.snapshot()["totals"]["requestCount"] == 0
    status, body = post(backend, "/v1/responses/compact", {"model": "alias", "input": []})
    assert status == 200 and body["output"][0]["encrypted_content"] == "opaque"
    assert backend["requests"][0][0] == "/v1/responses/input_tokens"
    assert backend["requests"][0][1]["model"] == "gpt-test"
    headers = {key.casefold(): value for key, value in backend["requests"][0][2].items()}
    assert headers["authorization"] == "Bearer provider-secret"
    assert "chatgpt-account-id" not in headers


def test_oauth_legacy_compact_uses_native_trigger_and_preserves_opaque_output(manager, monkeypatch):
    monkeypatch.setattr(core, "resolve_model_route", lambda *_args, **_kwargs: None)
    requests = []
    def upstream(payload, **_kwargs):
        requests.append(payload)
        return sse(final(output=[{"type": "compaction", "encrypted_content": "opaque-state"}]))
    monkeypatch.setattr(manager, "_upstream", upstream)
    result = manager.execute("/v1/responses/compact", {"model": "gpt-test", "input": [{"role": "user", "content": "history"}]})
    assert requests[0]["input"][-1] == {"type": "compaction_trigger"}
    assert json.loads(result["body"])["output"] == [{"type": "compaction", "encrypted_content": "opaque-state"}]
    assert "_onDelivered" not in result
    with pytest.raises(gateway.GatewayError) as error:
        manager.execute("/v1/responses/input_tokens", {"model": "gpt-test", "input": "text"})
    assert error.value.status == 501
    assert len(requests) == 1


@pytest.mark.parametrize("change", ["key", "host"])
def test_same_provider_id_changed_credentials_or_host_cannot_receive_continuation(backend, change):
    status, body = post(backend, "/v1/responses", {"model": "alias", "input": "first"})
    assert status == 200
    response_id = gateway._completed_response(json.dumps(body).encode())["id"] if "id" in body else None
    assert response_id == "resp-1"
    if change == "key":
        backend["secret"]["value"] = "replaced-secret"
    else:
        backend["provider"]["baseUrl"] = "http://127.0.0.1:1/v1"
    status, _ = post(backend, "/v1/responses", {"model": "alias", "input": "followup", "previous_response_id": response_id})
    assert status == 409 and len(backend["requests"]) == 1


def test_oauth_fingerprint_preserves_subject_across_refresh():
    def token(sub, nonce):
        return "e30." + base64.urlsafe_b64encode(json.dumps({"sub": sub, "nonce": nonce}).encode()).decode().rstrip("=") + ".sig"
    first = gateway._oauth_binding_fingerprint({"accountId": "workspace", "accessToken": token("user", 1)})
    second = gateway._oauth_binding_fingerprint({"accountId": "workspace", "accessToken": token("user", 2)})
    assert first == second
    assert first != gateway._oauth_binding_fingerprint({"accountId": "workspace", "accessToken": token("other", 2)})


def test_conflicting_response_ids_are_ambiguous_instead_of_rebound(manager):
    for identity in ("first", "second"):
        manager._remember_response_binding("same", access_scope="public", requested_model="m", routed_model="m", identity_kind="account", identity_id=identity)
    with pytest.raises(gateway.GatewayError) as error:
        manager._resolve_response_binding({"previous_response_id": "same"}, access_scope="public", requested_model="m", route=None)
    assert error.value.status == 409


def test_websocket_two_turns_preserve_events_bind_identity_and_use_http_upstream(backend):
    client = Client(backend)
    try:
        assert client.status == 101
        client.send({"type": "response.create", "model": "alias", "input": "first"})
        events = client.terminal()
        assert any(event.get("delta") == "hello" for event in events)
        response_id = events[-1]["response"]["id"]
        assert backend["manager"]._lookup_response_binding(response_id)
        client.send({"type": "response.create", "model": "alias", "previous_response_id": response_id, "input": [{"type": "function_call_output", "call_id": "c", "output": "result"}]})
        assert client.terminal()[-1]["type"] == "response.completed"
        assert backend["requests"][-1][1]["previous_response_id"] == response_id
        assert len(backend["requests"]) == 2
    finally:
        client.close()


@pytest.mark.parametrize("extra,authorization,status", [("", "wrong", 401), ("Origin: https://evil.example\r\n", "public-secret", 403), ("Sec-WebSocket-Version: 12\r\n", "public-secret", 400), ("Authorization: Bearer public-secret\r\n", "public-secret", 401)])
def test_websocket_handshake_fail_closed_before_upstream(backend, extra, authorization, status):
    client = Client(backend, extra=extra, authorization=authorization)
    try:
        assert client.status == status
        assert backend["requests"] == []
    finally:
        client.close()


@pytest.mark.parametrize("unsupported", [{"stream_id": "lane"}, {"generate": False}, {"background": True}])
def test_websocket_does_not_fake_native_capabilities(backend, unsupported):
    client = Client(backend)
    try:
        client.send({"type": "response.create", "model": "alias", "input": "text", **unsupported})
        assert client.terminal()[-1]["type"] == "error"
        assert backend["requests"] == []
    finally:
        client.close()


def test_websocket_fragmentation_ping_and_mask_validation(backend):
    client = Client(backend)
    try:
        raw = json.dumps({"type": "response.create", "model": "alias", "input": "text"}).encode()
        client.send(raw[:15], final=False)
        client.send(b"ping", opcode=9)
        assert client.receive() == (10, b"ping")
        client.send(raw[15:], opcode=0)
        assert client.terminal()[-1]["type"] == "response.completed"
        client.send(b"{}", masked=False)
        assert client.receive() == (8, struct.pack("!H", 1002))
    finally:
        client.close()


def test_websocket_disconnect_interrupts_pending_upstream_read(backend):
    backend["mode"]["pause"] = True
    backend["server"]._upstream_slots = threading.BoundedSemaphore(1)
    client = Client(backend)
    client.send({"type": "response.create", "model": "alias", "input": "text"})
    assert backend["started"].wait(2)
    client.close()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if backend["manager"].client_cancelled_count:
            break
        time.sleep(0.01)
    assert backend["manager"].client_cancelled_count == 1
    # Shutdown propagates through urllib's socket to free the upstream slot.
    acquired = backend["server"]._upstream_slots.acquire(timeout=2)
    assert acquired
    backend["server"]._upstream_slots.release()


def test_websocket_truncation_is_terminal_error_and_never_replayed(backend):
    backend["mode"]["truncate"] = True
    client = Client(backend)
    try:
        client.send({"type": "response.create", "model": "alias", "input": "text"})
        assert client.terminal()[-1]["type"] == "error"
        assert len(backend["requests"]) == 1
        assert backend["manager"]._lookup_response_binding("resp-1") is None
    finally:
        client.close()


def test_frame_size_is_rejected_before_reading_payload():
    raw = bytes([0x81, 0xff]) + struct.pack("!Q", ws.MAX_MESSAGE_BYTES + 1)
    with pytest.raises(ws.ProtocolError) as error:
        ws._frame(io.BytesIO(raw))
    assert error.value.code == 1009


def test_prefixed_read1_does_not_skip_capacity_probe_bytes():
    response = gateway._PrefixedResponse(io.BytesIO(b"tail"), b"prefix")
    assert response.read1(4) == b"pref"
    assert response.read1(4) == b"ix"
    assert response.read1(4) == b"tail"


def test_capabilities_are_public_safe_and_explicit_about_ws_semantics(manager):
    capabilities = manager.status("public")["protocolCapabilities"]
    assert capabilities["transports"]["websocketMode"] == "serial_http_sse_bridge"
    assert capabilities["transports"]["nativeUpstreamWebsocket"] is False
    assert capabilities["endpoints"]["inputTokens"]["account"] == "unsupported"
    assert "secret" not in json.dumps(capabilities)


def test_websocket_rejects_overlapping_turn_without_second_upstream(backend):
    backend["mode"]["pause"] = True
    client = Client(backend)
    try:
        client.send({"type": "response.create", "model": "alias", "input": "first"})
        assert backend["started"].wait(2)
        client.send({"type": "response.create", "model": "alias", "input": "second"})
        assert client.terminal()[-1]["status"] == 409
        assert len(backend["requests"]) == 1
    finally:
        client.close()


def test_websocket_continuation_honors_credential_fingerprint(backend):
    client = Client(backend)
    try:
        client.send({"type": "response.create", "model": "alias", "input": "first"})
        response_id = client.terminal()[-1]["response"]["id"]
        backend["secret"]["value"] = "new-key"
        client.send({"type": "response.create", "model": "alias", "previous_response_id": response_id, "input": "next"})
        assert client.terminal()[-1]["status"] == 409
        assert len(backend["requests"]) == 1
    finally:
        client.close()


def test_auxiliary_stream_request_never_reaches_generation(backend):
    status, _ = post(backend, "/v1/responses/input_tokens", {"model": "alias", "input": "first", "stream": True})
    assert status == 400 and backend["requests"] == []


def test_native_compaction_without_compaction_item_counts_one_failure(manager, monkeypatch):
    monkeypatch.setattr(core, "resolve_model_route", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_upstream", lambda *_args, **_kwargs: sse(final()))
    with pytest.raises(gateway.GatewayError) as error:
        manager.execute("/v1/responses/compact", {"model": "gpt-test", "input": []})
    assert error.value.status == 502
    totals = manager.usage_stats.snapshot()["totals"]
    assert totals["requestCount"] == totals["failureCount"] == 1
    assert totals["inputTokens"] == 10


def test_json_upstream_is_converted_to_single_terminal_sse_without_loss():
    class Response(io.BytesIO):
        headers = {"Content-Type": "application/json"}
    value = final(output=[{"type": "reasoning", "encrypted_content": "opaque"}])["response"]
    raw = b"".join(gateway.Web2APIManager._response_stream_chunks(Response(json.dumps(value).encode())))
    assert gateway._sse_events(raw) == [{"type": "response.completed", "response": value}]


def test_websocket_concurrency_limit_is_shared_with_http_requests(backend, monkeypatch):
    monkeypatch.setattr(gateway, "UPSTREAM_QUEUE_TIMEOUT_SECONDS", 0.05)
    backend["server"]._upstream_slots = threading.BoundedSemaphore(1)
    backend["server"]._upstream_slots.acquire()
    client = Client(backend)
    try:
        client.send({"type": "response.create", "model": "alias", "input": "text"})
        assert client.terminal()[-1]["status"] == 429
        assert backend["requests"] == []
        assert backend["manager"].rejected_request_count == 1
    finally:
        backend["server"]._upstream_slots.release()
        client.close()


def test_websocket_terminal_releases_capacity_before_immediate_next_turn(backend, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    backend["server"]._upstream_slots = slots
    encoded = ws._encoded_frame
    available_at_terminal = []
    def inspect_terminal(opcode, data):
        if opcode == 1 and json.loads(data).get("type") == "response.completed":
            available = slots.acquire(blocking=False)
            available_at_terminal.append(available)
            if available:
                slots.release()
        return encoded(opcode, data)
    monkeypatch.setattr(ws, "_encoded_frame", inspect_terminal)
    client = Client(backend)
    try:
        client.send({"type":"response.create","model":"alias","input":"first"})
        response_id = client.terminal()[-1]["response"]["id"]
        assert available_at_terminal == [True]
        client.send({"type":"response.create","model":"alias","previous_response_id":response_id,"input":"next"})
        assert client.terminal()[-1]["type"] == "response.completed"
        assert available_at_terminal == [True, True]
    finally:
        client.close()


def test_same_account_record_changed_workspace_rejects_before_network(manager, monkeypatch):
    monkeypatch.setattr(manager, "_accounts", lambda *_args, **_kwargs: [{"id": "same-account"}])
    monkeypatch.setattr(core, "_account_chatgpt_credentials", lambda *_args, **_kwargs: {"accountId": "new-workspace", "accessToken": "new-token"})
    opened = []
    monkeypatch.setattr(core, "_open_same_origin_request", lambda *_args, **_kwargs: opened.append(True))
    expected = gateway._oauth_binding_fingerprint({"accountId": "old-workspace", "accessToken": "old-token"})
    with pytest.raises(gateway.GatewayError) as error:
        manager._open_upstream({"model": "gpt-test", "input": "next", "previous_response_id": "r"}, {"same-account"}, expected_identity=expected)
    assert error.value.status == 409 and opened == []
