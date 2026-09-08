from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.sessions.live_selection as live


class ExternalSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, relative in {
            "CODEX_HOME": ".",
            "CONFIG_FILE": "config.toml",
            "STATE_DIR": "state",
            "SETTINGS_FILE": "state/settings.json",
            "SECRETS_FILE": "state/secrets.json",
            "AGENTS_FILE": "AGENTS.md",
            "AGENTS_DIR": "agents",
            "BACKUPS_DIR": "backups",
            "RUNTIME_OVERLAY_FILE": "state/overlay.json",
            "MODELS_CACHE_FILE": "models.json",
            "MODEL_CATALOG_FILE": "state/catalog.json",
            "LEGACY_STATE_DIR": "legacy",
            "LEGACY_PROFILE_FILE": "legacy/profile.toml",
        }.items():
            context = patch.object(core, name, self.root / relative)
            context.start()
            self.addCleanup(context.stop)
        self.settings = core._initial_settings()
        self.settings["providers"] += [
            {
                "id": f"relay{i}",
                "kind": "custom",
                "name": f"Relay {i}",
                "baseUrl": "https://relay.example.test/v1",
                "envKey": f"TEST_RELAY_{i}",
                "sourceType": "relay_account",
                "relayAccountId": f"web{i}",
                "relayKeyId": "key",
                "groupId": "relay",
                "models": ["test-model"],
            }
            for i in (1, 2)
        ]
        self.settings["modelWorkspace"].update(
            {
                "activeSourceId": "provider:relay1",
                "defaultModelKey": "provider:relay1::test-model",
            }
        )
        for name, value in {
            "load_relay_account_key": lambda account, *_a, **_kw: f"secret-{account}",
            "_read_user_environment": lambda _key: None,
        }.items():
            context = patch.object(core, name, side_effect=value)
            context.start()
            self.addCleanup(context.stop)
        self.write_config()

    def write_config(
        self, provider="cockpit", key="secret-web2", url="https://relay.example.test/v1"
    ):
        core.CONFIG_FILE.write_text(
            f'model="test-model"\nmodel_provider={json.dumps(provider)}\n[model_providers.{provider}]\nbase_url={json.dumps(url)}\nexperimental_bearer_token={json.dumps(key)}\n',
            encoding="utf-8",
        )

    def test_cockpit_alias_is_matched_by_endpoint_and_key(self):
        result = live.inspect_live_selection(self.settings)
        self.assertEqual(result["sourceId"], "provider:relay2")
        self.assertTrue(result["recognized"])
        self.assertNotIn("secret", json.dumps(result))

    def test_public_current_source_changes_when_cockpit_only_rewrites_config(self):
        with (
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(core, "provider_key_configured", return_value=True),
            patch.object(
                core, "running_codex_processes", return_value=core.CodexProcessScan()
            ),
            patch.object(core, "local_model_catalog", return_value=[]),
            patch.object(core, "_reasoning_capabilities", return_value={}),
        ):
            first = core.current_auth_state(self.settings, force=True)
            self.assertEqual(first["liveSelection"]["sourceId"], "provider:relay2")
            sources = core.model_sources(self.settings, [])
            self.assertEqual(
                [source["id"] for source in sources if source["active"]],
                ["provider:relay2"],
            )
            configured = core.selected_model_records(self.settings)
            self.assertEqual(
                {record["sourceId"] for record in configured}, {"provider:relay1"}
            )
            visible = core._public_connections_state(self.settings, [])
            self.assertEqual(
                visible["settings"]["modelWorkspace"]["activeSourceId"],
                "provider:relay2",
            )
            self.assertIn("provider:relay2::test-model", visible["selectedModelKeys"])
            self.write_config(key="secret-web1")
            after = core.current_auth_state(self.settings)
            self.assertEqual(after["liveSelection"]["sourceId"], "provider:relay1")
            self.assertEqual(
                self.settings["modelWorkspace"]["activeSourceId"], "provider:relay1"
            )

    def test_selected_website_key_is_not_overridden_by_inherited_environment(self):
        provider = self.settings["providers"][-1]
        with (
            patch.object(core, "provider_by_id", return_value=provider),
            patch.dict(core.os.environ, {provider["envKey"]: "stale-cockpit-key"}),
        ):
            self.assertEqual(core.load_provider_key(provider["id"]), "secret-web2")

    def test_provider_name_alone_cannot_override_another_key_identity(self):
        self.write_config(provider="relay1")
        self.assertEqual(
            live.inspect_live_selection(self.settings)["sourceId"], "provider:relay2"
        )

    def test_unimported_key_does_not_highlight_previous_account(self):
        self.write_config(key="not-imported")
        result = live.inspect_live_selection(self.settings)
        self.assertEqual(result["sourceId"], "")
        self.assertTrue(result["configured"])
        self.assertFalse(result["recognized"])

    def test_identical_key_on_different_site_is_not_matched(self):
        self.write_config(url="https://another.example.test/v1")
        self.assertFalse(live.inspect_live_selection(self.settings)["recognized"])

    def test_legacy_auth_json_api_key_can_match_relay(self):
        core.CONFIG_FILE.write_text(
            'model_provider="cockpit"\n[model_providers.cockpit]\nbase_url="https://relay.example.test/"\n',
            encoding="utf-8",
        )
        (core.CODEX_HOME / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "secret-web2"}), encoding="utf-8"
        )
        self.assertEqual(
            live.inspect_live_selection(self.settings)["sourceId"], "provider:relay2"
        )

    def test_external_selection_updates_manager_only_and_requests_preservation(self):
        original = core.CONFIG_FILE.read_bytes()
        source = {
            "id": "provider:relay2",
            "kind": "provider",
            "recordId": "relay2",
            "models": [{"id": "test-model", "key": "provider:relay2::test-model"}],
        }
        with (
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(core, "model_sources", return_value=[source]),
            patch.object(core, "save_settings") as save,
            patch.object(core, "_runtime_overlay_read", return_value=None),
        ):
            result = live.reconcile_startup_selection()
        self.assertTrue(result["preserveCurrent"])
        self.assertEqual(
            self.settings["modelWorkspace"]["activeSourceId"], "provider:relay2"
        )
        self.assertEqual(self.settings["mainProfiles"][0]["provider"], "relay2")
        save.assert_called_once_with(self.settings)
        self.assertEqual(core.CONFIG_FILE.read_bytes(), original)

    def test_external_configuration_is_preserved_by_read_only_restart_decision(self):
        with (
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(core, "save_settings") as save,
            patch.object(core, "_runtime_overlay_read", return_value=None),
        ):
            self.assertTrue(live.preserve_live_configuration_on_restart())
        save.assert_not_called()

    def test_unknown_external_source_is_not_saved_as_an_imported_account(self):
        self.write_config(key="unrecognized")
        with (
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(core, "save_settings") as save,
            patch.object(core, "_runtime_overlay_read", return_value=None),
        ):
            result = live.reconcile_startup_selection()
        self.assertTrue(result["preserveCurrent"])
        save.assert_not_called()

    def test_passive_startup_remains_passive_after_account_id_is_reconciled(self):
        self.write_config(provider="relay2")
        self.settings["modelWorkspace"]["activeSourceId"] = "provider:relay2"
        snapshot_hash = core._overlay_value_hash(core.CONFIG_FILE.read_bytes())
        record = {"capturedHash": snapshot_hash, "appliedHash": None}
        with (
            patch.object(core, "load_settings", return_value=self.settings),
            patch.object(
                core,
                "_runtime_overlay_read",
                return_value={"files": {"config.toml": record}},
            ),
        ):
            self.assertTrue(live.preserve_live_configuration_on_restart())
            record["appliedHash"] = snapshot_hash
            self.assertFalse(live.preserve_live_configuration_on_restart())

    def test_official_identity_supersedes_saved_relay_selection(self):
        core.CONFIG_FILE.write_text('model="test-model"\n', encoding="utf-8")
        result = live.inspect_live_selection(
            self.settings,
            auth={
                "authMode": "chatgpt",
                "activeAccountId": "official2",
                "signedIn": True,
            },
        )
        self.assertEqual(result["sourceId"], "account:official2")


class FirstRunDiscoveryTests(unittest.TestCase):
    def test_fresh_install_discovery_never_calls_high_level_process_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(core, "STATE_DIR", Path(directory)),
                patch.dict(
                    core.CODEX_WINDOWS_APP_CACHE, {"at": 0, "value": None}, clear=True
                ),
                patch.object(
                    core.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "[]", ""),
                ) as query,
                patch.object(
                    core,
                    "_running_windows_codex_candidates",
                    return_value=core.CodexProcessScan(),
                ) as raw,
                patch.object(
                    core,
                    "running_codex_processes",
                    side_effect=AssertionError("recursive discovery"),
                ),
            ):
                self.assertIsNone(core._detect_codex_windows_app(force=True))
                self.assertIsNone(core._detect_codex_windows_app())
                self.assertEqual(query.call_count, 1)
                self.assertEqual(raw.call_count, 1)

    def test_process_detection_without_store_uses_trusted_cli_and_preserves_unknown_status(
        self,
    ):
        candidate = {
            "name": "codex.exe",
            "pid": "55",
            "executable": "C:/OpenAI/Codex/bin/version/codex.exe",
        }
        with (
            patch.object(core, "_detect_codex_windows_app", return_value=None),
            patch.object(
                core,
                "_running_windows_codex_candidates",
                return_value=core.CodexProcessScan(
                    [candidate], known=False, error="another path unavailable"
                ),
            ),
        ):
            result = core.running_codex_processes()
        self.assertEqual(len(result), 1)
        self.assertFalse(result.known)
        self.assertTrue(result[0]["verified"])


if __name__ == "__main__":
    unittest.main()
