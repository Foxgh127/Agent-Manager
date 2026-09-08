from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import agent_manager.core as core


class NativeSubagentPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, relative in {
            "CODEX_HOME": ".",
            "STATE_DIR": "state",
            "SETTINGS_FILE": "state/settings.json",
            "CONFIG_FILE": "config.toml",
            "AGENTS_FILE": "AGENTS.md",
            "AGENTS_DIR": "agents",
            "SECRETS_FILE": "state/secrets.json",
            "MODEL_CATALOG_FILE": "state/catalog.json",
            "MODELS_CACHE_FILE": "models_cache.json",
            "BACKUPS_DIR": "backups",
            "RUNTIME_OVERLAY_FILE": "state/overlay.json",
            "RUNTIME_RESTORE_STATUS_FILE": "state/restore.json",
        }.items():
            mocked = patch.object(core, name, root / relative)
            mocked.start()
            self.addCleanup(mocked.stop)
        core.CONFIG_FILE.write_text('model = "test-model"\n', encoding="utf-8")
        core.AGENTS_FILE.write_text(
            "# User instructions\n\nKeep my spacing.\n\n", encoding="utf-8"
        )
        core.AGENTS_DIR.mkdir()
        self.model = {
            "key": "account:a::test-model",
            "id": "test-model",
            "slug": "test-model",
            "name": "Test",
            "displayName": "Test",
            "sourceId": "account:a",
            "sourceKind": "account",
            "sourceRecordId": "a",
            "sourceName": "A",
            "reasoningKnown": True,
            "efforts": list(core.VALID_EFFORTS),
            "defaultEffort": "low",
        }
        for name in (
            "gateway_model_records",
            "selected_model_records",
            "_configuration_model_records",
        ):
            mocked = patch.object(core, name, return_value=[self.model])
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, value in {
            "codex_version": "codex-cli 0.153.4",
            "local_model_catalog": [self.model],
            "current_auth_state": {"authMode": "chatgpt", "activeAccountId": "a"},
            "_reasoning_capabilities": {"test-model": self.model},
        }.items():
            mocked = patch.object(core, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.settings = core._initial_settings()
        self.settings["accounts"] = [
            {
                "id": "a",
                "authMode": "chatgpt",
                "codexCompatible": True,
                "models": ["test-model"],
            }
        ]
        self.settings["modelWorkspace"].update(
            {
                "activeSourceId": "account:a",
                "defaultModelKey": self.model["key"],
                "selectAll": True,
            }
        )

    def test_managed_routes_and_policy_do_not_depend_on_parent_effort(self):
        for strategy in ("adaptive", "manual_only"):
            self.settings["subagentRouting"].update(
                {"strategyId": strategy, "prompt": strategy + " policy"}
            )
            expected = None
            for effort in core.VALID_EFFORTS:
                self.settings["mainProfiles"][0]["effort"] = effort
                specs = core._managed_subagent_specs(self.settings)
                signature = [
                    (item["name"], item["model"], item["effort"]) for item in specs
                ]
                expected = signature if expected is None else expected
                self.assertEqual(signature, expected)
                self.assertEqual(len(specs), 4)
                doc = tomlkit.parse(core.build_codex_config(self.settings))
                self.assertEqual(
                    doc["features"]["multi_agent_v2"]["multi_agent_mode_hint_text"],
                    strategy + " policy",
                )
                self.assertTrue(doc["features"]["multi_agent_v2"]["enabled"])

    def test_native_mode_removes_own_routes_without_disabling_user_roles(self):
        self.settings["subagentRouting"]["strategyId"] = "verification_first"
        (core.AGENTS_DIR / "cam-simple-1.toml").write_text(
            'name="cam_simple_1"\ndeveloper_instructions="ordered fallback route"\n',
            encoding="utf-8",
        )
        core.CONFIG_FILE.write_text(
            f'model="test-model"\n[agents]\nmax_depth=9\n[agents.scout]\nconfig_file="user-agent.toml"\n[agents.cam_simple_1]\nconfig_file={(core.AGENTS_DIR / "cam-simple-1.toml").as_posix()!r}\n',
            encoding="utf-8",
        )
        self.assertEqual(core._managed_subagent_specs(self.settings), [])
        self.assertEqual(core.build_routing_block(self.settings), "")
        original = core.AGENTS_FILE.read_text(encoding="utf-8")
        self.assertEqual(core.build_agents_file(self.settings), original)
        doc = tomlkit.parse(core.build_codex_config(self.settings))
        self.assertEqual(doc["agents"]["max_depth"], 9)
        self.assertIn("scout", doc["agents"])
        self.assertNotIn("cam_simple_1", doc["agents"])
        self.assertNotIn("features", doc)

    def test_native_release_restores_feature_boolean_and_custom_user_hint(self):
        for original in (
            "[features]\nmulti_agent_v2=false\nother=true\n",
            '[features.multi_agent_v2]\nenabled=false\nmulti_agent_mode_hint_text="User policy"\nmax_wait_timeout_ms=777\n',
        ):
            self.settings["subagentRouting"].update(
                {"strategyId": "adaptive", "prompt": "Managed policy"}
            )
            doc = tomlkit.parse(original)
            journal = core._next_managed_subagent_policy(self.settings, original)
            core._apply_managed_subagent_mode_hint(doc, self.settings)
            self.assertTrue(doc["features"]["multi_agent_v2"]["enabled"])
            self.settings["managedSubagentPolicy"] = journal
            self.settings["subagentRouting"]["strategyId"] = "verification_first"
            core._apply_managed_subagent_mode_hint(doc, self.settings)
            self.assertEqual(doc.unwrap(), tomlkit.parse(original).unwrap())

    def test_native_release_preserves_later_user_policy_edit(self):
        self.settings["subagentRouting"].update(
            {"strategyId": "adaptive", "prompt": "Managed policy"}
        )
        journal = core._next_managed_subagent_policy(self.settings, "")
        doc = tomlkit.document()
        core._apply_managed_subagent_mode_hint(doc, self.settings)
        doc["features"]["multi_agent_v2"]["multi_agent_mode_hint_text"] = (
            "Later user edit"
        )
        self.settings["managedSubagentPolicy"] = journal
        self.settings["subagentRouting"]["strategyId"] = "verification_first"
        core._apply_managed_subagent_mode_hint(doc, self.settings)
        self.assertEqual(
            doc["features"]["multi_agent_v2"]["multi_agent_mode_hint_text"],
            "Later user edit",
        )

    def test_old_cli_receives_roles_without_unsupported_feature_table(self):
        with patch.object(core, "codex_version", return_value="codex-cli 0.130.0"):
            self.assertIsNone(core._managed_subagent_mode_hint(self.settings))
            self.assertEqual(len(core._managed_subagent_specs(self.settings)), 4)


if __name__ == "__main__":
    unittest.main()
