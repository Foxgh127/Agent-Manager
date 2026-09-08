"""Offline regressions for explicit official-account route isolation.

Only temporary config/session files and synthetic identities are used. Live
processes, credentials, registry access and network are blocked by the fixture.
The failing assertions describe required behavior, not guessed desktop state.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tomllib
from unittest.mock import Mock

import pytest

import agent_manager.core as core
import agent_manager.sessions.live_selection as live
import agent_manager.sessions.visibility_engine as visibility


ORPHAN_RELAY = "https://orphan-relay.example.invalid/v1"
MODEL = "gpt-fixture"
APP_SERVER_REQUESTS = core.codex_app_server_requests


@pytest.fixture
def official(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    root.mkdir()
    for name, relative in {
        "CODEX_HOME": ".", "CONFIG_FILE": "config.toml",
        "STATE_DIR": "agent-manager", "SETTINGS_FILE": "agent-manager/settings.json",
        "SECRETS_FILE": "agent-manager/secrets.json", "AGENTS_FILE": "AGENTS.md",
        "AGENTS_DIR": "agents", "BACKUPS_DIR": "agent-manager/backups",
        "MODEL_CATALOG_FILE": "agent-manager/catalog.json",
        "MODELS_CACHE_FILE": "models_cache.json",
        "RUNTIME_OVERLAY_FILE": "agent-manager/overlay.json",
        "LEGACY_STATE_DIR": "legacy", "LEGACY_PROFILE_FILE": "legacy/profile.toml",
    }.items():
        monkeypatch.setattr(core, name, root / relative)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Audit test attempted live process, credential or registry access")

    for name in ("_read_user_environment", "_sync_user_environment", "_remove_user_environment",
                 "_secret_store", "_read_live_snapshot", "codex_app_server_requests",
                 "codex_app_server_request", "running_codex_processes", "_open_same_origin_request"):
        monkeypatch.setattr(core, name, forbidden)
    monkeypatch.setattr(core.subprocess, "Popen", forbidden)
    monkeypatch.setattr(core.subprocess, "run", forbidden)
    monkeypatch.setattr(core.urllib.request, "urlopen", forbidden)
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = core._initial_settings()
    account = {"id": "official-a", "email": "official-a@example.invalid",
               "label": "Official A", "authMode": "chatgpt", "sourceType": "codex_auth",
               "codexCompatible": True, "models": [MODEL]}
    record = {"id": MODEL, "slug": f"account-official-a--{MODEL}",
              "sourceId": "account:official-a", "sourceKind": "account",
              "sourceRecordId": "official-a", "key": f"account:official-a::{MODEL}"}
    source = {"id": "account:official-a", "kind": "account", "recordId": "official-a",
              "models": [record]}
    settings["accounts"] = [account]
    settings["providers"] = []
    settings["modelWorkspace"].update(mode="independent", activeSourceId=source["id"],
                                       defaultModelKey=record["key"], selectAll=True,
                                       selectedModels=[record["key"]], syncToCodex=True)
    settings["web2api"].update(activeForCodex=False, activeAccountId=None)
    core._active_main(settings).update(provider="openai", model=MODEL)
    monkeypatch.setattr(core, "load_settings", lambda: settings)
    monkeypatch.setattr(core, "_configuration_model_records", lambda _s: [record])
    monkeypatch.setattr(core, "_default_main_record", lambda _s, _r: record)
    monkeypatch.setattr(core, "_managed_subagent_specs", lambda _s: [])
    monkeypatch.setattr(core, "_subagents_require_shared_gateway", lambda _s: False)
    monkeypatch.setattr(core, "_use_native_official_model_catalog", lambda _s, _r: True)
    monkeypatch.setattr(core, "_set_main_profile_reasoning_effort", lambda *_a: None)
    monkeypatch.setattr(core, "_inactive_provider_environment_overrides", lambda *_a: [])
    core.CONFIG_FILE.write_text(f'model = "{MODEL}"\n', encoding="utf-8")
    return root, settings, account, source


def orphan_config():
    return f'model = "{MODEL}"\nopenai_base_url = "{ORPHAN_RELAY}"\n'


def test_official_config_removes_orphan_endpoint_absent_from_cards(official):
    _root, settings, _account, _source = official
    core.CONFIG_FILE.write_text(orphan_config(), encoding="utf-8")
    config = tomllib.loads(core.build_codex_config(settings))
    assert not config.get("openai_base_url"), "An unlisted relay must not survive an explicit official configuration"
    assert config.get("model_provider", "openai") == "openai"


def test_direct_official_route_check_rejects_custom_endpoint(official):
    _root, settings, _account, source = official
    assert not core._switch_runtime_model_matches(settings, tomllib.loads(orphan_config()), source, "openai")


def test_official_active_fast_path_rejects_orphan_endpoint(official, monkeypatch):
    _root, settings, account, source = official
    core.CONFIG_FILE.write_text(orphan_config(), encoding="utf-8")
    monkeypatch.setattr(core, "_read_live_snapshot", lambda: ({}, {"email": account["email"]}))
    monkeypatch.setattr(core, "_account_matches_identity", lambda *_a: True)
    monkeypatch.setattr(core, "_live_auth_files_match", lambda *_a: True)
    assert not core._official_account_target_is_active(settings, account, {}, source), (
        "Matching auth.json cannot turn a custom endpoint into an official route"
    )


def test_official_active_fast_path_cannot_verify_keyring_identity_from_auth_file(official, monkeypatch):
    _root, settings, account, source = official
    core.CONFIG_FILE.write_text(f'model = "{MODEL}"\ncli_auth_credentials_store = "keyring"\n', encoding="utf-8")
    monkeypatch.setattr(core, "_read_live_snapshot", lambda: ({}, {"email": account["email"]}))
    monkeypatch.setattr(core, "_account_matches_identity", lambda *_a: True)
    monkeypatch.setattr(core, "_live_auth_files_match", lambda *_a: True)
    assert not core._official_account_target_is_active(settings, account, {}, source), (
        "The already-active path must respect the same keyring restriction as the switching path"
    )


def test_official_readiness_cannot_accept_identity_and_model_on_orphan_route(official, monkeypatch):
    _root, _settings, account, _source = official
    core.CONFIG_FILE.write_text(orphan_config(), encoding="utf-8")
    def app_server(requests, **_kwargs):
        responses = {
            "account/read": {"account": {"type": "chatgpt", "email": account["email"]}},
            "model/list": {"data": [{"id": MODEL}]},
            "config/read": {"config": tomllib.loads(orphan_config()), "origins": {}, "layers": []},
        }
        return [responses[method] for method, _params in requests]
    monkeypatch.setattr(core, "codex_app_server_requests", app_server)
    # Advance the deadline synthetically so a correct rejecting implementation
    # does not sleep or retry real processes.
    clock = iter([0.0, 0.1, 0.2, 10.0, 10.0, 10.0])
    monkeypatch.setattr(core.time, "monotonic", lambda: next(clock, 10.0))
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)
    with pytest.raises(core.ManagerError):
        core.wait_for_codex_runtime_ready(account["email"], MODEL, timeout_seconds=1)


def test_windows_app_activation_forwards_the_same_codex_home_as_probe(official, monkeypatch):
    root, _settings, _account, _source = official
    launch = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(core.subprocess, "run", launch)
    monkeypatch.setattr(core, "_codex_launch_process_observation", lambda: ([{"pid": 123}], None))
    monkeypatch.setattr(core.shutil, "which", lambda _name: "explorer.exe")
    monkeypatch.setenv("CODEX_HOME", str(root / "different-inherited-home"))
    core.launch_codex_app(launch_plan={"strategy": "windows_app", "appUserModelId": "OpenAI.Codex_test!App"})
    assert launch.call_args.kwargs.get("env", {}).get("CODEX_HOME") == str(root), (
        "Primary AppUserModelId activation drops the constructed environment; its home is not bound to the probe"
    )


def test_live_selection_does_recognize_orphan_root_endpoint_as_external(official):
    _root, settings, account, _source = official
    core.CONFIG_FILE.write_text(orphan_config(), encoding="utf-8")
    result = live.inspect_live_selection(settings, auth={"signedIn": True, "activeAccountId": account["id"]})
    assert result["kind"] == "external"
    assert not result["recognized"]
    assert result["sourceId"] == ""


def test_session_visibility_repair_preserves_turn_route_overrides(official):
    root, _settings, _account, _source = official
    folder = root / "sessions"
    folder.mkdir()
    path = folder / "rollout-root-fixture.jsonl"
    context = {"type": "turn_context", "payload": {"model": "old-account--gpt-fixture",
               "model_provider": "old-relay", "config": {"openai_base_url": ORPHAN_RELAY}}}
    records = [
        {"type": "session_meta", "payload": {"id": "root-fixture", "model_provider": "old-relay",
                                               "source": "cli", "cwd": str(root)}},
        context,
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "fixture"}},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    with sqlite3.connect(root / "state_5.sqlite") as database:
        database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, source TEXT, "
                         "rollout_path TEXT, has_user_event INTEGER, archived INTEGER)")
        database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 1, 0)",
                         ("root-fixture", "old-relay", "cli", str(path)))
    report = visibility.inspect_session_visibility(root, target_provider="openai", mode="deep")
    result = visibility.repair_session_visibility(report, confirm_codex_stopped=True, backup_parent=root / "backups")
    assert result["changed"]
    after = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert after[0]["payload"]["model_provider"] == "openai"
    assert after[1] == context, "Visibility repair does not promise to migrate saved turn routing"


@pytest.mark.parametrize("table", [
    {"base_url": ORPHAN_RELAY}, {"env_key": "OLD_TOKEN"},
    {"experimental_bearer_token": "fixture-only"}, {"http_headers": {"Authorization": "fixture-only"}},
])
def test_legacy_builtin_provider_overrides_are_rejected(official, table):
    _root, settings, _account, source = official
    config = {"model": MODEL, "model_providers": {"openai": table}}
    assert not core._switch_runtime_model_matches(settings, config, source, "openai")


def test_explicit_official_configuration_binds_file_store_and_preserves_inactive_tables(official):
    _root, settings, _account, _source = official
    core.CONFIG_FILE.write_text(
        f'model = "{MODEL}"\ncli_auth_credentials_store = "auto"\n'
        f'[model_providers.openai]\nbase_url = "{ORPHAN_RELAY}"\n'
        '[model_providers.inactive]\nname = "Keep me"\n'
        '[profiles.legacy]\nmodel = "historical-fixture"\n', encoding="utf-8")
    config = tomllib.loads(core.build_codex_config(settings))
    assert config["cli_auth_credentials_store"] == "file"
    assert "openai" not in config["model_providers"]
    assert config["model_providers"]["inactive"]["name"] == "Keep me"
    assert config["profiles"]["legacy"]["model"] == "historical-fixture"


def test_official_runtime_environment_is_child_only_and_cannot_be_re_overridden(official, monkeypatch):
    root, _settings, _account, _source = official
    for name in core._OFFICIAL_AUTH_ENV_OVERRIDES:
        monkeypatch.setenv(name, "synthetic-old-credential")
    monkeypatch.setenv("SAFE_PROVIDER_TOKEN", "synthetic-provider-credential")
    monkeypatch.setenv("CODEX_CLI_PATH", "unsafe-wrapper.cmd")
    overrides = {"CODEX_ACCESS_TOKEN": "another-synthetic-token", "CODEX_HOME": "wrong-home"}
    env = core._codex_runtime_environment(overrides, official=True)
    assert env["CODEX_HOME"] == str(root)
    assert env["SAFE_PROVIDER_TOKEN"] == "synthetic-provider-credential"
    assert all(name not in env for name in core._OFFICIAL_AUTH_ENV_OVERRIDES)
    assert "CODEX_CLI_PATH" not in env
    assert core.os.environ["CODEX_ACCESS_TOKEN"] == "synthetic-old-credential"
    assert core._codex_runtime_environment(official=False)["CODEX_ACCESS_TOKEN"] == "synthetic-old-credential"


def test_official_windows_launch_uses_executable_with_clean_environment(official, monkeypatch):
    root, _settings, account, _source = official
    executable = root / "ChatGPT.exe"
    executable.write_bytes(b"synthetic executable, never run")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "fixture-old-token")
    child = Mock(pid=123)
    popen = Mock(return_value=child)
    monkeypatch.setattr(core.subprocess, "Popen", popen)
    monkeypatch.setattr(core, "_codex_launch_process_observation", lambda: ([{"pid": 123}], None))
    result = core.launch_codex_app(launch_plan={
        "strategy": "windows_app", "appUserModelId": "OpenAI.Codex_test!App",
        "executable": str(executable), "officialAccountId": account["id"],
    })
    assert result["strategy"] == "desktop_executable"
    assert popen.call_args.args[0] == [str(executable)]
    assert popen.call_args.kwargs["env"]["CODEX_HOME"] == str(root)
    assert "CODEX_ACCESS_TOKEN" not in popen.call_args.kwargs["env"]


def test_probe_prefers_selected_desktop_runtime_over_path(official, monkeypatch):
    root, _settings, _account, _source = official
    desktop = root / "ChatGPT.exe"
    desktop.write_bytes(b"synthetic")
    runtime = root / "resources" / "codex.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"synthetic")
    monkeypatch.setattr(core, "codex_prefix", lambda: pytest.fail("Unrelated PATH CLI must not win"))
    assert core._codex_launch_probe_prefix({"executable": str(desktop)}) == [str(runtime)]


@pytest.mark.parametrize("override", [{"model_provider": "orphan-relay"},
                                      {"openai_base_url": ORPHAN_RELAY},
                                      {"cli_auth_credentials_store": "keyring"}])
def test_readiness_rejects_effective_profile_route_or_store_override(official, monkeypatch, override):
    _root, _settings, account, _source = official
    core.CONFIG_FILE.write_text(f'model = "{MODEL}"\ncli_auth_credentials_store = "file"\n', encoding="utf-8")
    def request(requests, **kwargs):
        assert kwargs["launch_plan"]["officialAccountId"] == account["id"]
        assert requests[-1][0] == "config/read"
        return [{"account": {"email": account["email"]}}, {"data": [{"id": MODEL}]},
                {"config": {"model": MODEL, **override}, "origins": {}, "layers": []}]
    monkeypatch.setattr(core, "codex_app_server_requests", request)
    clock = iter([0.0, 0.1, 0.2, 10.0, 10.0])
    monkeypatch.setattr(core.time, "monotonic", lambda: next(clock, 10.0))
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)
    with pytest.raises(core.ManagerError, match="探针有效配置"):
        core.wait_for_codex_runtime_ready(account["email"], MODEL, timeout_seconds=1,
                                          launch_plan={"officialAccountId": account["id"]})


def test_readiness_reports_probe_scope_and_actual_workspace(official, monkeypatch):
    root, _settings, account, _source = official
    plan = {"officialAccountId": account["id"], "workspace": str(root)}
    def request(requests, **kwargs):
        assert kwargs["launch_plan"] == plan
        assert requests[-1] == ("config/read", {"includeLayers": True, "cwd": str(root)})
        return [{"account": {"email": account["email"]}}, {"data": [{"id": MODEL}]},
                {"config": {"model": MODEL}, "origins": {}, "layers": []}]
    monkeypatch.setattr(core, "codex_app_server_requests", request)
    result = core.wait_for_codex_runtime_ready(account["email"], MODEL, launch_plan=plan)
    assert result["verificationScope"] == "app_server_probe"
    assert result["effectiveConfigChecked"]
    assert not result["desktopThreadsChecked"]


def test_same_workspace_or_cached_fingerprint_cannot_match_another_chatgpt_user(official):
    _root, _settings, account, _source = official
    account = {**account, "fingerprint": "synthetic-shared-fingerprint", "accountId": "shared-workspace"}
    other = {"authMode": "chatgpt", "email": "different-user@example.invalid",
             "accountId": "shared-workspace", "fingerprint": account["fingerprint"]}
    assert not core._account_matches_identity(account, other)
    assert core._find_account_for_identity({"accounts": [account]}, other) is None
    assert core._account_matches_identity(account, {**other, "email": account["email"].upper()})
    assert core._find_account_for_identity({"accounts": [{"id": "missing-fingerprint"}]}, {}) is None


def test_app_server_transport_uses_bound_runtime_and_clean_official_environment(official, monkeypatch):
    root, _settings, account, _source = official
    runtime = root / "codex.exe"
    runtime.write_bytes(b"synthetic")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "synthetic-wrong-token")
    def spawn(command, **kwargs):
        assert command == [str(runtime), "app-server", "--listen", "stdio://"]
        assert kwargs["env"]["CODEX_HOME"] == str(root)
        assert "CODEX_ACCESS_TOKEN" not in kwargs["env"]
        raise core.ManagerError("synthetic spawn checkpoint")
    monkeypatch.setattr(core.subprocess, "Popen", spawn)
    with pytest.raises(core.ManagerError, match="synthetic spawn checkpoint"):
        APP_SERVER_REQUESTS([("account/read", {})],
            launch_plan={"officialAccountId": account["id"], "appServerExecutable": str(runtime)})


@pytest.mark.parametrize("method", ["quota", "models"])
def test_native_fallback_rejects_another_users_actual_probe_identity(official, monkeypatch, method):
    _root, _settings, account, _source = official
    monkeypatch.setattr(core, "_live_official_account_matches", lambda _account: True)
    def request(requests, **kwargs):
        assert requests[0][0] == "account/read"
        assert kwargs["launch_plan"]["officialAccountId"] == account["id"]
        return [{"account": {"email": "another@example.invalid"}}, {"data": [{"id": MODEL}]}]
    monkeypatch.setattr(core, "codex_app_server_requests", request)
    with pytest.raises(core.ManagerError, match="身份与目标不一致"):
        (core._active_codex_rate_limits if method == "quota" else core._active_codex_model_catalog)(account)


def test_native_quota_fallback_binds_same_process_account_and_operation(official, monkeypatch):
    _root, _settings, account, _source = official
    payload = {"rateLimits": {"primary": {"usedPercent": 23}}}
    monkeypatch.setattr(core, "_live_official_account_matches", lambda _account: True)
    def request(requests, **kwargs):
        assert requests == [("account/read", {"refreshToken": False}), ("account/rateLimits/read", {})]
        assert kwargs["launch_plan"]["officialAccountId"] == account["id"]
        return [{"account": {"type": "chatgpt", "email": account["email"]}}, payload]
    monkeypatch.setattr(core, "codex_app_server_requests", request)
    assert core._active_codex_rate_limits(account) == payload
