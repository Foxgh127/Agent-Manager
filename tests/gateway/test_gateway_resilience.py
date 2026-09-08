from email.message import Message
from email.utils import format_datetime
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.gateway.service as web2api


class FakeResponse:
    def __init__(self, headers=None, chunks=None):
        self.headers = headers or Message()
        self.chunks = list(chunks or [])
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def rate_limit_error(retry_after=None):
    headers = Message()
    headers["Content-Type"] = "application/json"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return web2api.HTTPError(
        web2api.UPSTREAM_RESPONSES_URL,
        429,
        "rate limited",
        headers,
        io.BytesIO(b'{"error":{"type":"rate_limit_exceeded"}}'),
    )


def sse(*events):
    return b"".join(
        ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
        for event in events
    )


class GatewayResilienceV9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(core, "STATE_DIR", Path(self.temp.name) / "state")
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def account(account_id, remaining=50):
        return {
            "id": account_id,
            "label": account_id,
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "codexCompatible": True,
            "quotaOnly": False,
            "models": ["gpt-test", "gpt-other"],
            "usage": {"weekly": {"remainingPercent": remaining}},
        }

    @classmethod
    def settings(cls, routing="ordered"):
        accounts = [cls.account("first", 90), cls.account("second", 40)]
        return {
            "accounts": accounts,
            "web2api": {
                "accountIds": ["first", "second"],
                "routing": routing,
                "activeForCodex": False,
                "activeAccountId": None,
            },
        }

    def test_retry_after_parses_seconds_http_date_and_codex_reset(self):
        numeric = Message()
        numeric["Retry-After"] = "1.5"
        self.assertEqual(web2api._retry_after_seconds(numeric, now=1000), 1.5)

        retry_at = web2api.datetime.fromtimestamp(1012, web2api.timezone.utc)
        dated = Message()
        dated["Retry-After"] = format_datetime(retry_at, usegmt=True)
        self.assertEqual(web2api._retry_after_seconds(dated, now=1000), 12)

        reset = Message()
        reset["X-Codex-Primary-Reset-At"] = "1020"
        self.assertEqual(web2api._retry_after_seconds(reset, now=1000), 20)
        self.assertIsNone(web2api._retry_after_seconds({"Retry-After": "invalid"}, now=1000))

    def test_http_429_cools_only_that_account_and_uses_next_healthy_candidate(self):
        manager = web2api.Web2APIManager()
        accounts = [self.account("first"), self.account("second")]
        response = FakeResponse()
        attempts = [rate_limit_error("1.5"), response]
        opened = []

        def open_request(request, *, timeout):
            opened.append((timeout, request.data))
            result = attempts.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(manager, "_accounts", return_value=accounts),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                side_effect=lambda account_id: {
                    "accessToken": f"token-{account_id}",
                    "accountId": f"workspace-{account_id}",
                },
            ),
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            opened_response, account, _headers = manager._open_upstream(
                {"model": "gpt-test", "input": "hello"}
            )

        self.assertIs(opened_response, response)
        self.assertEqual(account["id"], "second")
        self.assertEqual(
            [item[0] for item in opened],
            [web2api.UPSTREAM_OPEN_TIMEOUT_SECONDS] * 2,
        )
        self.assertEqual(opened[0][1], opened[1][1])
        self.assertEqual(json.loads(opened[1][1]), {"model": "gpt-test", "input": "hello"})
        self.assertEqual(response.timeout, web2api.UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS)
        cooldown = manager.account_cooldowns[("first", "gpt-test")]
        self.assertEqual(cooldown["delaySeconds"], 1.5)
        self.assertNotIn(("second", "gpt-test"), manager.account_cooldowns)

    def test_healthy_sorting_skips_cooldown_and_recovers_after_deadline(self):
        manager = web2api.Web2APIManager()
        settings = self.settings(routing="quota_first")
        first = settings["accounts"][0]
        with patch.object(web2api.time, "monotonic", return_value=100.0):
            manager._cooldown_account(first, "gpt-test", {"Retry-After": "5"})

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(web2api.time, "monotonic", return_value=102.0),
        ):
            cooling_order = manager._accounts(requested_model="gpt-test")
        self.assertEqual([item["id"] for item in cooling_order], ["second"])

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(web2api.time, "monotonic", return_value=106.0),
        ):
            recovered_order = manager._accounts(requested_model="gpt-test")
        self.assertEqual([item["id"] for item in recovered_order], ["first", "second"])
        self.assertNotIn(("first", "gpt-test"), manager.account_cooldowns)

    def test_cooldown_is_model_scoped_and_bounded_without_fixed_ten_seconds(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        with patch.object(web2api.time, "monotonic", return_value=50.0):
            manager._cooldown_account(account, "gpt-test", {"Retry-After": "invalid"})
            manager._cooldown_account(account, "gpt-other", {"Retry-After": "999999"})
            manager._cooldown_account(account, "gpt-zero", {"Retry-After": "0"})

        self.assertEqual(
            manager.account_cooldowns[("first", "gpt-test")]["delaySeconds"],
            web2api.UPSTREAM_COOLDOWN_DEFAULT_SECONDS,
        )
        self.assertEqual(
            manager.account_cooldowns[("first", "gpt-other")]["delaySeconds"],
            web2api.UPSTREAM_COOLDOWN_MAX_SECONDS,
        )
        self.assertEqual(
            manager.account_cooldowns[("first", "gpt-zero")]["delaySeconds"],
            web2api.UPSTREAM_COOLDOWN_MIN_SECONDS,
        )
        settings = self.settings()
        settings["web2api"]["accountIds"] = ["first"]
        settings["accounts"] = [account]
        account["models"].append("gpt-uncooled")
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(web2api.time, "monotonic", return_value=53.0),
        ):
            available_other_model = manager._accounts(requested_model="gpt-uncooled")
        self.assertEqual([item["id"] for item in available_other_model], ["first"])

    def test_all_cooling_accounts_return_bounded_retry_after_without_credentials(self):
        manager = web2api.Web2APIManager()
        settings = self.settings()
        manager.account_cooldowns = {
            ("first", "gpt-test"): {"until": 103.2},
            ("second", "gpt-test"): {"until": 107.0},
        }

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(web2api.time, "monotonic", return_value=100.0),
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager._accounts(requested_model="gpt-test")

        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "4")
        self.assertEqual(raised.exception.route_account["id"], "first")

        handler = object.__new__(web2api.GatewayHandler)
        sent = []
        handler._send = lambda *args: sent.append(args)
        handler._error(raised.exception)
        self.assertEqual(sent[0][1], 429)
        self.assertEqual(sent[0][3]["Retry-After"], "4")

    def test_session_affine_request_never_guesses_across_a_multi_account_pool(self):
        manager = web2api.Web2APIManager()
        settings = self.settings()

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "_account_chatgpt_credentials") as credentials,
            patch.object(core, "_open_same_origin_request") as opened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager._open_upstream(
                {
                    "model": "gpt-test",
                    "input": "continue",
                    "previous_response_id": "resp-account-bound",
                }
            )

        self.assertEqual(raised.exception.status, 409)
        credentials.assert_not_called()
        opened.assert_not_called()

    def test_session_affine_single_account_429_is_not_replayed(self):
        manager = web2api.Web2APIManager()
        settings = self.settings()
        settings["accounts"] = [settings["accounts"][0]]
        settings["web2api"]["accountIds"] = ["first"]

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={"accessToken": "token", "accountId": "workspace"},
            ),
            patch.object(core, "_open_same_origin_request", side_effect=rate_limit_error("3")) as opened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager._open_upstream(
                {
                    "model": "gpt-test",
                    "input": "continue",
                    "previous_response_id": "resp-first",
                }
            )

        self.assertEqual(raised.exception.status, 429)
        opened.assert_called_once()
        self.assertIn(("first", "gpt-test"), manager.account_cooldowns)

    def test_sse_rate_limit_cools_future_requests_without_replaying_stream(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        event = {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "type": "rate_limit_exceeded",
                    "message": "limited",
                    "retry_after_seconds": 4,
                },
            },
        }
        response = FakeResponse(
            chunks=[f"data: {json.dumps(event)}\n\n".encode("utf-8")]
        )

        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(manager, "_open_upstream", return_value=(response, account, {})) as opened,
            patch.object(manager, "_record_usage"),
        ):
            stream = manager.stream(
                "/v1/responses",
                {"model": "gpt-test", "input": "hello", "stream": True},
            )
            body = b"".join(stream["chunks"])

        opened.assert_called_once()
        self.assertIn(b"response.failed", body)
        self.assertEqual(
            manager.account_cooldowns[("first", "gpt-test")]["delaySeconds"],
            4,
        )

    def test_nested_response_socket_gets_stream_idle_timeout(self):
        class Socket:
            timeout = None

            def settimeout(self, value):
                self.timeout = value

        socket = Socket()
        response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=socket)))

        self.assertTrue(web2api._set_response_idle_timeout(response))
        self.assertEqual(socket.timeout, web2api.UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS)

    def test_provider_transport_uses_header_and_stream_idle_timeouts(self):
        manager = web2api.Web2APIManager()
        response = FakeResponse()
        observed = []

        def open_request(_request, *, timeout):
            observed.append(timeout)
            return response

        with (
            patch.object(
                core,
                "provider_by_id",
                return_value={
                    "id": "provider-one",
                    "name": "Provider One",
                    "baseUrl": "https://provider.example.test/v1",
                },
            ),
            patch.object(core, "load_provider_key", return_value="provider-key"),
            patch.object(
                core,
                "_provider_runtime_base_url",
                return_value="https://provider.example.test/v1",
            ),
            patch.object(core, "_codex_client_version", return_value="1.0"),
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            opened = manager._open_provider_upstream(
                "/v1/responses",
                {"model": "alias", "input": "hello"},
                {"sourceRecordId": "provider-one", "id": "native-model"},
            )

        self.assertIs(opened, response)
        self.assertEqual(observed, [web2api.UPSTREAM_OPEN_TIMEOUT_SECONDS])
        self.assertEqual(response.timeout, web2api.UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS)

    def test_observed_response_id_reuses_the_actual_account(self):
        manager = web2api.Web2APIManager()
        settings = self.settings(routing="ordered")
        responses = [
            FakeResponse(
                chunks=[
                    sse(
                        {"type": "response.created", "response": {"id": "resp-root", "status": "in_progress"}},
                        {"type": "response.output_text.delta", "delta": "one"},
                        {"type": "response.completed", "response": {"id": "resp-root", "status": "completed"}},
                    )
                ]
            ),
            FakeResponse(
                chunks=[
                    sse(
                        {"type": "response.created", "response": {"id": "resp-child", "status": "in_progress"}},
                        {"type": "response.completed", "response": {"id": "resp-child", "status": "completed"}},
                    )
                ]
            ),
        ]
        credential_accounts = []

        def credentials(account_id):
            credential_accounts.append(account_id)
            return {"accessToken": f"token-{account_id}", "accountId": f"workspace-{account_id}"}

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(core, "_account_chatgpt_credentials", side_effect=credentials),
            patch.object(core, "_codex_client_version", return_value="1.0"),
            patch.object(core, "_open_same_origin_request", side_effect=responses),
            patch.object(manager, "_record_usage"),
        ):
            first = manager.stream(
                "/v1/responses",
                {"model": "gpt-test", "input": "first", "stream": True},
            )
            self.assertIn(b"resp-root", b"".join(first["chunks"]))
            second = manager.stream(
                "/v1/responses",
                {
                    "model": "gpt-test",
                    "input": "second",
                    "stream": True,
                    "previous_response_id": "resp-root",
                },
            )
            self.assertIn(b"resp-child", b"".join(second["chunks"]))

        self.assertEqual(credential_accounts, ["first", "first"])
        root_key = manager._response_binding_key("resp-root")
        self.assertIn(root_key, manager.response_bindings)
        self.assertNotIn("resp-root", manager.response_bindings)
        self.assertNotIn("resp-root", json.dumps(manager.response_bindings))

    def test_response_binding_is_scope_and_model_isolated(self):
        manager = web2api.Web2APIManager()
        manager._remember_response_binding(
            "resp-private",
            access_scope="public",
            requested_model="gpt-test",
            routed_model="gpt-test",
            identity_kind="account",
            identity_id="first",
        )

        with self.assertRaises(web2api.GatewayError) as wrong_scope:
            manager._resolve_response_binding(
                {"previous_response_id": "resp-private"},
                access_scope="internal",
                requested_model="gpt-test",
                route=None,
            )
        self.assertEqual(wrong_scope.exception.status, 409)

        with self.assertRaises(web2api.GatewayError) as wrong_model:
            manager._resolve_response_binding(
                {"previous_response_id": "resp-private"},
                access_scope="public",
                requested_model="gpt-other",
                route=None,
            )
        self.assertEqual(wrong_model.exception.status, 409)

        with self.assertRaises(web2api.GatewayError) as conversation:
            manager._resolve_response_binding(
                {"conversation": {"id": "conversation-unsupported"}},
                access_scope="public",
                requested_model="gpt-test",
                route=None,
            )
        self.assertEqual(conversation.exception.status, 409)

    def test_response_binding_ttl_and_lru_capacity_are_bounded(self):
        manager = web2api.Web2APIManager()

        def remember(response_id):
            manager._remember_response_binding(
                response_id,
                access_scope="public",
                requested_model="gpt-test",
                routed_model="gpt-test",
                identity_kind="account",
                identity_id="first",
            )

        with (
            patch.object(web2api, "RESPONSE_BINDING_MAX_ENTRIES", 2),
            patch.object(web2api, "RESPONSE_BINDING_TTL_SECONDS", 5.0),
            patch.object(web2api.time, "monotonic", return_value=100.0),
        ):
            remember("resp-one")
        with (
            patch.object(web2api, "RESPONSE_BINDING_MAX_ENTRIES", 2),
            patch.object(web2api, "RESPONSE_BINDING_TTL_SECONDS", 5.0),
            patch.object(web2api.time, "monotonic", return_value=101.0),
        ):
            remember("resp-two")
            self.assertIsNotNone(manager._lookup_response_binding("resp-one"))
        with (
            patch.object(web2api, "RESPONSE_BINDING_MAX_ENTRIES", 2),
            patch.object(web2api, "RESPONSE_BINDING_TTL_SECONDS", 5.0),
            patch.object(web2api.time, "monotonic", return_value=102.0),
        ):
            remember("resp-three")
            self.assertIsNone(manager._lookup_response_binding("resp-two"))
            self.assertIsNotNone(manager._lookup_response_binding("resp-one"))
        with (
            patch.object(web2api, "RESPONSE_BINDING_MAX_ENTRIES", 2),
            patch.object(web2api.time, "monotonic", return_value=106.0),
        ):
            self.assertIsNone(manager._lookup_response_binding("resp-one"))

        self.assertLessEqual(len(manager.response_bindings), 2)

    def test_removed_bound_account_fails_without_spillover_and_forgets_binding(self):
        manager = web2api.Web2APIManager()
        manager._remember_response_binding(
            "resp-removed",
            access_scope="public",
            requested_model="gpt-test",
            routed_model="gpt-test",
            identity_kind="account",
            identity_id="first",
        )
        settings = self.settings()
        settings["accounts"] = [settings["accounts"][1]]
        settings["web2api"]["accountIds"] = ["second"]

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(core, "_account_chatgpt_credentials") as credentials,
            patch.object(core, "_open_same_origin_request") as opened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager.stream(
                "/v1/responses",
                {
                    "model": "gpt-test",
                    "input": "continue",
                    "stream": True,
                    "previous_response_id": "resp-removed",
                },
            )

        self.assertEqual(raised.exception.status, 409)
        credentials.assert_not_called()
        opened.assert_not_called()
        self.assertIsNone(manager._lookup_response_binding("resp-removed"))

    def test_failed_or_cancelled_stream_never_commits_response_binding(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        failed_response = FakeResponse(
            chunks=[
                sse(
                    {"type": "response.created", "response": {"id": "resp-failed", "status": "in_progress"}},
                    {
                        "type": "response.failed",
                        "response": {"id": "resp-failed", "status": "failed", "error": {"type": "server_error"}},
                    },
                )
            ]
        )
        cancelled_response = FakeResponse(
            chunks=[
                sse(
                    {"type": "response.created", "response": {"id": "resp-cancelled", "status": "in_progress"}},
                    {"type": "response.output_text.delta", "delta": "partial"},
                )
            ]
        )

        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(manager, "_record_usage"),
        ):
            with patch.object(manager, "_open_upstream", return_value=(failed_response, account, {})):
                failed = manager.stream(
                    "/v1/responses",
                    {"model": "gpt-test", "input": "fail", "stream": True},
                )
                self.assertIn(b"response.failed", b"".join(failed["chunks"]))
            with patch.object(manager, "_open_upstream", return_value=(cancelled_response, account, {})):
                cancelled = manager.stream(
                    "/v1/responses",
                    {"model": "gpt-test", "input": "cancel", "stream": True},
                )
                iterator = cancelled["chunks"]
                self.assertIn(b"partial", next(iterator))
                iterator.close()

        self.assertIsNone(manager._lookup_response_binding("resp-failed"))
        self.assertIsNone(manager._lookup_response_binding("resp-cancelled"))
        self.assertTrue(cancelled_response.closed)

    def test_json_response_binding_commits_only_after_delivery(self):
        manager = web2api.Web2APIManager()
        account = self.account("first")
        body = json.dumps(
            {
                "id": "resp-json",
                "object": "response",
                "status": "completed",
                "output": [],
            }
        ).encode("utf-8")

        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(
                manager,
                "_upstream",
                return_value={"body": body, "headers": {}, "account": account},
            ),
            patch.object(manager, "_record_usage"),
        ):
            result = manager.execute(
                "/v1/responses",
                {"model": "gpt-test", "input": "hello", "stream": False},
            )

        self.assertIsNone(manager._lookup_response_binding("resp-json"))
        self.assertTrue(callable(result.get("_onDelivered")))
        result["_onDelivered"]()
        binding = manager._lookup_response_binding("resp-json")
        self.assertEqual(binding["identityKind"], "account")
        self.assertEqual(binding["identityId"], "first")

    def test_provider_response_binding_is_reused_and_invalidated_when_route_disappears(self):
        manager = web2api.Web2APIManager()
        route = {
            "id": "native-provider-model",
            "sourceKind": "provider",
            "sourceRecordId": "provider-one",
        }
        response = FakeResponse(
            chunks=[
                sse(
                    {"type": "response.created", "response": {"id": "resp-provider", "status": "in_progress"}},
                    {"type": "response.completed", "response": {"id": "resp-provider", "status": "completed"}},
                )
            ]
        )

        with (
            patch.object(core, "resolve_model_route", return_value=route),
            patch.object(manager, "_open_provider_upstream", return_value=response) as opened,
            patch.object(manager, "_record_usage"),
        ):
            first = manager.stream(
                "/v1/responses",
                {"model": "provider-alias", "input": "hello", "stream": True},
            )
            b"".join(first["chunks"])

        binding = manager._lookup_response_binding("resp-provider")
        self.assertEqual(binding["identityKind"], "provider")
        self.assertEqual(binding["identityId"], "provider-one")
        opened.assert_called_once()

        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(manager, "_open_provider_upstream") as reopened,
            self.assertRaises(web2api.GatewayError) as raised,
        ):
            manager.stream(
                "/v1/responses",
                {
                    "model": "provider-alias",
                    "input": "continue",
                    "stream": True,
                    "previous_response_id": "resp-provider",
                },
            )

        self.assertEqual(raised.exception.status, 409)
        reopened.assert_not_called()
        self.assertIsNone(manager._lookup_response_binding("resp-provider"))

    def test_unknown_previous_response_still_works_with_one_account(self):
        manager = web2api.Web2APIManager()
        settings = self.settings()
        settings["accounts"] = [settings["accounts"][0]]
        settings["web2api"]["accountIds"] = ["first"]
        response = FakeResponse()

        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={"accessToken": "token", "accountId": "workspace"},
            ),
            patch.object(core, "_codex_client_version", return_value="1.0"),
            patch.object(core, "_open_same_origin_request", return_value=response) as opened,
        ):
            opened_response, account, _headers = manager._open_upstream(
                {
                    "model": "gpt-test",
                    "input": "continue",
                    "previous_response_id": "resp-unknown",
                }
            )

        self.assertIs(opened_response, response)
        self.assertEqual(account["id"], "first")
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
