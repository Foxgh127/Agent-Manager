from pathlib import Path
from contextlib import nullcontext
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import model_preferences


class ModelCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        for name, path in {
            "CODEX_HOME": root,
            "CONFIG_FILE": root / "config.toml",
            "AGENTS_FILE": root / "AGENTS.md",
            "SETTINGS_FILE": root / "settings.json",
            "SECRETS_FILE": root / "secrets.json",
            "STATE_DIR": root / "state",
            "AGENTS_DIR": root / "agents",
            "BACKUPS_DIR": root / "backups",
            "MODEL_CATALOG_FILE": root / "catalog.json",
            "RUNTIME_OVERLAY_FILE": root / "overlay.json",
        }.items():
            context = patch.object(core, name, path)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(core, "codex_version", return_value="codex-cli 0.153.4")
        context.start()
        self.addCleanup(context.stop)
        self.native = [
            {
                "id": "gpt-6-astra",
                "efforts": list(core.VALID_EFFORTS),
                "defaultEffort": "medium",
            }
        ]
        self.provider = {
            "id": "relay",
            "name": "Relay",
            "kind": "custom",
            "models": ["gpt-6-astra"],
            "modelCapabilities": {},
            "runtimeRevision": 1,
            "appliedRuntimeRevision": 1,
        }

    def test_default_medium_is_not_an_exhaustive_supported_list(self):
        raw = {"default_reasoning_effort": "medium"}
        normalized = core._provider_model_capability(raw)
        self.assertFalse(normalized["reasoningKnown"])
        self.assertEqual(normalized["efforts"], [])
        self.assertEqual(core._provider_model_capability(normalized), normalized)
        self.provider["modelCapabilities"] = {"gpt-6-astra": normalized}
        effective = core._effective_provider_model_capabilities(
            self.provider, self.native
        )["gpt-6-astra"]
        self.assertEqual(effective["efforts"], list(core.VALID_EFFORTS))
        self.assertEqual(effective["reasoningSource"], "codex_compatibility")
        self.assertEqual(effective["defaultEffort"], "medium")

    def test_explicit_single_effort_or_no_reasoning_remains_authoritative(self):
        for declaration, expected in [
            ({"efforts": ["medium"], "defaultEffort": "medium"}, ["medium"]),
            ({"supports_reasoning": False}, []),
        ]:
            self.provider["modelCapabilities"] = {"gpt-6-astra": declaration}
            effective = core._effective_provider_model_capabilities(
                self.provider, self.native
            )["gpt-6-astra"]
            self.assertEqual(effective["efforts"], expected)
            self.assertEqual(effective["reasoningSource"], "provider")

    def test_namespaced_gpt_uses_effort_compatibility_without_inventing_context(self):
        self.provider["models"] = ["cpa/gpt-6-astra", "unrelated-model"]
        effective = core._effective_provider_model_capabilities(
            self.provider, self.native
        )
        self.assertEqual(
            effective["cpa/gpt-6-astra"]["efforts"], list(core.VALID_EFFORTS)
        )
        self.assertNotIn("contextWindow", effective["cpa/gpt-6-astra"])
        self.assertNotIn("unrelated-model", effective)

    def test_client_version_filters_new_enums_without_mutating_discovery(self):
        with patch.object(core, "codex_version", return_value="codex-cli 0.137.0"):
            effective = core._effective_provider_model_capabilities(
                self.provider, self.native
            )["gpt-6-astra"]
        self.assertEqual(effective["efforts"], ["low", "medium", "high", "xhigh"])
        self.assertIn("ultra", self.native[0]["efforts"])

    def test_custom_provider_preference_wins_and_auto_restores_native_range(self):
        self.provider["modelReasoningOverrides"] = {
            "gpt-6-astra": {
                "reasoningKnown": True,
                "efforts": ["high", "ultra"],
                "defaultEffort": "ultra",
            }
        }
        effective = core._effective_provider_model_capabilities(
            self.provider, self.native
        )["gpt-6-astra"]
        self.assertEqual(effective["efforts"], ["high", "ultra"])
        self.assertEqual(effective["reasoningSource"], "custom")
        self.provider["modelReasoningOverrides"] = {}
        self.assertEqual(
            core._effective_provider_model_capabilities(self.provider, self.native)[
                "gpt-6-astra"
            ]["efforts"],
            list(core.VALID_EFFORTS),
        )

    def test_generated_catalog_exposes_all_six_levels_for_a_relay(self):
        capability = core._effective_provider_model_capabilities(
            self.provider, self.native
        )["gpt-6-astra"]
        record = {
            "id": "gpt-6-astra",
            "slug": "gpt-6-astra",
            "displayName": "Astra",
            "sourceName": "Relay",
            "sourceKind": "provider",
            **capability,
        }
        raw = {
            "models": [
                {
                    "slug": "gpt-6-astra",
                    "supported_reasoning_levels": [
                        {"effort": level, "description": level}
                        for level in core.VALID_EFFORTS
                    ],
                    "default_reasoning_level": "medium",
                    "context_window": 1050000,
                }
            ]
        }
        settings = core._initial_settings()
        with (
            patch.object(core, "_configuration_model_records", return_value=[record]),
            patch.object(core, "_raw_local_model_catalog", return_value=raw),
            patch.object(core, "managed_subagent_model_records", return_value=[]),
            patch.object(core, "_subagents_require_shared_gateway", return_value=False),
        ):
            catalog, _ = core.build_synced_model_catalog(settings)
        self.assertEqual(
            [
                level["effort"]
                for level in catalog["models"][0]["supported_reasoning_levels"]
            ],
            list(core.VALID_EFFORTS),
        )
        self.assertEqual(catalog["models"][0]["default_reasoning_level"], "medium")
        self.assertNotIn("context_window", catalog["models"][0])

    def test_saving_inactive_model_preferences_does_not_apply_or_switch_account(self):
        settings = core._initial_settings()
        settings["providers"].append(self.provider)
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "save_settings") as save,
            patch.object(core, "backup_file", return_value=None),
            patch.object(core, "_settings_file_lock", return_value=nullcontext()),
            patch.object(
                model_preferences.codex_live_selection,
                "inspect_live_selection",
                return_value={"sourceId": "account:other"},
            ),
            patch.object(core, "_apply_configuration_locked") as apply,
        ):
            result = model_preferences.save_reasoning(
                "relay",
                "gpt-6-astra",
                {
                    "mode": "custom",
                    "efforts": ["high", "ultra"],
                    "defaultEffort": "high",
                },
            )
        self.assertTrue(result["changed"])
        self.assertFalse(result["applied"])
        self.assertEqual(
            self.provider["modelReasoningOverrides"]["gpt-6-astra"]["efforts"],
            ["high", "ultra"],
        )
        self.assertEqual(self.provider["runtimeRevision"], 2)
        save.assert_called_once()
        apply.assert_not_called()

    def test_invalid_preference_is_rejected_before_reading_accounts(self):
        with patch.object(core, "load_settings") as load:
            with self.assertRaises(core.ManagerError):
                model_preferences.save_reasoning(
                    "relay", "gpt-6-astra", {"mode": "custom", "efforts": []}
                )
            load.assert_not_called()


    def test_failed_active_save_restores_pre_edit_settings_snapshot(self):
        settings = core._initial_settings()
        settings["providers"].append(self.provider)
        settings["modelWorkspace"]["activeSourceId"] = "provider:relay"
        before = core._json_clone(settings)
        snapshot = {"files": {}, "environment": {}, "settingsDocument": settings}
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_settings_file_lock", return_value=nullcontext()),
            patch.object(model_preferences.codex_live_selection, "inspect_live_selection", return_value={"sourceId":"provider:relay"}),
            patch.object(core, "_preflight_orchestration_artifacts"),
            patch.object(core, "_capture_orchestration_transaction_snapshot", return_value=snapshot),
            patch.object(core, "_apply_configuration_locked", side_effect=core.ManagerError("write failed")),
            patch.object(core, "_restore_switch_transaction_snapshot", return_value=[]) as restore,
        ):
            with self.assertRaises(core.ManagerError):
                model_preferences.save_reasoning("relay", "gpt-6-astra", {"mode":"custom","efforts":["high"]})
        self.assertEqual(restore.call_args.args[0]["settingsValue"], before)


if __name__ == "__main__":
    unittest.main()
