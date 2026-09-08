from pathlib import Path
import json
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import agent_manager.core as core


class MainReasoningCapabilitiesV9Tests(unittest.TestCase):
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
            "SETTINGS_FILE": self.root / "agent-manager" / "settings.json",
            "MODEL_CATALOG_FILE": self.root / "agent-manager" / "model-catalog.json",
            "MODELS_CACHE_FILE": self.root / "models_cache.json",
            "SECRETS_FILE": self.root / "agent-manager" / "provider-secrets.json",
            "BACKUPS_DIR": self.root / "agent-manager" / "backups",
        }
        self.originals = {name: getattr(core, name) for name in paths}
        for name, value in paths.items():
            setattr(core, name, value)
        core.CONFIG_FILE.write_text(
            'model = "legacy"\nmodel_reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        core.AGENTS_FILE.write_text("# test\n", encoding="utf-8")
        self.dpapi = patch.multiple(
            core,
            dpapi_protect=lambda value: value.encode("utf-8"),
            dpapi_unprotect=lambda value: value.decode("utf-8"),
        )
        self.dpapi.start()
        self.addCleanup(self.dpapi.stop)

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(core, name, value)

    def install_provider(self, model, capability=None, provider_id="reasoning_relay"):
        core.save_provider(
            {
                "id": provider_id,
                "name": "Reasoning Relay",
                "baseUrl": "https://reasoning.example.test/v1",
                "envKey": "REASONING_RELAY_KEY",
                "models": [model],
                "modelCapabilities": {model: capability} if capability is not None else {},
            }
        )
        core.store_provider_key(provider_id, "sk-test-reasoning-capability")
        return core.provider_by_id(provider_id)

    @staticmethod
    def activate_provider(settings, provider_id, model, profile):
        source_id = f"provider:{provider_id}"
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": source_id,
                "selectAll": True,
                "defaultModelKey": f"{source_id}::{model}",
                "syncToCodex": True,
            }
        )
        settings["subagentRouting"]["strategyId"] = "verification_first"
        settings["mainProfiles"] = [profile]
        settings["activeMainProfileId"] = profile["id"]
        return settings

    def test_missing_effort_on_unknown_provider_model_uses_model_default(self):
        provider = self.install_provider("future-model")
        profile = core.save_main_profile(
            {
                "id": "future",
                "name": "Future",
                "provider": provider["id"],
                "model": "future-model",
            }
        )
        self.assertEqual(profile["effort"], "")

        settings = self.activate_provider(
            core.load_settings(),
            provider["id"],
            "future-model",
            profile,
        )
        parsed = tomllib.loads(core.build_codex_config(settings))
        self.assertEqual(parsed["model"], "future-model")
        self.assertNotIn("model_reasoning_effort", parsed)
    def test_known_medium_only_model_rejects_high_and_accepts_medium(self):
        provider = self.install_provider(
            "medium-model",
            {
                "reasoningKnown": True,
                "reasoningSupported": True,
                "efforts": ["medium"],
                "defaultEffort": "medium",
            },
        )
        with self.assertRaisesRegex(core.ManagerError, "仅支持推理强度：medium"):
            core.save_main_profile(
                {
                    "id": "bad-medium",
                    "name": "Bad medium",
                    "provider": provider["id"],
                    "model": "medium-model",
                    "effort": "high",
                }
            )
        profile = core.save_main_profile(
            {
                "id": "medium",
                "name": "Medium",
                "provider": provider["id"],
                "model": "medium-model",
                "effort": "medium",
            }
        )
        settings = self.activate_provider(
            core.load_settings(),
            provider["id"],
            "medium-model",
            profile,
        )
        self.assertEqual(
            tomllib.loads(core.build_codex_config(settings))["model_reasoning_effort"],
            "medium",
        )

    def test_explicit_no_reasoning_rejects_new_choice_and_repairs_legacy_high(self):
        provider = self.install_provider(
            "plain-model",
            {
                "reasoningKnown": True,
                "reasoningSupported": False,
                "efforts": [],
                "defaultEffort": "",
            },
        )
        with self.assertRaisesRegex(core.ManagerError, "明确声明不支持推理强度"):
            core.save_main_profile(
                {
                    "id": "plain",
                    "name": "Plain",
                    "provider": provider["id"],
                    "model": "plain-model",
                    "effort": "high",
                }
            )

        legacy = {
            "id": "legacy-plain",
            "name": "Legacy plain",
            "provider": provider["id"],
            "model": "plain-model",
            "effort": "high",
        }
        settings = self.activate_provider(
            core.load_settings(),
            provider["id"],
            "plain-model",
            legacy,
        )
        parsed = tomllib.loads(core.build_codex_config(settings))
        self.assertNotIn("model_reasoning_effort", parsed)

    def test_api_import_without_effort_keeps_auto_for_no_reasoning_model(self):
        result = core.import_api_account(
            {
                "baseUrl": "https://import-reasoning.example.test/v1",
                "key": "sk-import-reasoning-test",
                "model": "plain-imported-model",
                "models": ["plain-imported-model"],
                "modelCapabilities": {
                    "plain-imported-model": {
                        "reasoningKnown": True,
                        "reasoningSupported": False,
                        "efforts": [],
                    }
                },
                "fetchModels": False,
                "fetchBalance": False,
                "activate": False,
            }
        )
        self.assertEqual(result["profile"]["effort"], "")

    def test_official_capability_is_not_polluted_by_same_named_provider_model(self):
        settings = core._initial_settings()
        shared_model = "shared-model"
        settings["accounts"] = [
            {
                "id": "official-account",
                "label": "Official",
                "authMode": "chatgpt",
                "sourceType": "codex_auth",
                "codexCompatible": True,
                "models": [shared_model],
                "modelCapabilities": {
                    shared_model: {
                        "reasoningKnown": True,
                        "reasoningSupported": True,
                        "efforts": ["medium"],
                        "defaultEffort": "medium",
                    }
                },
                "groupId": "official",
            }
        ]
        settings["providers"].append(
            {
                "id": "same_name_relay",
                "name": "Same name relay",
                "kind": "custom",
                "baseUrl": "https://same-name.example.test/v1",
                "envKey": "SAME_NAME_RELAY_KEY",
                "models": [shared_model],
                "modelCapabilities": {
                    shared_model: {
                        "reasoningKnown": True,
                        "reasoningSupported": False,
                        "efforts": [],
                    }
                },
                "modelDiscoveryState": "ready",
                "lastCheckStatus": "ok",
                "groupId": "relay",
            }
        )
        settings["modelWorkspace"].update(
            {
                "mode": "independent",
                "activeSourceId": "account:official-account",
                "selectAll": True,
                "defaultModelKey": f"account:official-account::{shared_model}",
            }
        )
        settings["subagentRouting"]["strategyId"] = "verification_first"
        settings["mainProfiles"] = [
            {
                "id": "official",
                "name": "Official",
                "provider": "openai",
                "model": shared_model,
                "effort": "medium",
            }
        ]
        settings["activeMainProfileId"] = "official"
        with (
            patch.object(core, "current_auth_state", return_value={"activeAccountId": "official-account"}),
            patch.object(core, "account_invalid_reason", return_value=None),
            patch.object(core, "provider_key_configured", return_value=True),
        ):
            parsed = tomllib.loads(core.build_codex_config(settings))
        self.assertEqual(parsed["model"], shared_model)
        self.assertEqual(parsed["model_reasoning_effort"], "medium")
        self.assertNotIn("model_provider", parsed)

    def test_first_run_without_configured_effort_defaults_to_auto(self):
        core.CONFIG_FILE.write_text('model = "future-official"\n', encoding="utf-8")
        self.assertEqual(core._initial_settings()["mainProfiles"][0]["effort"], "")

    def test_provider_catalog_uses_conservative_cross_version_required_fields(self):
        settings = core._initial_settings()
        record = {
            "key": "provider:p::future",
            "id": "future",
            "slug": "future",
            "displayName": "Future",
            "sourceName": "Provider",
            "sourceKind": "provider",
            "reasoningKnown": False,
            "reasoningSupported": None,
            "efforts": [],
            "defaultEffort": "",
        }
        template = {
            "slug": "gpt-5.4",
            "display_name": "Official template",
            "supported_reasoning_levels": [{"effort": "high"}],
            "shell_type": "unified_exec",
            "support_verbosity": True,
            "truncation_policy": {"mode": "tokens", "limit": 99999},
            "experimental_supported_tools": ["official-only"],
            "supports_reasoning_summary_parameter": True,
            "supports_reasoning_summaries": True,
            "supports_parallel_tool_calls": True,
            "base_instructions": "official-only instructions",
        }
        with (
            patch.object(core, "_configuration_model_records", return_value=[record]),
            patch.object(core, "managed_subagent_model_records", return_value=[]),
            patch.object(core, "_subagents_require_shared_gateway", return_value=False),
            patch.object(core, "_raw_local_model_catalog", return_value={"models": [template]}),
        ):
            catalog, _records = core.build_synced_model_catalog(settings)
        model = catalog["models"][0]
        self.assertEqual(model["supported_reasoning_levels"], [])
        self.assertEqual(model["shell_type"], "default")
        self.assertFalse(model["support_verbosity"])
        self.assertEqual(model["truncation_policy"], {"mode": "bytes", "limit": 10_000})
        self.assertEqual(model["experimental_supported_tools"], [])
        self.assertFalse(model["supports_reasoning_summary_parameter"])
        self.assertFalse(model["supports_reasoning_summaries"])
        self.assertFalse(model["supports_parallel_tool_calls"])
        # Standard Codex instructions remain from the local runtime template;
        # only model capability claims are cleared.
        self.assertEqual(model["base_instructions"], "official-only instructions")

    def test_config_common_context_uses_exact_provider_metadata_not_official_cache(self):
        shared_model = "shared-context-model"
        core.MODELS_CACHE_FILE.write_text(
            json.dumps({"models": [{
                "slug": shared_model,
                "context_window": 272000,
                "max_context_window": 872000,
                "effective_context_window_percent": 95,
            }]}),
            encoding="utf-8",
        )
        settings = core._initial_settings()
        settings["providers"].append(
            {
                "id": "context_relay",
                "name": "Context relay",
                "kind": "custom",
                "baseUrl": "https://context.example.test/v1",
                "envKey": "CONTEXT_RELAY_KEY",
                "models": [shared_model, "unknown-context-model"],
                "modelCapabilities": {
                    shared_model: {
                        "contextWindow": 65536,
                        "maxContextWindow": 131072,
                        "effectiveContextWindowPercent": 80,
                    }
                },
            }
        )
        settings["modelWorkspace"]["activeSourceId"] = "provider:context_relay"
        provider_table = {
            "context_relay": {
                "name": "Context relay",
                "base_url": "https://context.example.test/v1",
            }
        }
        core.atomic_write_json(core.SETTINGS_FILE, settings)
        known = core._codex_config_common_values(
            {
                "model": shared_model,
                "model_provider": "context_relay",
                "model_providers": provider_table,
            }
        )
        unknown = core._codex_config_common_values(
            {
                "model": "unknown-context-model",
                "model_provider": "context_relay",
                "model_providers": provider_table,
            }
        )
        self.assertEqual(known["modelContextDefault"], 65536)
        self.assertEqual(known["modelContextMax"], 131072)
        self.assertEqual(known["modelContextEffectivePercent"], 80)
        self.assertEqual(unknown["modelContextDefault"], 0)
        self.assertEqual(unknown["modelContextMax"], 0)
        self.assertEqual(unknown["modelContextEffectivePercent"], 0)


if __name__ == "__main__":
    unittest.main()
