"""Account balance regressions; all settings and network access are isolated."""
import copy
from contextlib import ExitStack, nullcontext
import unittest
from unittest.mock import patch

import relay_portal_service as relay


def response(data, status=200):
    return {"status": status, "json": True,
            "body": {"success": status == 200, "data": data}}


class RelayBalanceV913Tests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "id": "balance_test", "adapter": "sub2api", "providerId": "provider-test",
            "origin": "https://relay.example.test", "portalUrl": "https://relay.example.test/dashboard",
            "user": {"id": "42"}, "models": ["old-model"], "keys": [], "groups": [],
            "balance": {"remaining": 17, "used": 3, "currency": "USD"},
            "balanceUpdatedAt": "2026-01-01T00:00:00Z", "balanceFreshness": "fresh",
        }
        self.state = {"relayAccounts": [copy.deepcopy(self.account)], "providers": [
            {"id": "provider-test", "balance": {"amount": 999999, "used": 80, "currency": "USD"}}
        ]}
        self.service = relay.RelayPortalService()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(relay.core, "load_settings", side_effect=lambda: copy.deepcopy(self.state)))
        self.stack.enter_context(patch.object(relay.core, "save_settings", side_effect=self.save))
        self.stack.enter_context(patch.object(relay.core, "_settings_file_lock", side_effect=nullcontext))
        self.stack.enter_context(patch.object(relay.core, "now_iso", return_value="2026-09-09T00:00:00Z"))
        # Fail closed if a regression accidentally escapes the mocked HTTP layer.
        self.stack.enter_context(patch.object(relay.core, "_open_same_origin_request", side_effect=AssertionError("unexpected network")))

    def save(self, state):
        self.state = copy.deepcopy(state)

    def key_refresh(self, *_args, **_kwargs):
        # Reproduce the old core bug: even a models-only refresh copied cached
        # provider quota into the account and bumped its generic updatedAt.
        account = self.state["relayAccounts"][0]
        account["balance"] = {"remaining": 999999, "used": 80, "currency": "USD"}
        account["models"] = ["new-model"]
        account["updatedAt"] = "2026-09-09T00:00:00Z"
        return {"account": copy.deepcopy(account), "warnings": [], "modelRefresh": {"status": "ready"}}

    def refresh(self, balance=None, *, error=None, reason="", key_error=None):
        raw = {"adapter": "sub2api", "_refreshScope": "account", "_requestCount": 1,
               "_accountUser": {"id": "42"}, "_balance": balance, "_balanceUnavailableReason": reason}
        with patch.object(self.service, "_scan_saved_account", side_effect=error,
                          return_value=(raw, {}, {})), patch.object(
                              relay.core, "refresh_relay_account", side_effect=key_error or self.key_refresh):
            return self.service.refresh_account("balance_test")

    def assert_stale(self, result, *, requires_login=False):
        self.assertEqual(result["account"]["balance"]["remaining"], 17)
        self.assertEqual(self.state["relayAccounts"][0]["balance"]["remaining"], 17)
        self.assertEqual(result["balanceRefresh"]["freshness"], "stale")
        self.assertEqual(result["balanceRefresh"]["updatedAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["balanceRefresh"]["checkedAt"], "2026-09-09T00:00:00Z")
        self.assertTrue(result["balanceRefresh"]["error"])
        self.assertEqual(result["requiresLogin"], requires_login)

    def test_zero_remaining_replaces_old_balance_and_clears_missing_usage(self):
        result = self.refresh({"remaining": 0, "used": None, "currency": "USD"})
        self.assertEqual(result["account"]["balance"]["remaining"], 0)
        self.assertIsNone(result["account"]["balance"]["used"])
        self.assertIsNone(self.state["providers"][0]["balance"]["used"])
        self.assertEqual(result["balanceRefresh"]["freshness"], "fresh")
        self.assertEqual(result["balanceRefresh"]["updatedAt"], "2026-09-09T00:00:00Z")
        self.assertIsNone(result["balanceRefresh"]["error"])

    def test_dashboard_failure_does_not_promote_high_key_quota(self):
        result = self.refresh(error=relay.DashboardTemporarilyUnavailable("HTTP 503"))
        self.assert_stale(result)
        self.assertEqual(result["modelRefresh"]["status"], "ready")

    def test_expired_dashboard_session_marks_balance_stale_and_requests_login(self):
        self.assert_stale(self.refresh(error=relay.DashboardLoginRequired("请重新登录")), requires_login=True)

    def test_identity_change_preserves_account_balance(self):
        self.assert_stale(self.refresh(error=relay.DashboardIdentityChanged("不同账号")), requires_login=True)

    def test_usage_only_response_does_not_claim_remaining_is_fresh(self):
        result = self.refresh({"remaining": None, "used": 12, "currency": "USD"})
        self.assert_stale(result)
        self.assertEqual(result["catalogCache"]["balance"], "preserved_dashboard_balance")

    def test_unknown_conversion_marks_old_balance_stale(self):
        self.assert_stale(self.refresh(reason="quota_per_unit_unknown"))

    def test_key_failure_does_not_prevent_dashboard_balance_write(self):
        result = self.refresh({"remaining": 4, "used": 13}, key_error=relay.core.ManagerError("model endpoint failed"))
        self.assertEqual(result["account"]["balance"]["remaining"], 4)
        self.assertEqual(result["balanceRefresh"]["freshness"], "fresh")
        self.assertTrue(any("model endpoint failed" in warning for warning in result["warnings"]))

    def test_metadata_failure_never_claims_success(self):
        with patch.object(self.service, "_update_quick_account_metadata", side_effect=relay.core.ManagerError("write failed")):
            result = self.refresh({"remaining": 4})
        self.assert_stale(result)
        self.assertEqual(result["catalogCache"]["balance"], "preserved_dashboard_balance")

    def test_missing_old_balance_is_unavailable_not_zero(self):
        self.state["relayAccounts"][0].pop("balance")
        self.state["relayAccounts"][0].pop("balanceUpdatedAt")
        result = self.refresh(error=relay.DashboardTemporarilyUnavailable("HTTP 503"))
        self.assertEqual(result["balanceRefresh"]["freshness"], "unavailable")
        self.assertIsNone(result["balanceRefresh"]["updatedAt"])
        self.assertIsNone(result["account"]["balance"])

    def test_repeated_success_uses_new_dashboard_value_then_failure_keeps_that_value(self):
        self.refresh({"remaining": 5})
        second = self.refresh({"remaining": 0})
        third = self.refresh(error=relay.DashboardTemporarilyUnavailable("timeout"))
        self.assertEqual(second["account"]["balance"]["remaining"], 0)
        self.assertEqual(third["account"]["balance"]["remaining"], 0)
        self.assertEqual(third["balanceRefresh"]["freshness"], "stale")

    def test_sub2api_renewed_session_reads_zero_balance_from_auth_me(self):
        session = {"adapter": "sub2api", "userId": "42", "accessToken": "test-expired",
                   "refreshToken": "test-renewal", "origin": "https://relay.example.test"}
        replies = [response({}, 401), response({"access_token": "test-rotated"}), response({"id": 42, "balance": 0})]
        with patch.object(relay, "_saved_json_request", side_effect=replies) as requested:
            raw, renewed = relay._probe_saved_dashboard_quick(session)
        self.assertEqual([call.args[1] for call in requested.call_args_list],
                         ["/api/v1/auth/me", "/api/v1/auth/refresh", "/api/v1/auth/me"])
        self.assertEqual(raw["_balance"]["remaining"], 0)
        self.assertEqual(renewed["accessToken"], "test-rotated")

    def test_new_api_zero_raw_quota_is_zero_even_without_conversion_factor(self):
        snapshot = relay._quick_dashboard_account_snapshot(
            response({"id": 42, "quota": 0, "used_quota": 17}), "new-api"
        )
        self.assertEqual(snapshot["balance"]["remaining"], 0)
        self.assertIsNone(snapshot["balance"]["used"])
        self.assertEqual(snapshot["balanceUnavailableReason"], "")

    def test_full_refresh_reapplies_dashboard_balance_after_key_catalog_refresh(self):
        preview = {"adapter": "sub2api", "balance": {"remaining": 0, "used": 20}}
        with patch.object(self.service, "_scan_saved_account", return_value=(preview, {}, {})), \
                patch.object(relay.core, "sync_relay_account_snapshot", return_value={"account": self.account}), \
                patch.object(relay.core, "refresh_relay_account", side_effect=self.key_refresh):
            result = self.service.refresh_account("balance_test", full=True)
        self.assertEqual(result["refreshMode"], "full")
        self.assertEqual(result["account"]["balance"]["remaining"], 0)
        self.assertEqual(result["balanceRefresh"]["freshness"], "fresh")

    def test_full_refresh_failure_retains_original_balance_and_timestamp(self):
        with patch.object(self.service, "_scan_saved_account", side_effect=relay.DashboardLoginRequired("请重新登录")), \
                patch.object(relay.core, "refresh_relay_account", side_effect=self.key_refresh):
            result = self.service.refresh_account("balance_test", full=True)
        self.assertEqual(result["refreshMode"], "full_fallback")
        self.assert_stale(result, requires_login=True)

    def test_full_dashboard_and_key_failure_still_persists_stale_state(self):
        with patch.object(self.service, "_scan_saved_account", side_effect=relay.DashboardLoginRequired("请重新登录")), \
                patch.object(relay.core, "refresh_relay_account", side_effect=relay.core.ManagerError("Key unavailable")):
            result = self.service.refresh_account("balance_test", full=True)
        self.assert_stale(result, requires_login=True)
        self.assertTrue(any("Key unavailable" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
