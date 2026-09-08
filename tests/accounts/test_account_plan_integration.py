"""Offline integration of account metadata and bounded OAuth recovery."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import agent_manager.core as core
import tests.usage.test_oauth_quota as fixtures


class AccountPlanIntegrationV97Tests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(core, "_open_same_origin_request", side_effect=AssertionError("No network"))
        guard.start()
        self.addCleanup(guard.stop)
        live = patch.object(core, "_live_official_account_matches", return_value=False)
        live.start()
        self.addCleanup(live.stop)
        self.account = fixtures.OAuthQuotaV97Tests.account()

    def probe(self, subscription, usage=None, *, account=None, parallel=False):
        account = account or self.account
        usage = usage or fixtures.OAuthQuotaV97Tests.usage()
        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_account_chatgpt_credentials", return_value={"accessToken": "fixture", "accountId": "selected"}),
            patch.object(core, "_fetch_chatgpt_json", side_effect=lambda url, *_a, **_k: usage if url == core.CHATGPT_USAGE_URL else {"models": [{"slug": "gpt-current"}]}),
            patch.object(core, "_fetch_chatgpt_subscription_status", return_value=subscription) as sub,
        ):
            result = core._probe_codex_account(account["id"], parallel, force_metadata=True)
        return result, sub.call_count

    def test_manual_refresh_reads_subscription_even_before_expiry(self):
        result, count = self.probe({"plan": "pro20x", "subscriptionExpiresAt": self.account["subscriptionExpiresAt"]})
        self.assertEqual(count, 1)
        self.assertEqual(result["planLabel"], "Pro 20x")
        self.assertEqual(result["usage"]["planLabel"], "Pro 20x")

    def test_concurrent_batch_authority_not_completion_time_selects_tier(self):
        for parallel in (False, True):
            usage = {**fixtures.OAuthQuotaV97Tests.usage(), "plan_type": "pro5x"}
            subscription = core._parse_chatgpt_subscription({"plan_type": "pro20x"}, "selected")
            subscription["planEvidence"]["observedAt"] = "2000-01-01T00:00:00Z"
            result, _ = self.probe(subscription, usage, parallel=parallel)
            self.assertEqual(result["planLabel"], "Pro 20x")
            self.assertEqual(result["planEvidence"]["observedAt"], result["usage"]["updatedAt"])

    def test_generic_subscription_keeps_current_explicit_usage(self):
        usage = {**fixtures.OAuthQuotaV97Tests.usage(), "plan_type": "pro20x"}
        result, _ = self.probe({"plan": "pro"}, usage)
        self.assertEqual(result["planLabel"], "Pro 20x")

    def test_expired_cached_five_x_cannot_replace_current_twenty_x(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.account.update(subscriptionExpiresAt=past, planEvidence={"source": "entitlement", "expiresAt": None})
        result, _ = self.probe({"plan": "pro20x"})
        self.assertEqual(result["planLabel"], "Pro 20x")

    def test_selected_account_entitlement_and_product_are_preserved(self):
        result = core._parse_chatgpt_subscription_account_check({"accounts": {
            "unrelated": {"account": {"id": "other", "is_default": True, "plan_type": "pro5x"}},
            "wanted": {"account": {"id": "selected", "plan_type": "pro"},
                       "entitlement": {"subscription_plan": "chatgptpro", "product_id": "chatgpt_pro_20x"}},
        }}, preferred_account_id="selected")
        self.assertEqual(result["accountId"], "selected")
        self.assertEqual(result["planLabel"], "Pro 20x")
        self.assertEqual(result["planEvidence"]["source"], "entitlement")

    def test_manual_subscription_retry_preserves_429_gate(self):
        self.account.update(subscriptionNextRetryAt=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                            refreshWarnings={"subscription": "HTTP 429"})
        result, count = self.probe({"plan": "pro20x"})
        self.assertEqual(count, 0)
        self.assertIn("subscription", result["refreshWarnings"])

    def test_generic_pro_and_unknown_display_values_are_not_guessed(self):
        self.account.update(planLabel="Pro", usage={})
        result, _ = self.probe({"plan": "pro"})
        self.assertEqual(result["planLabel"], "Pro")
        self.assertEqual(core._parse_chatgpt_usage({"plan_type": "future-plan"})["planLabel"], "future-plan")

    def test_only_rejected_operations_replay_and_success_is_retained(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                calls = []
                def fetch(url, token, *_a, **_k):
                    calls.append((url, token))
                    if url == core.CHATGPT_MODELS_URL and token == "old":
                        raise fixtures.OAuthQuotaV97Tests.unauthorized(status)
                    return fixtures.OAuthQuotaV97Tests.usage() if url == core.CHATGPT_USAGE_URL else {"models": [{"slug": "gpt-current"}]}
                with (
                    patch.object(core, "load_settings", return_value={"accounts": [self.account]}),
                    patch.object(core, "_account_chatgpt_credentials", side_effect=lambda _id, **kw: {"accessToken": "new" if kw.get("force_refresh") else "old", "accountId": "selected"}) as creds,
                    patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
                    patch.object(core, "_fetch_chatgpt_subscription_status", return_value={"plan": "pro"}),
                ):
                    result = core._probe_codex_account(self.account["id"], True, force_metadata=True)
                self.assertEqual(sum(url == core.CHATGPT_USAGE_URL for url, _ in calls), 1)
                self.assertEqual(creds.call_count, 2 if status == 401 else 1)
                self.assertEqual(result["usage"]["weekly"]["remainingPercent"], 80)

    def test_error_prose_cannot_trigger_credential_rotation(self):
        self.assertFalse(core._chatgpt_error_is_unauthorized(core.ManagerError("401 Unauthorized")))
        self.assertFalse(core._chatgpt_error_is_unauthorized(fixtures.OAuthQuotaV97Tests.unauthorized(403)))
        self.assertTrue(core._chatgpt_error_is_unauthorized(fixtures.OAuthQuotaV97Tests.unauthorized()))

    def test_standalone_reset_detail_401_refreshes_once(self):
        settings = {"accounts": [self.account]}
        with (
            patch.object(core, "load_settings", return_value=settings), patch.object(core, "save_settings"),
            patch.object(core, "_account_chatgpt_credentials", side_effect=[{"accessToken": "old", "accountId": "selected"}, {"accessToken": "new", "accountId": "selected"}]) as creds,
            patch.object(core, "_fetch_chatgpt_json", side_effect=[fixtures.OAuthQuotaV97Tests.unauthorized(), {"available_count": 1, "credits": [{"id": "fixture-credit"}]}]) as fetch,
        ):
            result = core.refresh_account_reset_credit_details(self.account["id"], force=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(creds.call_args.kwargs, {"force_refresh": True, "rejected_access_token": "old"})
        self.assertEqual(result["usage"]["resetCredits"]["availableCount"], 1)
        self.assertEqual(result["refreshWarnings"], {})




    def test_reimport_expired_token_does_not_downgrade_current_cached_tier(self):
        self.account.update(plan="pro20x", planLabel="Pro 20x", fingerprint="same",
                            planVariantOverride="pro20x", planVariantConfirmedAt=core.now_iso(),
                            planVariantExpiresAt=self.account["subscriptionExpiresAt"])
        identity = {"email": "fixture@test.invalid", "name": "fixture", "display": "fixture",
                    "plan": "pro5x", "authMode": "chatgpt", "fingerprint": "same",
                    "subscriptionExpiresAt": "2000-01-01T00:00:00Z"}
        with patch.object(core, "_account_group"):
            result = core._account_record_from_import({"accounts": [self.account]}, {}, identity)
        self.assertEqual(result["planLabel"], "Pro 20x")
        self.assertEqual(core._session_expiry(result["subscriptionExpiresAt"]),
                         core._session_expiry(self.account["subscriptionExpiresAt"]))
        self.assertNotIn("planVariantOverride", result)

    def test_reset_detail_manual_refresh_respects_rate_limit_backoff(self):
        self.account["usage"]["resetCredits"] = {"detailsNextRetryAt":
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.account["refreshWarnings"] = {"resetCredits": "HTTP 429"}
        with (patch.object(core, "load_settings", return_value={"accounts": [self.account]}),
              patch.object(core, "_account_chatgpt_credentials") as creds):
            core.refresh_account_reset_credit_details(self.account["id"], force=True)
        creds.assert_not_called()


if __name__ == "__main__":
    unittest.main()
