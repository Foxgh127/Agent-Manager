"""Synthetic cross-upstream continuation checks; no user sessions or network."""
from copy import deepcopy
from email.message import Message
import io
import json
from urllib.error import HTTPError, URLError

import pytest

import agent_manager.core as core
import agent_manager.gateway.service as gateway


def history():
    return {"model": "private-alias", "instructions": "retain the user goal",
            "include": ["reasoning.encrypted_content"], "input": [
                {"role": "user", "content": "Read the file"},
                {"type": "reasoning", "id": "rs-old", "encrypted_content": "old-opaque", "summary": []},
                {"type": "function_call", "id": "fc-old", "call_id": "call-1", "name": "read", "arguments": '{"path":"a.txt"}'},
                {"type": "function_call_output", "call_id": "call-1", "output": "exact file content"},
                {"type": "message", "id": "msg-old", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "The file says hello", "annotations": []}]},
                {"role": "user", "content": "Continue"},
            ]}


def rejection(code="invalid_encrypted_content", status=400):
    headers = Message()
    headers["Content-Type"] = "application/json"
    return HTTPError("https://fixture.invalid/v1/responses", status, "rejected", headers,
                     io.BytesIO(json.dumps({"error": {"code": code}}).encode()))


class Response(io.BytesIO):
    status = 200

    def __init__(self):
        super().__init__(b'data: {"type":"response.completed","response":{"id":"resp-new","object":"response","status":"completed","output":[]}}\n\n')
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    accounts = [{"id": value, "label": value, "authMode": "chatgpt", "sourceType": "codex_auth",
                 "codexCompatible": True, "models": ["native-model"]} for value in ("first", "second")]
    monkeypatch.setattr(core, "load_settings", lambda: {"accounts": accounts, "web2api": {
        "accountIds": ["first", "second"], "routing": "ordered"}})
    monkeypatch.setattr(core, "_codex_client_version", lambda: "fixture")
    monkeypatch.setattr(core, "_account_chatgpt_credentials", lambda value, **kw: {
        "accessToken": "token-" + value, "accountId": "workspace-" + value})
    monkeypatch.setattr(core, "provider_by_id", lambda value: {"id": value, "name": "Fixture"})
    monkeypatch.setattr(core, "load_provider_key", lambda *args, **kw: "provider-key")
    monkeypatch.setattr(core, "_provider_runtime_base_url", lambda value: "https://fixture.invalid/v1")
    obj = gateway.Web2APIManager()
    monkeypatch.setattr(obj, "_record_usage", lambda *args, **kw: None)
    monkeypatch.setattr(obj, "_remember_quota", lambda *args, **kw: None)
    return obj


def test_replay_preserves_messages_phases_tool_association_and_original():
    original = history()
    snapshot = deepcopy(original)
    replay = gateway._portable_history_replay(original)
    assert original == snapshot
    assert replay["instructions"] == original["instructions"]
    assert replay["include"] == original["include"]
    assert replay["input"][1] == {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path":"a.txt"}'}
    assert replay["input"][2]["output"] == "exact file content"
    assert replay["input"][3]["phase"] == "final_answer"
    assert replay["input"][3]["content"] == original["input"][4]["content"]
    assert all("encrypted_content" not in item and "id" not in item for item in replay["input"])
    replay["input"][3]["content"][0]["text"] = "changed copy"
    assert original == snapshot


def test_custom_tool_pairs_remain_custom():
    value = history()
    value["input"][2] = {"type": "custom_tool_call", "call_id": "call-1", "name": "patch", "input": "*** exact patch"}
    value["input"][3]["type"] = "custom_tool_call_output"
    assert gateway._portable_history_replay(value)["input"][1:3] == value["input"][2:4]


@pytest.mark.parametrize("case", ["cursor", "conversation", "compaction", "reference", "orphan", "missing_output", "duplicate", "wrong_kind", "text_missing", "server_file", "server_tool_file", "invalid_tool_output"])
def test_unprovable_or_incomplete_history_is_never_rewritten(case):
    value = history()
    if case == "cursor":
        value["previous_response_id"] = "resp-old"
    elif case == "conversation":
        value["conversation"] = {"id": "conv-old"}
    elif case == "compaction":
        value["input"].insert(0, {"type": "compaction", "encrypted_content": "only-remaining-context"})
    elif case == "reference":
        value["input"].insert(0, {"type": "item_reference", "id": "msg-old"})
    elif case == "orphan":
        value["input"].pop(2)
    elif case == "missing_output":
        value["input"].pop(3)
    elif case == "duplicate":
        value["input"].insert(3, deepcopy(value["input"][2]))
    elif case == "wrong_kind":
        value["input"][3]["type"] = "custom_tool_call_output"
    elif case == "text_missing":
        value["input"] = [value["input"][1], value["input"][-1]]
    elif case == "server_tool_file":
        value["input"][3]["output"] = [{"type": "input_file", "file_id": "server-owned-tool-file"}]
    elif case == "invalid_tool_output":
        value["input"][3]["output"] = {"missing": "serialized-output"}
    else:
        value["input"][0]["content"] = [{"type": "input_file", "file_id": "server-owned-file"}]
    before = deepcopy(value)
    assert gateway._portable_history_replay(value) is None
    with pytest.raises(gateway.GatewayError, match="原会话未修改") as caught:
        gateway._encrypted_history_replay(value, 400, b'{"error":{"code":"invalid_encrypted_content"}}')
    assert caught.value.status == 409 and value == before


@pytest.mark.parametrize("kind", ["provider", "account"])
@pytest.mark.parametrize("mode", ["stream", "execute"])
def test_explicit_rejection_replays_once_on_same_identity_and_native_model(setup, monkeypatch, kind, mode):
    route = {"id": "native-model", "sourceKind": kind, "sourceRecordId": "first"}
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    seen = []
    def opened(request, **kwargs):
        seen.append((request.full_url, json.loads(request.data), dict(request.headers)))
        if len(seen) == 1:
            raise rejection()
        return Response()
    monkeypatch.setattr(core, "_open_same_origin_request", opened)
    original = history()
    before = deepcopy(original)
    result = getattr(setup, mode)("/v1/responses", original, access_scope="internal")
    if mode == "stream":
        assert b"response.completed" in b"".join(result["chunks"])
    else:
        assert result["status"] == 200
    assert len(seen) == 2 and original == before
    assert seen[0][0] == seen[1][0]
    assert seen[0][2]["Authorization"] == seen[1][2]["Authorization"]
    assert seen[0][1]["model"] == seen[1][1]["model"] == "native-model"
    assert seen[0][1]["input"] == original["input"]
    assert seen[1][1]["input"] == gateway._portable_history_replay(original)["input"]
    assert gateway._header_value(result["headers"], "X-Codex-History-Compatibility") == "plaintext-replay"


@pytest.mark.parametrize("kind", ["provider", "account"])
def test_compatible_history_passes_unchanged_without_speculative_cleanup(setup, monkeypatch, kind):
    route = {"id": "native-model", "sourceKind": kind, "sourceRecordId": "first"}
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    seen = []
    monkeypatch.setattr(core, "_open_same_origin_request", lambda request, **kw: seen.append(json.loads(request.data)) or Response())
    setup.execute("/v1/responses", history(), access_scope="internal")
    assert len(seen) == 1 and seen[0]["input"] == history()["input"]


@pytest.mark.parametrize("kind", ["provider", "account"])
@pytest.mark.parametrize("failure", ["again", "quota", "capacity", "other400", "transport"])
@pytest.mark.parametrize("binding", ["unbound", "dedicated"])
def test_failure_does_not_loop_or_spill_opaque_history_to_other_identity(setup, monkeypatch, kind, failure, binding):
    route = {"id": "native-model", "sourceKind": kind, "sourceRecordId": "first"}
    if binding == "unbound":
        route = ({**route, "poolCandidates": [dict(route), {**route, "sourceRecordId": "second"}]}
                 if kind == "provider" else None)
    # Unknown multi-identity history is rejected before transmitting anything;
    # dedicated routes still exercise same-identity compatibility retry/failure.
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    value = history()
    value["model"] = "native-model"
    seen = []
    def opened(request, **kwargs):
        seen.append(dict(request.headers))
        if failure == "transport":
            raise URLError("fixture disconnect")
        if failure == "quota":
            raise rejection("rate_limit_exceeded", 429)
        if failure == "capacity":
            raise rejection("server_is_overloaded", 503)
        if failure == "other400":
            raise rejection("invalid_request_error")
        raise rejection()
    monkeypatch.setattr(core, "_open_same_origin_request", opened)
    before = deepcopy(value)
    with pytest.raises(gateway.GatewayError) as caught:
        setup.stream("/v1/responses", value)
    assert value == before
    if binding == "unbound":
        assert caught.value.status == 409
        assert seen == []
    else:
        assert caught.value.status == {"again": 400, "quota": 429, "capacity": 503,
                                       "other400": 400, "transport": 502}[failure]
        assert len(seen) == (2 if failure == "again" else 1)
        assert len({item["Authorization"] for item in seen}) == 1
    assert not setup.scheduler.inflight


def test_sse_failure_after_output_never_replays(setup, monkeypatch):
    route = {"id": "native-model", "sourceKind": "provider", "sourceRecordId": "first"}
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    response = Response()
    response.seek(0)
    response.truncate()
    response.write(b'data: {"type":"response.output_text.delta","delta":"observed"}\n\ndata: {"type":"response.failed","response":{"status":"failed","error":{"code":"invalid_encrypted_content"}}}\n\n')
    response.seek(0)
    calls = []
    monkeypatch.setattr(core, "_open_same_origin_request", lambda request, **kw: calls.append(request) or response)
    result = setup.stream("/v1/responses", history())
    assert b"observed" in b"".join(result["chunks"])
    assert len(calls) == 1


def test_compatibility_retry_refuses_reauthenticated_identity(setup, monkeypatch):
    credentials = iter([{"accessToken": "old-token", "accountId": "old-workspace"},
                        {"accessToken": "other-token", "accountId": "other-workspace"}])
    monkeypatch.setattr(core, "_account_chatgpt_credentials", lambda *args, **kw: next(credentials))
    calls = []
    def opened(request, **kwargs):
        calls.append(request)
        raise rejection()
    monkeypatch.setattr(core, "_open_same_origin_request", opened)
    value = history()
    value["model"] = "native-model"
    with pytest.raises(gateway.GatewayError, match="身份已改变"):
        setup._open_upstream(value, {"first"})
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["stream", "execute"])
def test_delivered_oauth_response_keeps_identity_through_capacity_probe(setup, monkeypatch, mode):
    route = {"id": "native-model", "sourceKind": "account", "sourceRecordId": "first"}
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    credentials = {"accessToken": "original-token", "accountId": "original-workspace"}
    monkeypatch.setattr(core, "_account_chatgpt_credentials", lambda *args, **kw: dict(credentials))
    requests = []
    monkeypatch.setattr(core, "_open_same_origin_request", lambda request, **kw: requests.append(request) or Response())
    payload = {"model": "private-alias", "input": "first"}
    first = getattr(setup, mode)("/v1/responses", payload, access_scope="internal")
    if mode == "stream":
        assert b"response.completed" in b"".join(first["chunks"])
    else:
        first["_onDelivered"]()
    binding = setup._lookup_response_binding("resp-new")
    assert binding["identityFingerprint"] == gateway._oauth_binding_fingerprint(credentials)

    credentials.update(accessToken="different-token", accountId="different-workspace")
    followup = {"model": "private-alias", "previous_response_id": "resp-new", "input": "next"}
    before = deepcopy(followup)
    with pytest.raises(gateway.GatewayError, match="身份已改变") as error:
        getattr(setup, mode)("/v1/responses", followup, access_scope="internal")
    assert error.value.status == 409
    assert len(requests) == 1 and followup == before


@pytest.mark.parametrize("mode", ["stream", "execute"])
def test_single_explicit_identity_can_resume_unknown_cursor_after_manager_restart(setup, monkeypatch, mode):
    route = {"id": "native-model", "sourceKind": "account", "sourceRecordId": "first"}
    monkeypatch.setattr(core, "resolve_model_route", lambda *args, **kw: route)
    requests = []
    monkeypatch.setattr(core, "_open_same_origin_request", lambda request, **kw: requests.append(request) or Response())
    payload = {"model": "private-alias", "previous_response_id": "from-previous-manager", "input": "next"}
    result = getattr(setup, mode)("/v1/responses", payload, access_scope="internal")
    if mode == "stream":
        assert b"response.completed" in b"".join(result["chunks"])
    assert len(requests) == 1
    assert json.loads(requests[0].data)["previous_response_id"] == "from-previous-manager"
    assert requests[0].get_header("Authorization") == "Bearer token-first"
