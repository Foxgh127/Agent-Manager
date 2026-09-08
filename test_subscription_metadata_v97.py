import unittest
from datetime import datetime, timezone

from subscription_metadata import extract_plan_metadata, normalize_plan_label, select_plan_metadata


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class SubscriptionMetadataTests(unittest.TestCase):
    def extract(self, payload, source="usage"):
        return extract_plan_metadata(payload, source=source, now=NOW)

    def test_generic_pro_is_not_a_multiplier(self):
        self.assertEqual(self.extract({"plan_type": "pro"})["planLabel"], "Pro")

    def test_explicit_aliases_and_product_slugs(self):
        for value, expected in (("codex-pro-20x", "Pro 20x"),
                                ("chatgpt_pro_20x_monthly", "Pro 20x"),
                                ("chatgptprolite", "Pro 5x"),
                                ("pro-max", "Pro 20x")):
            with self.subTest(value=value):
                self.assertEqual(normalize_plan_label(value), expected)

    def test_no_substring_or_unknown_product_guessing(self):
        for value in ("professional", "not-pro-max", "Team 20x capacity", "prod_unknown_20x"):
            with self.subTest(value=value):
                self.assertEqual(normalize_plan_label(value), value)
                self.assertEqual(self.extract({"product_id": value}), {})

    def test_account_check_entitlement_refines_generic(self):
        result = self.extract({"account": {"plan_type": "pro"},
                               "entitlement": {"subscription_plan": "pro", "product_id": "chatgpt_pro_20x"}}, "entitlement")
        self.assertEqual(result["planLabel"], "Pro 20x")
        self.assertEqual(result["planEvidence"]["field"], "entitlement.productid")

    def test_nested_product_object(self):
        self.assertEqual(self.extract({"subscription": {"plan_type": "pro", "product": {"id": "pro_20x_monthly"}}})["planLabel"], "Pro 20x")

    def test_product_evidence_cannot_be_masked_by_key_order(self):
        for payload in ({"plan_type": "pro", "product_id": "pro_max"},
                        {"product_id": "pro_max", "plan_type": "pro"}):
            self.assertEqual(self.extract(payload)["planLabel"], "Pro 20x")

    def test_subscription_tier_and_multiplier_require_pro_context(self):
        for detail in ({"tier": "20x"}, {"plan_multiplier": 20}, {"multiplier": "20x"}):
            with self.subTest(detail=detail):
                self.assertEqual(self.extract({"plan_type": "pro", "subscription": detail})["planLabel"], "Pro 20x")
                self.assertEqual(self.extract({"plan_type": "team", "subscription": detail})["planLabel"], "Team")
        self.assertEqual(self.extract({"subscription": {"multiplier": 20}}), {})

    def test_rate_multiplier_and_quota_boost_are_not_tier(self):
        result = self.extract({"plan_type": "pro", "rate_multiplier": 20,
                               "multiplier": 20, "rate_limit": {"tier": "pro20x"},
                               "additional_rate_limits": [{"plan": "pro20x"}]})
        self.assertEqual(result["planLabel"], "Pro")

    def test_does_not_join_unrelated_sibling_values(self):
        result = self.extract({"subscription": {"plan_type": "pro"},
                               "billing": {"tier": "20x"}})
        self.assertEqual(result["planLabel"], "Pro")

    def test_ended_five_x_does_not_mask_current_twenty_x(self):
        result = self.extract({"plan_type": "pro", "entitlements": [
            {"subscription_plan": "pro5x", "expires_at": "2026-08-01T00:00:00Z"},
            {"subscription_plan": "pro20x", "expires_at": "2026-10-01T00:00:00Z"},
        ]})
        self.assertEqual(result["planLabel"], "Pro 20x")

    def test_expired_inactive_and_history_do_not_resurrect_tier(self):
        for record in ({"status": "expired"}, {"is_active": False},
                       {"expires_at": "2026-08-01T00:00:00Z"}):
            payload = {"plan_type": "pro", "subscription": {"plan_type": "pro20x", **record},
                       "history": [{"plan_type": "pro20x"}]}
            self.assertEqual(self.extract(payload)["planLabel"], "Pro")

    def test_future_period_end_does_not_rank_as_update_time(self):
        result = self.extract({"entitlements": [
            {"subscription_plan": "pro5x", "current_period_end": "2027-01-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z"},
            {"subscription_plan": "pro20x", "current_period_end": "2026-10-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"},
        ]})
        self.assertEqual(result["planLabel"], "Pro 20x")

    def test_same_strength_conflict_is_unknown_pro(self):
        result = self.extract({"entitlements": [{"plan": "pro5x"}, {"plan": "pro20x"}]})
        self.assertEqual(result["planLabel"], "Pro")
        self.assertTrue(result["planEvidence"]["conflict"])

    def test_account_collections_require_caller_selection(self):
        self.assertEqual(self.extract({"accounts": {"other": {"plan_type": "pro20x"}}}), {})

    def test_authoritative_current_explicit_beats_old_usage(self):
        usage = self.extract({"plan_type": "pro5x"})
        subscription = self.extract({"plan_type": "pro20x"}, "entitlement")
        self.assertEqual(select_plan_metadata(usage, subscription, now=NOW)["planLabel"], "Pro 20x")

    def test_generic_subscription_does_not_erase_current_explicit_usage(self):
        usage = self.extract({"plan_type": "pro20x"})
        subscription = self.extract({"plan_type": "pro"}, "entitlement")
        self.assertEqual(select_plan_metadata(usage, subscription, now=NOW)["planLabel"], "Pro 20x")

    def test_newer_dated_generic_change_invalidates_old_variant(self):
        old = self.extract({"plan_type": "pro5x", "updated_at": "2026-08-01T00:00:00Z"})
        new = self.extract({"plan_type": "pro", "updated_at": "2026-09-01T00:00:00Z"}, "entitlement")
        self.assertEqual(select_plan_metadata(old, new, now=NOW)["planLabel"], "Pro")

    def test_newer_usage_change_beats_older_authoritative_subscription(self):
        old = self.extract({"plan_type": "pro5x", "updated_at": "2026-08-01T00:00:00Z"}, "entitlement")
        new = self.extract({"plan_type": "pro20x", "updated_at": "2026-09-01T00:00:00Z"})
        self.assertEqual(select_plan_metadata(old, new, now=NOW)["planLabel"], "Pro 20x")

    def test_current_free_subscription_replaces_paid_usage(self):
        old = self.extract({"plan_type": "pro20x"})
        new = self.extract({"plan_type": "free"}, "entitlement")
        self.assertEqual(select_plan_metadata(old, new, now=NOW)["planLabel"], "Free")

    def test_fresh_observation_replaces_stale_authoritative_variant(self):
        old = extract_plan_metadata({"plan_type": "pro5x"}, source="entitlement", observed_at="2026-08-01T00:00:00Z", now=NOW)
        new = extract_plan_metadata({"plan_type": "pro20x"}, source="usage", observed_at="2026-09-08T00:00:00Z", now=NOW)
        self.assertEqual(select_plan_metadata(old, new, now=NOW)["planLabel"], "Pro 20x")

    def test_plain_legacy_snapshot_and_malformed_evidence_are_supported(self):
        for evidence in (None, "malformed", {"specificity": "not-an-integer"}):
            self.assertEqual(select_plan_metadata({"plan": "pro20x", "planEvidence": evidence}, now=NOW)["planLabel"], "Pro 20x")

    def test_output_does_not_copy_credentials(self):
        result = self.extract({"plan_type": "pro", "access_token": "FAKE_SECRET", "session": {"token": "FAKE_SECRET"}})
        self.assertNotIn("FAKE_SECRET", repr(result))


if __name__ == "__main__":
    unittest.main()
