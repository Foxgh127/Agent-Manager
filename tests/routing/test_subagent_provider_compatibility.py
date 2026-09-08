from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import agent_manager.core as core


class SubagentProviderCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {"CODEX_HOME": self.root, "CONFIG_FILE": self.root / "config.toml", "AGENTS_DIR": self.root / "agents", "MODEL_CATALOG_FILE": self.root / "catalog.json"}.items():
            p = patch.object(core, name, value); p.start(); self.addCleanup(p.stop)
        self.settings = core._initial_settings()
        self.settings["accounts"] = [{"id": "a", "label": "A", "authMode": "chatgpt", "sourceType": "codex_auth", "codexCompatible": True, "models": ["gpt-6-astra", "gpt-5.6-sol"]}]
        self.settings["modelWorkspace"].update({"mode": "independent", "activeSourceId": "account:a", "selectAll": True, "defaultModelKey": "account:a::gpt-6-astra"})
        self.settings["subagentRouting"]["routes"]["hard"] = {"models": ["account:a::gpt-5.6-sol"], "efforts": ["high"]}
        self.patches = [patch.object(core, "current_auth_state", return_value={"activeAccountId": "a"}),
                        patch.object(core, "account_invalid_reason", return_value=None),
                        patch.object(core, "local_model_catalog", return_value=[]),
                        patch.object(core, "_raw_local_model_catalog", return_value={"models": [{"slug": "gpt-6-astra", "supported_reasoning_levels": [{"effort": "high"}], "default_reasoning_level": "high"}]})]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def test_same_account_roles_use_real_model_ids_and_inherit_provider(self):
        specs = core._managed_subagent_specs(self.settings)
        hard = next(spec for spec in specs if spec["level"] == "hard")
        self.assertEqual(hard["model"], "gpt-5.6-sol")
        self.assertEqual(hard["routingMode"], "native")
        self.assertNotIn("model_provider", tomllib.loads(core._render_managed_agent(hard)))
        config = tomllib.loads(core.build_codex_config(self.settings))
        self.assertNotIn("model_provider", config)
        self.assertNotIn("model_catalog_json", config)
        self.assertEqual(core.managed_subagent_model_records(self.settings), [])

    def test_cross_account_roles_share_parent_gateway_and_keep_main_pinned(self):
        self.settings["accounts"].append({"id": "b", "label": "B", "authMode": "chatgpt", "sourceType": "codex_auth", "codexCompatible": True, "models": ["gpt-6-astra", "gpt-5.6-sol"]})
        self.settings["subagentRouting"]["routes"]["expert"] = {"models": ["account:b::gpt-6-astra"], "efforts": ["high"]}
        specs = core._managed_subagent_specs(self.settings)
        self.assertTrue(all(spec["routingMode"] == "gateway" for spec in specs))
        config = tomllib.loads(core.build_codex_config(self.settings))
        self.assertEqual(config["model_provider"], core.AGGREGATE_PROVIDER_ID)
        self.assertEqual(core.resolve_model_route(config["model"], self.settings)["sourceRecordId"], "a")
        expert = next(spec for spec in specs if spec["level"] == "expert")
        self.assertEqual(core.resolve_model_route(expert["model"], self.settings)["sourceRecordId"], "b")
        self.assertNotIn("model_provider", tomllib.loads(core._render_managed_agent(expert)))
        catalog, visible = core.build_synced_model_catalog(self.settings)
        hidden = [record for record in catalog["models"] if record["visibility"] == "hide"]
        self.assertEqual(len(hidden), len(specs))
        self.assertEqual(len(visible), 2)
        self.assertTrue(all(record["prefer_websockets"] is False for record in hidden))

    def test_parent_pool_mode_uses_private_aliases_even_for_same_account(self):
        self.settings["web2api"].update({"activeForCodex": True, "accountIds": ["a"]})
        self.assertTrue(core._subagents_require_shared_gateway(self.settings))
        self.assertTrue(all(spec["model"].startswith("cam-agent-") for spec in core._managed_subagent_specs(self.settings)))

    def test_native_routes_do_not_require_gateway(self):
        self.settings["subagentRouting"]["strategyId"] = "verification_first"
        self.assertFalse(core._subagents_require_shared_gateway(self.settings))
        self.assertEqual(core.subagent_runtime_summary(self.settings)["mode"], "native")


if __name__ == "__main__":
    unittest.main()
