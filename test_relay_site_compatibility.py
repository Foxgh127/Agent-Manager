"""Site-denial and authorized browser refresh regressions; no live credentials."""
import copy
import io
import threading
from contextlib import ExitStack, nullcontext
from email.message import Message
import unittest
from unittest.mock import patch

import agent_manager_core as core
import relay_portal_service as relay


def user_response(user_id="42", balance=34.78):
    return {"adapter": "sub2api", "user": {
        "status": 200, "json": True,
        "body": {"data": {"id": user_id, "balance": balance}},
    }}


class SiteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.account = {
            "id": "site_test", "origin": "https://www.fastaitoken.com",
            "portalUrl": "https://www.fastaitoken.com/dashboard", "adapter": "sub2api",
            "user": {"id": "42"}, "keys": [], "groups": [], "models": [],
            "balance": {"remaining": 17, "currency": "USD"},
            "balanceFreshness": "fresh", "balanceUpdatedAt": "2026-09-01T00:00:00Z",
        }
        self.state = {"relayAccounts": [copy.deepcopy(self.account)], "providers": []}
        self.service = relay.RelayPortalService()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, "load_settings", side_effect=lambda: copy.deepcopy(self.state)))
        self.stack.enter_context(patch.object(core, "save_settings", side_effect=self.save))
        self.stack.enter_context(patch.object(core, "_settings_file_lock", side_effect=nullcontext))
        self.stack.enter_context(patch.object(core, "_open_same_origin_request", side_effect=AssertionError("unexpected network")))

    def save(self, value):
        self.state = copy.deepcopy(value)

    def open_window(self, values=None, origin=None):
        class Window:
            def get_current_url(inner):
                return (origin or self.account["origin"]) + "/dashboard"

        self.service.window = Window()
        self.service._session.update({"sessionId": "window-test", "accountId": "site_test",
                                      "portalUrl": self.account["portalUrl"]})
        return self.stack.enter_context(patch.object(self.service, "_evaluate", side_effect=values or [user_response(), user_response()]))

    def refresh(self):
        with patch.object(self.service, "_scan_saved_account", side_effect=relay.DashboardSiteRejected("site_challenge")), \
                patch.object(core, "refresh_relay_account", return_value={"account": copy.deepcopy(self.account), "warnings": []}):
            return self.service.refresh_account("site_test")

    def test_html_challenge_has_distinct_reason_and_never_rotates_token(self):
        session = {"origin": self.account["origin"], "adapter": "sub2api", "userId": "42",
                   "accessToken": "test-access", "refreshToken": "test-refresh"}
        rejected = {"status": 403, "json": False, "body": {"message": "<title>Just a moment...</title>"}}
        with patch.object(relay, "_saved_json_request", return_value=rejected) as requested:
            with self.assertRaises(relay.DashboardSiteRejected) as error:
                relay._probe_saved_dashboard_quick(session)
        self.assertEqual(error.exception.reason, "site_challenge")
        self.assertEqual(requested.call_count, 1)
        self.assertEqual(requested.call_args.args[1], "/api/v1/auth/me")
        self.assertEqual(session["refreshToken"], "test-refresh")

    def test_403_does_not_retry_cookie_or_raw_identity(self):
        session = {"origin": self.account["origin"], "adapter": "new-api", "accessToken": "test-access"}
        with patch.object(relay, "_saved_json_request", return_value={"status": 403, "json": True, "body": {"code": "FORBIDDEN"}}) as requested:
            with self.assertRaises(relay.DashboardSiteRejected):
                relay._probe_saved_dashboard_quick(session)
        self.assertEqual(requested.call_count, 1)

    def test_csrf_denial_permission_denial_and_expired_login_are_distinct(self):
        for body, reason in [({"code": "CSRF_INVALID"}, "csrf_rejected"), ({"message": "Forbidden"}, "access_denied")]:
            with self.subTest(reason=reason), self.assertRaises(relay.DashboardSiteRejected) as error:
                relay._check_dashboard_transient({"status": 403, "json": True, "body": body})
            self.assertEqual(error.exception.reason, reason)
        with self.assertRaises(relay.DashboardLoginRequired):
            relay._raise_dashboard_user_error({"status": 401, "json": True})

    def test_full_new_api_scan_stops_at_denied_refresh(self):
        with patch.object(relay, "_saved_json_request", return_value={"status": 403, "json": True, "body": {}}) as requested:
            with self.assertRaises(relay.DashboardSiteRejected):
                relay._probe_saved_dashboard({"adapter": "new-api"})
        self.assertEqual(requested.call_count, 1)

    def test_response_header_detects_challenge_without_exposing_html(self):
        response = io.BytesIO(b"<html>opaque challenge</html>")
        response.status = 403
        response.headers = Message()
        response.headers["Content-Type"] = "text/html"
        response.headers["cf-mitigated"] = "challenge"
        with patch.object(core, "_open_same_origin_request", return_value=response):
            result = relay._saved_json_request({"origin": self.account["origin"]}, "/api/v1/auth/me")
        self.assertTrue(result["siteChallenge"])
        with self.assertRaises(relay.DashboardSiteRejected) as error:
            relay._check_dashboard_transient(result)
        self.assertEqual(error.exception.reason, "site_challenge")
        self.assertNotIn("opaque", str(error.exception))

    def test_no_window_requests_browser_and_preserves_stale_balance(self):
        result = self.refresh()
        self.assertTrue(result["requiresBrowser"])
        self.assertFalse(result["requiresLogin"])
        self.assertEqual(result["dashboardFailure"]["reason"], "site_challenge")
        self.assertEqual(result["account"]["balance"]["remaining"], 17)
        self.assertEqual(result["balanceRefresh"]["freshness"], "stale")
        self.assertEqual(result["balanceRefresh"]["updatedAt"], self.account["balanceUpdatedAt"])

    def test_authorized_window_updates_balance_after_two_identity_checks(self):
        evaluated = self.open_window()
        with patch.object(core, "store_relay_account_dashboard_session") as store:
            result = self.refresh()
        self.assertEqual(evaluated.call_count, 2)
        self.assertEqual(result["account"]["balance"]["remaining"], 34.78)
        self.assertEqual(result["balanceRefresh"]["freshness"], "fresh")
        self.assertEqual(result["dashboardTransport"], "browser")
        self.assertFalse(result["requiresBrowser"])
        store.assert_not_called()

    def test_zero_is_a_valid_live_balance(self):
        self.open_window([user_response(balance=0), user_response(balance=0)])
        result = self.refresh()
        self.assertEqual(result["account"]["balance"]["remaining"], 0)
        self.assertEqual(result["balanceRefresh"]["freshness"], "fresh")

    def test_account_switch_on_second_read_cannot_overwrite_balance(self):
        self.open_window([user_response(), user_response(user_id="43")])
        result = self.refresh()
        self.assertEqual(result["account"]["balance"]["remaining"], 17)
        self.assertEqual(result["balanceRefresh"]["freshness"], "stale")
        self.assertTrue(result["requiresLogin"])

    def test_unrelated_account_window_is_never_read(self):
        evaluated = self.open_window()
        self.service._session["accountId"] = "other-account"
        self.assertTrue(self.refresh()["requiresBrowser"])
        evaluated.assert_not_called()

    def test_apex_and_www_are_not_implicitly_trusted_as_same_origin(self):
        evaluated = self.open_window(origin="https://fastaitoken.com")
        self.assertTrue(self.refresh()["requiresBrowser"])
        evaluated.assert_not_called()

    def test_failed_browser_read_does_not_publish_cached_balance_as_fresh(self):
        self.open_window([{"authenticated": False}])
        result = self.refresh()
        self.assertTrue(result["requiresBrowser"])
        self.assertEqual(result["balanceRefresh"]["freshness"], "stale")

    def test_cancelled_browser_read_is_discarded(self):
        self.open_window()
        cancelled = threading.Event()
        self.service._session["_cancelEvent"] = cancelled
        def completed(*_args, **_kwargs):
            cancelled.set()
            return user_response()
        with patch.object(self.service, "_evaluate", side_effect=completed):
            result = self.refresh()
        self.assertTrue(result["requiresBrowser"])
        self.assertEqual(result["account"]["balance"]["remaining"], 17)

    def test_browser_script_keeps_reads_same_origin_and_never_exports_credentials(self):
        script = relay._browser_balance_probe_script(self.account["origin"], "sub2api")
        self.assertIn("location.origin !== EXPECTED", script)
        self.assertIn("redirect: 'error'", script)
        self.assertIn("cache: 'no-store'", script)
        self.assertNotIn("auth/refresh", script)
        self.assertNotIn("setItem", script)
        self.assertNotIn("session:", script)


if __name__ == "__main__":
    unittest.main()
