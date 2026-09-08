import json
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import relay_portal_service as relay


def api_response(data, *, status=200):
    return {
        "status": status,
        "json": True,
        "body": {"success": 200 <= status < 300, "data": data},
    }


class _ClosedEvent:
    def __init__(self):
        self.callback = None

    def __iadd__(self, callback):
        self.callback = callback
        return self


class _Window:
    def __init__(self, url="https://relay.example.test/dashboard"):
        self.url = url
        self.destroyed = 0
        self.events = SimpleNamespace(closed=_ClosedEvent())

    def get_current_url(self):
        return self.url

    def get_cookies(self):
        return []

    def show(self):
        return None

    def destroy(self):
        self.destroyed += 1


class RelayReauthFlowV9Tests(unittest.TestCase):
    def preview(self, *, user_id="42"):
        return {
            "adapter": "new-api",
            "adapterLabel": "New API / One API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/dashboard",
            "origin": "https://relay.example.test",
            "baseUrl": "https://relay.example.test/v1",
            "modelsEndpoint": "https://relay.example.test/v1/models",
            "balanceEndpoint": "https://relay.example.test/api/usage/token",
            "apiEndpoints": [
                {
                    "id": "default",
                    "name": "Default",
                    "baseUrl": "https://relay.example.test/v1",
                    "modelsEndpoint": "https://relay.example.test/v1/models",
                    "balanceEndpoint": "https://relay.example.test/api/usage/token",
                    "isDefault": True,
                }
            ],
            "defaultEndpointId": "default",
            "integrationKind": "",
            "user": {
                "id": user_id,
                "email": "alice@example.test",
                "name": "Alice",
                "group": "default",
            },
            "balance": {"remaining": 8, "used": 2, "currency": "USD"},
            "models": ["gpt-test"],
            "groups": [
                {
                    "id": "vip",
                    "name": "VIP",
                    "platform": "openai",
                    "active": True,
                }
            ],
            "keys": [
                {
                    "id": "key-active",
                    "name": "Current Key",
                    "group": "VIP",
                    "groupId": "vip",
                    "groupPlatform": "openai",
                    "canImport": True,
                }
            ],
            "keysAuthoritative": True,
            "groupsAuthoritative": True,
            "supportsCreate": True,
            "detectedAt": "2026-09-06T00:00:00Z",
        }

    def auth_result(self, *, user_id="42"):
        return {
            "authenticated": True,
            "adapter": "new-api",
            "user": api_response(
                {"id": user_id, "email": "alice@example.test", "username": "Alice"}
            ),
            "session": {
                "accessToken": "rotated-access-token",
                "sessionId": "rotated-sid",
                "authMode": "bearer",
                "userId": user_id,
            },
        }

    def dashboard_session(self, *, user_id="42"):
        return {
            "version": 1,
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": "new-api",
            "authMode": "bearer",
            "userId": user_id,
            "sessionId": "rotated-sid",
            "accessToken": "rotated-access-token",
            "refreshToken": "",
            "cookies": [],
        }

    def service(self, *, mode="reauth"):
        service = relay.RelayPortalService()
        service.window = _Window()
        service._session = {
            **service._empty_state(),
            "sessionId": "session-v9",
            "status": "waiting_login",
            "mode": mode,
            "accountId": "relay-existing" if mode == "reauth" else "",
            "portalUrl": "https://relay.example.test/dashboard",
            "currentUrl": "https://relay.example.test/dashboard",
            "autoAuth": {
                "enabled": True,
                "state": "waiting",
                "attempts": 0,
                "maxAttempts": relay.AUTO_AUTH_MAX_ATTEMPTS,
                "nextCheckInMs": 0,
            },
            "_createdMonotonic": time.monotonic(),
            "_lastAutoCheckMonotonic": 0.0,
            "_autoAuthInFlight": False,
            "_fullScanStarted": False,
            "_cancelEvent": threading.Event(),
            "_expectedAdapter": "new-api" if mode == "reauth" else "",
            "_expectedIdentity": (
                {"id": "42", "email": "alice@example.test", "name": "Alice"}
                if mode == "reauth"
                else {"id": "", "email": "", "name": ""}
            ),
        }
        return service

    def test_start_links_existing_account_and_publishes_reauth_contract(self):
        window = _Window()
        account = {
            "id": "relay-existing",
            "portalUrl": "https://relay.example.test/dashboard",
            "origin": "https://relay.example.test",
            "adapter": "new-api",
            "user": {"id": "42", "email": "alice@example.test", "name": "Alice"},
            "selectedKeyId": "key-active",
            "selectedEndpointId": "default",
            "groupId": "saved-local-group",
            "providerId": "provider-existing",
        }
        fake_webview = SimpleNamespace(windows=[SimpleNamespace(gui=object())])
        service = relay.RelayPortalService(window_factory=lambda *_args, **_kwargs: window)
        with (
            patch.dict(sys.modules, {"webview": fake_webview}),
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(relay.core, "load_relay_account_dashboard_session", return_value=None),
        ):
            state = service.start(
                "https://relay.example.test/login",
                account_id="relay-existing",
            )

        self.assertEqual(state["mode"], "reauth")
        self.assertEqual(state["accountId"], "relay-existing")
        self.assertEqual(state["status"], "waiting_login")
        self.assertTrue(state["autoAuth"]["enabled"])
        self.assertEqual(state["autoAuth"]["state"], "waiting")
        self.assertEqual(state["autoAuth"]["maxAttempts"], relay.AUTO_AUTH_MAX_ATTEMPTS)
        self.assertNotIn("_expectedIdentity", state)

    def test_new_account_login_auto_scans_once_then_waits_for_user_key_selection(self):
        service = self.service(mode="new")
        preview = self.preview()
        with (
            patch.object(service, "_evaluate", side_effect=[self.auth_result(), {"full": True}]) as evaluated,
            patch.object(relay, "normalize_probe_result", return_value=(preview, {"key-active": "sk-secret"})),
            patch.object(relay, "_dashboard_session_from_probe", return_value=self.dashboard_session()),
            patch.object(relay.core, "import_relay_account") as imported,
        ):
            state = service.check_auto_auth("session-v9")

        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["mode"], "new")
        self.assertEqual(state["autoAuth"]["state"], "completed")
        self.assertEqual(state["preview"]["keys"][0]["id"], "key-active")
        self.assertEqual(evaluated.call_count, 2)
        imported.assert_not_called()

    def test_reauth_updates_existing_account_without_importing_or_reselecting(self):
        service = self.service()
        window = service.window
        preview = self.preview()
        existing = {
            "id": "relay-existing",
            "selectedKeyId": "key-active",
            "selectedEndpointId": "default",
            "groupId": "saved-local-group",
            "providerId": "provider-existing",
            "keys": [{"id": "key-active", "groupId": "vip"}],
            "groups": [{"id": "vip"}],
            "models": ["gpt-test"],
        }
        with (
            patch.object(service, "_evaluate", side_effect=[self.auth_result(), {"full": True}]),
            patch.object(relay, "normalize_probe_result", return_value=(preview, {"key-active": "sk-never-reimport"})),
            patch.object(relay, "_dashboard_session_from_probe", return_value=self.dashboard_session()),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(
                relay.core,
                "sync_relay_account_snapshot",
                return_value={"account": existing, "liveDashboard": True, "requiresReapply": False},
            ) as synced,
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": existing, "warnings": [], "modelRefresh": {"status": "ready"}},
            ) as refreshed,
            patch.object(relay.core, "import_relay_account") as imported,
        ):
            state = service.check_auto_auth("session-v9")

        self.assertEqual(state["status"], "reauthenticated")
        self.assertEqual(state["completion"]["accountId"], "relay-existing")
        stored.assert_called_once()
        self.assertEqual(stored.call_args.args[0], "relay-existing")
        self.assertEqual(stored.call_args.args[1]["userId"], "42")
        synced.assert_called_once()
        self.assertEqual(synced.call_args.args[0], "relay-existing")
        refreshed.assert_called_once_with(
            "relay-existing",
            refresh_balance=False,
            fallback_notice=False,
        )
        imported.assert_not_called()
        self.assertEqual(existing["selectedKeyId"], "key-active")
        self.assertEqual(existing["groupId"], "saved-local-group")
        self.assertEqual(existing["providerId"], "provider-existing")
        self.assertIsNone(service.window)
        self.assertEqual(window.destroyed, 1)
        self.assertNotIn("rotated-access-token", json.dumps(state))
        self.assertNotIn("sk-never-reimport", json.dumps(state))

    def test_wrong_account_never_persists_or_runs_full_scan(self):
        service = self.service()
        with (
            patch.object(service, "_evaluate", return_value=self.auth_result(user_id="43")) as evaluated,
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(relay.core, "sync_relay_account_snapshot") as synced,
            patch.object(relay.core, "import_relay_account") as imported,
        ):
            state = service.check_auto_auth("session-v9")

        self.assertEqual(state["status"], "identity_mismatch")
        self.assertEqual(state["autoAuth"]["state"], "identity_mismatch")
        self.assertEqual(evaluated.call_count, 1)
        stored.assert_not_called()
        synced.assert_not_called()
        imported.assert_not_called()

    def test_explicit_retry_after_wrong_account_keeps_frozen_identity_and_accepts_correct_login(self):
        service = self.service()
        preview = self.preview()
        existing = {
            "id": "relay-existing",
            "selectedKeyId": "key-active",
            "selectedEndpointId": "default",
            "groupId": "saved-local-group",
            "providerId": "provider-existing",
            "keys": [{"id": "key-active"}],
            "groups": [],
            "models": [],
        }
        with (
            patch.object(
                service,
                "_evaluate",
                side_effect=[self.auth_result(user_id="43"), self.auth_result(), {"full": True}],
            ) as evaluated,
            patch.object(relay, "normalize_probe_result", return_value=(preview, {})),
            patch.object(relay, "_dashboard_session_from_probe", return_value=self.dashboard_session()),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(relay.core, "sync_relay_account_snapshot", return_value={"account": existing}),
            patch.object(relay.core, "refresh_relay_account", return_value={"account": existing, "warnings": []}),
        ):
            wrong = service.check_auto_auth("session-v9")
            self.assertEqual(wrong["status"], "identity_mismatch")
            stored.assert_not_called()
            corrected = service.check_auto_auth("session-v9", force=True)

        self.assertEqual(corrected["status"], "reauthenticated")
        self.assertEqual(evaluated.call_count, 3)
        stored.assert_called_once()
        self.assertEqual(stored.call_args.args[1]["userId"], "42")

    def test_identity_is_rechecked_after_full_scan_before_rotated_credentials_are_saved(self):
        service = self.service()
        wrong_preview = self.preview(user_id="43")
        with (
            patch.object(service, "_evaluate", side_effect=[self.auth_result(), {"full": True}]) as evaluated,
            patch.object(relay, "normalize_probe_result", return_value=(wrong_preview, {})),
            patch.object(relay, "_dashboard_session_from_probe", return_value=self.dashboard_session(user_id="43")),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(relay.core, "sync_relay_account_snapshot") as synced,
        ):
            state = service.check_auto_auth("session-v9")

        self.assertEqual(state["status"], "identity_mismatch")
        self.assertEqual(evaluated.call_count, 2)
        stored.assert_not_called()
        synced.assert_not_called()

    def test_cancel_discards_late_probe_callback_before_any_write(self):
        service = self.service()

        def cancel_during_probe(*_args, **_kwargs):
            service.close()
            return self.auth_result()

        with (
            patch.object(service, "_evaluate", side_effect=cancel_during_probe) as evaluated,
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(relay.core, "sync_relay_account_snapshot") as synced,
        ):
            state = service.check_auto_auth("session-v9")

        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["autoAuth"]["state"], "cancelled")
        self.assertEqual(evaluated.call_count, 1)
        stored.assert_not_called()
        synced.assert_not_called()

    def test_completed_reauth_is_idempotent_under_repeated_poll_callback(self):
        service = self.service()
        preview = self.preview()
        existing = {
            "id": "relay-existing",
            "selectedKeyId": "key-active",
            "selectedEndpointId": "default",
            "groupId": "saved-local-group",
            "providerId": "provider-existing",
            "keys": [{"id": "key-active"}],
            "groups": [],
            "models": [],
        }
        with (
            patch.object(service, "_evaluate", side_effect=[self.auth_result(), {"full": True}]) as evaluated,
            patch.object(relay, "normalize_probe_result", return_value=(preview, {})),
            patch.object(relay, "_dashboard_session_from_probe", return_value=self.dashboard_session()),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
            patch.object(relay.core, "sync_relay_account_snapshot", return_value={"account": existing}) as synced,
            patch.object(relay.core, "refresh_relay_account", return_value={"account": existing, "warnings": []}),
        ):
            first = service.check_auto_auth("session-v9")
            second = service.check_auto_auth("session-v9")

        self.assertEqual(first["status"], "reauthenticated")
        self.assertEqual(second, first)
        self.assertEqual(evaluated.call_count, 2)
        stored.assert_called_once()
        synced.assert_called_once()

    def test_waiting_probe_is_server_throttled_and_reports_next_check(self):
        service = self.service(mode="new")
        with patch.object(
            service,
            "_evaluate",
            return_value={"authenticated": False, "message": "waiting"},
        ) as evaluated:
            first = service.check_auto_auth("session-v9")
            second = service.check_auto_auth("session-v9")

        self.assertEqual(first["status"], "waiting_login")
        self.assertEqual(second["autoAuth"]["attempts"], 1)
        self.assertGreater(second["autoAuth"]["nextCheckInMs"], 0)
        evaluated.assert_called_once()

    def test_automatic_detection_stops_at_the_server_side_attempt_bound(self):
        service = self.service(mode="new")
        service._session["autoAuth"]["attempts"] = relay.AUTO_AUTH_MAX_ATTEMPTS - 1
        with patch.object(
            service,
            "_evaluate",
            return_value={"authenticated": False, "message": "waiting"},
        ) as evaluated:
            first = service.check_auto_auth("session-v9")
            second = service.check_auto_auth("session-v9")

        self.assertEqual(first["autoAuth"]["state"], "exhausted")
        self.assertEqual(first["autoAuth"]["nextCheckInMs"], 0)
        self.assertEqual(second["autoAuth"]["attempts"], relay.AUTO_AUTH_MAX_ATTEMPTS)
        evaluated.assert_called_once()

    def test_lightweight_probe_never_scans_key_model_group_or_usage_catalogs(self):
        script = relay._auth_probe_script("https://relay.example.test", "")

        for forbidden in (
            "/api/token/",
            "/api/v1/keys",
            "/api/user/models",
            "/api/models",
            "/api/v1/groups",
            "/api/v1/usage/dashboard",
        ):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
