"""Real loopback HTTP acceptance for scheduling; no real accounts or model calls."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.request import urlopen

import pytest

import agent_manager.core as core
import agent_manager.gateway.service as gateway
from agent_manager.gateway.scheduling import Scheduler, SessionStateError


def sse(*events):
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


@pytest.fixture(params=["account", "provider"])
def pool(request, tmp_path, monkeypatch):
    kind = request.param
    accounts = [{"id": name, "label": name, "authMode": "chatgpt", "codexCompatible": True,
                 "models": ["gpt-test", "gpt-other"]} for name in ("first", "second")]
    settings = {"accounts": accounts, "web2api": {"enabled": True, "routing": "round_robin",
        "accountIds": ["first", "second"] if kind == "account" else [],
        "providerIds": ["first", "second"] if kind == "provider" else []}}
    records = [{"id": model, "sourceKind": kind, "sourceRecordId": name,
                "sourceId": f"{kind}:{name}", "key": f"{kind}:{name}:{model}", "available": True}
               for name in ("first", "second") for model in ("gpt-test", "gpt-other")]
    observed = []
    modes = {}
    secrets = {name: "fake-" + name for name in ("first", "second")}
    release = threading.Event()
    started = threading.Event()

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            identity = self.headers.get("ChatGPT-Account-Id") or self.headers["Authorization"].split("fake-")[-1]
            observed.append((identity, payload, dict(self.headers)))
            mode = modes.get(identity, "success")
            if mode == "429":
                body = b'{"error":{"type":"rate_limit_exceeded","message":"fixture limited"}}'
                self.send_response(429)
                self.send_header("Retry-After", "1800")
                self.send_header("Content-Type", "application/json")
            else:
                error = {"type": "response.failed", "response": {"status": "failed", "error": {
                    "type": "server_error", "code": "server_is_overloaded", "message": "The model is overloaded. Please try again later."}}}
                rid = "resp-" + str(len(observed))
                output = [{"type": "function_call", "id": "item-tool", "call_id": "call-tool",
                           "name": "lookup", "arguments": '{"q":"hello"}'}] if mode == "tools" else []
                final = {"type": "response.completed", "response": {"id": rid, "object": "response", "status": "completed",
                    "model": payload["model"], "output": output, "service_tier": "priority",
                    "usage": {"input_tokens": 300000, "input_tokens_details": {"cached_tokens": 200000,
                    "cache_write_tokens": 10000}, "output_tokens": 4, "total_tokens": 300004}}}
                preamble = sse({"type": "response.created", "response": {"id": rid}})
                delta = sse({"type": "response.output_text.delta", "delta": "hello"})
                if mode == "capacity":
                    body = preamble + sse(error)
                elif mode == "usage_then_capacity":
                    error["response"].update(model=payload["model"], service_tier="default", usage=final["response"]["usage"])
                    body = preamble + sse(error)
                elif mode == "reasoning_then_capacity":
                    body = preamble + sse({"type": "response.reasoning_summary_text.delta", "delta": "thinking"}, error)
                elif mode == "output_then_capacity":
                    body = preamble + delta + sse(error)
                elif mode == "tools":
                    body = preamble + sse({"type": "response.output_item.added", "output_index": 0, "item": output[0]},
                                          {"type": "response.output_item.done", "output_index": 0, "item": output[0]}, final)
                else:
                    body = preamble + delta + (b"" if mode == "truncate" else sse(final))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "hold":
                offset = body.index(b"response.completed")
                boundary = body.rfind(b"data: ", 0, offset)
                self.wfile.write(body[:boundary])
                self.wfile.flush()
                started.set()
                release.wait(5)
                body = body[boundary:]
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{upstream.server_port}"
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "load_settings", lambda: settings)
    monkeypatch.setattr(core, "_all_model_records", lambda _settings: [dict(row) for row in records])
    monkeypatch.setattr(core, "load_service_secret", lambda name, **kw: "public-secret" if name == "web2api" else "private-secret")
    monkeypatch.setattr(core, "service_secret_configured", lambda _name: True)
    monkeypatch.setattr(core, "_codex_client_version", lambda: "fixture")
    monkeypatch.setattr(core, "_account_chatgpt_credentials", lambda identity, **kw: {
        "accessToken": secrets[identity], "accountId": identity})
    monkeypatch.setattr(core, "provider_by_id", lambda identity: {"id": identity, "name": identity, "baseUrl": base + "/v1"})
    monkeypatch.setattr(core, "load_provider_key", lambda identity, **kw: secrets[identity])
    monkeypatch.setattr(core, "_provider_runtime_base_url", lambda provider: provider["baseUrl"])
    monkeypatch.setattr(core, "_open_same_origin_request", lambda req, timeout: urlopen(req, timeout=timeout))
    monkeypatch.setattr(gateway, "UPSTREAM_RESPONSES_URL", base + "/oauth/responses")
    manager = gateway.Web2APIManager()
    server = gateway.GatewayServer(("127.0.0.1", 0), manager)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(payload=None, headers=None, path="/v1/responses"):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
        conn.request("POST", path, json.dumps(payload or {"model": "gpt-test", "input": "hello", "reasoning": {"effort": "xhigh"}}),
                     {"Authorization": "Bearer public-secret", "Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.headers)
        conn.close()
        return result

    yield {"kind": kind, "settings": settings, "records": records, "observed": observed, "modes": modes,
           "secrets": secrets, "manager": manager, "post": post, "release": release, "started": started}
    release.set()
    server.shutdown()
    server.server_close()
    upstream.shutdown()
    upstream.server_close()
    manager.usage_stats.flush(force=True)
    if manager.usage_stats.flush_timer:
        manager.usage_stats.flush_timer.cancel()


@pytest.mark.parametrize("pool", ["provider"], indirect=True)
def test_generic_api_route_does_not_probe_codex_for_its_user_agent(pool, monkeypatch):
    monkeypatch.setattr(core, "_codex_client_version", lambda: pytest.fail("A generic API route must not require Codex"))
    assert pool["post"]()[0] == 200
    assert pool["observed"][0][2]["User-Agent"] == "AgentManager/" + gateway.MANAGER_VERSION


def test_reported_usage_before_failure_is_counted_without_replay(pool):
    pool["settings"]["web2api"]["routing"] = "ordered"
    pool["modes"]["first"] = "usage_then_capacity"
    assert pool["post"]()[0] != 200
    assert [row[0] for row in pool["observed"]] == ["first"]
    totals = pool["manager"].usage_stats.snapshot()["totals"]
    assert totals["inputTokens"] == 300000 and totals["outputTokens"] == 4
    assert totals["failureCount"] == 1
    assert not pool["manager"].scheduler.inflight


def test_session_stays_on_identity_and_rejects_changed_credentials(pool):
    headers = {"Session-Id": "same-thread"}
    assert pool["post"](headers=headers)[0] == 200
    assert pool["post"]()[0] == 200
    assert pool["post"](headers={"X-Session-ID": "same-thread"})[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second", "first"]
    pool["secrets"]["first"] = "replaced-credential"
    assert pool["post"](headers=headers)[0] == 409
    assert len(pool["observed"]) == 3
    assert not pool["manager"].scheduler.inflight


def test_http_rejection_fails_over_and_honors_long_deadline(pool):
    pool["settings"]["web2api"]["routing"] = "ordered"
    pool["modes"]["first"] = "429"
    assert pool["post"]()[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second"]
    identity = "provider:first" if pool["kind"] == "provider" else "first"
    assert pool["manager"]._account_cooldown_remaining(identity, "gpt-test") > 1700
    assert pool["post"]()[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second", "second"]
    assert all(row[1]["model"] == "gpt-test" and row[1]["reasoning"]["effort"] == "xhigh" for row in pool["observed"])


def test_capacity_only_before_any_output_can_change_identity(pool):
    pool["settings"]["web2api"]["routing"] = "ordered"
    pool["modes"]["first"] = "capacity"
    assert pool["post"]()[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second"]
    pool["manager"].account_cooldowns.clear()
    pool["modes"]["first"] = "output_then_capacity"
    before = len(pool["observed"])
    result = pool["post"]({"model": "gpt-test", "input": "hello", "stream": True})
    assert result[0] == 200 and b"hello" in result[1] and b"response.failed" in result[1]
    assert len(pool["observed"]) == before + 1
    assert not pool["manager"].scheduler.inflight


def test_response_continuation_preserves_original_pool_identity(pool):
    first = pool["post"]()
    rid = json.loads(first[1])["id"]
    assert pool["post"]()[0] == 200
    assert pool["post"]({"model": "gpt-test", "input": "continue", "previous_response_id": rid})[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second", "first"]
    pool["modes"]["first"] = "429"
    assert pool["post"]({"model": "gpt-test", "input": "continue", "previous_response_id": rid})[0] == 429
    assert pool["observed"][-1][0] == "first"
    before = len(pool["observed"])
    assert pool["post"]({"model": "gpt-test", "previous_response_id": "unknown", "input": "continue"})[0] == 409
    assert len(pool["observed"]) == before


def test_stream_load_lease_and_model_shards(pool):
    pool["modes"]["first"] = "hold"
    result = []
    thread = threading.Thread(target=lambda: result.append(pool["post"]({"model": "gpt-test", "input": "hello", "stream": True})))
    thread.start()
    assert pool["started"].wait(3)
    assert pool["manager"].scheduler.inflight[(pool["kind"], "first")] == 1
    # Force the fairness cursor to prefer first: the active lease must still
    # select the idle identity while the first stream has not completed.
    pool["manager"].scheduler.last[(pool["kind"], "gpt-test")] = "second"
    assert pool["post"]()[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second"]
    pool["release"].set()
    thread.join(5)
    assert not thread.is_alive() and result[0][0] == 200
    assert not pool["manager"].scheduler.inflight
    assert pool["post"]({"model": "gpt-other", "input": "other"})[0] == 200
    assert pool["observed"][-1][0] == "first"


def test_tools_survive_stream_and_complete_with_original_arguments(pool):
    pool["modes"]["first"] = "tools"
    status, body, _headers = pool["post"]({"model": "gpt-test", "input": "lookup", "stream": True,
                                          "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]})
    assert status == 200
    events = gateway._sse_events(body)
    completed = next(item["response"] for item in events if item.get("type") == "response.completed")
    assert completed["output"][0]["call_id"] == "call-tool"
    assert completed["output"][0]["arguments"] == '{"q":"hello"}'
    assert len(pool["observed"]) == 1
    stats = pool["manager"].usage_stats.snapshot()
    record = stats["recentRequests"][0]
    assert record["actualModel"] == "gpt-test" and record["modelEvidence"] == "actual"
    assert record["contextTier"] == "long" and record["serviceTier"] == "priority"
    assert record["cacheWriteEvidence"] == "known" and record["usageEvidenceVersion"] == 2
    assert (record["inputTokens"], record["cachedInputTokens"], record["cacheWriteTokens"], record["outputTokens"]) == (300000, 200000, 10000, 4)


def test_reasoning_output_also_prohibits_replay(pool):
    pool["settings"]["web2api"]["routing"] = "ordered"
    pool["modes"]["first"] = "reasoning_then_capacity"
    status, body, _headers = pool["post"]({"model": "gpt-test", "input": "hello", "stream": True})
    assert status == 200 and b"thinking" in body
    assert len(pool["observed"]) == 1
    assert not pool["manager"].scheduler.inflight


def test_session_restart_preserves_identity_and_pool_removal_rejects(pool):
    assert pool["post"]()[0] == 200
    headers = {"Session-Id": "persist-this-session"}
    assert pool["post"](headers=headers)[0] == 200
    manager = pool["manager"]
    path = manager.scheduler.session_path
    persisted = path.read_text(encoding="utf-8")
    assert "persist-this-session" not in persisted and "fake-" not in persisted
    manager.scheduler = Scheduler(manager.lock, path)
    assert pool["post"](headers=headers)[0] == 200
    assert [row[0] for row in pool["observed"]] == ["first", "second", "second"]
    pool["settings"]["web2api"]["accountIds" if pool["kind"] == "account" else "providerIds"] = ["first"]
    assert pool["post"](headers=headers)[0] == 409
    assert len(pool["observed"]) == 3


def test_unknown_opaque_history_is_not_assigned_to_multi_identity_pool(pool):
    status, _body, _headers = pool["post"]({"model": "gpt-test", "input": [
        {"type": "reasoning", "encrypted_content": "private-fixture-history"}]})
    assert status == 409
    assert not pool["observed"]


def test_corrupt_persistent_binding_state_fails_closed(pool):
    manager = pool["manager"]
    path = manager.scheduler.session_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken-json", encoding="utf-8")
    manager.scheduler = Scheduler(manager.lock, path)
    assert pool["post"](headers={"Session-Id": "same-session"})[0] == 503
    assert not pool["observed"]


def test_truncated_result_is_not_replayed_to_another_identity(pool):
    pool["settings"]["web2api"]["routing"] = "ordered"
    pool["modes"]["first"] = "truncate"
    assert pool["post"]()[0] == 502
    assert len(pool["observed"]) == 1
    assert not pool["manager"].scheduler.inflight


@pytest.mark.parametrize("failure_stage", ["post_open_persist", "capacity_probe"])
def test_untransferred_response_is_closed_once_and_lease_released(pool, monkeypatch, failure_stage):
    manager = pool["manager"]
    opened = []
    original_open = core._open_same_origin_request

    class TrackedResponse:
        def __init__(self, response):
            self.response = response
            self.close_count = 0

        def __getattr__(self, name):
            return getattr(self.response, name)

        def close(self):
            self.close_count += 1
            self.response.close()

    def open_local(request, *, timeout):
        response = TrackedResponse(original_open(request, timeout=timeout))
        opened.append(response)
        return response

    monkeypatch.setattr(core, "_open_same_origin_request", open_local)
    writes = []
    if failure_stage == "post_open_persist":
        persist = manager.scheduler._persist_sessions

        def fail_second_save():
            writes.append(len(opened))
            if len(writes) == 2:
                raise SessionStateError("fixture post-open persistence failure")
            persist()

        monkeypatch.setattr(manager.scheduler, "_persist_sessions", fail_second_save)
    else:
        pool["modes"]["first"] = "capacity"

    assert pool["post"](headers={"Session-Id": "resource-ownership"})[0] == 503
    assert len(opened) == 1 and opened[0].close_count == 1
    assert opened[0].response.closed
    assert not manager.scheduler.inflight
    if failure_stage == "post_open_persist":
        assert writes == [0, 1]  # preflight succeeds; post-open commit fails
