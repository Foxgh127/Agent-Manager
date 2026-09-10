"""Offline format-to-runtime acceptance tests; all credentials are fabricated."""
import base64
import json
from unittest.mock import patch

import pytest

import agent_manager.core as core
from agent_manager.accounts.portability import build_portable_account_export


def jwt(account="workspace-a", user="user-a", **extra):
    claims = {"exp": 2_000_000_000, "sub": user, "iss": "https://auth.openai.com",
              "client_id": core.CODEX_OAUTH_CLIENT_ID,
              "https://api.openai.com/auth": {"chatgpt_account_id": account}, **extra}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJSUzI1NiJ9." + payload + ".fake-signature"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, suffix in {
        "STATE_DIR": "state", "SETTINGS_FILE": "state/settings.json",
        "SECRETS_FILE": "state/secrets.json", "BACKUPS_DIR": "state/backups",
        "CODEX_HOME": ".codex", "CONFIG_FILE": ".codex/config.toml",
        "AGENTS_DIR": ".codex/agents", "AGENTS_FILE": ".codex/AGENTS.md",
    }.items():
        monkeypatch.setattr(core, name, tmp_path / suffix)
    monkeypatch.setattr(core, "dpapi_protect", lambda value: value.encode())
    monkeypatch.setattr(core, "dpapi_unprotect", lambda value: value.decode())
    monkeypatch.setattr(core, "_fetch_chatgpt_json", lambda *a, **k: {})
    monkeypatch.setattr(core, "fetch_provider_models", lambda *a, **k: [])


def preview(value):
    return core.preview_codex_accounts_batch({"items": [{"authJson": value}]})


@pytest.mark.parametrize("document", [
    'export OPENAI_API_KEY="sk-fake-relay-input"\nexport OPENAI_BASE_URL="https://relay.example.test/v1"\nmodel=gpt-test',
    {"baseURL": "https://relay.example.test/v1", "data": {"credentials": {"apiKey": "sk-fake-relay-input"}}, "models": ["gpt-test"]},
    {"api-key": "sk-fake-relay-input", "base-url": "https://relay.example.test/v1", "models": ["gpt-test"]},
])
def test_api_formats_reach_runtime_key_store(document):
    payload = {"items": [{"authJson": document}], "proxyEnabled": True}
    assert core.preview_codex_accounts_batch(payload)["valid"] == 1
    result = core.import_codex_accounts_batch(payload)
    assert not result["failed"]
    provider = result["importedProviders"][0]["provider"]
    assert provider["baseUrl"] == "https://relay.example.test/v1"
    assert core.load_provider_key(provider["id"], required=True) == "sk-fake-relay-input"
    assert "provider:" + provider["id"] in core.load_settings()["web2api"]["sourceOrder"]


@pytest.mark.parametrize("with_id", [True, False])
def test_explicit_oauth_without_refresh_reaches_runtime_without_refresh(with_id):
    raw = {"type": "codex", "access_token": jwt(), "account_id": "workspace-a",
           "admin": {"api_key": "admin-must-not-copy"}, "routing": {"weight": 90}}
    if with_id:
        raw["id_token"] = jwt()
    item = preview(raw)["items"][0]
    assert item["valid"] and item["codexCompatible"]
    assert item["credentialKind"] == "oauth_access_token"
    assert not item["refreshCapable"]
    assert item["remoteValidation"] == "unverified"
    result = core.import_codex_accounts_batch({"items": [{"authJson": raw}], "proxyEnabled": True, "deferRefresh": True})
    assert not result["failed"]
    account = result["imported"][0]
    assert account["proxyEnabled"]
    with patch.object(core, "_request_codex_oauth_refresh", side_effect=AssertionError("must never refresh")):
        runtime = core._account_chatgpt_credentials(account["id"])
    assert runtime["accessToken"] == raw["access_token"]
    assert runtime["accountId"] == "workspace-a"
    stored = core._decode_snapshot_files(core._load_account_snapshot(account["id"]))["auth.json"]
    assert b"admin-must-not-copy" not in stored and b'"routing"' not in stored
    exported = build_portable_account_export(json.loads(stored))
    assert exported["tokens"]["refresh_token"] is None
    assert "routing" not in exported and "admin" not in exported


def test_bare_or_browser_token_is_not_promoted_to_oauth():
    for document in [jwt(), {"accessToken": jwt(), "sessionToken": "cookie-fake"},
                     {"sourceType": "web_session", "tokens": {"access_token": jwt(), "id_token": jwt(), "refresh_token": "refresh-fake"}}]:
        item = preview(document)["items"][0]
        assert item["valid"]
        assert item["sourceType"] == "web_session"
        assert not item["codexCompatible"]
        assert not item["refreshCapable"]
        assert item["remoteValidation"] == "unverified"


@pytest.mark.parametrize("document", [
    {"refresh_token": "refresh-only-fake"}, {"sessionToken": "cookie-only-fake"},
    {"apiKey": "sk-fake-no-endpoint"},
    {"type": "admin", "OPENAI_API_KEY": "sk-fake-admin-key"},
    {"format": "codex-agent-manager-account", "version": 99},
    {"format": "codex-agent-manager-relay-account", "version": 99},
    {"type": "codex", "access_token": jwt(exp=100)},
    {"access_token": jwt(), "account_id": "other-workspace"},
    {"type": "codex", "access_token": jwt(), "accessToken": jwt(user="other-user")},
    {"type": "codex", "access_token": jwt(), "id_token": jwt(user="other-user")},
    {"apiKey": "sk-fake-a", "api_key": "sk-fake-b", "baseUrl": "https://relay.example.test/v1"},
    {"apiKey": "sk-fake-a", "baseUrl": "https://relay.example.test/v1", "access_token": jwt()},
])
def test_partial_success_preserves_valid_rows_and_rejects_ambiguous_material(document):
    data = [document, {"OPENAI_API_KEY": "sk-valid-other-row"}]
    output = preview(data)
    assert output["total"] == 2 and output["valid"] == 1
    assert output["items"][0]["error"]
    result = core.import_codex_accounts_batch({"items": [{"authJson": data}], "deferRefresh": True})
    assert len(result["failed"]) == 1 and len(result["imported"]) == 1
    assert "sk-fake-" not in json.dumps(output)


def test_admin_routes_and_inbound_keys_are_not_discovered_as_accounts():
    raw = {"admin": {"apiKey": "sk-admin-fake", "baseUrl": "https://admin.example.test"},
           "routes": [{"api_key": "sk-route-fake"}], "api-keys": ["sk-inbound-fake"]}
    output = preview(raw)
    assert output["valid"] == 0
    assert "sk-admin-fake" not in json.dumps(output)


def test_duplicate_json_fields_fail_one_file_without_overwriting_or_aborting_batch():
    output = core.preview_codex_accounts_batch({"items": [
        {"authJson": '{"OPENAI_API_KEY":"sk-first-fake","OPENAI_API_KEY":"sk-second-fake"}'},
        {"authJson": {"OPENAI_API_KEY": "sk-valid-other-row"}},
    ]})
    assert output["valid"] == 1 and output["invalid"] == 1
    assert "重复字段" in output["items"][0]["error"]


def test_refresh_placeholder_does_not_create_a_refresh_capability():
    item = preview({"type": "codex", "access_token": jwt(), "id_token": jwt(), "refresh_token": "placeholder"})["items"][0]
    assert not item["refreshCapable"]


@pytest.mark.parametrize("wrapper", ["config", "credentials", "data"])
@pytest.mark.parametrize("role", ["admin", "management"])
def test_nested_management_credentials_fail_only_their_batch_row(wrapper, role):
    bad = {wrapper: {"role": role, "apiKey": "sk-nested-admin-fake", "baseUrl": "https://relay.example.test/v1"}}
    good = {wrapper: {"apiKey": "sk-nested-upstream-fake", "baseUrl": "https://relay.example.test/v1", "models": ["gpt-test"]}}
    output = preview([bad, good])
    assert output["total"] == 2 and output["invalid"] == 1 and output["valid"] == 1
    assert "管理或入口凭据" in output["items"][0]["error"]
    assert output["items"][1]["kind"] == "provider"
    result = core.import_codex_accounts_batch({"items": [{"authJson": [bad, good]}], "deferRefresh": True})
    assert len(result["failed"]) == 1 and len(result["importedProviders"]) == 1
    assert "sk-nested-admin-fake" not in json.dumps(output)


def test_provider_wrapper_node_limit_preserves_normal_batch_sibling():
    def branch(depth):
        return {"config": branch(depth - 1), "data": branch(depth - 1)} if depth else {}
    oversized = branch(8)
    good = {"apiKey": "sk-normal-fake", "baseUrl": "https://relay.example.test/v1"}
    output = preview([oversized, good])
    assert output["total"] == 2 and output["invalid"] == 1 and output["valid"] == 1
    assert "节点超过 256" in output["items"][0]["error"]


def test_provider_wrapper_repeated_objects_and_excess_depth_fail_clearly():
    from agent_manager.accounts.import_formats import provider_contexts, ImportFormatError
    repeated = {"apiKey": "sk-fake-shared"}
    with pytest.raises(ImportFormatError, match="重复或循环对象"):
        provider_contexts({"config": repeated, "data": repeated})
    nested = {}
    for _ in range(9):
        nested = {"data": nested}
    with pytest.raises(ImportFormatError, match="嵌套超过 8"):
        provider_contexts(nested)
