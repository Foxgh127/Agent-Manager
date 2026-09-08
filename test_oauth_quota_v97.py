"""Isolated regressions for OAuth quota recovery; no real tokens or API calls.

These assert the intended recovery behavior, including cases missing in v9.6.
The fixtures deliberately use unexpired credentials that the server rejects:
subscription renewal must not depend on JWT expiry or reimporting the account.
"""

import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import agent_manager_core as core


class OAuthQuotaV97Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {
            "CODEX_HOME": Path(self.temp.name),
            "CONFIG_FILE": Path(self.temp.name) / "config.toml",
        }.items():
            mock = patch.object(core, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        guard = patch.object(
            core, "_open_same_origin_request",
            side_effect=AssertionError("This regression suite must not use the network"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def account(**overrides):
        account = {
            "id": "oauth-v97",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "plan": "pro",
            "planLabel": "Pro 5x",
            "models": ["gpt-current"],
            "modelsLastCheckedAt": core.now_iso(),
            "subscriptionExpiresAt": (
                datetime.now(timezone.utc) + timedelta(days=30)
            ).isoformat(),
            "usage": {"weekly": {"usedPercent": 50, "remainingPercent": 50}},
            "refreshErrors": {},
            "refreshState": "ready",
        }
        account.update(overrides)
        return account

    @staticmethod
    def unauthorized(status=401):
        error = core.ManagerError(f"HTTP {status}: unauthorized")
        error.__cause__ = urllib.error.HTTPError(
            core.CHATGPT_USAGE_URL, status, "Rejected", {}, io.BytesIO(b"{}")
        )
        error.__cause__.close()
        return error

    @staticmethod
    def usage():
        return {
            "plan_type": "pro",
            "rate_limit": {
                "secondary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 604800,
                }
            },
            "rate_limit_reset_credits": {"available_count": 0},
        }

    def _probe_401(self, *, parallel=False, permanent=False, status=401):
        account = self.account()
        calls = []
        rejected = "unexpired-but-rejected-access"

        def credentials(_id, *, force_refresh=False, rejected_access_token=None):
            if force_refresh:
                self.assertEqual(rejected_access_token, rejected)
            return {
                "accessToken": "renewed-access" if force_refresh else rejected,
                "accountId": "same-workspace",
            }

        def fetch(url, token, *_args, **_kwargs):
            calls.append((url, token))
            if permanent or token == rejected:
                raise self.unauthorized(status)
            return self.usage() if url == core.CHATGPT_USAGE_URL else {
                "models": [{"slug": "gpt-current"}]
            }

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_account_chatgpt_credentials", side_effect=credentials) as creds,
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(core, "_fetch_chatgpt_subscription_status", side_effect=core.ManagerError("fixture subscription unavailable")),
            patch.object(core, "_live_official_account_matches", return_value=False),
        ):
            updates = core._probe_codex_account(
                account["id"], parallel, force_metadata=True
            )
        return updates, calls, creds.call_args_list

    def test_quota_and_catalog_401_refresh_unexpired_oauth_once_and_recover(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                updates, calls, credential_calls = self._probe_401(parallel=parallel)
                self.assertEqual(updates["refreshState"], "ready")
                self.assertEqual(updates["refreshErrors"], {})
                self.assertEqual(updates["usage"]["weekly"]["remainingPercent"], 80)
                self.assertIsNone(updates["nextRefreshAt"])
                self.assertEqual(
                    sum(bool(call.kwargs.get("force_refresh")) for call in credential_calls), 1
                )
                self.assertIn((core.CHATGPT_USAGE_URL, "renewed-access"), calls)

    def test_repeated_401_is_bounded_and_preserves_actual_error(self):
        updates, calls, credential_calls = self._probe_401(permanent=True)
        self.assertEqual(updates["refreshState"], "error")
        self.assertIn("401", updates["refreshErrors"]["usage"])
        self.assertEqual(
            sum(bool(call.kwargs.get("force_refresh")) for call in credential_calls), 1
        )
        self.assertLessEqual(len(calls), 4)

    def test_403_does_not_rotate_oauth_or_invent_quota(self):
        updates, calls, credential_calls = self._probe_401(permanent=True, status=403)
        self.assertEqual(updates["refreshState"], "error")
        self.assertNotIn("usage", updates)
        self.assertEqual(
            sum(bool(call.kwargs.get("force_refresh")) for call in credential_calls), 0
        )
        self.assertEqual(len(calls), 2)

    def test_manual_refresh_bypasses_401_backoff_but_respects_429(self):
        for status in (401, 429):
            with self.subTest(status=status):
                account = self.account(
                    refreshState="error",
                    refreshErrors={"usage": f"HTTP {status}"},
                    nextRefreshAt=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    lastRefreshedAt="2020-01-01T00:00:00+00:00",
                )
                with (
                    patch.object(core, "load_settings", side_effect=lambda: {"accounts": [copy.deepcopy(account)]}),
                    patch.object(core, "save_settings"),
                    patch.object(core, "invalidate_codex_version_cache"),
                    patch.object(core, "_probe_codex_account", return_value={
                        "refreshState": "ready", "refreshErrors": {}, "nextRefreshAt": None,
                    }) as probe,
                ):
                    result = core._perform_account_refresh(account["id"], force_metadata=True)
                self.assertEqual(probe.call_count, 1 if status == 401 else 0)
                self.assertEqual(result.get("skipReason"), None if status == 401 else "backoff")

    @staticmethod
    def _jwt(payload):
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode(payload)}.fixture-signature"

    def _auth(self, *, access_label="first", refresh_label="first", access_expiry_hours=2,
              refreshed_minutes_ago=0, opaque=False, user="fixture-user"):
        now = datetime.now(timezone.utc)
        shared = {
            "sub": user,
            "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-workspace"},
        }
        return json.dumps({
            "auth_mode": "chatgptAuthTokens",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": self._jwt({**shared, "email": "fixture@example.test", "aud": [core.CODEX_OAUTH_CLIENT_ID]}),
                "access_token": f"opaque-{access_label}" if opaque else self._jwt({
                    **shared, "client_id": core.CODEX_OAUTH_CLIENT_ID,
                    "exp": int((now + timedelta(hours=access_expiry_hours)).timestamp()),
                    "jti": access_label,
                }),
                "refresh_token": f"refresh-{refresh_label}",
                "account_id": "fixture-workspace",
            },
            "last_refresh": (now - timedelta(minutes=refreshed_minutes_ago)).isoformat(),
        }).encode()

    def test_live_rotation_is_reused_when_rejected_token_already_changed(self):
        stale = self._auth(refreshed_minutes_ago=10, access_expiry_hours=1)
        fresh = self._auth(access_label="second", refresh_label="second", access_expiry_hours=2)
        old_snapshot, identity = core._snapshot_from_bytes(stale, None)
        new_snapshot, _ = core._snapshot_from_bytes(fresh, None)
        account = self.account(**identity)
        (core.CODEX_HOME / "auth.json").write_bytes(fresh)
        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(core, "_load_account_snapshot", return_value=old_snapshot),
            patch.object(core, "_read_live_snapshot", return_value=(new_snapshot, identity)),
            patch.object(core, "_request_codex_oauth_refresh") as refresh,
            patch.object(core, "_persist_account_oauth_auth") as persist,
        ):
            credentials = core._account_chatgpt_credentials(
                account["id"], force_refresh=True,
                rejected_access_token=json.loads(stale)["tokens"]["access_token"],
            )
        self.assertEqual(credentials["accessToken"], json.loads(fresh)["tokens"]["access_token"])
        refresh.assert_not_called()
        persist.assert_called_once()

    def test_newer_live_opaque_token_is_not_discarded_for_old_jwt_expiry(self):
        stale = self._auth(refreshed_minutes_ago=10)
        fresh = self._auth(access_label="second", refresh_label="second", opaque=True)
        # Some upstream access credentials are opaque. last_refresh is the
        # available ordering evidence; having no JWT exp does not make it old.
        self.assertTrue(core._codex_oauth_auth_is_newer(fresh, stale))
        self.assertFalse(core._codex_oauth_auth_is_newer(stale, fresh))

    def test_native_oauth_projection_omits_external_token_auth_mode(self):
        projected = json.loads(core._codex_auth_projection_bytes(self._auth()))
        self.assertNotIn("auth_mode", projected)
        self.assertNotIn("authMode", projected)
        self.assertIsNone(projected["OPENAI_API_KEY"])
        self.assertTrue(projected["tokens"]["refresh_token"])


if __name__ == "__main__":
    unittest.main()
