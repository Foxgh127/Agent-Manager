import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import agent_manager_core as core


class ContextCapacityTests(unittest.TestCase):
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
            self.assertFalse(common["vpnCompatibility"])

    def test_direct_provider_capability_never_uses_stale_workspace_route(self):
        settings={"modelWorkspace":{"activeSourceId":"provider:other"},"providers":[{"id":"selected","kind":"custom","models":["gpt-6-astra"],"modelCapabilities":{"gpt-6-astra":{"contextWindow":64000,"maxContextWindow":128000}}}]}
        with patch.object(core,"resolve_model_route",side_effect=AssertionError("direct provider must not resolve another workspace")):
            result=core._codex_model_context_metadata("gpt-6-astra",provider_id="selected",settings=settings)
        self.assertEqual(result["modelContextMax"],128000)
        self.assertEqual(result["modelContextReferenceMax"],0)

    def test_codex_limit_map_selects_base_codex_not_spark(self):
        parsed=core._parse_chatgpt_usage({"rateLimits":{"limitId":"codex_bengalfox","primary":{"usedPercent":100,"windowDurationMins":10080}},
            "rateLimitsByLimitId":{"codex":{"planType":"pro","primary":{"usedPercent":4,"windowDurationMins":10080}}}})
        self.assertEqual(parsed["weekly"]["remainingPercent"],96)
        self.assertEqual(parsed["planLabel"],"Pro")
        only_spark=core._parse_chatgpt_usage({"rateLimits":{"limitId":"codex_bengalfox","primary":{"usedPercent":100,"windowDurationMins":10080}}})
        self.assertIsNone(only_spark["weekly"])


if __name__=="__main__":
    unittest.main()
