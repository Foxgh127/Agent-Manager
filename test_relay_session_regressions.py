from email.message import Message
import io
import json
import unittest
from unittest.mock import patch

import agent_manager_core as core
import relay_portal_service as relay


class Response(io.BytesIO):
    def __init__(self, payload, cookies=()):
        super().__init__(json.dumps(payload).encode())
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        for value in cookies:
            self.headers["Set-Cookie"] = value


class RelaySessionRegressions(unittest.TestCase):
    def session(self):
        return {"origin": "https://relay.example.test", "portalUrl": "https://relay.example.test/dashboard",
                "adapter": "new-api", "userId": "42", "sessionId": "session-42", "accessToken": "old-access",
                "cookies": [{"name": "refresh", "value": "old-cookie", "domain": "relay.example.test", "path": "/api/user/auth", "secure": True}]}

    def test_refresh_sends_same_origin_and_remembers_rotated_http_only_cookie(self):
        session = self.session()
        response = Response({"success": True, "data": {"access_token": "new-access"}}, ["refresh=new-cookie; Path=/api/user/auth; Secure; HttpOnly; SameSite=Strict"])
        with patch.object(core, "_open_same_origin_request", return_value=response) as opened:
            relay._saved_json_request(session, "/api/user/auth/refresh", method="POST")
        headers = {key.lower(): value for key, value in opened.call_args.args[0].header_items()}
        self.assertEqual(headers["origin"], "https://relay.example.test")
        self.assertEqual(headers["referer"], "https://relay.example.test/")
        self.assertEqual(headers["x-auth-session"], "session-42")
        self.assertEqual(headers["cookie"], "refresh=old-cookie")
        self.assertEqual(relay._cookie_header(session, "https://relay.example.test/api/user/auth/refresh"), "refresh=new-cookie")
        self.assertTrue(session["cookies"][0]["httpOnly"])
        self.assertTrue(session["cookies"][0]["hostOnly"])

    def test_cookie_deletion_expiry_and_other_origin_are_respected(self):
        session = self.session()
        headers = Message()
        headers["Set-Cookie"] = "refresh=; Path=/api/user/auth; Max-Age=0"
        headers["Set-Cookie"] = "foreign=secret; Domain=other.test; Path=/"
        relay._merge_response_cookies(session, session["origin"] + "/api/user/auth/refresh", headers)
        self.assertEqual(session["cookies"], [])
        session["cookies"] = [{"name": "expired", "value": "bad", "domain": "relay.example.test", "expires": "Thu, 01 Jan 1970 00:00:00 GMT"}]
        self.assertEqual(relay._cookie_header(session, session["origin"]), "")

    def test_rotation_headers_survive_response_body_disconnect(self):
        session = self.session()
        response = Response({}, ["refresh=rotated; Path=/api/user/auth; Secure; HttpOnly"])
        with patch.object(response, "read", side_effect=TimeoutError()), patch.object(core, "_open_same_origin_request", return_value=response):
            with self.assertRaises(relay.DashboardTemporarilyUnavailable):
                relay._saved_json_request(session, "/api/user/auth/refresh", method="POST")
        self.assertEqual(session["cookies"][0]["value"], "rotated")

    def test_rotated_credentials_are_saved_when_later_dashboard_read_fails(self):
        session = self.session()
        def probe(current):
            current["accessToken"] = "rotated-access"
            raise relay.DashboardTemporarilyUnavailable("temporary failure")
        with patch.object(core, "load_relay_account_dashboard_session", return_value=session), \
                patch.object(relay, "_probe_saved_dashboard", side_effect=probe), \
                patch.object(core, "store_relay_account_dashboard_session") as store:
            with self.assertRaises(relay.DashboardTemporarilyUnavailable):
                relay.RelayPortalService._scan_saved_account({"id": "test", "origin": session["origin"]})
        self.assertEqual(store.call_args.args[1]["accessToken"], "rotated-access")

    def test_different_identity_is_not_saved_into_existing_account(self):
        session = self.session()
        def probe(current):
            current["accessToken"] = "different-account-access"
            relay._verify_dashboard_identity(current, {"status": 200, "body": {"data": {"id": 43}}}, "42")
        with patch.object(core, "load_relay_account_dashboard_session", return_value=session), \
                patch.object(relay, "_probe_saved_dashboard", side_effect=probe), \
                patch.object(core, "store_relay_account_dashboard_session") as store:
            with self.assertRaises(relay.DashboardIdentityChanged):
                relay.RelayPortalService._scan_saved_account({"id": "test", "origin": session["origin"]})
        store.assert_not_called()

    def test_temporary_dashboard_failure_does_not_request_relogin(self):
        service = relay.RelayPortalService()
        with patch.object(service, "_saved_account", return_value=({"id": "test"}, {})), \
                patch.object(service, "_scan_saved_account", side_effect=relay.DashboardTemporarilyUnavailable("HTTP 503")), \
                patch.object(core, "refresh_relay_account", return_value={"warnings": []}):
            result = service.refresh_account("test")
        self.assertFalse(result["requiresLogin"])
        self.assertIn("503", result["warnings"][0])

    def test_server_failure_and_expired_login_have_different_outcomes(self):
        with self.assertRaises(relay.DashboardTemporarilyUnavailable):
            relay._raise_dashboard_user_error({"status": 503})
        with self.assertRaises(core.ManagerError) as error:
            relay._raise_dashboard_user_error({"status": 401})
        self.assertNotIsInstance(error.exception, relay.DashboardTemporarilyUnavailable)

    def test_encrypted_session_normalization_keeps_session_binding(self):
        session = self.session()
        session["cookies"][0]["hostOnly"] = True
        normalized = core._normalize_relay_dashboard_session(session)
        self.assertEqual(normalized["sessionId"], "session-42")
        self.assertTrue(normalized["cookies"][0]["hostOnly"])
        session["accessToken"] = "bad\nheader"
        with self.assertRaises(core.ManagerError):
            core._normalize_relay_dashboard_session(session)

    def test_new_api_stale_session_binding_retries_once_and_keeps_same_identity(self):
        session = self.session()
        bindings = []
        def request(current, path, **kwargs):
            self.assertEqual(path, "/api/user/auth/refresh")
            bindings.append(current.get("sessionId"))
            if len(bindings) == 1:
                return {"status": 409, "body": {"code": "AUTH_SESSION_MISMATCH"}}
            return {"status": 200, "body": {"data": {"access_token": "rotated-access", "session": {"sid": "rotated-sid"}}}}
        def parallel(current, requests):
            return {name: {"status": 200, "body": {"data": {"id": 42} if name == "user" else []}}
                    for name in requests}
        with patch.object(relay, "_saved_json_request", side_effect=request), \
                patch.object(relay, "_saved_parallel_requests", side_effect=parallel):
            _preview, refreshed = relay._probe_saved_dashboard(session)
        self.assertEqual(bindings, ["session-42", None])
        self.assertEqual(refreshed["sessionId"], "rotated-sid")
        self.assertEqual(refreshed["accessToken"], "rotated-access")
        self.assertEqual(refreshed["userId"], "42")

    def test_new_api_session_mismatch_cannot_silently_switch_saved_identity(self):
        responses = [{"status": 409, "body": {"code": "AUTH_SESSION_MISMATCH"}},
                     {"status": 200, "body": {"data": {"access_token": "other-user-token"}}}]
        def parallel(current, requests):
            return {name: {"status": 200, "body": {"data": {"id": 43} if name == "user" else []}}
                    for name in requests}
        with patch.object(relay, "_saved_json_request", side_effect=responses) as request, \
                patch.object(relay, "_saved_parallel_requests", side_effect=parallel):
            with self.assertRaises(relay.DashboardIdentityChanged):
                relay._probe_saved_dashboard(self.session())
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
