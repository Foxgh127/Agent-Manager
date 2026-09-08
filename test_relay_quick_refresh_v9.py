import unittest
from contextlib import nullcontext
from unittest.mock import patch

import relay_portal_service as relay


def response(data, *, status=200):
    return {
        "status": status,
        "json": True,
        "body": {"success": 200 <= status < 300, "data": data},
    }


class RelayQuickRefreshV9Tests(unittest.TestCase):
    def account(self, *, adapter="new-api"):
        return {
            "id": "relay-existing",
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": adapter,
            "selectedKeyId": "key-active",
            "selectedEndpointId": "default",
            "groupId": "saved-local-group",
            "providerId": "provider-existing",
            "keys": [{"id": "key-active", "groupId": "vip"}],
            "groups": [{"id": "vip", "name": "VIP"}],
            "models": ["gpt-cached"],
        }

    def session(self, *, adapter="new-api"):
        return {
            "version": 1,
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
            "adapter": adapter,
            "authMode": "bearer",
            "userId": "42",
            "sessionId": "old-sid",
            "accessToken": "old-access",
            "refreshToken": "old-refresh" if adapter == "sub2api" else "",
            "cookies": [],
        }

    def full_preview(self):
        return {
            "adapter": "new-api",
            "adapterLabel": "New API / One API",
            "siteName": "Relay",
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/dashboard",
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
            "user": {"id": "42", "email": "alice@example.test"},
            "balance": {"remaining": 8, "used": 2, "currency": "USD"},
            "models": ["gpt-live"],
            "groups": [{"id": "vip", "name": "VIP", "platform": "openai", "active": True}],
            "keys": [
                {
                    "id": "key-active",
                    "name": "Current",
                    "groupId": "vip",
                    "group": "VIP",
                    "groupPlatform": "openai",
                }
            ],
            "keysAuthoritative": True,
            "groupsAuthoritative": True,
            "quotaPerUnit": 1_000_000,
        }

    def test_healthy_new_api_quick_probe_uses_one_user_request_and_no_catalog(self):
        session = self.session()
        paths = []

        def request(_session, path, **_kwargs):
            paths.append(path)
            return response(
                {
                    "id": 42,
                    "email": "alice@example.test",
                    "quota": 4_000_000,
                    "used_quota": 1_000_000,
                }
            )

        with patch.object(relay, "_saved_json_request", side_effect=request):
            raw, updated = relay._probe_saved_dashboard_quick(
                session,
                quota_per_unit=500_000,
            )

        self.assertEqual(paths, ["/api/user/self"])
        self.assertEqual(raw["_requestCount"], 1)
        self.assertEqual(raw["_refreshScope"], "account")
        self.assertEqual(raw["_balance"], {"remaining": 8.0, "used": 2.0, "currency": "USD"})
        self.assertEqual(updated["userId"], "42")
        self.assertFalse(
            any(
                marker in path
                for path in paths
                for marker in ("token", "keys", "groups", "models", "usage", "status")
            )
        )

    def test_cached_custom_quota_multiplier_prevents_default_500k_balance_error(self):
        session = self.session()
        with patch.object(
            relay,
            "_saved_json_request",
            return_value=response(
                {"id": 42, "quota": 4_000_000, "used_quota": 1_000_000}
            ),
        ) as requested:
            raw, _updated = relay._probe_saved_dashboard_quick(
                session,
                quota_per_unit=1_000_000,
            )

        requested.assert_called_once()
        self.assertEqual(raw["_balance"]["remaining"], 4.0)
        self.assertEqual(raw["_balance"]["used"], 1.0)
        self.assertNotEqual(raw["_balance"]["remaining"], 8.0)

    def test_missing_quota_multiplier_is_learned_with_one_bounded_status_request(self):
        session = self.session()
        paths = []

        def request(_session, path, **_kwargs):
            paths.append(path)
            if path == "/api/status":
                return response({"quota_per_unit": 1_000_000})
            return response({"id": 42, "quota": 4_000_000, "used_quota": 1_000_000})

        with patch.object(relay, "_saved_json_request", side_effect=request):
            raw, _updated = relay._probe_saved_dashboard_quick(session)

        self.assertEqual(paths, ["/api/user/self", "/api/status"])
        self.assertEqual(raw["_requestCount"], 2)
        self.assertEqual(raw["_quotaPerUnitLearned"], 1_000_000)
        self.assertEqual(raw["_balance"]["remaining"], 4.0)

    def test_failed_multiplier_lookup_never_applies_the_historical_default(self):
        session = self.session()

        def request(_session, path, **_kwargs):
            if path == "/api/status":
                return response({}, status=503)
            return response({"id": 42, "quota": 4_000_000, "used_quota": 1_000_000})

        with patch.object(relay, "_saved_json_request", side_effect=request):
            raw, _updated = relay._probe_saved_dashboard_quick(session)

        self.assertIsNone(raw["_balance"])
        self.assertEqual(raw["_balanceUnavailableReason"], "quota_per_unit_unknown")
        self.assertIsNone(raw["_quotaPerUnit"])
        self.assertIsNone(raw["_quotaPerUnitLearned"])

    def test_full_scan_exposes_only_explicit_quota_multiplier_for_persistent_cache(self):
        raw = {
            "adapter": "new-api",
            "status": response(
                {
                    "system_name": "Relay",
                    "server_address": "https://relay.example.test",
                    "quota_per_unit": 1_000_000,
                }
            ),
            "user": response({"id": 42, "quota": 4_000_000, "used_quota": 1_000_000}),
            "keys": [response([])],
            "models": [response([])],
            "keysAuthoritative": True,
            "groupsAuthoritative": False,
        }

        preview, _secrets = relay.normalize_probe_result(
            raw,
            portal_url="https://relay.example.test/dashboard",
        )

        self.assertEqual(preview["quotaPerUnit"], 1_000_000)
        self.assertEqual(preview["balance"]["remaining"], 4.0)

    def test_full_scan_without_multiplier_does_not_render_raw_quota_with_default_500k(self):
        raw = {
            "adapter": "new-api",
            "status": response(
                {
                    "system_name": "Relay",
                    "server_address": "https://relay.example.test",
                }
            ),
            "user": response({"id": 42, "quota": 4_000_000, "used_quota": 1_000_000}),
            "keys": [response([])],
            "models": [response([])],
            "keysAuthoritative": True,
            "groupsAuthoritative": False,
        }

        preview, _secrets = relay.normalize_probe_result(
            raw,
            portal_url="https://relay.example.test/dashboard",
        )

        self.assertIsNone(preview["quotaPerUnit"])
        self.assertIsNone(preview["balance"]["remaining"])
        self.assertIsNone(preview["balance"]["used"])

    def test_new_api_quick_probe_refreshes_only_after_rejection_and_retries_stale_sid_once(self):
        session = self.session()
        paths = []

        def request(current, path, **_kwargs):
            paths.append(path)
            if path == "/api/user/self" and len(paths) in {1, 2}:
                return response({}, status=401)
            if path == "/api/user/auth/refresh" and paths.count(path) == 1:
                self.assertEqual(current.get("sessionId"), "old-sid")
                return {"status": 409, "json": True, "body": {"code": "AUTH_SESSION_MISMATCH"}}
            if path == "/api/user/auth/refresh":
                self.assertNotIn("sessionId", current)
                return response({"access_token": "new-access", "session": {"sid": "new-sid"}})
            if path == "/api/user/self":
                return response({"id": 42, "email": "alice@example.test", "quota": 8})
            raise AssertionError(path)

        with patch.object(relay, "_saved_json_request", side_effect=request):
            raw, updated = relay._probe_saved_dashboard_quick(
                session,
                quota_per_unit=500_000,
            )

        self.assertEqual(
            paths,
            [
                "/api/user/self",
                "/api/user/self",
                "/api/user/auth/refresh",
                "/api/user/auth/refresh",
                "/api/user/self",
            ],
        )
        self.assertEqual(raw["_requestCount"], 5)
        self.assertEqual(updated["accessToken"], "new-access")
        self.assertEqual(updated["sessionId"], "new-sid")

    def test_healthy_sub2api_quick_probe_uses_one_auth_request(self):
        session = self.session(adapter="sub2api")
        with patch.object(
            relay,
            "_saved_json_request",
            return_value=response({"id": 42, "email": "alice@example.test", "balance": 8}),
        ) as requested:
            raw, _updated = relay._probe_saved_dashboard_quick(session)

        self.assertEqual(raw["_requestCount"], 1)
        requested.assert_called_once()
        self.assertEqual(requested.call_args.args[1], "/api/v1/auth/me")

    def test_quick_identity_change_never_persists_rotated_session(self):
        account = self.account()
        session = self.session()
        with (
            patch.object(relay.core, "load_relay_account_dashboard_session", return_value=session),
            patch.object(
                relay,
                "_saved_json_request",
                return_value=response({"id": 43, "email": "other@example.test", "quota": 8}),
            ),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
        ):
            with self.assertRaises(relay.DashboardIdentityChanged):
                relay.RelayPortalService._scan_saved_account(account, full=False)

        stored.assert_not_called()

    def test_healthy_quick_check_does_not_rewrite_unchanged_dashboard_secret(self):
        account = self.account()
        account["dashboardMetadata"] = {"quotaPerUnit": 500_000}
        session = self.session()
        with (
            patch.object(relay.core, "load_relay_account_dashboard_session", return_value=session),
            patch.object(
                relay,
                "_saved_json_request",
                return_value=response(
                    {
                        "id": 42,
                        "email": "alice@example.test",
                        "quota": 4_000_000,
                    }
                ),
            ),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
        ):
            raw, _secrets, _updated = relay.RelayPortalService._scan_saved_account(
                account,
                full=False,
            )

        self.assertEqual(raw["_requestCount"], 1)
        stored.assert_not_called()

    def test_quick_refresh_persists_token_rotation_after_identity_verification(self):
        account = self.account()
        account["user"] = {"id": "42", "email": "alice@example.test"}
        account["dashboardMetadata"] = {"quotaPerUnit": 500_000}
        session = self.session()
        calls = 0

        def request(_current, path, **_kwargs):
            nonlocal calls
            calls += 1
            if path == "/api/user/self" and calls in {1, 2}:
                return response({}, status=401)
            if path == "/api/user/auth/refresh":
                return response({"access_token": "rotated", "session": {"sid": "rotated-sid"}})
            return response({"id": 42, "email": "alice@example.test", "quota": 4_000_000})

        with (
            patch.object(relay.core, "load_relay_account_dashboard_session", return_value=session),
            patch.object(relay, "_saved_json_request", side_effect=request),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
        ):
            relay.RelayPortalService._scan_saved_account(account, full=False)

        stored.assert_called_once()
        self.assertEqual(stored.call_args.args[1]["accessToken"], "rotated")
        self.assertEqual(stored.call_args.args[1]["sessionId"], "rotated-sid")

    def test_default_refresh_preserves_catalogs_and_refreshes_selected_key_once(self):
        service = relay.RelayPortalService()
        account = self.account()
        refreshed_account = {**account, "models": ["gpt-new"]}
        quick = {
            "adapter": "new-api",
            "_refreshScope": "account",
            "_requestCount": 1,
            "user": response({"id": 42}),
            "_accountUser": {"id": "42", "email": "alice@example.test"},
            "_balance": {"remaining": 8, "used": 2, "currency": "USD"},
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(quick, {}, self.session())) as scanned,
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={
                    "account": refreshed_account,
                    "warnings": [],
                    "modelRefresh": {"status": "ready", "models": ["gpt-new"]},
                },
            ) as key_refresh,
            patch.object(
                service,
                "_update_quick_account_metadata",
                return_value=refreshed_account,
            ) as metadata_update,
            patch.object(relay.core, "sync_relay_account_snapshot") as full_sync,
        ):
            result = service.refresh_account("relay-existing")

        scanned.assert_called_once_with(account, full=False)
        key_refresh.assert_called_once_with(
            "relay-existing",
            refresh_balance=False,
            fallback_notice=False,
        )
        metadata_update.assert_called_once()
        full_sync.assert_not_called()
        self.assertEqual(result["refreshMode"], "quick")
        self.assertFalse(result["requiresLogin"])
        self.assertEqual(result["catalogCache"]["keys"], "preserved")
        self.assertEqual(result["catalogCache"]["groups"], "preserved")
        self.assertEqual(result["catalogCache"]["models"], "requested_from_selected_key")
        self.assertEqual(result["catalogCache"]["balance"], "updated_from_dashboard")
        self.assertEqual(result["requestBudget"]["dashboardRequests"], 1)
        self.assertEqual(result["requestBudget"]["selectedKeyRefreshCalls"], 1)
        self.assertEqual(result["requestBudget"]["selectedKeyOperations"], 1)
        self.assertFalse(result["requestBudget"]["selectedKeyBalanceRequested"])
        self.assertEqual(result["account"]["selectedKeyId"], "key-active")
        self.assertEqual(result["account"]["groupId"], "saved-local-group")
        self.assertEqual(result["account"]["providerId"], "provider-existing")

    def test_quick_metadata_patch_preserves_keys_groups_models_selection_and_extra_fields(self):
        account = {
            **self.account(),
            "user": {"id": "42", "name": "Old", "email": "alice@example.test"},
            "balance": {"remaining": 1, "used": 1, "currency": "USD"},
            "customMetadata": {"keep": True},
        }
        provider = {
            "id": "provider-existing",
            "models": ["gpt-cached"],
            "balance": {"amount": 1, "currency": "USD"},
            "customProviderField": "keep",
        }
        settings = {"relayAccounts": [account], "providers": [provider]}
        with (
            patch.object(relay.core, "_settings_file_lock", return_value=nullcontext()),
            patch.object(relay.core, "load_settings", return_value=settings),
            patch.object(relay.core, "save_settings") as saved,
        ):
            updated = relay.RelayPortalService._update_quick_account_metadata(
                "relay-existing",
                {"id": "42", "name": "Alice", "email": "alice@example.test", "group": "vip"},
                {"remaining": 8, "used": 2, "currency": "USD"},
                1_000_000,
            )

        saved.assert_called_once_with(settings)
        self.assertEqual(updated["selectedKeyId"], "key-active")
        self.assertEqual(updated["selectedEndpointId"], "default")
        self.assertEqual(updated["groupId"], "saved-local-group")
        self.assertEqual(updated["providerId"], "provider-existing")
        self.assertEqual(updated["keys"], [{"id": "key-active", "groupId": "vip"}])
        self.assertEqual(updated["groups"], [{"id": "vip", "name": "VIP"}])
        self.assertEqual(updated["models"], ["gpt-cached"])
        self.assertEqual(updated["customMetadata"], {"keep": True})
        self.assertEqual(updated["dashboardMetadata"]["quotaPerUnit"], 1_000_000)
        self.assertEqual(updated["balance"]["remaining"], 8)
        self.assertEqual(provider["customProviderField"], "keep")
        self.assertEqual(provider["balance"]["amount"], 8)

    def test_missing_dashboard_balance_falls_back_to_key_balance_and_models(self):
        service = relay.RelayPortalService()
        account = self.account()
        quick = {
            "adapter": "new-api",
            "_refreshScope": "account",
            "_requestCount": 1,
            "_accountUser": {"id": "42"},
            "_balance": None,
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(quick, {}, self.session())),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": []},
            ) as key_refresh,
            patch.object(service, "_update_quick_account_metadata", return_value=account),
        ):
            result = service.refresh_account("relay-existing")

        key_refresh.assert_called_once_with(
            "relay-existing",
            refresh_balance=True,
            fallback_notice=False,
        )
        self.assertEqual(result["requestBudget"]["selectedKeyOperations"], 2)
        self.assertTrue(result["requestBudget"]["selectedKeyBalanceRequested"])

    def test_unknown_quota_multiplier_preserves_previous_balance_without_key_balance_probe(self):
        service = relay.RelayPortalService()
        account = {**self.account(), "balance": {"remaining": 9, "used": 3, "currency": "USD"}}
        quick = {
            "adapter": "new-api",
            "_refreshScope": "account",
            "_requestCount": 2,
            "_accountUser": {"id": "42"},
            "_balance": None,
            "_balanceUnavailableReason": "quota_per_unit_unknown",
            "_quotaPerUnitLearned": None,
        }

        def preserve_metadata(_account_id, _user, balance, _quota_per_unit):
            self.assertIsNone(balance)
            return account

        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(quick, {}, self.session())),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": [], "modelRefresh": {"status": "ready"}},
            ) as key_refresh,
            patch.object(service, "_update_quick_account_metadata", side_effect=preserve_metadata),
        ):
            result = service.refresh_account("relay-existing")

        key_refresh.assert_called_once_with(
            "relay-existing",
            refresh_balance=False,
            fallback_notice=False,
        )
        self.assertEqual(result["account"]["balance"]["remaining"], 9)
        self.assertEqual(
            result["catalogCache"]["balance"],
            "preserved_quota_conversion_unknown",
        )
        self.assertFalse(result["requestBudget"]["selectedKeyBalanceRequested"])
        self.assertIn("保留原余额", result["warnings"][0])

    def test_successful_key_models_are_not_reported_as_failed_when_balance_writeback_fails(self):
        service = relay.RelayPortalService()
        account = self.account()
        quick = {
            "adapter": "new-api",
            "_refreshScope": "account",
            "_requestCount": 1,
            "_accountUser": {"id": "42"},
            "_balance": {"remaining": 8, "used": 2, "currency": "USD"},
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(quick, {}, self.session())),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={
                    "account": {**account, "models": ["gpt-new"]},
                    "warnings": [],
                    "modelRefresh": {"status": "ready", "models": ["gpt-new"]},
                },
            ),
            patch.object(
                service,
                "_update_quick_account_metadata",
                side_effect=relay.core.ManagerError("settings busy"),
            ),
        ):
            result = service.refresh_account("relay-existing")

        self.assertEqual(result["modelRefresh"]["status"], "ready")
        self.assertEqual(result["account"]["models"], ["gpt-new"])
        self.assertIn("当前 Key 已刷新", result["warnings"][0])

    def test_temporary_dashboard_failure_keeps_successful_key_refresh_and_does_not_request_login(self):
        service = relay.RelayPortalService()
        account = self.account()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(
                service,
                "_scan_saved_account",
                side_effect=relay.DashboardTemporarilyUnavailable("dashboard HTTP 503"),
            ),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": [], "modelRefresh": {"status": "ready"}},
            ) as key_refresh,
        ):
            result = service.refresh_account("relay-existing")

        self.assertFalse(result["requiresLogin"])
        self.assertFalse(result["liveDashboard"])
        self.assertIn("503", result["warnings"][0])
        self.assertEqual(result["account"]["selectedKeyId"], "key-active")
        key_refresh.assert_called_once_with(
            "relay-existing",
            refresh_balance=True,
            fallback_notice=False,
        )

    def test_conclusive_dashboard_401_requests_login_without_discarding_key_success(self):
        service = relay.RelayPortalService()
        account = self.account()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(
                service,
                "_scan_saved_account",
                side_effect=relay.DashboardLoginRequired("中转站登录凭据已失效"),
            ),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": [], "modelRefresh": {"status": "ready"}},
            ),
        ):
            result = service.refresh_account("relay-existing")

        self.assertTrue(result["requiresLogin"])
        self.assertEqual(result["account"]["selectedKeyId"], "key-active")
        self.assertIn("网页登录续期失败", result["warnings"][0])

    def test_explicit_full_refresh_keeps_authoritative_catalog_scan(self):
        service = relay.RelayPortalService()
        account = self.account()
        preview = self.full_preview()
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(preview, {}, self.session())) as scanned,
            patch.object(
                relay.core,
                "sync_relay_account_snapshot",
                return_value={"account": account, "liveDashboard": True},
            ) as synced,
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": [], "modelRefresh": {"status": "ready"}},
            ) as key_refresh,
            patch.object(
                service,
                "_update_quick_account_metadata",
                return_value={**account, "dashboardMetadata": {"quotaPerUnit": 1_000_000}},
            ) as cached_multiplier,
        ):
            result = service.refresh_account("relay-existing", full=True)

        scanned.assert_called_once_with(account, full=True)
        synced.assert_called_once()
        key_refresh.assert_called_once_with(
            "relay-existing",
            refresh_balance=False,
            fallback_notice=False,
        )
        cached_multiplier.assert_called_once_with(
            "relay-existing",
            {},
            preview["balance"],
            1_000_000,
        )
        self.assertEqual(result["account"]["dashboardMetadata"]["quotaPerUnit"], 1_000_000)
        self.assertEqual(result["refreshMode"], "full")


if __name__ == "__main__":
    unittest.main()
