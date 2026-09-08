from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_manager.core as core


def _account(account_id: str) -> dict:
    return {
        "id": account_id,
        "label": f"Account {account_id.upper()}",
        "email": f"{account_id}@example.test",
        "authMode": "chatgpt",
        "sourceType": "codex_auth",
        "codexCompatible": True,
        "models": ["gpt-shared"],
        "refreshState": "ready",
    }


def _provider() -> dict:
    return {
        "id": "relay",
        "kind": "custom",
        "name": "Relay",
        "baseUrl": "https://relay.example.test/v1",
        "resolvedBaseUrl": "https://relay.example.test/v1",
        "envKey": "RELAY_API_KEY",
        "models": ["gpt-shared"],
        "modelDiscoveryState": "ready",
        "runtimeRevision": "stable",
        "appliedRuntimeRevision": "stable",
    }


class SharedGatewaySwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".codex"
        self.root.mkdir(parents=True)
        paths = {
            "CODEX_HOME": self.root,
            "CONFIG_FILE": self.root / "config.toml",
            "AGENTS_FILE": self.root / "AGENTS.md",
            "AGENTS_DIR": self.root / "agents",
            "STATE_DIR": self.root / "agent-manager",
            "SETTINGS_FILE": self.root / "agent-manager/settings.json",
            "MODEL_CATALOG_FILE": self.root / "agent-manager/model-catalog.json",
            "MODELS_CACHE_FILE": self.root / "models_cache.json",
            "SECRETS_FILE": self.root / "agent-manager/provider-secrets.json",
            "BACKUPS_DIR": self.root / "agent-manager/backups",
            "RUNTIME_OVERLAY_FILE": self.root / "agent-manager/runtime-overlay.json",
            "RUNTIME_RESTORE_STATUS_FILE": self.root / "agent-manager/runtime-restore.json",
            "ACCOUNT_ACTIVATION_HISTORY_FILE": self.root / "agent-manager/activation-history.json",
            "MANAGED_CODEX_RUNTIME_DIR": self.root / "agent-manager/runtime/codex",
            "LEGACY_STATE_DIR": self.root / "legacy",
            "LEGACY_PROFILE_FILE": self.root / "legacy/config.toml",
        }
        for name, value in paths.items():
            mocked = patch.object(core, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.live_account = "a"
        common = [
            patch.object(
                core,
                "current_auth_state",
                side_effect=lambda _settings=None: {"activeAccountId": self.live_account},
            ),
            patch.object(core, "account_invalid_reason", return_value=None),
            patch.object(core, "provider_key_configured", return_value=True),
            patch.object(core, "_provider_runtime_requires_reapply", return_value=False),
            patch.object(
                core,
                "_raw_local_model_catalog",
                return_value={
                    "models": [
                        {
                            "slug": "gpt-shared",
                            "supported_reasoning_levels": [{"effort": "high"}],
                            "default_reasoning_level": "high",
                        }
                    ]
                },
            ),
        ]
        for mocked in common:
            mocked.start()
            self.addCleanup(mocked.stop)

    def _base_settings(self) -> dict:
        settings = core._initial_settings()
        settings["accounts"] = [_account("a"), _account("b")]
        settings["providers"] = [_provider()]
        settings["web2api"].update(
            {
                "enabled": False,
                "activeForCodex": False,
                "activeAccountId": None,
                "port": 17860,
            }
        )
        for level in core.DIFFICULTIES:
            settings["subagentRouting"]["routes"][level] = {
                "models": [],
                "efforts": [],
            }
        # The child intentionally uses another credential source. This makes
        # the generated parent config use the shared local gateway.
        settings["subagentRouting"]["routes"]["expert"] = {
            "models": ["account:b::gpt-shared"],
            "efforts": ["high"],
        }
        settings["subagentRouting"]["strategyId"] = "adaptive"
        return settings

    def _official_settings(self, *, initially_active: str = "a") -> dict:
        settings = self._base_settings()
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": f"account:{initially_active}",
                "selectAll": True,
                "selectedModels": [f"account:{initially_active}::gpt-shared"],
                "defaultModelKey": f"account:{initially_active}::gpt-shared",
                "syncToCodex": True,
            }
        )
        profile = core._active_main(settings)
        profile["provider"] = "openai"
        profile["model"] = "gpt-shared"
        return settings

    def _provider_settings(self, *, initially_active: bool = True) -> dict:
        settings = self._base_settings()
        source_id = "provider:relay" if initially_active else "account:b"
        key = "provider:relay::gpt-shared" if initially_active else "account:b::gpt-shared"
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": source_id,
                "selectAll": True,
                "selectedModels": [key],
                "defaultModelKey": key,
                "syncToCodex": True,
            }
        )
        profile = core._active_main(settings)
        profile["provider"] = "relay" if initially_active else "openai"
        profile["model"] = "gpt-shared"
        return settings

    def _write_generated_config(self, settings: dict) -> dict:
        content = core.build_codex_config(settings)
        core.CONFIG_FILE.write_text(content, encoding="utf-8")
        return tomllib.loads(content)

    def test_official_active_check_accepts_aggregate_runtime_but_verifies_account_identity(self):
        settings = self._official_settings()
        config = self._write_generated_config(settings)
        source = core._account_model_source(settings, "a")
        account = next(item for item in settings["accounts"] if item["id"] == "a")
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(
            core.resolve_model_route(config["model"], settings)["sourceRecordId"],
            "a",
        )

        with (
            patch.object(core, "_read_live_snapshot", return_value=({}, {"email": "a@example.test"})),
            patch.object(core, "_account_matches_identity", return_value=True) as identity,
            patch.object(core, "_live_auth_files_match", return_value=True),
            patch.object(core, "_inactive_provider_environment_overrides", return_value=[]),
        ):
            self.assertTrue(core._official_account_target_is_active(settings, account, {}, source))
        identity.assert_called_once()

    def test_provider_active_check_accepts_aggregate_runtime_but_keeps_provider_scope(self):
        settings = self._provider_settings()
        settings["web2api"]["enabled"] = True
        config = self._write_generated_config(settings)
        provider = _provider()
        source = next(
            item for item in core.model_sources(settings) if item["id"] == "provider:relay"
        )
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(
            core.resolve_model_route(config["model"], settings)["sourceRecordId"],
            "relay",
        )

        with (
            patch.object(
                core,
                "_read_user_environment",
                side_effect=lambda name: "sk-relay" if name == "RELAY_API_KEY" else "gateway-key",
            ),
            patch.object(core, "_inactive_provider_environment_overrides", return_value=[]),
        ):
            self.assertTrue(
                core._provider_target_is_active(settings, provider, source, "sk-relay")
            )

    def test_official_configuration_verifies_generated_runtime_and_selected_account_separately(self):
        settings = self._official_settings()

        def apply_configuration(_sync_secrets=False):
            settings["web2api"]["enabled"] = True
            config = self._write_generated_config(settings)
            self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
            return {
                "changed": True,
                "gatewayRequired": True,
                "syncedEnvKeys": [core.AGGREGATE_ENV_KEY],
            }

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings"),
            patch.object(core, "apply_configuration", side_effect=apply_configuration),
            patch.object(core, "_inactive_provider_environment_overrides", return_value=[]),
        ):
            configured = core._apply_official_account_configuration("a")

        config = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        route = core.resolve_model_route(config["model"], settings)
        self.assertTrue(configured["applied"]["gatewayRequired"])
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(route["sourceKind"], "account")
        self.assertEqual(route["sourceRecordId"], "a")
        self.assertEqual(configured["model"], config["model"])

    def test_provider_switch_starts_required_gateway_before_codex_launch(self):
        settings = self._provider_settings(initially_active=False)
        self.live_account = "b"
        events: list[str] = []
        environment: dict[str, str] = {}

        def apply_configuration(_sync_secrets=False):
            settings["web2api"]["enabled"] = True
            environment["RELAY_API_KEY"] = "sk-relay"
            environment[core.AGGREGATE_ENV_KEY] = "gateway-key"
            config = self._write_generated_config(settings)
            self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
            return {
                "changed": True,
                "restartRequired": True,
                "gatewayRequired": True,
                "syncedEnvKeys": ["RELAY_API_KEY", core.AGGREGATE_ENV_KEY],
            }

        def ensure_gateway():
            events.append("gateway")
            return {"running": True}

        def launch_codex(**_kwargs):
            self.assertIn("gateway", events)
            events.append("launch")
            return {"started": True}

        snapshot = {
            "files": {self.root / name: None for name in core.AUTH_FILES},
            "environment": {},
        }
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings"),
            patch.object(core, "load_provider_key", return_value="sk-relay"),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "mock"}),
            patch.object(
                core,
                "_capture_switch_transaction_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                core,
                "_read_user_environment",
                side_effect=lambda name: environment.get(name),
            ),
            patch.object(core, "_inactive_provider_environment_overrides", return_value=[]),
            patch.object(core, "apply_configuration", side_effect=apply_configuration),
            patch.object(core, "auto_sync_sessions_after_switch", return_value={"preserved": True}),
            patch.object(core, "_repair_switch_session_visibility", side_effect=lambda: events.append("visibility") or {}),
            patch.object(core, "launch_codex_app", side_effect=launch_codex),
            patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
        ):
            result = core.switch_api_provider_and_launch(
                "relay",
                close_processes_callback=lambda **_kwargs: events.append("close") or {"closed": [1]},
                ensure_gateway=ensure_gateway,
            )

        self.assertTrue(result["verified"])
        self.assertEqual(events, ["close", "gateway", "visibility", "launch"])
        config = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        route = core.resolve_model_route(config["model"], settings)
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(route["sourceRecordId"], "relay")
        self.assertEqual(result["model"], config["model"])

    def test_official_switch_starts_required_gateway_before_codex_launch(self):
        settings = self._official_settings(initially_active="b")
        self.live_account = "b"
        target = next(item for item in settings["accounts"] if item["id"] == "a")
        events: list[str] = []

        def configure(_account_id):
            settings["modelWorkspace"].update(
                {
                    "activeSourceId": "account:a",
                    "selectedModels": ["account:a::gpt-shared"],
                    "defaultModelKey": "account:a::gpt-shared",
                }
            )
            settings["web2api"]["enabled"] = True
            config = self._write_generated_config(settings)
            return {
                "workspace": settings["modelWorkspace"],
                "applied": {"gatewayRequired": True},
                "model": config["model"],
                "models": ["gpt-shared"],
            }

        def ensure_gateway():
            events.append("gateway")
            return {"running": True}

        def launch_codex(**_kwargs):
            self.assertIn("gateway", events)
            events.append("launch")
            return {"started": True}

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_prepare_account_switch_target", return_value=(target, {})),
            patch.object(core, "_official_account_target_is_active", return_value=False),
            patch.object(core, "_account_chatgpt_credentials", return_value={}),
            patch.object(core, "resolve_codex_launch_plan", return_value={"strategy": "mock"}),
            patch.object(core, "_capture_switch_transaction_snapshot", return_value={"files": {}}),
            patch.object(core, "switch_codex_account", return_value={"changed": True}),
            patch.object(core, "_apply_official_account_configuration", side_effect=configure),
            patch.object(core, "auto_sync_sessions_after_switch", return_value={"preserved": True}),
            patch.object(core, "_repair_switch_session_visibility", side_effect=lambda: events.append("visibility") or {}),
            patch.object(core, "launch_codex_app", side_effect=launch_codex),
            patch.object(core, "wait_for_codex_runtime_ready", return_value={"ready": True}),
        ):
            result = core.switch_codex_account_and_launch(
                "a",
                close_processes_callback=lambda **_kwargs: events.append("close") or {"closed": [1]},
                ensure_gateway=ensure_gateway,
            )

        self.assertTrue(result["verified"])
        self.assertEqual(events, ["close", "gateway", "visibility", "launch"])
        config = tomllib.loads(core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(
            core.resolve_model_route(config["model"], settings)["sourceRecordId"],
            "a",
        )


    def test_visibility_rollback_precedes_original_codex_launch(self):
        events = []
        with (
            patch.object(core, "_restore_switch_transaction_snapshot", side_effect=lambda *args, **kwargs: events.append("config") or []),
            patch.object(core, "_restore_switch_session_visibility", side_effect=lambda value: events.append("visibility")),
            patch.object(core, "launch_codex_app", side_effect=lambda **kwargs: events.append("launch")),
        ):
            core._rollback_failed_switch({}, {}, closed={"closed":[1]}, launch_attempted=False, session_visibility={"backupId":"saved"})
        self.assertEqual(events, ["config", "visibility", "launch"])

    def test_visibility_restore_conflict_blocks_original_codex_launch(self):
        with (
            patch.object(core, "_restore_switch_transaction_snapshot", return_value=[]),
            patch.object(core, "_restore_switch_session_visibility", side_effect=core.ManagerError("conflict")),
            patch.object(core, "launch_codex_app") as launch,
        ):
            detail = core._rollback_failed_switch({}, {}, closed={"closed":[1]}, launch_attempted=False, session_visibility={"backupId":"saved"})
        launch.assert_not_called()
        self.assertIn("恢复历史对话标记", detail)


if __name__ == "__main__":
    unittest.main()
