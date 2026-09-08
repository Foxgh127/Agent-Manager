"""Site-denial and authorized browser refresh regressions; no live credentials."""
import copy
import io
import sys
import threading
import time
from contextlib import ExitStack, nullcontext
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.accounts.relay as relay


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
        class ClosedEvent:
            callback = None

            def __iadd__(inner, callback):
                inner.callback = callback
                return inner

        class Window:
            hidden = 0
            destroyed = 0
            events = SimpleNamespace(closed=ClosedEvent())

            def get_current_url(inner):
                return (origin or self.account["origin"]) + "/dashboard"

            def hide(inner):
                inner.hidden += 1

            def destroy(inner):
                inner.destroyed += 1

            def show(inner):
                return None

        self.service.window = Window()
        self.service._session.update({"sessionId": "window-test", "accountId": "site_test",
                                      "portalUrl": self.account["portalUrl"]})
        return self.stack.enter_context(patch.object(self.service, "_evaluate", side_effect=values or [user_response(), user_response()]))

    def retain_window(self):
        self.service._session.update({"status": "reauthenticated", "mode": "reauth"})
        self.service._retain_window_for_balance("window-test", {"id": "42"}, "sub2api")

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
        result = self.refresh()
        self.assertTrue(result["requiresBrowser"])
        self.assertEqual(result["originMismatch"], {
            "savedOrigin": "https://www.fastaitoken.com", "currentOrigin": "https://fastaitoken.com",
            "reconnectUrl": "https://fastaitoken.com", "requiresNewConnection": True,
        })
        self.assertEqual(result["account"]["origin"], self.account["origin"])
        evaluated.assert_not_called()

    def test_www_apex_login_navigation_reports_new_connection_and_stops_polling(self):
        evaluated = self.open_window(origin="https://fastaitoken.com")
        self.service._session.update({"mode": "reauth", "status": "waiting_login",
                                      "autoAuth": {"enabled": True, "state": "waiting", "attempts": 0}})
        with patch.object(core, "store_relay_account_dashboard_session") as store:
            result = self.service.check_auto_auth("window-test", force=True)
        self.assertEqual(result["status"], "origin_mismatch")
        self.assertIn("重新连接", result["message"])
        self.assertEqual(result["currentUrl"], "https://fastaitoken.com")
        self.assertFalse(result["autoAuth"]["enabled"])
        self.assertEqual(result["autoAuth"]["nextCheckInMs"], 0)
        evaluated.assert_not_called()
        store.assert_not_called()

    def test_origin_notice_does_not_redirect_credentials_or_expose_url_parameters(self):
        for current in ("https://www.fastaitoken.com/dashboard", "http://fastaitoken.com/dashboard",
                        "https://other.example/dashboard", "https://login.fastaitoken.com/dashboard",
                        "https://fastaitoken.com:8443/dashboard", "https://fastaitoken.com/oauth/authorize",
                        "https://fastaitoken.com/callback?code=secret", "https://fastaitoken.com/?state=secret"):
            with self.subTest(current=current):
                self.assertIsNone(relay._browser_origin_mismatch(self.account["origin"], current))
        details = relay._browser_origin_mismatch(self.account["origin"], "https://fastaitoken.com/dashboard?session=private#private")
        self.assertNotIn("private", str(details))
        self.assertEqual(details["reconnectUrl"], "https://fastaitoken.com")

    def test_third_party_oauth_page_keeps_waiting_without_new_connection_hint(self):
        evaluated = self.open_window(origin="https://identity.example")
        self.service._session.update({"mode": "reauth", "status": "waiting_login",
                                      "autoAuth": {"enabled": True, "state": "waiting", "attempts": 0}})
        result = self.service.check_auto_auth("window-test", force=True)
        self.assertEqual(result["status"], "waiting_login")
        self.assertIsNone(result["originMismatch"])
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

    def test_successful_reauth_hides_one_window_instead_of_destroying_it(self):
        auth = {**user_response(), "authenticated": True}
        self.open_window([auth, {"full": True}])
        window = self.service.window
        self.service._session.update({
            "mode": "reauth", "status": "waiting_login", "_expectedAdapter": "sub2api",
            "_expectedIdentity": {"id": "42"}, "_cancelEvent": threading.Event(),
            "autoAuth": {"enabled": True, "state": "waiting", "attempts": 0},
        })
        preview = {**self.account, "adapterLabel": "Sub2API", "apiEndpoints": []}
        with patch.object(relay, "normalize_probe_result", return_value=(preview, {})), \
                patch.object(relay, "_dashboard_session_from_probe", return_value={"userId": "42"}), \
                patch.object(core, "store_relay_account_dashboard_session"), \
                patch.object(core, "sync_relay_account_snapshot", return_value={"account": self.account}), \
                patch.object(core, "refresh_relay_account", return_value={"account": self.account}):
            result = self.service.check_auto_auth("window-test")
        self.assertEqual(result["status"], "reauthenticated")
        self.assertIs(self.service.window, window)
        self.assertEqual(window.hidden, 1)
        self.assertEqual(window.destroyed, 0)
        self.assertNotIn("_retainedBalanceIdentity", result)
        self.assertFalse(result["autoAuth"]["enabled"])

    def test_retained_window_survives_interactive_ttl_and_repeated_refresh(self):
        evaluated = self.open_window([user_response()] * 4)
        self.retain_window()
        self.service._session["_createdMonotonic"] = time.monotonic() - relay.SESSION_TTL_SECONDS - 100
        window = self.service.window
        self.assertFalse(self.service._expired_locked())
        self.assertEqual(self.refresh()["balanceRefresh"]["freshness"], "fresh")
        self.assertEqual(self.refresh()["balanceRefresh"]["freshness"], "fresh")
        self.assertEqual(evaluated.call_count, 4)
        self.assertIs(self.service.window, window)
        self.assertEqual(window.hidden, 1)

    def test_cancel_and_manager_shutdown_destroy_retained_window(self):
        for silent in (False, True):
            with self.subTest(silent=silent):
                self.open_window()
                self.retain_window()
                window = self.service.window
                self.service.close(silent=silent)
                self.assertIsNone(self.service.window)
                self.assertEqual(window.destroyed, 1)
                self.assertNotIn("_retainedBalanceIdentity", self.service._session)

    def test_denied_retained_page_is_destroyed_and_requests_manual_browser(self):
        self.open_window([{"adapter": "sub2api", "user": {"status": 403, "json": False}}])
        self.retain_window()
        window = self.service.window
        self.assertTrue(self.refresh()["requiresBrowser"])
        self.assertIsNone(self.service.window)
        self.assertEqual(window.destroyed, 1)

    def test_retained_page_identity_switch_is_destroyed_without_balance_write(self):
        self.open_window([user_response(user_id="43")])
        self.retain_window()
        window = self.service.window
        result = self.refresh()
        self.assertEqual(result["account"]["balance"]["remaining"], 17)
        self.assertEqual(window.destroyed, 1)
        self.assertIsNone(self.service.window)

    def test_different_account_operation_releases_old_retained_window(self):
        self.open_window()
        self.retain_window()
        window = self.service.window
        with patch.object(self.service, "_saved_account", side_effect=core.ManagerError("test stop")):
            with self.assertRaises(core.ManagerError):
                self.service.refresh_account("different-account")
        self.assertIsNone(self.service.window)
        self.assertEqual(window.destroyed, 1)

    def test_opening_new_login_releases_previous_retained_window(self):
        self.open_window()
        self.retain_window()
        old = self.service.window
        fake_webview = SimpleNamespace(windows=[SimpleNamespace(gui=object())], create_window=lambda *_args, **_kwargs: None)
        with patch.dict(sys.modules, {"webview": fake_webview}):
            with self.assertRaises(core.ManagerError):
                self.service.start("https://relay.example.test")
        self.assertEqual(old.destroyed, 1)
        self.assertIsNone(self.service.window)

    def test_native_window_closed_event_invalidates_retention(self):
        self.open_window()
        window = self.service.window
        self.service.window = None
        fake_webview = SimpleNamespace(windows=[SimpleNamespace(gui=object())])
        self.service.window_factory = lambda *_args, **_kwargs: window
        with patch.dict(sys.modules, {"webview": fake_webview}), \
                patch.object(core, "load_relay_account_dashboard_session", return_value=None):
            status = self.service.start(account_id="site_test")
        self.service._session["status"] = "reauthenticated"
        self.service._retain_window_for_balance(status["sessionId"], {"id": "42"}, "sub2api")
        self.assertIs(self.service.window, window)
        window.events.closed.callback()
        self.assertIsNone(self.service.window)
        self.assertNotIn("_retainedBalanceIdentity", self.service._session)

    def test_retained_cross_origin_navigation_is_released_before_any_user_read(self):
        evaluated = self.open_window()
        self.retain_window()
        window = self.service.window
        with patch.object(window, "get_current_url", return_value="https://fastaitoken.com/dashboard"):
            self.assertTrue(self.refresh()["requiresBrowser"])
        evaluated.assert_not_called()
        self.assertEqual(window.destroyed, 1)
        self.assertIsNone(self.service.window)

    def test_hide_failure_releases_window_without_retaining_binding(self):
        self.open_window()
        window = self.service.window
        with patch.object(window, "hide", side_effect=RuntimeError("unsupported")):
            self.retain_window()
        self.assertEqual(window.destroyed, 1)
        self.assertIsNone(self.service.window)
        self.assertNotIn("_retainedBalanceIdentity", self.service._session)

    def test_challenge_cookies_stay_in_browser_and_are_not_replayed_by_http(self):
        cookies = [{"name": name, "value": "test-value", "domain": "www.fastaitoken.com", "path": "/"}
                   for name in ("cf_clearance", "__cf_bm", "__cfseq", "cf_chl_rc_i", "refresh")]
        window = SimpleNamespace(get_cookies=lambda: cookies)
        captured = relay._window_cookie_records(window, self.account["origin"])
        self.assertEqual([row["name"] for row in captured], ["refresh"])
        self.assertEqual(relay._cookie_header({"cookies": cookies}, self.account["origin"]), "refresh=test-value")
        self.assertEqual(len(cookies), 5)  # No deletion/mutation of browser state.

    def test_http_response_challenge_cookies_are_not_saved_with_dashboard_session(self):
        session = {"cookies": [{"name": "cf_clearance", "value": "old-challenge", "domain": "www.fastaitoken.com"}]}
        headers = Message()
        headers.add_header("Set-Cookie", "__cf_bm=challenge-state; Secure; Path=/")
        headers.add_header("Set-Cookie", "refresh=renewed-session; Secure; HttpOnly; Path=/")
        relay._merge_response_cookies(session, self.account["origin"] + "/api/v1/auth/me", headers)
        self.assertEqual([row["name"] for row in session["cookies"]], ["refresh"])


if __name__ == "__main__":
    unittest.main()
