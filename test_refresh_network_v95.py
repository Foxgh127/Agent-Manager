import copy
import io
import ssl
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import agent_manager_core as core


TRANSIENT_TLS_MESSAGE = (
    "ChatGPT 临时中断了安全连接；已自动重试仍未成功，请稍后再刷新。"
)


class RefreshNetworkV95Regressions(unittest.TestCase):
    """Isolated reproductions for official-account refresh state handling."""

    @staticmethod
    def _account(**overrides):
        account = {
            "id": "network-v95",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "plan": "pro",
            "planLabel": "Pro 5x",
            "models": ["gpt-cached"],
            "modelsLastCheckedAt": "2020-01-01T00:00:00+00:00",
            "subscriptionExpiresAt": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
            "usage": {
                "weekly": {
                    "remainingPercent": 75,
                    "usedPercent": 25,
                    "windowMinutes": 10_080,
                },
                "resetCredits": {
                    "availableCount": 1,
                    "detailsAvailable": True,
                    "detailsCheckedAt": "2020-01-01T00:00:00+00:00",
                    "credits": [{"id": "cached-credit"}],
                },
            },
            "refreshState": "ready",
            "refreshErrors": {},
        }
        account.update(overrides)
        return account

    def test_get_survives_two_consecutive_transient_tls_interruptions(self):
        class FakeResponse(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        failure = urllib.error.URLError(
            ssl.SSLEOFError(
                8,
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol",
            )
        )
        response = FakeResponse(b'{"ok":true}')
        with (
            patch.object(core, "_throttle_chatgpt_request"),
            patch.object(core.time, "sleep") as sleep,
            patch.object(
                core,
                "_open_same_origin_request",
                side_effect=[failure, failure, response],
            ) as opened,
        ):
            try:
                payload = core._fetch_chatgpt_json(
                    "https://chatgpt.example.test/data", "fake-token", "fake-account"
                )
            except core.ManagerError as exc:
                self.fail(f"bounded GET retry stopped before recovery: {exc}")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertGreater(
            sleep.call_args_list[1].args[0], sleep.call_args_list[0].args[0]
        )

    def test_optional_subscription_tls_failure_does_not_downgrade_core_refresh(self):
        account = self._account()

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {
                    "plan_type": "pro",
                    "rate_limit": {
                        "secondary_window": {
                            "used_percent": 20,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 1},
                }
            if url == core.CHATGPT_MODELS_URL:
                return {"models": [{"slug": "gpt-current"}]}
            raise AssertionError(f"unexpected endpoint: {url}")

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={
                    "accessToken": "fake-token",
                    "accountId": "fake-account",
                    "subscriptionExpiresAt": account["subscriptionExpiresAt"],
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(
                core,
                "_fetch_chatgpt_subscription_status",
                side_effect=core.ManagerError(TRANSIENT_TLS_MESSAGE),
            ),
            patch.object(core, "_live_official_account_matches", return_value=False),
        ):
            updates = core._probe_codex_account(
                account["id"], force_metadata=True, include_reset_details=False
            )

        self.assertEqual(updates["usage"]["weekly"]["remainingPercent"], 80)
        self.assertEqual(updates["models"], ["gpt-current"])
        self.assertEqual(updates["refreshState"], "ready")
        self.assertNotIn("subscription", updates["refreshErrors"])
        self.assertIn("subscription", updates["refreshWarnings"])

    def test_suppressed_reset_detail_tls_failure_does_not_back_off_core_refresh(self):
        account = self._account(
            subscriptionExpiresAt=(
                datetime.now(timezone.utc) + timedelta(days=7)
            ).isoformat(),
            modelsLastCheckedAt=core.now_iso(),
        )

        def fetch(url, *_args, **_kwargs):
            if url == core.CHATGPT_USAGE_URL:
                return {
                    "plan_type": "pro",
                    "rate_limit": {
                        "secondary_window": {
                            "used_percent": 20,
                            "limit_window_seconds": 604_800,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 1},
                }
            if url == core.CHATGPT_RESET_CREDITS_URL:
                raise core.ManagerError(TRANSIENT_TLS_MESSAGE)
            raise AssertionError(f"unexpected endpoint: {url}")

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={
                    "accessToken": "fake-token",
                    "accountId": "fake-account",
                    "subscriptionExpiresAt": account["subscriptionExpiresAt"],
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
            patch.object(core, "_live_official_account_matches", return_value=False),
        ):
            updates = core._probe_codex_account(
                account["id"], include_reset_details=True
            )

        self.assertEqual(updates["refreshState"], "ready")
        self.assertEqual(updates["refreshErrors"], {})
        self.assertIn("resetCredits", updates["refreshWarnings"])
        self.assertIsNotNone(
            updates["usage"]["resetCredits"]["detailsNextRetryAt"]
        )
        self.assertIsNone(updates["nextRefreshAt"])

    def test_successful_reset_detail_retry_clears_sole_partial_state(self):
        account = self._account(
            refreshState="partial",
            refreshErrors={"resetCredits": TRANSIENT_TLS_MESSAGE},
        )
        settings = {"accounts": [account]}

        with (
            patch.object(core, "load_settings", side_effect=lambda: settings),
            patch.object(core, "save_settings"),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={"accessToken": "fake-token", "accountId": "fake-account"},
            ),
            patch.object(
                core,
                "_fetch_chatgpt_json",
                return_value={
                    "available_count": 1,
                    "credits": [{"id": "fresh-credit"}],
                },
            ),
            patch.object(core, "_live_official_account_matches", return_value=False),
        ):
            refreshed = core.refresh_account_reset_credit_details(
                account["id"], force=True
            )

        self.assertEqual(refreshed["refreshErrors"], {})
        self.assertEqual(refreshed["refreshWarnings"], {})
        self.assertEqual(refreshed["refreshState"], "ready")

    def test_ancillary_warning_and_retry_gate_survive_core_only_refresh(self):
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        account = self._account(
            modelsLastCheckedAt=core.now_iso(),
            subscriptionNextRetryAt=retry_at,
            refreshWarnings={
                "subscription": TRANSIENT_TLS_MESSAGE,
                "resetCredits": TRANSIENT_TLS_MESSAGE,
            },
        )
        account["usage"]["resetCredits"].update(
            {
                "detailsAvailable": False,
                "credits": [],
                "detailsNextRetryAt": retry_at,
            }
        )
        calls = []

        def fetch(url, *_args, **_kwargs):
            calls.append(url)
            return {
                "plan_type": "pro",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 20,
                        "limit_window_seconds": 604_800,
                    }
                },
                "rate_limit_reset_credits": {"available_count": 1},
            }

        with (
            patch.object(core, "load_settings", return_value={"accounts": [account]}),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={
                    "accessToken": "fake-token",
                    "accountId": "fake-account",
                    "subscriptionExpiresAt": account["subscriptionExpiresAt"],
                },
            ),
            patch.object(core, "_fetch_chatgpt_json", side_effect=fetch),
        ):
            updates = core._probe_codex_account(account["id"])

        self.assertEqual(calls, [core.CHATGPT_USAGE_URL])
        self.assertEqual(updates["refreshState"], "ready")
        self.assertEqual(
            set(updates["refreshWarnings"]), {"subscription", "resetCredits"}
        )
        self.assertEqual(
            updates["usage"]["resetCredits"]["detailsNextRetryAt"], retry_at
        )
        self.assertIsNone(updates["nextRefreshAt"])

    def test_successful_full_refresh_replaces_old_transient_warning(self):
        account = self._account(
            refreshState="partial",
            refreshErrors={"models": TRANSIENT_TLS_MESSAGE},
        )
        settings = {"accounts": [account]}
        updates = {
            "lastRefreshedAt": core.now_iso(),
            "nextRefreshAt": None,
            "refreshState": "ready",
            "refreshErrors": {},
        }

        with (
            patch.object(core, "load_settings", side_effect=lambda: copy.deepcopy(settings)),
            patch.object(core, "save_settings"),
            patch.object(core, "_probe_codex_account", return_value=updates),
        ):
            result = core._perform_account_refresh(account["id"], force_metadata=True)

        self.assertEqual(result["account"]["refreshState"], "ready")
        self.assertEqual(result["account"]["refreshErrors"], {})


if __name__ == "__main__":
    unittest.main()
