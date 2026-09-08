"""Generated Codex labels and delegation contracts; no live config writes."""
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import agent_manager.core as core


class CodexLabelsPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("CODEX_HOME", "CONFIG_FILE", "AGENTS_FILE", "AGENTS_DIR", "STATE_DIR",
                     "SETTINGS_FILE", "SECRETS_FILE", "MODEL_CATALOG_FILE", "MODELS_CACHE_FILE"):
            target = self.root if name == "CODEX_HOME" else self.root / name.lower()
            p = patch.object(core, name, target)
            p.start()
            self.addCleanup(p.stop)
        for name, value in {
            "discover_agents": [], "_initial_providers": [],
            "current_auth_state": {}, "account_invalid_reason": None,
            "provider_key_configured": True, "local_model_catalog": [],
            "codex_version": "codex-cli 0.153.4",
            "_raw_local_model_catalog": {"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6 Astra"}]},
        }.items():
            p = patch.object(core, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.settings = core._initial_settings()
        for provider_id, name in (("hajimi", "哈基米"), ("other", "另一站点")):
            self.settings["providers"].append({
                "id": provider_id, "kind": "custom", "name": name,
                "baseUrl": f"https://{provider_id}.example.test/v1",
                "envKey": f"{provider_id.upper()}_KEY", "models": ["gpt-6-astra"],
                "modelCapabilities": {}, "modelDiscoveryState": "ready",
                "lastCheckStatus": "ok", "groupId": "relay",
            })
        self.settings["modelWorkspace"].update({
            "mode": "aggregate", "activeSourceId": "provider:hajimi", "selectAll": True,
            "defaultModelKey": "provider:hajimi::gpt-6-astra",
        })

    def test_generated_provider_uses_selected_card_and_preserves_gateway_identity(self):
        doc = tomllib.loads(core.build_codex_config(self.settings))
        self.assertEqual(doc["model_provider"], core.AGGREGATE_PROVIDER_ID)
        provider = doc["model_providers"][core.AGGREGATE_PROVIDER_ID]
        self.assertEqual(provider["name"], "哈基米")
        self.assertEqual(provider["base_url"], "http://127.0.0.1:17860/v1")
        self.assertEqual(provider["env_key"], core.AGGREGATE_ENV_KEY)
        self.assertEqual(core.resolve_model_route(doc["model"], self.settings)["sourceRecordId"], "hajimi")
        # The target selection wins even while the old active card is visible.
        self.settings["modelWorkspace"]["defaultModelKey"] = "provider:other::gpt-6-astra"
        switched = tomllib.loads(core.build_codex_config(self.settings))
        self.assertEqual(switched["model_providers"][core.AGGREGATE_PROVIDER_ID]["name"], "另一站点")

    def test_card_rename_changes_only_label_not_route(self):
        before = tomllib.loads(core.build_codex_config(self.settings))
        self.settings["providers"][0]["name"] = '自定义 "API" 名称'
        after = tomllib.loads(core.build_codex_config(self.settings))
        self.assertEqual(before["model"], after["model"])
        self.assertEqual(after["model_providers"][core.AGGREGATE_PROVIDER_ID]["name"], '自定义 "API" 名称')

    def test_duplicate_models_and_private_children_have_plain_names_unique_slugs(self):
        self.settings["subagentRouting"]["routes"]["hard"] = {
            "models": ["provider:other::gpt-6-astra"], "efforts": ["high"]}
        catalog, records = core.build_synced_model_catalog(self.settings)
        rows = catalog["models"]
        self.assertTrue(any(row["visibility"] == "hide" for row in rows))
        self.assertEqual({row["display_name"] for row in rows}, {"gpt-6-astra"})
        self.assertEqual(len({row["slug"] for row in rows}), len(rows))
        self.assertTrue(all("路由到" in row["description"] for row in rows))
        self.assertTrue(any("·" in record["displayName"] for record in records))
        for row in rows:
            routed = core.resolve_model_route(row["slug"], self.settings)
            self.assertEqual(routed["id"], "gpt-6-astra")
            self.assertIn(routed["sourceRecordId"], {"hajimi", "other"})

    def test_independent_provider_and_official_account_labels(self):
        self.settings["modelWorkspace"]["mode"] = "independent"
        doc = tomllib.loads(core.build_codex_config(self.settings))
        self.assertEqual(doc["model_provider"], "hajimi")
        self.assertEqual(doc["model_providers"]["hajimi"]["name"], "哈基米")
        self.assertEqual(core._codex_gateway_provider_name(self.settings, {
            "sourceKind": "account", "sourceName": "Official account"}), "Official account")
        self.assertEqual(core._codex_gateway_provider_name(self.settings, None), "哈基米")

    def test_adaptive_migration_preserves_custom_prompt_and_route_choices(self):
        custom = "User-owned dispatch policy: keep this exact text."
        self.settings["strategies"][2]["instructions"] = custom
        self.settings["strategies"][0]["instructions"] = "obsolete builtin"
        self.settings["subagentRouting"]["prompt"] = "obsolete builtin"
        self.settings["subagentRouting"]["routes"]["expert"] = {
            "models": ["provider:hajimi::gpt-6-astra"], "efforts": ["high"]}
        normalized, _ = core._migrate_settings(self.settings)
        self.assertEqual(normalized["subagentRouting"]["prompt"], core.OPTIMAL_ADAPTIVE_INSTRUCTIONS)
        self.assertEqual(normalized["strategies"][2]["instructions"], custom)
        self.assertEqual(normalized["subagentRouting"]["routes"]["expert"]["efforts"], ["high"])
        normalized["subagentRouting"].update({"strategyId": "parallel_first", "prompt": custom})
        again, _ = core._migrate_settings(normalized)
        self.assertEqual(again["subagentRouting"]["prompt"], custom)

    def test_generated_policy_has_contract_capacity_lifecycle_and_model_guidance(self):
        block = core.build_routing_block(self.settings)
        for phrase in ("acceptance criteria", "owned files", "completion window", "local agent-limit",
                       "uncertain spawn outcome", "ledger", "compacted handoff", "terminal-but-unread",
                       "GPT-6 Astra", "GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna",
                       "Do not automatically maximize effort", "task-quality failure"):
            self.assertIn(phrase, block)
        for level in core.DIFFICULTIES:
            self.assertIn(f"invoke `cam_{level}_1`", block)
        spec = core._managed_subagent_specs(self.settings)[0]
        worker = tomllib.loads(core._render_managed_agent(spec))
        self.assertIn("Other agents share this workspace", worker["developer_instructions"])
        self.assertIn("complete, blocked, or failed", worker["developer_instructions"])
        self.assertNotIn("model_provider", worker)

    def test_effort_defaults_respect_source_support_and_explicit_effort(self):
        for level, effort in core.DEFAULT_EFFORT_BY_DIFFICULTY.items():
            self.assertEqual(core._default_model_reasoning_effort({
                "reasoningKnown": True, "efforts": list(core.VALID_EFFORTS)}, level), effort)
        self.assertEqual(core._default_model_reasoning_effort({
            "reasoningKnown": True, "efforts": ["medium"], "defaultEffort": "medium"}, "expert"), "medium")
        self.assertEqual(core._default_model_reasoning_effort({"reasoningKnown": False}, "expert"), "")

    def test_worker_contract_uses_native_model_without_changing_configured_effort(self):
        spec = core._managed_subagent_specs(self.settings)[0]
        for model, phrase in {
            "gpt-6-astra": "Calibrate testing",
            "gpt-5.6-sol": "domain context",
            "gpt-5.6": "domain context",
            "gpt-5.6-terra": "integration boundary",
            "gpt-5.6-luna": "output fields",
        }.items():
            with self.subTest(model=model):
                role = {**spec, "nativeModel": model, "model": "cam-private-alias", "effort": "high"}
                generated = tomllib.loads(core._render_managed_agent(role))
                self.assertIn(phrase, generated["developer_instructions"])
                self.assertEqual(generated["model"], "cam-private-alias")
                self.assertEqual(generated["model_reasoning_effort"], "high")

    def test_native_policy_stays_unmanaged_and_preview_has_no_file_writes(self):
        self.settings["subagentRouting"]["strategyId"] = "verification_first"
        self.assertEqual(core.build_routing_block(self.settings), "")
        before = sorted(str(path) for path in self.root.rglob("*"))
        core.build_codex_config(self.settings)
        core.build_synced_model_catalog(self.settings)
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
