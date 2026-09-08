import json
from http.cookies import SimpleCookie
import threading
import time
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import agent_manager.application as app
import agent_manager.accounts.relay as relay


def response(data, *, status=200):
    return {"status": status, "body": {"success": True, "data": data}}


class RelayNormalizationTests(unittest.TestCase):
    def test_isolated_webview_session_captures_same_origin_http_only_cookie(self):
        same_origin = SimpleCookie()
        same_origin.load("session=opaque-refresh-cookie; Path=/; Domain=.example.test; Secure; HttpOnly")
        foreign = SimpleCookie()
        foreign.load("foreign=must-not-leave; Path=/; Domain=evil.test; Secure")
        window = SimpleNamespace(get_cookies=lambda: [same_origin, foreign])

        session = relay._dashboard_session_from_probe(
            {
                "adapter": "new-api",
                "session": {
                    "accessToken": "short-lived-access",
                    "authMode": "bearer",
                    "userId": "7",
                },
            },
            portal_url="https://console.example.test/dashboard",
            window=window,
        )

        self.assertEqual(session["origin"], "https://console.example.test")
        self.assertEqual(session["accessToken"], "short-lived-access")
        self.assertEqual([item["name"] for item in session["cookies"]], ["session"])
        self.assertTrue(session["cookies"][0]["httpOnly"])

    def test_saved_sub2api_session_rotates_tokens_without_a_webview(self):
        auth_calls = 0

        def request(_session, path, **_kwargs):
            nonlocal auth_calls
            if path == "/api/v1/settings/public":
                return response({"site_name": "Relay", "api_base_url": "https://relay.example.test"})
            if path == "/api/v1/auth/me":
                auth_calls += 1
                return response({}, status=401) if auth_calls == 1 else response({"id": 9, "balance": 8})
            if path == "/api/v1/auth/refresh":
                return response({"access_token": "new-access", "refresh_token": "new-refresh"})
            if path.startswith("/api/v1/keys"):
                return response({"items": []})
            if path == "/api/v1/groups/available":
                return response([])
            if path == "/api/v1/groups/rates":
                return response({})
            if path == "/api/v1/usage/dashboard/models":
                return response({"models": []})
            if path == "/api/v1/usage/dashboard/stats":
                return response({})
            raise AssertionError(path)

        with patch.object(relay, "_saved_json_request", side_effect=request):
            raw, renewed = relay._probe_saved_dashboard(
                {
                    "origin": "https://relay.example.test",
                    "portalUrl": "https://relay.example.test/dashboard",
                    "adapter": "sub2api",
                    "authMode": "bearer",
                    "accessToken": "expired-access",
                    "refreshToken": "old-refresh",
                    "cookies": [],
                }
            )

        self.assertEqual(raw["adapter"], "sub2api")
        self.assertTrue(raw["keysAuthoritative"])
        self.assertTrue(raw["groupsAuthoritative"])
        self.assertEqual(auth_calls, 2)
        self.assertEqual(renewed["accessToken"], "new-access")
        self.assertEqual(renewed["refreshToken"], "new-refresh")

    def test_normalizes_new_api_without_exposing_full_keys(self):
        raw = {
            "adapter": "new-api",
            "status": response(
                {
                    "system_name": "Example Relay",
                    "server_address": "https://api.example.test",
                    "quota_per_unit": 500_000,
                }
            ),
            "user": response(
                {
                    "id": 7,
                    "username": "alice",
                    "quota": 6_500_000,
                    "used_quota": 1_250_000,
                    "group": "default",
                }
            ),
            "keys": [
                response(
                    {
                        "items": [
                            {
                                "id": 41,
                                "name": "codex",
                                "key": "abcDEF0123456789abcdef",
                                "status": 1,
                                "unlimited_quota": True,
                            }
                        ]
                    }
                )
            ],
            "models": [response([{"id": "gpt-5.6"}, {"id": "gpt-5.6-mini"}])],
        }

        preview, secrets = relay.normalize_probe_result(
            raw, portal_url="https://console.example.test/login"
        )

        self.assertEqual(preview["adapter"], "new-api")
        self.assertEqual(preview["siteName"], "Example Relay")
        self.assertEqual(preview["baseUrl"], "https://api.example.test/v1")
        self.assertEqual(preview["balance"]["remaining"], 13.0)
        self.assertEqual(preview["balance"]["used"], 2.5)
        self.assertEqual(preview["models"], ["gpt-5.6", "gpt-5.6-mini"])
        self.assertNotIn("abcDEF0123456789abcdef", json.dumps(preview))
        self.assertEqual(secrets, {"41": "sk-abcDEF0123456789abcdef"})

    def test_normalizes_sub2api_dashboard_data(self):
        raw = {
            "adapter": "sub2api",
            "status": response(
                {
                    "site_name": "哈基米",
                    "api_base_url": "https://api.hajimi.chat",
                    "custom_endpoints": [
                        {
                            "name": "回国路线",
                            "endpoint": "https://api.hajimi.chat",
                            "description": "访问更快",
                        },
                        {
                            "name": "生图专用",
                            "endpoint": "https://image.hajimi.chat",
                            "description": "",
                        },
                    ],
                }
            ),
            "user": response(
                {
                    "id": 9,
                    "username": "ganrenbo",
                    "email": "user@example.test",
                    "balance": "13.22",
                }
            ),
            "stats": response({"total_actual_cost": 29.4803, "currency": "usd"}),
            "keys": [
                response(
                    {
                        "items": [
                            {
                                "id": "k-1",
                                "name": "claudecode",
                                "key": "sk-sub2api0123456789",
                                "status": "active",
                                "expires_at": None,
                                "group_id": 7,
                                "group": {
                                    "id": 7,
                                    "name": "特惠",
                                    "platform": "openai",
                                    "rate_multiplier": 0.08,
                                },
                            }
                        ]
                    }
                )
            ],
            "models": [response({"models": [{"model": "gpt-5.6"}]})],
            "groups": response(
                [
                    {
                        "id": 7,
                        "name": "特惠",
                        "platform": "openai",
                        "status": "active",
                        "rate_multiplier": 0.08,
                    },
                    {
                        "id": 8,
                        "name": "Claude-kiro-低缓",
                        "platform": "anthropic",
                        "status": "active",
                        "rate_multiplier": 0.07,
                    },
                ]
            ),
            "groupRates": response({"7": 0.06, "8": 0.065}),
            "keyUsage": response(
                {
                    "stats": {
                        "k-1": {
                            "api_key_id": "k-1",
                            "today_actual_cost": 0.0,
                            "total_actual_cost": 29.4803,
                        }
                    }
                }
            ),
        }

        preview, secrets = relay.normalize_probe_result(
            raw, portal_url="https://relay.example.test/keys"
        )

        self.assertEqual(preview["adapterLabel"], "Sub2API")
        self.assertEqual(preview["siteName"], "哈基米")
        self.assertEqual(preview["baseUrl"], "https://api.hajimi.chat/v1")
        self.assertEqual(preview["balance"], {"remaining": 13.22, "used": 29.4803, "currency": "USD"})
        self.assertEqual(preview["models"], ["gpt-5.6"])
        self.assertEqual(
            [item["baseUrl"] for item in preview["apiEndpoints"]],
            ["https://api.hajimi.chat/v1", "https://image.hajimi.chat/v1"],
        )
        self.assertEqual(preview["apiEndpoints"][0]["aliases"], ["回国路线"])
        self.assertEqual(preview["groups"][0]["rateMultiplier"], 0.06)
        self.assertEqual(preview["keys"][0]["group"], "特惠")
        self.assertEqual(preview["keys"][0]["groupRateMultiplier"], 0.06)
        self.assertEqual(preview["keys"][0]["totalUsed"], 29.4803)
        self.assertNotIn("sk-sub2api0123456789", json.dumps(preview))
        self.assertEqual(secrets["k-1"], "sk-sub2api0123456789")

    def test_probe_rejects_html_spa_fallback_and_prefers_sub2api(self):
        script = relay._probe_script("https://relay.example.test")

        self.assertIn("response.json !== true", script)
        self.assertIn("'X-User-UI-Request': '1'", script)
        self.assertLess(
            script.index("await request('/api/v1/auth/me'"),
            script.index("await request('/api/user/self'"),
        )
        self.assertIn("/api/v1/usage/dashboard/api-keys-usage", script)

    def test_rejects_unsupported_or_logged_out_sites(self):
        with self.assertRaisesRegex(relay.core.ManagerError, "没有识别到"):
            relay.normalize_probe_result(
                {"adapter": "", "message": "没有识别到登录会话。"},
                portal_url="https://relay.example.test",
            )

    def test_public_state_omits_internal_clock_and_secrets(self):
        service = relay.RelayPortalService()
        service._session = {
            **service._empty_state(),
            "sessionId": "session",
            "_createdMonotonic": time.monotonic(),
        }
        service._secrets = {"k-1": "sk-secret-value"}

        public = service.public_state()

        self.assertNotIn("_createdMonotonic", public)
        self.assertNotIn("sk-secret-value", json.dumps(public))

    def test_selected_key_uses_existing_encrypted_provider_import_path(self):
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(
            get_current_url=lambda: "https://relay.example.test/keys"
        )
        service._session = {
            **service._empty_state(),
            "sessionId": "session-1",
            "portalUrl": "https://relay.example.test/keys",
            "_createdMonotonic": time.monotonic(),
        }
        service._preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/keys",
            "baseUrl": "https://relay.example.test/v1",
            "modelsEndpoint": "https://relay.example.test/v1/models",
            "balanceEndpoint": "https://relay.example.test/v1/usage",
            "integrationKind": "sub2api",
            "models": ["gpt-test"],
            "keys": [
                {
                    "id": "k-1",
                    "name": "codex",
                    "models": [],
                    "canImport": True,
                }
            ],
        }
        service._secrets = {"k-1": "sk-selected-secret"}

        with patch.object(
            relay.core,
            "import_relay_account",
            return_value={
                "account": {"id": "relay-test"},
                "provider": {"id": "provider-test"},
                "configuredKeyCount": 1,
            },
        ) as imported:
            result = service.import_keys(
                "session-1",
                {"keyIds": ["k-1"], "groupId": "relay", "proxyEnabled": True},
            )

        self.assertEqual(result["count"], 1)
        preview, secrets = imported.call_args.args
        self.assertEqual(secrets["k-1"], "sk-selected-secret")
        self.assertEqual(preview["baseUrl"], "https://relay.example.test/v1")
        self.assertTrue(imported.call_args.kwargs["proxy_enabled"])

    def test_import_persists_dashboard_session_and_releases_login_window(self):
        destroyed = []
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(
            get_current_url=lambda: "https://relay.example.test/keys",
            destroy=lambda: destroyed.append(True),
        )
        service._session = {
            **service._empty_state(),
            "sessionId": "session-persist",
            "portalUrl": "https://relay.example.test/keys",
            "_createdMonotonic": time.monotonic(),
        }
        service._preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/keys",
            "origin": "https://relay.example.test",
            "baseUrl": "https://relay.example.test/v1",
            "modelsEndpoint": "https://relay.example.test/v1/models",
            "balanceEndpoint": "https://relay.example.test/v1/usage",
            "integrationKind": "sub2api",
            "models": ["gpt-test"],
            "groups": [{"id": "7", "name": "Codex", "platform": "openai", "active": True}],
            "keys": [{"id": "k-1", "name": "codex", "groupPlatform": "openai", "canImport": True}],
        }
        service._secrets = {"k-1": "sk-selected-secret"}
        service._dashboard_session = {
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/keys",
            "adapter": "sub2api",
            "authMode": "bearer",
            "accessToken": "saved-access",
            "refreshToken": "saved-refresh",
            "cookies": [],
        }

        with (
            patch.object(
                relay.core,
                "import_relay_account",
                return_value={"account": {"id": "relay-test"}, "provider": {"id": "provider-test"}, "configuredKeyCount": 1},
            ),
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
        ):
            result = service.import_keys(
                "session-persist",
                {"keyIds": ["k-1"], "groupId": "relay"},
            )

        stored.assert_called_once_with("relay-test", service._dashboard_session)
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(service.window)
        self.assertEqual(destroyed, [True])

    def test_import_writes_only_checked_codex_keys(self):
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(
            get_current_url=lambda: "https://relay.example.test/keys"
        )
        service._session = {
            **service._empty_state(),
            "sessionId": "session-selective",
            "portalUrl": "https://relay.example.test/keys",
            "_createdMonotonic": time.monotonic(),
        }
        service._preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/keys",
            "baseUrl": "https://relay.example.test/v1",
            "models": ["gpt-test", "claude-test"],
            "groups": [
                {"id": "1", "name": "Codex", "platform": "openai", "active": True},
                {"id": "2", "name": "Claude", "platform": "anthropic", "active": True},
            ],
            "keys": [
                {"id": "k-1", "name": "one", "group": "Codex", "groupPlatform": "openai", "canImport": True},
                {"id": "k-2", "name": "two", "group": "Codex", "groupPlatform": "openai", "canImport": True},
                {"id": "k-mixed", "name": "mixed", "group": "通用", "groupPlatform": "", "models": ["gpt-test", "claude-test"], "canImport": True},
                {"id": "k-3", "name": "claude", "group": "Claude", "groupPlatform": "anthropic", "canImport": True},
            ],
        }
        service._secrets = {
            "k-1": "sk-one",
            "k-2": "sk-two",
            "k-mixed": "sk-mixed",
            "k-3": "sk-claude",
        }

        with patch.object(
            relay.core,
            "import_relay_account",
            return_value={
                "account": {"id": "relay-test"},
                "provider": {"id": "provider-test"},
                "configuredKeyCount": 1,
            },
        ) as imported:
            result = service.import_keys(
                "session-selective",
                {"keyIds": ["k-1", "k-mixed"], "groupId": "relay"},
            )

        preview, secrets = imported.call_args.args
        self.assertEqual(set(secrets), {"k-1", "k-mixed"})
        self.assertEqual([item["id"] for item in preview["keys"]], ["k-1", "k-mixed"])
        self.assertEqual(preview["keys"][1]["models"], ["gpt-test"])
        self.assertEqual([item["id"] for item in preview["groups"]], ["1"])
        self.assertEqual(result["count"], 2)

    def test_selected_endpoint_is_used_for_import_and_keeps_dashboard_balance(self):
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(
            get_current_url=lambda: "https://hajimi.chat/keys"
        )
        service._session = {
            **service._empty_state(),
            "sessionId": "session-endpoint",
            "portalUrl": "https://hajimi.chat/keys",
            "_createdMonotonic": time.monotonic(),
        }
        service._preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "哈基米",
            "portalUrl": "https://hajimi.chat/keys",
            "baseUrl": "https://api.hajimi.chat/v1",
            "modelsEndpoint": "https://api.hajimi.chat/v1/models",
            "balanceEndpoint": "https://api.hajimi.chat/v1/usage",
            "integrationKind": "sub2api",
            "defaultEndpointId": "default",
            "apiEndpoints": [
                {
                    "id": "default",
                    "name": "默认 API",
                    "baseUrl": "https://api.hajimi.chat/v1",
                    "modelsEndpoint": "https://api.hajimi.chat/v1/models",
                    "balanceEndpoint": "https://api.hajimi.chat/v1/usage",
                    "isDefault": True,
                },
                {
                    "id": "endpoint-2",
                    "name": "生图专用",
                    "baseUrl": "https://image.hajimi.chat/v1",
                    "modelsEndpoint": "https://image.hajimi.chat/v1/models",
                    "balanceEndpoint": "https://image.hajimi.chat/v1/usage",
                    "isDefault": False,
                },
            ],
            "balance": {"remaining": 13.22, "used": 29.4803, "currency": "USD"},
            "detectedAt": "2026-09-01T00:00:00Z",
            "models": ["gpt-test"],
            "keys": [
                {
                    "id": "k-1",
                    "name": "codex",
                    "group": "特惠",
                    "models": [],
                    "canImport": True,
                }
            ],
        }
        service._secrets = {"k-1": "sk-selected-secret"}

        with patch.object(
            relay.core,
            "import_relay_account",
            return_value={
                "account": {"id": "relay-test"},
                "provider": {"id": "provider-test"},
                "configuredKeyCount": 1,
            },
        ) as imported:
            service.import_keys(
                "session-endpoint",
                {
                    "keyIds": ["k-1"],
                    "endpointId": "endpoint-2",
                    "groupId": "relay",
                },
            )

        preview, secrets = imported.call_args.args
        self.assertEqual(secrets["k-1"], "sk-selected-secret")
        self.assertEqual(imported.call_args.kwargs["endpoint_id"], "endpoint-2")
        self.assertEqual(preview["balance"]["remaining"], 13.22)
        self.assertEqual(
            [item["baseUrl"] for item in preview["apiEndpoints"]],
            ["https://api.hajimi.chat/v1"],
        )

    def test_new_api_create_rescans_when_success_response_has_no_data(self):
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(
            get_current_url=lambda: "https://relay.example.test/tokens"
        )
        service._session = {
            **service._empty_state(),
            "sessionId": "session-2",
            "portalUrl": "https://relay.example.test/tokens",
            "_createdMonotonic": time.monotonic(),
        }
        initial_preview = {
            "adapter": "new-api",
            "adapterLabel": "New API / One API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/tokens",
            "baseUrl": "https://relay.example.test/v1",
            "modelsEndpoint": "https://relay.example.test/v1/models",
            "balanceEndpoint": "https://relay.example.test/api/usage/token",
            "integrationKind": "",
            "models": ["gpt-test"],
            "keys": [{"id": "1", "name": "existing", "models": []}],
        }
        created_record = {
            "id": "2",
            "name": "Agent Manager",
            "models": [],
            "canImport": True,
            "requiresReveal": True,
        }
        refreshed_preview = {**initial_preview, "keys": [*initial_preview["keys"], created_record]}
        service._preview = initial_preview

        def rescan(_session_id):
            service._preview = refreshed_preview
            service._secrets["2"] = "sk-created-secret"
            return {"status": "ready", "preview": refreshed_preview}

        with (
            patch.object(
                service,
                "_evaluate",
                return_value={"status": 200, "body": {"success": True, "message": ""}},
            ),
            patch.object(service, "scan", side_effect=rescan) as scanned,
        ):
            result = service.create_key(
                "session-2",
                {"name": "Agent Manager", "groupId": "relay"},
            )

        self.assertEqual(result["created"]["id"], "2")
        self.assertGreaterEqual(scanned.call_count, 1)

    def test_sub2api_create_uses_selected_relay_group(self):
        service = relay.RelayPortalService()
        service.window = SimpleNamespace(get_current_url=lambda: "https://hajimi.chat/keys")
        service._session = {
            **service._empty_state(),
            "sessionId": "session-sub-create",
            "portalUrl": "https://hajimi.chat/keys",
            "_createdMonotonic": time.monotonic(),
        }
        service._preview = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "哈基米",
            "portalUrl": "https://hajimi.chat/keys",
            "baseUrl": "https://api.hajimi.chat/v1",
            "modelsEndpoint": "https://api.hajimi.chat/v1/models",
            "balanceEndpoint": "https://api.hajimi.chat/v1/usage",
            "integrationKind": "sub2api",
            "balance": {"remaining": 13.22, "used": 0, "currency": "USD"},
            "models": ["gpt-test"],
            "groups": [
                {"id": "7", "name": "特惠", "active": True, "rateMultiplier": 0.06}
            ],
            "keys": [],
        }

        with (
            patch.object(
                service,
                "_evaluate",
                return_value=response(
                    {
                        "id": 91,
                        "name": "Agent Manager",
                        "key": "sk-created-sub2api-0123456789",
                        "group_id": 7,
                        "status": "active",
                    }
                ),
            ) as evaluated,
            patch.object(service, "scan", return_value={"status": "ready"}),
        ):
            result = service.create_key(
                "session-sub-create",
                {
                    "name": "Agent Manager",
                    "relayGroupId": "7",
                    "groupId": "relay",
                },
            )

        script = evaluated.call_args.args[1]
        self.assertIn("const RELAY_GROUP_ID = 7", script)
        self.assertIn("group_id: RELAY_GROUP_ID", script)
        self.assertEqual(result["created"]["groupId"], "7")

    def test_saved_session_create_imports_only_the_new_key(self):
        service = relay.RelayPortalService()
        account = {
            "id": "relay-test",
            "providerId": "provider-test",
            "groupId": "relay",
            "selectedKeyId": "k-old",
            "selectedEndpointId": "default",
        }
        initial = {
            "adapter": "sub2api",
            "adapterLabel": "Sub2API",
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/keys",
            "origin": "https://relay.example.test",
            "baseUrl": "https://relay.example.test/v1",
            "modelsEndpoint": "https://relay.example.test/v1/models",
            "balanceEndpoint": "https://relay.example.test/v1/usage",
            "integrationKind": "sub2api",
            "models": ["gpt-test"],
            "apiEndpoints": [{"id": "default", "name": "默认", "baseUrl": "https://relay.example.test/v1", "modelsEndpoint": "https://relay.example.test/v1/models", "balanceEndpoint": "https://relay.example.test/v1/usage", "isDefault": True}],
            "groups": [{"id": "7", "name": "特惠", "platform": "openai", "active": True, "rateMultiplier": 0.06}],
            "keys": [{"id": "k-old", "name": "old", "groupPlatform": "openai", "canImport": True}],
        }
        new_record = {
            "id": "k-new",
            "name": "Codex 备用",
            "group": "特惠",
            "groupId": "7",
            "groupPlatform": "openai",
            "canImport": True,
        }
        refreshed = {**initial, "keys": [*initial["keys"], new_record]}
        session = {
            "origin": "https://relay.example.test",
            "portalUrl": "https://relay.example.test/keys",
            "adapter": "sub2api",
            "authMode": "bearer",
            "accessToken": "access",
            "refreshToken": "refresh",
            "cookies": [],
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {"web2api": {"providerIds": ["provider-test"]}})),
            patch.object(
                service,
                "_scan_saved_account",
                side_effect=[(initial, {}, session), (refreshed, {"k-new": "sk-new-secret"}, session)],
            ),
            patch.object(relay, "_saved_json_request", return_value=response({"id": "k-new", "name": "Codex 备用"})),
            patch.object(
                relay.core,
                "import_relay_account",
                return_value={"account": {**account, "keys": [new_record]}, "provider": {"id": "provider-test"}},
            ) as imported,
            patch.object(relay.core, "store_relay_account_dashboard_session") as stored,
        ):
            result = service.create_saved_key(
                "relay-test",
                {"name": "Codex 备用", "relayGroupId": "7"},
            )

        selected_preview, selected_secrets = imported.call_args.args
        self.assertEqual([item["id"] for item in selected_preview["keys"]], ["k-new"])
        self.assertEqual(selected_secrets, {"k-new": "sk-new-secret"})
        self.assertTrue(imported.call_args.kwargs["proxy_enabled"])
        self.assertEqual(imported.call_args.kwargs["selected_key_id"], "k-old")
        stored.assert_called_once_with("relay-test", session)
        self.assertEqual(result["created"]["id"], "k-new")

    def test_saved_sub2api_group_update_changes_remote_before_local_metadata(self):
        service = relay.RelayPortalService()
        account = {"id": "relay-test"}
        preview = {
            "adapter": "sub2api",
            "keys": [
                {
                    "id": "17",
                    "name": "Codex",
                    "groupId": "3",
                    "groupPlatform": "openai",
                }
            ],
            "groups": [
                {
                    "id": "7",
                    "name": "GPT Plus",
                    "platform": "openai",
                    "active": True,
                }
            ],
        }
        session = {
            "origin": "https://relay.example.test",
            "adapter": "sub2api",
            "authMode": "bearer",
            "accessToken": "access",
            "cookies": [],
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(preview, {}, session)),
            patch.object(relay, "_saved_json_request", return_value=response({"id": 17, "group_id": 7})) as requested,
            patch.object(
                relay.core,
                "update_relay_account_key_group",
                return_value={"account": {"id": "relay-test"}, "requiresReapply": False},
            ) as updated,
        ):
            result = service.update_saved_key_group("relay-test", "17", "7")

        self.assertTrue(result["remoteUpdated"])
        requested.assert_called_once_with(
            session,
            "/api/v1/keys/17",
            method="PUT",
            headers={
                "X-User-UI-Request": "1",
                "Authorization": "Bearer access",
                "Content-Type": "application/json",
            },
            payload={"group_id": 7},
        )
        updated.assert_called_once_with("relay-test", "17", "7")

    def test_saved_group_update_does_not_change_local_metadata_when_site_rejects_it(self):
        service = relay.RelayPortalService()
        account = {"id": "relay-test"}
        preview = {
            "adapter": "sub2api",
            "keys": [{"id": "17", "groupId": "3", "groupPlatform": "openai"}],
            "groups": [{"id": "7", "name": "GPT Plus", "platform": "openai", "active": True}],
        }
        session = {
            "origin": "https://relay.example.test",
            "adapter": "sub2api",
            "authMode": "bearer",
            "accessToken": "access",
            "cookies": [],
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(service, "_scan_saved_account", return_value=(preview, {}, session)),
            patch.object(
                relay,
                "_saved_json_request",
                return_value={"status": 403, "body": {"message": "group denied"}},
            ),
            patch.object(relay.core, "update_relay_account_key_group") as updated,
        ):
            with self.assertRaisesRegex(relay.core.ManagerError, "group denied"):
                service.update_saved_key_group("relay-test", "17", "7")

        updated.assert_not_called()

    def test_refresh_account_requests_relogin_when_saved_dashboard_session_is_missing(self):
        service = relay.RelayPortalService()
        account = {
            "id": "relay-needs-login",
            "portalUrl": "https://relay.example.test/dashboard",
            "origin": "https://relay.example.test",
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(
                service,
                "_scan_saved_account",
                side_effect=relay.core.ManagerError("没有已保存的网页登录凭据"),
            ),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={"account": account, "warnings": [], "liveDashboard": False},
            ),
        ):
            result = service.refresh_account(account["id"])

        self.assertTrue(result["requiresLogin"])
        self.assertIn("网页登录续期失败", result["warnings"][0])

    def test_refresh_account_keeps_windowless_flow_when_saved_session_is_valid(self):
        service = relay.RelayPortalService()
        account = {"id": "relay-saved-login"}
        preview = {
            "siteName": "Relay",
            "portalUrl": "https://relay.example.test/dashboard",
            "origin": "https://relay.example.test",
            "baseUrl": "https://relay.example.test/v1",
            "models": ["gpt-test"],
            "keys": [],
            "groups": [],
        }
        with (
            patch.object(service, "_saved_account", return_value=(account, {})),
            patch.object(
                service,
                "_scan_saved_account",
                return_value=(preview, {}, {"origin": "https://relay.example.test"}),
            ),
            patch.object(
                relay.core,
                "sync_relay_account_snapshot",
                return_value={"account": account, "liveDashboard": True},
            ),
            patch.object(
                relay.core,
                "refresh_relay_account",
                return_value={
                    "account": {**account, "models": ["gpt-test"]},
                    "warnings": [],
                    "modelRefresh": {"status": "ready", "models": ["gpt-test"]},
                },
            ) as refresh_models,
            patch.object(
                service,
                "_record_stale_balance",
                side_effect=lambda _id, _previous, error: {
                    **account, "models": ["gpt-test"], "balance": None,
                    "balanceFreshness": "unavailable", "balanceError": error,
                    "balanceUpdatedAt": None,
                },
            ),
        ):
            result = service.refresh_account(account["id"])

        self.assertNotIn("requiresLogin", result)
        self.assertTrue(any("未返回可验证的账号余额" in item for item in result["warnings"]))
        self.assertEqual(result["balanceRefresh"]["freshness"], "unavailable")
        self.assertIsNone(result["balanceRefresh"]["updatedAt"])
        self.assertEqual(result["account"]["models"], ["gpt-test"])
        refresh_models.assert_called_once_with(
            account["id"],
            refresh_balance=False,
            fallback_notice=False,
        )


class _FakeRelayPortal:
    def __init__(self):
        self.calls = []

    def public_state(self):
        return {"status": "idle"}

    def start(self, url):
        self.calls.append(("start", url))
        return {"status": "waiting_login", "sessionId": "s-1"}

    def scan(self, session_id):
        self.calls.append(("scan", session_id))
        return {"status": "ready", "sessionId": session_id}

    def import_keys(self, session_id, payload):
        self.calls.append(("import", session_id, payload["keyIds"]))
        return {"count": len(payload["keyIds"]), "imported": [], "failed": []}

    def create_key(self, session_id, payload):
        self.calls.append(("create", session_id, payload["name"]))
        return {"created": {"name": payload["name"]}}

    def refresh_account(self, account_id, *, full=False):
        self.calls.append(("refresh", account_id, full))
        return {"account": {"id": account_id}, "counts": {"keys": 1, "groups": 1}}

    def delete_saved_key(self, account_id, key_id, *, delete_remote=False):
        self.calls.append(("delete-key",account_id,key_id,delete_remote))
        return app.core.remove_relay_account_key(account_id,key_id)

    def create_saved_key(self, account_id, payload):
        self.calls.append(("create-saved", account_id, payload["name"], payload.get("relayGroupId")))
        return {"created": {"id": "k-new", "name": payload["name"]}}

    def update_saved_key_group(self, account_id, key_id, group_id):
        self.calls.append(("update-saved-group", account_id, key_id, group_id))
        return {"account": {"id": account_id}, "remoteUpdated": True}

    def close(self):
        self.calls.append(("close",))
        return {"status": "cancelled"}


class RelayRouteTests(unittest.TestCase):
    def setUp(self):
        self.portal = _FakeRelayPortal()
        runtime = SimpleNamespace(relay_portal=self.portal, web2api=SimpleNamespace(lock=threading.RLock()))
        backup = patch.object(app.recovery, "create", return_value={"id": "relay-delete-backup", "scope": "configuration"})
        backup.start()
        self.addCleanup(backup.stop)
        self.server = app.ManagerServer(("127.0.0.1", 0), app.RequestHandler, runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method="GET", payload=None):
        data = None if method == "GET" else json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Manager-Token": self.server.api_token,
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response_object:
            return json.loads(response_object.read().decode("utf-8"))

    def test_relay_login_routes_cover_complete_explicit_flow(self):
        self.assertEqual(self.request("/api/relay-login/status")["status"]["status"], "idle")
        started = self.request(
            "/api/relay-login/start",
            method="POST",
            payload={"url": "https://relay.example.test"},
        )
        self.assertEqual(started["status"]["sessionId"], "s-1")
        self.assertEqual(
            self.request(
                "/api/relay-login/scan",
                method="POST",
                payload={"sessionId": "s-1"},
            )["status"]["status"],
            "ready",
        )
        self.assertEqual(
            self.request(
                "/api/relay-login/import",
                method="POST",
                payload={"sessionId": "s-1", "keyIds": ["k-1"]},
            )["result"]["count"],
            1,
        )
        self.assertEqual(
            self.request(
                "/api/relay-login/create",
                method="POST",
                payload={"sessionId": "s-1", "name": "Agent Manager"},
            )["result"]["created"]["name"],
            "Agent Manager",
        )
        self.assertEqual(
            self.request("/api/relay-login/cancel", method="POST")["status"]["status"],
            "cancelled",
        )
        self.assertEqual(
            self.portal.calls,
            [
                ("start", "https://relay.example.test"),
                ("scan", "s-1"),
                ("import", "s-1", ["k-1"]),
                ("create", "s-1", "Agent Manager"),
                ("close",),
            ],
        )

    def test_relay_account_selection_route_updates_one_account_provider(self):
        with patch.object(
            app.core,
            "update_relay_account_selection",
            return_value={"account": {"id": "relay-1", "selectedKeyId": "k-2"}},
        ) as updated:
            result = self.request(
                "/api/relay-accounts/relay-1/selection",
                method="POST",
                payload={"keyId": "k-2", "endpointId": "fast"},
            )
        self.assertEqual(result["result"]["account"]["selectedKeyId"], "k-2")
        updated.assert_called_once_with(
            "relay-1",
            {"keyId": "k-2", "endpointId": "fast"},
        )

    def test_relay_account_refresh_group_and_key_delete_routes(self):
        refreshed = self.request(
            "/api/relay-accounts/relay-1/refresh",
            method="POST",
        )
        self.assertEqual(refreshed["result"]["counts"]["keys"], 1)
        self.assertIn(("refresh","relay-1",True),self.portal.calls)
        with patch.object(
            app.core,
            "move_relay_account_group",
            return_value={"account": {"id": "relay-1", "groupId": "official"}},
        ) as moved:
            result = self.request(
                "/api/relay-accounts/relay-1/group",
                method="POST",
                payload={"groupId": "official"},
            )
        self.assertEqual(result["result"]["account"]["groupId"], "official")
        moved.assert_called_once_with("relay-1", "official")
        self.request(
            "/api/relay-accounts/relay-1/keys/k-1/group",
            method="POST",
            payload={"groupId": "7"},
        )
        self.assertIn(("update-saved-group", "relay-1", "k-1", "7"), self.portal.calls)
        with patch.object(
            app.core,
            "remove_relay_account_key",
            return_value={"removedKeyId": "k-1"},
        ) as removed:
            deleted = self.request(
                "/api/relay-accounts/relay-1/keys/k-1",
                method="DELETE",
            )
        self.assertEqual(deleted["backup"]["id"], "relay-delete-backup")
        removed.assert_called_once_with("relay-1", "k-1")

    def test_relay_account_export_routes_keep_account_level_format(self):
        exported_payload = {
            "format": "codex-agent-manager-relay-account",
            "version": 1,
        }
        with patch.object(
            app.core,
            "export_relay_account",
            return_value=exported_payload,
        ) as exported:
            result = self.request("/api/relay-accounts/relay-1/export")
        self.assertEqual(result["export"]["format"], "codex-agent-manager-relay-account")
        exported.assert_called_once_with("relay-1")

        with patch.object(
            app.core,
            "export_relay_account_to_downloads",
            return_value={"fileName": "relay.json", "path": "C:/Downloads/relay.json"},
        ) as downloaded:
            result = self.request(
                "/api/relay-accounts/relay-1/export-download",
                method="POST",
            )
        self.assertEqual(result["result"]["fileName"], "relay.json")
        downloaded.assert_called_once_with("relay-1")

    def test_relay_account_metadata_and_windowless_create_routes(self):
        with patch.object(
            app.core,
            "update_relay_account_metadata",
            return_value={"account": {"id": "relay-1", "portalUrl": "https://relay.example.test/keys"}},
        ) as updated:
            result = self.request(
                "/api/relay-accounts/relay-1/metadata",
                method="POST",
                payload={"portalUrl": "https://relay.example.test/keys"},
            )
        self.assertEqual(result["result"]["account"]["portalUrl"], "https://relay.example.test/keys")
        updated.assert_called_once_with("relay-1", {"portalUrl": "https://relay.example.test/keys"})

        created = self.request(
            "/api/relay-accounts/relay-1/keys",
            method="POST",
            payload={"name": "Codex 备用", "relayGroupId": "7"},
        )
        self.assertEqual(created["result"]["created"]["id"], "k-new")
        self.assertIn(("create-saved", "relay-1", "Codex 备用", "7"), self.portal.calls)


if __name__ == "__main__":
    unittest.main()
