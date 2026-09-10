import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import agent_manager.core as core


class ContextCapacityTests(unittest.TestCase):
    def catalog(self, *, requested=1000000, managed=True, kind="account", maximum=872000):
        settings=core._initial_settings()
        settings["runtimeTuning"].update(modelContextWindow=requested,
            managedFields=["modelContextWindow"] if managed else [])
        record={"key":"account:a::gpt-6-astra", "id":"gpt-6-astra", "slug":"gpt-6-astra",
                "sourceKind":kind, "sourceRecordId":"a", "sourceName":"Test", "sourceId":"account:a"}
        if kind == "provider":
            record.update(contextWindow=64000, maxContextWindow=128000, effectiveContextWindowPercent=90)
        raw={"models":[{"slug":"gpt-6-astra", "context_window":272000,
            "max_context_window":maximum, "effective_context_window_percent":95,
            "supported_reasoning_levels":[], "base_instructions":"native instructions"}]}
        with (patch.object(core,"_configuration_model_records",return_value=[record]),
              patch.object(core,"_raw_local_model_catalog",return_value=raw),
              patch.object(core,"managed_subagent_model_records",return_value=[]),
              patch.object(core,"_subagents_require_shared_gateway",return_value=False),
              patch.object(core,"_codex_compatible_reasoning_efforts",side_effect=lambda value:value)):
            result,_=core.build_synced_model_catalog(settings)
        return settings, record, result

    def test_explicit_one_million_updates_the_catalog_ceiling_and_keeps_headroom(self):
        _,_,catalog=self.catalog()
        model=catalog["models"][0]
        self.assertEqual(model["max_context_window"],922000)
        self.assertEqual(model["effective_context_window_percent"],95)
        self.assertEqual(model["context_window"],272000)
        self.assertEqual(model["base_instructions"],"native instructions")

    def test_default_unmanaged_provider_and_larger_native_limits_are_preserved(self):
        for request,managed in ((0,True),(1000000,False)):
            with self.subTest(request=request,managed=managed):
                self.assertEqual(self.catalog(requested=request,managed=managed)[2]["models"][0]["max_context_window"],872000)
        provider=self.catalog(kind="provider")[2]["models"][0]
        self.assertEqual(provider["max_context_window"],128000)
        self.assertEqual(provider["effective_context_window_percent"],90)
        self.assertEqual(self.catalog(maximum=1100000)[2]["models"][0]["max_context_window"],1100000)
        self.assertEqual(self.catalog(requested=2000000)[2]["models"][0]["max_context_window"],922000)

    def test_official_extended_context_keeps_a_synced_catalog_until_reset(self):
        settings,record,_=self.catalog()
        settings["accounts"]=[{"id":"a","authMode":"chatgpt","codexCompatible":True,"models":["gpt-6-astra"]}]
        settings["modelWorkspace"].update(mode="independent",activeSourceId="account:a",syncToCodex=True)
        with patch.object(core,"_subagents_require_shared_gateway",return_value=False):
            self.assertFalse(core._use_native_official_model_catalog(settings,[record]))
            settings["runtimeTuning"]["modelContextWindow"]=0
            self.assertTrue(core._use_native_official_model_catalog(settings,[record]))

    def test_health_detects_stale_context_ceiling_even_when_toml_matches(self):
        settings,record,catalog=self.catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); catalog_path=root/"catalog.json"; config_path=root/"config.toml"
            config=f'model="gpt-6-astra"\nmodel_context_window=1000000\nmodel_catalog_json="{catalog_path.as_posix()}"\n'
            config_path.write_text(config,encoding="utf-8")
            with (patch.object(core,"CONFIG_FILE",config_path),patch.object(core,"MODEL_CATALOG_FILE",catalog_path),
                  patch.object(core,"AGENTS_FILE",root/"AGENTS.md"),
                  patch.object(core,"build_codex_config",return_value=config),
                  patch.object(core,"build_agents_file",return_value=""),
                  patch.object(core,"selected_model_records",return_value=[record])):
                # Use the exact Windows path string emitted by the renderer.
                config_path.write_text(config.replace(catalog_path.as_posix(),str(catalog_path).replace('\\','\\\\')),encoding="utf-8")
                catalog_path.write_text(json.dumps({"models":[{"slug":"gpt-6-astra","max_context_window":872000}]}),encoding="utf-8")
                with patch.object(core,"build_codex_config",return_value=config_path.read_text(encoding="utf-8")):
                    self.assertFalse(core.configuration_status(settings)["fullyApplied"])
                    catalog_path.write_text(json.dumps(catalog),encoding="utf-8")
                    self.assertTrue(core.configuration_status(settings)["fullyApplied"])
                    catalog_path.write_text('{"models":null}',encoding="utf-8")
                    self.assertFalse(core.configuration_status(settings)["fullyApplied"])
                    catalog_path.write_text(json.dumps(catalog),encoding="utf-8")
                    config_path.write_text(config_path.read_text(encoding="utf-8").replace("1000000","900000"),encoding="utf-8")
                    self.assertFalse(core.configuration_status(settings)["contextActive"])

    def test_official_context_ignores_inactive_provider_and_exposes_reference_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/"models_cache.json"
            cache.write_text(json.dumps({"models":[{"slug":"gpt-6-astra","context_window":272000,"max_context_window":872000}]}),encoding="utf-8")
            with patch.object(core,"MODELS_CACHE_FILE",cache), patch.object(core,"read_json",return_value={}):
                common=core._codex_config_common_values({"model":"gpt-6-astra","model_providers":{"old":{"base_url":"https://old.example.test/v1","stream_max_retries":0}}})
            self.assertEqual(common["providerId"],"")
            self.assertEqual(common["modelContextDefault"],272000)
            self.assertEqual(common["modelContextMax"],872000)
            self.assertEqual(common["modelContextReferenceMax"],1050000)
            self.assertEqual(common["modelInputReferenceMax"],922000)
            self.assertFalse(common["vpnCompatibility"])

    def test_direct_provider_capability_never_uses_stale_workspace_route(self):
        settings={"modelWorkspace":{"activeSourceId":"provider:other"},"providers":[{"id":"selected","kind":"custom","models":["gpt-6-astra"],"modelCapabilities":{"gpt-6-astra":{"contextWindow":64000,"maxContextWindow":128000}}}]}
        with patch.object(core,"resolve_model_route",side_effect=AssertionError("direct provider must not resolve another workspace")):
            result=core._codex_model_context_metadata("gpt-6-astra",provider_id="selected",settings=settings)
        self.assertEqual(result["modelContextMax"],128000)
        self.assertEqual(result["modelContextReferenceMax"],0)

    def test_official_gateway_alias_recovers_native_context_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/"models_cache.json"
            cache.write_text(json.dumps({"models":[{"slug":"gpt-6-astra","context_window":272000,
                "max_context_window":872000,"effective_context_window_percent":95}]}),encoding="utf-8")
            with (patch.object(core,"MODELS_CACHE_FILE",cache),
                  patch.object(core,"resolve_model_route",return_value={"id":"gpt-6-astra","sourceKind":"account"})):
                metadata=core._codex_model_context_metadata("astra-alias",provider_id=core.AGGREGATE_PROVIDER_ID,
                    settings={"modelWorkspace":{"mode":"aggregate"}})
            self.assertEqual(metadata["modelId"],"astra-alias")
            self.assertEqual(metadata["modelContextDefault"],272000)
            self.assertEqual(metadata["modelContextEffectivePercent"],95)
            self.assertEqual(metadata["modelContextReferenceMax"],1050000)

    def test_codex_limit_map_selects_base_codex_not_spark(self):
        parsed=core._parse_chatgpt_usage({"rateLimits":{"limitId":"codex_bengalfox","primary":{"usedPercent":100,"windowDurationMins":10080}},
            "rateLimitsByLimitId":{"codex":{"planType":"pro","primary":{"usedPercent":4,"windowDurationMins":10080}}}})
        self.assertEqual(parsed["weekly"]["remainingPercent"],96)
        self.assertEqual(parsed["planLabel"],"Pro")
        only_spark=core._parse_chatgpt_usage({"rateLimits":{"limitId":"codex_bengalfox","primary":{"usedPercent":100,"windowDurationMins":10080}}})
        self.assertIsNone(only_spark["weekly"])


if __name__=="__main__":
    unittest.main()
