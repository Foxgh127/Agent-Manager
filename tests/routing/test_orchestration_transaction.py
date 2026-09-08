from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core


class OrchestrationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        self.originals = {}
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
            self.originals[name] = getattr(core, name)
            setattr(core, name, value)
        self.environment = {}
        self.patchers = [
            # This transaction fixture deliberately routes max from the account
            # while the local model advertises low. Supply the compatible
            # runtime separately so validation still intersects both sources.
            patch.object(core, "codex_version", return_value="codex-cli 0.144.0"),
            patch.object(core, "_raw_local_model_catalog", return_value={
                "models": [{
                    "slug": "gpt-account",
                    "display_name": "Account model",
                    "default_reasoning_level": "low",
                    "supported_reasoning_levels": [{"effort": "low", "description": "Low"}],
                }]
            }),
            patch.object(
                core,
                "local_model_catalog",
                return_value=[
                    {
                        "id": "gpt-account",
                        "name": "Account model",
                        "description": "",
                        "efforts": ["low"],
                        "defaultEffort": "low",
                        "priority": 1,
                    }
                ],
            ),
            patch.object(core, "dpapi_protect", side_effect=lambda value: value.encode("utf-8")),
            patch.object(core, "dpapi_unprotect", side_effect=lambda value: value.decode("utf-8")),
            patch.object(
                core,
                "_read_user_environment",
                side_effect=lambda name: self.environment.get(name),
            ),
            patch.object(
                core,
                "_sync_user_environment",
                side_effect=lambda name, value: self.environment.__setitem__(name, value),
            ),
            patch.object(
                core,
                "_remove_user_environment",
                side_effect=lambda name: self.environment.pop(name, None),
            ),
        ]
        for mocked in self.patchers:
            mocked.start()
        self.root.mkdir(parents=True)
        core.CONFIG_FILE.write_text(
            'model = "old-model"\nmodel_reasoning_effort = "medium"\n\n[desktop]\nkeep_me = true\n',
            encoding="utf-8",
        )
        core.AGENTS_FILE.write_text("# Personal rules\n\nKeep this line.\n", encoding="utf-8")
        core.ensure_state()
        settings = core.load_settings()
        settings["accounts"] = [
            {
                "id": "account-one",
                "label": "Account One",
                "authMode": "chatgpt",
                "codexCompatible": True,
                "models": ["gpt-account"],
                "modelCapabilities": {
                    "gpt-account": {"efforts": ["max"], "defaultEffort": "max"}
                },
                "refreshState": "ready",
            }
        ]
        settings["unrelatedSentinel"] = {"keep": [1, 2, 3]}
        core.save_settings(settings)

    def tearDown(self):
        for mocked in reversed(self.patchers):
            mocked.stop()
        for name, value in self.originals.items():
            setattr(core, name, value)
        self.temp.cleanup()

    def payload(self, *, include_config=True):
        key = "account:account-one::gpt-account"
        routes = {
            level: {"models": [], "efforts": []}
            for level in core.DIFFICULTIES
        }
        routes["simple"] = {"models": [key], "efforts": ["max"]}
        payload = {
            "modelWorkspace": {
                "mode": "aggregate",
                "activeSourceId": "account:account-one",
                "selectAll": True,
                "selectedModels": [key],
                "defaultModelKey": key,
                "syncToCodex": True,
            },
            "subagentRouting": {
                "advanced": True,
                "strategyId": "adaptive",
                "routes": routes,
            },
            "runtimeTuning": {"planningMode": "plan_first"},
        }
        if include_config:
            document = core.codex_config_document()
            payload["codexConfig"] = {
                "expectedFingerprint": document["fingerprint"],
                "content": document["content"] + '\n[custom]\npreserved = "yes"\n',
            }
        return payload

    def state(self):
        paths = [
            core.SETTINGS_FILE,
            core.SECRETS_FILE,
            core.CONFIG_FILE,
            core.AGENTS_FILE,
            core.MODEL_CATALOG_FILE,
            core.RUNTIME_OVERLAY_FILE,
            *(path for path, _kind in core._runtime_overlay_targets()),
        ]
        return {
            "files": {
                path: path.read_bytes() if path.is_file() else None
                for path in dict.fromkeys(paths)
            },
            "environment": dict(self.environment),
        }

    def assert_state_equal(self, expected):
        actual = self.state()
        self.assertEqual(actual["files"], expected["files"])
        self.assertEqual(actual["environment"], expected["environment"])

    def test_success_validates_account_capability_and_commits_settings_once(self):
        gateway_observations = []
        original_save = core.save_settings

        def ensure_gateway():
            gateway_observations.append(
                {
                    "settings": core.load_settings()["modelWorkspace"]["mode"],
                    "document": core.codex_config_document()["valid"],
                }
            )

        with (
            patch.object(core, "_reasoning_capabilities", return_value={
                "gpt-account": {"efforts": ["low"], "defaultEffort": "low"}
            }),
            patch.object(core, "save_settings", wraps=original_save) as save,
        ):
            response = core.save_orchestration_and_apply(
                self.payload(),
                ensure_gateway=ensure_gateway,
            )

        self.assertEqual(save.call_count, 1)
        self.assertTrue(response["result"]["gatewayRequired"])
        self.assertEqual(response["runtimeTuning"]["planningMode"], "plan_first")
        self.assertTrue(response["document"]["valid"])
        self.assertEqual(gateway_observations, [{"settings": "aggregate", "document": True}])
        saved = core.load_settings()
        self.assertEqual(saved["unrelatedSentinel"], {"keep": [1, 2, 3]})
        self.assertEqual(saved["subagentRouting"]["routes"]["simple"]["efforts"], ["max"])
        self.assertIn('[custom]\npreserved = "yes"', core.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIn("Keep this line.", core.AGENTS_FILE.read_text(encoding="utf-8"))
        self.assertTrue(any(core.AGENTS_DIR.glob("cam-*.toml")))
        internal = core.load_service_secret("gateway_internal", required=True)
        public = core.load_service_secret("web2api", required=True)
        self.assertNotEqual(internal, public)
        self.assertEqual(self.environment[core.AGGREGATE_ENV_KEY], internal)
        self.assertTrue(response["result"]["backups"])

    def test_explicit_toml_edit_replaces_previous_managed_search_value(self):
        core.save_runtime_tuning({"webSearch": "cached"})
        core.CONFIG_FILE.write_text('web_search = "cached"\n', encoding="utf-8")
        payload = self.payload()
        payload["codexConfig"]["content"] = 'web_search = "live"\n'
        result = core.save_orchestration_and_apply(payload)
        self.assertEqual(core.read_toml(core.CONFIG_FILE)["web_search"], "live")
        self.assertEqual(result["runtimeTuning"]["webSearch"], "live")

    def test_conflicting_visual_and_toml_search_edits_leave_files_unchanged(self):
        core.CONFIG_FILE.write_text('web_search = "cached"\n', encoding="utf-8")
        payload = self.payload()
        payload["codexConfig"]["content"] = 'web_search = "live"\n'
        payload["runtimeTuning"]["webSearch"] = "disabled"
        before = self.state()
        with self.assertRaisesRegex(core.ManagerError, "冲突"):
            core.save_orchestration_and_apply(payload)
        self.assert_state_equal(before)

    def test_direct_toml_save_survives_a_later_apply(self):
        core.save_runtime_tuning({"webSearch": "cached"})
        core.CONFIG_FILE.write_text('web_search = "cached"\n', encoding="utf-8")
        core.save_codex_config_document({"content": 'web_search = "live"\n'})
        core.apply_configuration()
        self.assertEqual(core.read_toml(core.CONFIG_FILE)["web_search"], "live")
        self.assertNotIn("webSearch", core.load_settings()["runtimeTuning"]["managedFields"])

    def test_direct_toml_ownership_save_failure_restores_config_and_settings(self):
        core.save_runtime_tuning({"webSearch": "cached"})
        core.CONFIG_FILE.write_text('web_search = "cached"\n', encoding="utf-8")
        before = self.state()
        save = core.save_settings
        def fail_after_save(settings):
            save(settings)
            raise OSError("late settings error")
        with patch.object(core, "save_settings", side_effect=fail_after_save):
            with self.assertRaises(core.ManagerError):
                core.save_codex_config_document({"content": 'web_search = "live"\n'})
        self.assert_state_equal(before)

    def test_config_fingerprint_conflict_changes_nothing(self):
        before = self.state()
        payload = self.payload()
        payload["codexConfig"]["expectedFingerprint"] = "0" * 64
        with self.assertRaisesRegex(core.ManagerError, "配置已被其他程序"):
            core.save_orchestration_and_apply(payload)
        self.assert_state_equal(before)

    def test_config_document_context_lookup_never_migrates_settings(self):
        settings = json.loads(core.SETTINGS_FILE.read_text(encoding="utf-8"))
        settings["accounts"][0].pop("groupId", None)
        settings["accounts"][0].pop("sourceType", None)
        core.SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
        before = core.SETTINGS_FILE.read_bytes()

        document = core.codex_config_document()

        self.assertTrue(document["valid"])
        self.assertEqual(core.SETTINGS_FILE.read_bytes(), before)

    def test_managed_agent_write_failure_rolls_back_every_surface(self):
        before = self.state()
        original_write = core.atomic_write_text

        def fail_agent(path, content):
            if Path(path).parent == core.AGENTS_DIR:
                raise OSError("agent write failure")
            return original_write(path, content)

        with patch.object(core, "atomic_write_text", side_effect=fail_agent):
            with self.assertRaisesRegex(core.ManagerError, "agent write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_environment_write_failure_rolls_back_files_settings_and_secrets(self):
        before = self.state()

        def fail_environment(name, value):
            if name == core.AGGREGATE_ENV_KEY:
                raise OSError("environment write failure")
            self.environment[name] = value

        with patch.object(core, "_sync_user_environment", side_effect=fail_environment):
            with self.assertRaisesRegex(core.ManagerError, "environment write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_gateway_failure_after_apply_rolls_back_committed_state(self):
        before = self.state()

        def fail_gateway():
            self.assertEqual(core.load_settings()["modelWorkspace"]["mode"], "aggregate")
            raise RuntimeError("gateway start failure")

        with self.assertRaisesRegex(core.ManagerError, "gateway start failure"):
            core.save_orchestration_and_apply(
                self.payload(),
                ensure_gateway=fail_gateway,
            )
        self.assert_state_equal(before)

    def test_late_settings_write_failure_rolls_back_runtime_outputs(self):
        before = self.state()
        with patch.object(core, "save_settings", side_effect=OSError("settings write failure")):
            with self.assertRaisesRegex(core.ManagerError, "settings write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_internal_secret_failure_rolls_back_public_secret_creation(self):
        before = self.state()
        original_store = core.store_service_secret

        def fail_internal(service_id, secret):
            if service_id == "gateway_internal":
                raise OSError("secret write failure")
            return original_store(service_id, secret)

        with patch.object(core, "store_service_secret", side_effect=fail_internal):
            with self.assertRaisesRegex(core.ManagerError, "secret write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_config_write_failure_rolls_back_without_consuming_other_sections(self):
        before = self.state()
        original_write = core.atomic_write_text

        def fail_config(path, content):
            if Path(path) == core.CONFIG_FILE:
                raise OSError("config write failure")
            return original_write(path, content)

        with patch.object(core, "atomic_write_text", side_effect=fail_config):
            with self.assertRaisesRegex(core.ManagerError, "config write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_catalog_write_failure_restores_overlay_and_secrets(self):
        before = self.state()
        original_write = core.atomic_write_text

        def fail_catalog(path, content):
            if Path(path) == core.MODEL_CATALOG_FILE:
                raise OSError("catalog write failure")
            return original_write(path, content)

        with patch.object(core, "atomic_write_text", side_effect=fail_catalog):
            with self.assertRaisesRegex(core.ManagerError, "catalog write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)

    def test_overlay_capture_failure_restores_secrets_created_before_it(self):
        before = self.state()
        with patch.object(
            core,
            "_runtime_overlay_capture_files",
            side_effect=OSError("overlay write failure"),
        ):
            with self.assertRaisesRegex(core.ManagerError, "overlay write failure"):
                core.save_orchestration_and_apply(self.payload())
        self.assert_state_equal(before)


if __name__ == "__main__":
    unittest.main()
