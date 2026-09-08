import unittest
from unittest.mock import patch
from types import SimpleNamespace
import agent_manager_core as core
import usage_export_service as service


class UsageRangeTests(unittest.TestCase):
    def test_export_inclusive_range_matches_beijing_day_and_excludes_unknown(self):
        records = [
            {"timestamp": "2026-09-06T15:59:59Z", "tokens": 1},
            {"timestamp": "2026-09-06T16:00:00Z", "tokens": 2},
            {"date": "2026-09-08", "tokens": 3},
            {"date": "2026-09-09", "tokens": 4},
            {"tokens": 99},
        ]
        runtime = SimpleNamespace(web2api=SimpleNamespace(usage_snapshot=lambda: {"records": records}))
        payload = {"source": "gateway", "sourceFilter": "all", "model": "all", "date": "all", "dateFrom": "2026-09-07", "dateTo": "2026-09-08"}
        with patch.object(core, "save_json_export_to_downloads", side_effect=lambda doc, name: doc):
            document = service.export_usage(runtime, payload)
        self.assertEqual([item["totalTokens"] for item in document["records"]], [2, 3])
        self.assertEqual(document["filters"]["dateTo"], "2026-09-08")

    def test_export_rejects_partial_invalid_or_inverted_range(self):
        base = {"source": "gateway", "sourceFilter": "all", "model": "all", "date": "all"}
        for values in [dict(dateFrom="2026-09-01"), dict(dateFrom="2026-02-30", dateTo="2026-09-08"), dict(dateFrom="2026-09-09", dateTo="2026-09-08")]:
            with self.subTest(values=values), self.assertRaises(core.ManagerError):
                service._validate_payload({**base, **values})

    def test_preference_validation_does_not_store_invalid_custom_dates(self):
        self.assertEqual(core._normalize_usage_range({}, strict=False)["mode"], "last7")
        with self.assertRaises(core.ManagerError):
            core._normalize_usage_range({"mode": "custom", "customStart": "2026-09-08", "customEnd": "2026-09-01"}, strict=True)
        result = core._normalize_usage_range({"mode": "custom", "customStart": "2026-09-01", "customEnd": "2026-09-08"}, strict=True)
        self.assertEqual(result["customStart"], "2026-09-01")

if __name__ == "__main__":
    unittest.main()
