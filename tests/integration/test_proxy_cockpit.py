from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import call, patch

import agent_manager.core as core
import agent_manager.gateway.service as web2api


def sse(*events):
    return b"".join(
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
        for event in events
    )


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace("_", "-")] = str(value)
    return result


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", *, response_headers=None, status=200):
        super().__init__(body)
        self.headers = response_headers or headers(Content_Type="text/event-stream")
        self.status = status
        self.timeout = None
        self.was_closed = False

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.was_closed = True
        super().close()


def http_error(status, body, response_headers=None):
    return web2api.HTTPError(
        web2api.UPSTREAM_RESPONSES_URL,
        status,
        "upstream failure",
        response_headers or headers(Content_Type="application/json"),
        io.BytesIO(body),
    )


class ProxyCockpitV96Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(core, "STATE_DIR", Path(self.temp.name) / "state")
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def account(account_id, remaining=50, credits=None):
        usage = {"weekly": {"remainingPercent": remaining}}
        if credits is not None:
            usage["credits"] = credits
        return {
            "id": account_id,
            "label": account_id,
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "codexCompatible": True,
            "quotaOnly": False,
            "models": ["gpt-test"],
            "usage": usage,
        }

    @classmethod
    def settings(cls, *accounts):
        records = list(accounts) or [cls.account("first"), cls.account("second")]
        return {
            "accounts": records,
            "web2api": {
                "accountIds": [item["id"] for item in records],
                "routing": "ordered",
                "activeForCodex": False,
                "activeAccountId": None,
            },
        }

    def test_capacity_classifier_uses_only_explicit_error_fields(self):
        accepted = [
            {"error": {"code": "server_is_overloaded", "message": "busy"}},
            {"response": {"error": {"code": "slow_down"}}},
            {"error": {"type": "invalid_request_error", "message": "Selected model is at capacity"}},
        ]
        rejected = [
            {"error": {"type": "usage_limit_reached", "message": "Selected model is at capacity"}},
            {"error": {"code": "rate_limit_exceeded", "message": "Selected model is at capacity"}},
            {"error": {"code": "cyber_policy", "message": "Selected model is at capacity"}},
            {"error": {"type": "authentication_error", "message": "Selected model is at capacity"}},
            {"message": "Selected model is at capacity"},
            {"echo": "Selected model is at capacity"},
        ]
        self.assertTrue(all(web2api._capacity_error_object(item) for item in accepted))
        self.assertTrue(all(web2api._capacity_error_object(item) is None for item in rejected))

    def test_preoutput_capacity_retries_once_and_exposes_only_success(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        failed = FakeResponse(
            sse(
                {"type": "response.created", "response": {"id": "discarded"}},
                {
                    "type": "response.failed",
                    "response": {
                        "status": "failed",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Selected model is at capacity. Please retry.",
                        },
                    },
                },
            )
        )
        succeeded = FakeResponse(
            sse(
                {"type": "response.created", "response": {"id": "kept"}},
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.completed", "response": {"id": "kept", "status": "completed"}},
            )
        )
        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(manager, "_open_upstream", side_effect=[(failed, account, {}), (succeeded, self.account("second"), {})]) as opened,
            patch.object(manager, "_record_usage"),
        ):
            result = manager.stream("/v1/responses", {"model": "gpt-test", "input": "hi", "stream": True})
            body = b"".join(result["chunks"])
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(failed.was_closed)
        self.assertIn(b"hello", body)
        self.assertIn(b"kept", body)
        self.assertNotIn(b"discarded", body)
        self.assertNotIn(b"at capacity", body)
        self.assertTrue(succeeded.was_closed)
        cooldown = manager.account_cooldowns[("first", "gpt-test")]
        self.assertEqual(cooldown["reason"], "upstream_capacity")
        self.assertEqual(cooldown["delaySeconds"], web2api.UPSTREAM_COOLDOWN_DEFAULT_SECONDS)
        self.assertGreater(manager._account_cooldown_remaining("first", "gpt-test"), 0)
        self.assertNotIn(("second", "gpt-test"), manager.account_cooldowns)

    def test_capacity_after_semantic_output_is_never_replayed(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        response = FakeResponse(
            sse(
                {"type": "response.created", "response": {"id": "partial"}},
                {"type": "response.output_text.delta", "delta": "already emitted"},
                {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": {"code": "server_is_overloaded", "message": "busy"}},
                },
            )
        )
        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(manager, "_open_upstream", return_value=(response, account, {})) as opened,
            patch.object(manager, "_record_usage"),
        ):
            result = manager.stream("/v1/responses", {"model": "gpt-test", "input": "hi", "stream": True})
            body = b"".join(result["chunks"])
        opened.assert_called_once()
        self.assertIn(b"already emitted", body)
        self.assertIn(b"server_is_overloaded", body)
        self.assertFalse(manager.account_cooldowns)

    def test_capacity_retry_budget_returns_503_with_distinct_capacity_cooldown(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        capacity = sse(
            {"type": "response.created", "response": {"id": "discard"}},
            {"type": "response.failed", "response": {"error": {"code": "slow_down", "message": "busy"}}},
        )
        responses = [FakeResponse(capacity), FakeResponse(capacity)]
        with patch.object(manager, "_open_upstream", side_effect=[
            (item, self.account(identity), {}) for item, identity in zip(responses, ("first", "second"))
        ]) as opened:
            with self.assertRaises(web2api.GatewayError) as raised:
                manager._open_upstream_with_capacity_retry({"model": "gpt-test", "input": "hi"})
        self.assertEqual(opened.call_count, web2api.UPSTREAM_CAPACITY_MAX_ATTEMPTS)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(json.loads(raised.exception.body)["error"]["code"], "server_error")
        self.assertTrue(all(item.was_closed for item in responses))
        self.assertEqual(set(manager.account_cooldowns), {("first", "gpt-test"), ("second", "gpt-test")})
        self.assertTrue(all(item["reason"] == "upstream_capacity" for item in manager.account_cooldowns.values()))
        self.assertTrue(all(item["source"] == "remote" and not item["recoverable"] for item in manager.account_cooldowns.values()))

    def test_capacity_503_does_not_invalidate_a_verified_response_binding(self):
        manager = web2api.Web2APIManager()
        manager._remember_response_binding(
            "resp-bound",
            access_scope="public",
            requested_model="gpt-test",
            routed_model="gpt-test",
            identity_kind="account",
            identity_id="first",
        )
        binding = manager._lookup_response_binding("resp-bound")
        error = web2api._capacity_gateway_error(
            {"code": "server_is_overloaded", "message": "busy"},
            {},
            self.account("first"),
        )
        result = manager._bound_identity_error(binding, "resp-bound", error)
        self.assertIs(result, error)
        self.assertIsNotNone(manager._lookup_response_binding("resp-bound"))

    def test_http_capacity_is_not_quota_and_is_bounded(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        body = b'{"error":{"type":"invalid_request_error","message":"Selected model is at capacity"}}'
        attempts = [http_error(429, body), http_error(503, body)]
        with (
            patch.object(core, "load_settings", return_value=self.settings(account, self.account("second"))),
            patch.object(core, "_account_chatgpt_credentials", side_effect=lambda identity: {
                "accessToken": "token-" + identity, "accountId": "workspace-" + identity}) as credentials,
            patch.object(core, "_open_same_origin_request", side_effect=attempts) as opened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager._open_upstream_with_capacity_retry({"model": "gpt-test", "input": "hi"})
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(credentials.call_args_list, [call("first"), call("second")])
        self.assertEqual(set(manager.account_cooldowns), {("first", "gpt-test"), ("second", "gpt-test")})
        self.assertTrue(all(item["reason"] == "upstream_capacity" for item in manager.account_cooldowns.values()))
        self.assertFalse(manager.scheduler.inflight)

    def test_positive_or_unlimited_credits_do_not_bypass_remote_wait(self):
        for credits in ({"hasCredits": True, "balance": "3.5"}, {"unlimited": True, "balance": "0"}):
            with self.subTest(credits=credits):
                manager = web2api.Web2APIManager()
                account = self.account("first", remaining=0, credits=credits)
                now = time.monotonic()
                record = {"until": now + 60, "reason": "upstream_quota_exhausted",
                          "source": "remote", "recoverable": False, "advertisedRetryAfter": True}
                manager.account_cooldowns[("first", "gpt-test")] = record
                with patch.object(core, "load_settings", return_value=self.settings(account)):
                    with self.assertRaises(web2api.GatewayError) as raised:
                        manager._accounts(requested_model="gpt-test")
                    self.assertEqual(raised.exception.status, 429)
                    self.assertGreaterEqual(int(raised.exception.headers["Retry-After"]), 59)
                    self.assertIs(manager.account_cooldowns[("first", "gpt-test")], record)
                    with patch.object(web2api.time, "monotonic", return_value=now + 61):
                        selected = manager._accounts(requested_model="gpt-test")
                self.assertEqual([item["id"] for item in selected], ["first"])
                self.assertNotIn(("first", "gpt-test"), manager.account_cooldowns)
                self.assertGreater(web2api._quota_score(account), 0)

    def test_confirmed_quota_without_credits_keeps_remote_cooldown(self):
        manager = web2api.Web2APIManager()
        account = self.account("first", remaining=0, credits={"hasCredits": False, "unlimited": False, "balance": "0"})
        manager.account_cooldowns[("first", "gpt-test")] = {
            "until": time.monotonic() + 60,
            "reason": "upstream_quota_exhausted",
            "source": "remote",
            "recoverable": False,
            "advertisedRetryAfter": True,
        }
        with patch.object(core, "load_settings", return_value=self.settings(account)):
            with self.assertRaises(web2api.GatewayError) as raised:
                manager._accounts(requested_model="gpt-test")
        self.assertEqual(raised.exception.status, 429)
        self.assertIn(("first", "gpt-test"), manager.account_cooldowns)

    def test_usable_credit_headers_honor_remote_retry_after_when_advertised(self):
        for retry_after in (None, "30"):
            with self.subTest(retry_after=retry_after):
                manager = web2api.Web2APIManager()
                account = self.account("first", remaining=0)
                error_headers = headers(Content_Type="application/json",
                                        X_Codex_Credits_Has_Credits="true", X_Codex_Credits_Balance="5")
                if retry_after is not None:
                    error_headers["Retry-After"] = retry_after
                error = http_error(429, b'{"error":{"type":"usage_limit_reached"}}', error_headers)
                with (
                    patch.object(manager, "_accounts", return_value=[account]),
                    patch.object(core, "_account_chatgpt_credentials", return_value={"accessToken": "token", "accountId": "workspace"}),
                    patch.object(core, "_open_same_origin_request", side_effect=error),
                    self.assertRaises(web2api.GatewayError) as raised,
                ):
                    manager._open_upstream({"model": "gpt-test", "input": "hi"})
                self.assertEqual(raised.exception.status, 429)
                if retry_after is None:
                    self.assertFalse(manager.account_cooldowns)
                else:
                    record = manager.account_cooldowns[("first", "gpt-test")]
                    self.assertEqual(record["reason"], "upstream_quota_exhausted")
                    self.assertEqual(record["delaySeconds"], 30)
                    self.assertTrue(record["advertisedRetryAfter"])
                    self.assertEqual(record["source"], "remote")
                self.assertFalse(manager.scheduler.inflight)

    def test_only_explicit_local_recoverable_cooldowns_are_auto_recovered(self):
        accounts = [self.account("first"), self.account("second")]
        manager = web2api.Web2APIManager()
        now = time.monotonic()
        manager.account_cooldowns = {
            (item["id"], "gpt-test"): {
                "until": now + 60,
                "reason": "local_scheduler_unavailable",
                "source": "local",
                "recoverable": True,
                "advertisedRetryAfter": False,
            }
            for item in accounts
        }
        with patch.object(core, "load_settings", return_value=self.settings(*accounts)):
            selected = manager._accounts(requested_model="gpt-test")
        self.assertEqual([item["id"] for item in selected], ["first", "second"])
        self.assertFalse(manager.account_cooldowns)

        manager.account_cooldowns[("first", "gpt-test")] = {
            "until": now + 60,
            "reason": "upstream_rate_limit",
            "source": "remote",
            "recoverable": False,
            "advertisedRetryAfter": True,
        }
        with patch.object(core, "load_settings", return_value=self.settings(accounts[0])):
            with self.assertRaises(web2api.GatewayError):
                manager._accounts(requested_model="gpt-test")
        self.assertIn(("first", "gpt-test"), manager.account_cooldowns)

    def test_runtime_state_cleanup_is_account_scoped(self):
        manager = web2api.Web2APIManager()
        manager.account_cooldowns = {
            ("first", "gpt-test"): {"until": 100},
            ("second", "gpt-test"): {"until": 100},
        }
        manager.account_last_used = {"first": 1.0, "second": 2.0}
        manager.last_account = {"id": "first"}
        manager.last_quota = {"remainingPercent": 1}
        manager._remember_response_binding(
            "resp-first", access_scope="public", requested_model="gpt-test",
            routed_model="gpt-test", identity_kind="account", identity_id="first",
        )
        manager._remember_response_binding(
            "resp-second", access_scope="public", requested_model="gpt-test",
            routed_model="gpt-test", identity_kind="account", identity_id="second",
        )
        manager._remember_alpha_search_binding(
            "public", "search-first", requested_model="gpt-test", routed_model="gpt-test", identity_id="first",
        )
        removed = manager.clear_account_runtime_state(["first"])
        self.assertEqual(removed, {"cooldowns": 1, "lastUsed": 1, "responseBindings": 1, "alphaBindings": 1})
        self.assertIn(("second", "gpt-test"), manager.account_cooldowns)
        self.assertEqual(manager.account_last_used, {"second": 2.0})
        self.assertEqual(len(manager.response_bindings), 1)
        self.assertIsNone(manager.last_account)
        self.assertIsNone(manager.last_quota)

    def test_oauth_headers_match_and_401_refreshes_same_account_once(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        observed = []
        first_error = http_error(401, b'{"error":{"type":"authentication_error"}}')
        response = FakeResponse()

        def open_request(request, **_kwargs):
            observed.append(request)
            if len(observed) == 1:
                raise first_error
            return response

        with (
            patch.object(manager, "_accounts", return_value=[account]),
            patch.object(core, "_codex_client_version", return_value="0.153.4"),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                side_effect=[
                    {"accessToken": "old-token", "accountId": "workspace"},
                    {"accessToken": "new-token", "accountId": "workspace"},
                ],
            ) as credentials,
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            opened, routed, _ = manager._open_upstream({"model": "gpt-test", "input": "hi"})
        self.assertIs(opened._response, response)
        self.assertIs(routed, account)
        self.assertEqual(manager.scheduler.inflight, {("account", "first"): 1})
        opened.close()
        opened.close()
        self.assertTrue(response.was_closed)
        self.assertFalse(manager.scheduler.inflight)
        self.assertEqual(observed[1].get_header("Authorization"), "Bearer new-token")
        for request in observed:
            self.assertEqual(request.get_header("Version"), "0.153.4")
            self.assertEqual(request.get_header("User-agent"), "codex_cli_rs/0.153.4")
        self.assertEqual(
            credentials.call_args_list,
            [
                call("first"),
                call("first", force_refresh=True, rejected_access_token="old-token"),
            ],
        )

    def test_api_key_provider_preserves_client_identity_headers(self):
        manager = web2api.Web2APIManager()
        route = {"sourceRecordId": "provider", "id": "upstream-model"}
        response = FakeResponse(response_headers=headers(Content_Type="application/json"))
        observed = []
        with (
            patch.object(core, "provider_by_id", return_value={"id": "provider", "name": "Provider"}),
            patch.object(core, "load_provider_key", return_value="provider-secret"),
            patch.object(core, "_provider_runtime_base_url", return_value="https://provider.example/v1"),
            patch.object(core, "_open_same_origin_request", side_effect=lambda request, **_kwargs: observed.append(request) or response),
        ):
            manager._open_provider_upstream(
                "/v1/responses",
                {"model": "alias", "input": "hi"},
                route,
                {"User-Agent": "downstream/9", "Originator": "downstream", "Version": "9"},
            )
        request = observed[0]
        self.assertEqual(request.get_header("User-agent"), "downstream/9")
        self.assertEqual(request.get_header("Originator"), "downstream")
        self.assertEqual(request.get_header("Version"), "9")

    def test_alpha_search_is_sticky_refreshes_401_and_preserves_scope(self):
        manager = web2api.Web2APIManager()
        accounts = [self.account("first"), self.account("second")]
        observed = []
        responses = [
            http_error(401, b'{"error":{"type":"authentication_error"}}'),
            FakeResponse(b'{"results":[1]}', response_headers=headers(Content_Type="application/json")),
            FakeResponse(b'{"results":[2]}', response_headers=headers(Content_Type="application/json")),
        ]

        def open_request(request, **_kwargs):
            observed.append(request)
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def credentials(account_id, *, force_refresh=False, rejected_access_token=None):
            self.assertEqual(account_id, "first")
            if force_refresh:
                self.assertEqual(rejected_access_token, "old-token")
                return {"accessToken": "new-token", "accountId": "workspace-first"}
            return {
                "accessToken": "new-token" if observed else "old-token",
                "accountId": "workspace-first",
            }

        with (
            patch.object(core, "load_settings", return_value=self.settings(*accounts)),
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(core, "_codex_client_version", return_value="0.153.4"),
            patch.object(core, "_account_chatgpt_credentials", side_effect=credentials),
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            first = manager.execute(
                "/v1/alpha/search",
                {"id": "search-session", "model": "gpt-test", "query": "one"},
                access_scope="public",
                client_headers={
                    "X-Client-Request-Id": "request-1",
                    "X-OpenAI-Actor-Authorization": "actor-proof",
                },
            )
            second = manager.execute(
                "/v1/alpha/search",
                {"id": "search-session", "query": "two"},
                access_scope="public",
            )
        self.assertEqual(first["body"], b'{"results":[1]}')
        self.assertEqual(second["body"], b'{"results":[2]}')
        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[1].get_header("Authorization"), "Bearer new-token")
        self.assertEqual(observed[0].get_header("Version"), "0.153.4")
        self.assertEqual(observed[0].get_header("User-agent"), "codex_cli_rs/0.153.4")
        self.assertEqual(observed[0].get_header("Session-id"), "search-session")
        self.assertEqual(observed[0].get_header("X-client-request-id"), "request-1")
        self.assertEqual(observed[0].get_header("X-openai-actor-authorization"), "actor-proof")
        public_binding = manager._lookup_alpha_search_binding("public", "search-session")
        self.assertEqual(public_binding["identityId"], "first")
        self.assertIsNone(manager._lookup_alpha_search_binding("internal", "search-session"))

    def test_alpha_search_without_model_rejects_ambiguous_pool_before_credentials(self):
        manager = web2api.Web2APIManager()
        with (
            patch.object(core, "load_settings", return_value=self.settings()),
            patch.object(core, "_account_chatgpt_credentials") as credentials,
            patch.object(core, "_open_same_origin_request") as opened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager.execute("/v1/alpha/search", {"query": "ambiguous"}, access_scope="public")
        self.assertEqual(raised.exception.status, 409)
        credentials.assert_not_called()
        opened.assert_not_called()

    def test_alpha_search_provider_route_and_out_of_scope_account_fail_closed(self):
        manager = web2api.Web2APIManager()
        first, second = self.account("first"), self.account("second")
        provider_route = {"sourceKind": "provider", "sourceRecordId": "provider", "id": "gpt-test"}
        with (
            patch.object(core, "resolve_model_route", return_value=provider_route),
            patch.object(core, "load_provider_key") as provider_key,
            self.assertRaises(web2api.GatewayError) as provider_error,
        ):
            manager.execute("/v1/alpha/search", {"model": "gpt-test", "query": "x"})
        self.assertEqual(provider_error.exception.status, 404)
        provider_key.assert_not_called()

        account_route = {"sourceKind": "account", "sourceRecordId": "second", "id": "gpt-test"}
        restricted = self.settings(first)
        restricted["accounts"].append(second)
        with (
            patch.object(core, "resolve_model_route", return_value=account_route),
            patch.object(core, "load_settings", return_value=restricted),
            patch.object(core, "_account_chatgpt_credentials") as credentials,
            self.assertRaises(web2api.GatewayError) as scope_error,
        ):
            manager.execute("/v1/alpha/search", {"model": "gpt-test", "query": "x"}, access_scope="public")
        self.assertEqual(scope_error.exception.status, 503)
        credentials.assert_not_called()

    def test_responses_lite_filters_only_declarations_and_preserves_opaque_history(self):
        reasoning = {
            "type": "reasoning",
            "id": "rs_opaque",
            "encrypted_content": "gAAAA-valid-opaque-state",
            "signature": "provider-signature",
            "summary": [],
        }
        historical_message = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "keep history"}],
            "tools": [{"type": "web_search", "state": "historical-not-a-declaration"}],
        }
        payload = {
            "model": "gpt-6-astra",
            "input": [
                reasoning,
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "function", "name": "lookup"},
                        {"type": "custom", "name": "apply_patch"},
                        {"type": "tool_search", "execution": "client"},
                        {"type": "tool_search", "execution": "server"},
                        {"type": "web_search"},
                        {"type": "image_generation"},
                        {
                            "type": "namespace",
                            "name": "collaboration",
                            "tools": [{"type": "function", "name": "spawn_agent"}],
                        },
                    ],
                    "tool_choice": {"type": "web_search"},
                },
                {"type": "additional_tools", "tools": [{"type": "web_search"}]},
                historical_message,
            ],
            "tools": [{"type": "function", "name": "root"}, {"type": "web_search"}],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "auto",
                "tools": [{"type": "custom", "name": "apply_patch"}, {"type": "web_search"}],
            },
            "response": {
                "tools": [{"type": "tool_search", "execution": "client"}, {"type": "tool_search", "execution": "server"}],
                "tool_choice": {"type": "tool_search", "execution": "server"},
            },
            "include": ["reasoning.encrypted_content"],
            "parallel_tool_calls": True,
            "store": True,
        }
        normalized = web2api._responses_input(payload, responses_lite=True)

        self.assertTrue(normalized["store"])
        self.assertTrue(normalized["stream"])
        self.assertFalse(normalized["parallel_tool_calls"])
        self.assertEqual(normalized["include"], ["reasoning.encrypted_content"])
        self.assertEqual(normalized["input"][0], reasoning)
        self.assertEqual(normalized["input"][-1], historical_message)
        self.assertEqual(
            [tool["type"] for tool in normalized["input"][1]["tools"]],
            ["function", "custom", "tool_search", "namespace"],
        )
        self.assertNotIn("tool_choice", normalized["input"][1])
        self.assertEqual(len(normalized["input"]), 3)
        self.assertEqual([tool["type"] for tool in normalized["tools"]], ["function"])
        self.assertEqual([tool["type"] for tool in normalized["tool_choice"]["tools"]], ["custom"])
        self.assertEqual([tool["type"] for tool in normalized["response"]["tools"]], ["tool_search"])
        self.assertNotIn("tool_choice", normalized["response"])
        self.assertEqual(payload["input"][0], reasoning)
        self.assertTrue(payload["parallel_tool_calls"])

    def test_regular_responses_preserve_explicit_store_and_tools(self):
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "reasoning", "encrypted_content": "opaque", "signature": "sig"}],
            "tools": [{"type": "web_search"}],
            "parallel_tool_calls": True,
            "store": True,
        }
        normalized = web2api._responses_input(payload)
        self.assertTrue(normalized["store"])
        self.assertTrue(normalized["parallel_tool_calls"])
        self.assertEqual(normalized["input"], payload["input"])
        self.assertEqual(normalized["tools"], payload["tools"])

    def test_responses_lite_uses_explicit_header_route_or_cached_catalog_without_discovery(self):
        self.assertTrue(
            web2api._responses_lite_enabled(
                "unknown-model",
                client_headers={web2api.CODEX_RESPONSES_LITE_HEADER: "true"},
            )
        )
        self.assertTrue(web2api._responses_lite_enabled("routed", {"useResponsesLite": True}))
        with (
            patch.object(core, "MODEL_CACHE", {"raw": {"models": [{"slug": "gpt-6-astra", "use_responses_lite": True}]}}),
            patch.object(core, "_raw_local_model_catalog") as discovery,
        ):
            self.assertTrue(web2api._responses_lite_enabled("gpt-6-astra"))
        discovery.assert_not_called()
        self.assertFalse(web2api._responses_lite_enabled("unknown-model"))

    def test_oauth_lite_request_is_normalized_and_header_is_forwarded(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        route = {
            "sourceKind": "account",
            "sourceRecordId": "first",
            "id": "gpt-6-astra",
            "use_responses_lite": True,
        }
        upstream = FakeResponse(
            sse(
                {"type": "response.output_text.delta", "delta": "ok"},
                {"type": "response.completed", "response": {"status": "completed"}},
            )
        )
        requests = []
        with (
            patch.object(core, "resolve_model_route", return_value=route),
            patch.object(manager, "_accounts", return_value=[account]),
            patch.object(core, "_account_chatgpt_credentials", return_value={"accessToken": "token", "accountId": "workspace"}),
            patch.object(core, "_open_same_origin_request", side_effect=lambda request, **_kwargs: requests.append(request) or upstream),
            patch.object(manager, "_record_usage"),
        ):
            result = manager.stream(
                "/v1/responses",
                {
                    "model": "alias-astra",
                    "input": "hi",
                    "parallel_tool_calls": True,
                    "store": True,
                    "tools": [{"type": "function", "name": "keep"}, {"type": "web_search"}],
                },
            )
            b"".join(result["chunks"])
        request = requests[0]
        forwarded = json.loads(request.data)
        request_headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(request_headers[web2api.CODEX_RESPONSES_LITE_HEADER.casefold()], "true")
        self.assertEqual(forwarded["model"], "gpt-6-astra")
        self.assertFalse(forwarded["parallel_tool_calls"])
        self.assertTrue(forwarded["store"])
        self.assertEqual([tool["type"] for tool in forwarded["tools"]], ["function"])

    def test_bind_error_diagnostics_separate_occupied_and_denied_ports(self):
        occupied = OSError("address already in use")
        occupied.winerror = 10048
        occupied_message, occupied_status = web2api._gateway_bind_error("127.0.0.1", 17860, occupied)
        self.assertIn("已被其他程序占用", occupied_message)
        self.assertIn("10048", occupied_message)
        self.assertNotIn("excludedportrange", occupied_message)
        self.assertIn("占用", occupied_status)

        denied = OSError("permission denied")
        denied.winerror = 10013
        denied_message, denied_status = web2api._gateway_bind_error("127.0.0.1", 17860, denied)
        self.assertIn("拒绝绑定", denied_message)
        self.assertIn("10013", denied_message)
        self.assertIn("Hyper-V/WSL", denied_message)
        self.assertIn("excludedportrange", denied_message)
        self.assertIn("手动选择", denied_message)
        self.assertIn("不会自动修改端口", denied_message)
        self.assertIn("拒绝", denied_status)

    def test_start_reports_bind_failure_without_changing_config_or_retrying(self):
        manager = web2api.Web2APIManager()
        denied = OSError("permission denied")
        denied.winerror = 10013
        settings = {"web2api": {"bindHost": "127.0.0.1", "port": 17860}}
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(web2api, "GatewayServer", side_effect=denied) as server,
            self.assertRaises(core.ManagerError) as raised,
        ):
            manager.start()
        server.assert_called_once_with(("127.0.0.1", 17860), manager)
        self.assertIn("10013", str(raised.exception))
        self.assertEqual(settings["web2api"]["port"], 17860)
        self.assertIn("拒绝", manager.last_error)


if __name__ == "__main__":
    unittest.main()
