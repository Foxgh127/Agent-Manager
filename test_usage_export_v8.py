import copy
import unittest
from unittest.mock import patch

import agent_manager_core as core
import usage_export_service as service


class _Web2Api:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def usage_snapshot(self):
        self.calls += 1
        return self.snapshot


class _Runtime:
    def __init__(self, snapshot):
        self.web2api = _Web2Api(snapshot)


class UsageExportV8Tests(unittest.TestCase):
    def export(self, snapshot, payload):
        captured = {}

        def writer(document, name):
            captured.update({"document": document, "name": name})
            return {"fileName": name + ".json", "path": "mock-download"}

        runtime = _Runtime(snapshot)
        with patch.object(service.core, "save_json_export_to_downloads", side_effect=writer):
            result = service.export_usage(runtime, payload)
        return result, captured, runtime

    def test_export_preserves_partial_scan_coverage_without_arbitrary_metadata(self):
        snapshot = {"accountAttribution": {"records": []}, "codexSessions": {"coverage": {
            "indexedFiles": 3, "partialFiles": 1, "pendingBytes": 1024, "token": "do-not-export"
        }}}
        _result, saved, _runtime = self.export(snapshot, {"source": "accounts", "sourceFilter": "all", "model": "all", "date": "all"})
        self.assertEqual(saved["document"]["sessionCoverage"], {"indexedFiles": 3, "partialFiles": 1, "pendingBytes": 1024})

    def test_accounts_filters_normalizes_and_removes_secrets_without_mutation(self):
        snapshot = {
            "accountAttribution": {
                "records": [
                    {
                        "date": "2026-09-06", "accountId": "same", "modelName": "gpt-a",
                        "agentRole": "mainAgent", "requestCount": 2, "inputTokens": 10,
                        "outputTokens": 3, "cachedInputTokens": 4, "reasoningOutputTokens": 1,
                        "token": "secret", "prompt": "private", "password": "never",
                    },
                    {"date": "2026-09-06", "providerId": "same", "model": "gpt-a", "tokens": 99},
                    {"date": "2026-09-05", "accountName": "Legacy", "model": "gpt-b", "tokens": 7},
                ],
                "recentRequests": [{"date": "2026-09-06", "accountId": "wrong", "tokens": 999}],
            }
        }
        original = copy.deepcopy(snapshot)
        result, saved, runtime = self.export(snapshot, {
            "source": "accounts", "sourceFilter": "account:same", "model": "gpt-a", "date": "2026-09-06"
        })
        self.assertEqual(result["fileName"], "usage-accounts.json")
        self.assertEqual(runtime.web2api.calls, 1)
        self.assertEqual(snapshot, original)
        document = saved["document"]
        self.assertEqual(document["recordCount"], 1)
        self.assertEqual(document["records"][0]["sourceKey"], "account:same")
        self.assertEqual(document["records"][0]["model"], "gpt-a")
        self.assertEqual(document["records"][0]["totalTokens"], 13)
        self.assertEqual(document["records"][0]["cachedInputTokens"], 4)
        self.assertFalse(document["containsRawConversations"])
        self.assertNotIn("prompt", document["records"][0])
        self.assertNotIn("token", document["records"][0])

    def test_account_and_provider_ids_do_not_collide(self):
        records = [
            {"date": "2026-09-06", "accountId": "same", "tokens": 1},
            {"date": "2026-09-06", "providerId": "same", "tokens": 2},
        ]
        snapshot = {"accountAttribution": {"records": records}}
        _, account_saved, _ = self.export(snapshot, {"source": "accounts", "sourceFilter": "account:same", "model": "all", "date": "all"})
        _, provider_saved, _ = self.export(snapshot, {"source": "accounts", "sourceFilter": "provider:same", "model": "all", "date": "all"})
        self.assertEqual(account_saved["document"]["records"][0]["totalTokens"], 1)
        self.assertEqual(provider_saved["document"]["records"][0]["totalTokens"], 2)

    def test_codex_role_derivation_and_role_filter(self):
        snapshot = {"codexSessions": {"items": [
            {"day": "2026-09-06", "requestedModel": "gpt-x", "requestClassification": "explicit_subagent", "tokens": 5},
            {"day": "2026-09-06", "requestedModel": "gpt-x", "requestClassification": "explicit_main_agent", "tokens": 8},
        ]}}
        _, saved, _ = self.export(snapshot, {"source": "codex", "sourceFilter": "role:subagent", "model": "all", "date": "all"})
        self.assertEqual(saved["document"]["recordCount"], 1)
        self.assertEqual(saved["document"]["records"][0]["sourceKey"], "role:subagent")

    def test_gateway_falls_back_from_empty_arrays_to_days_routes_not_recent(self):
        snapshot = {
            "records": [], "items": [], "requests": [],
            "days": {"2026-09-06": {"routes": {"r": {"providerId": "p1", "routedModel": "m", "requests": 4, "total_tokens": 20}}}},
            "recentRequests": [{"providerId": "sample", "tokens": 999}],
        }
        _, saved, _ = self.export(snapshot, {"source": "gateway", "sourceFilter": "all", "model": "all", "date": "all"})
        row = saved["document"]["records"][0]
        self.assertEqual(row["sourceKey"], "provider:p1")
        self.assertEqual(row["requestCount"], 4)
        self.assertEqual(row["totalTokens"], 20)

    def test_empty_selected_source_exports_empty_document(self):
        _, saved, _ = self.export({"accountAttribution": {}}, {"source": "accounts", "sourceFilter": "all", "model": "all", "date": "all"})
        self.assertEqual(saved["document"]["recordCount"], 0)
        self.assertEqual(saved["document"]["records"], [])

    def test_missing_selected_source_is_an_error(self):
        with self.assertRaises(core.ManagerError):
            service.export_usage(_Runtime({"records": [{"tokens": 9}]}), {"source": "accounts", "sourceFilter": "all", "model": "all", "date": "all"})

    def test_invalid_source_payload_and_dates_are_rejected_before_snapshot(self):
        cases = [
            None,
            {"source": "bogus", "sourceFilter": "all", "model": "all", "date": "all"},
            {"source": "gateway", "sourceFilter": "role:admin", "model": "all", "date": "all"},
            {"source": "gateway", "sourceFilter": "all", "model": "all", "date": "2026-02-30"},
            {"source": "gateway", "sourceFilter": "all", "model": "x" * 257, "date": "all"},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(core.ManagerError):
                service.export_usage(_Runtime({}), payload)


if __name__ == "__main__":
    unittest.main()
