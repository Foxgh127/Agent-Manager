import hashlib
import io
import json
from contextlib import nullcontext
from email.message import Message
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.gateway.service as web2api


class FakeResponse:
    def __init__(self, chunks, headers=None, status=200):
        self.chunks = list(chunks)
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.status = status
        self.closed = False

    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Web2APIUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stats_path = Path(self.temp.name) / "agent-manager" / "web2api-usage.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_reset_write_failure_keeps_pending_usage_and_generation(self):
        store = web2api.UsageStatsStore(self.stats_path)
        with patch.object(store, "_schedule_flush_locked"):
            store.record({"requestedModel": "test"}, {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7})
        generation = store.generation
        with patch.object(core, "atomic_write_bytes", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                store.reset()
        self.assertEqual(store.generation, generation)
        self.assertEqual(store.snapshot()["totals"]["totalTokens"], 7)
        store.flush(force=True)

    def test_unexpected_gateway_error_is_redacted_from_response_and_status(self):
        class Manager:
            last_error = None

        class Server:
            manager = Manager()

        handler = object.__new__(web2api.GatewayHandler)
        handler.server = Server()
        responses = []
        handler._json = lambda payload, status=200: responses.append((payload, status))

        handler._error(RuntimeError("internal-sentinel sk-secret-value"))

        self.assertEqual(responses[0][1], 500)
        encoded = json.dumps(responses[0][0], ensure_ascii=False)
        self.assertNotIn("internal-sentinel", encoded)
        self.assertNotIn("sk-secret-value", encoded)
        self.assertEqual(handler.server.manager.last_error, "内部请求错误（RuntimeError）")

    def test_gateway_worker_applies_slow_client_timeout_and_releases_slot(self):
        class FakeRequest:
            timeout = None

            def settimeout(self, value):
                self.timeout = value

        class Manager:
            lock = threading.RLock()
            active_request_count = 1

        request = FakeRequest()
        server = web2api.GatewayServer.__new__(web2api.GatewayServer)
        server.manager = Manager()
        server._thread_slots = threading.BoundedSemaphore(1)
        self.assertTrue(server._thread_slots.acquire(blocking=False))
        with patch.object(web2api.ThreadingHTTPServer, "process_request_thread") as inherited:
            server.process_request_thread(request, ("127.0.0.1", 12345))
        self.assertEqual(request.timeout, web2api.GATEWAY_CLIENT_TIMEOUT_SECONDS)
        self.assertEqual(server.manager.active_request_count, 0)
        inherited.assert_called_once_with(request, ("127.0.0.1", 12345))
        self.assertTrue(server._thread_slots.acquire(blocking=False))

    def test_gateway_rejects_ambiguous_framing_and_non_json_posts(self):
        class Server:
            server_address = ("127.0.0.1", 48888)

        def validate(headers):
            handler = object.__new__(web2api.GatewayHandler)
            handler.server = Server()
            handler.headers = headers
            handler.close_connection = False
            errors = []
            handler._error = errors.append
            return handler._validate_http_request(json_body=True), handler, errors

        duplicate_length = Message()
        duplicate_length["Host"] = "127.0.0.1:48888"
        duplicate_length["Content-Type"] = "application/json"
        duplicate_length["Content-Length"] = "2"
        duplicate_length["Content-Length"] = "3"
        valid, handler, errors = validate(duplicate_length)
        self.assertFalse(valid)
        self.assertTrue(handler.close_connection)
        self.assertRegex(str(errors[0]), "Content-Length")

        wrong_type = Message()
        wrong_type["Host"] = "localhost:48888"
        wrong_type["Content-Type"] = "text/plain"
        wrong_type["Content-Length"] = "2"
        valid, handler, errors = validate(wrong_type)
        self.assertFalse(valid)
        self.assertTrue(handler.close_connection)
        self.assertRegex(str(errors[0]), "application/json")

    def test_gateway_rejects_duplicate_authorization_headers(self):
        handler = object.__new__(web2api.GatewayHandler)
        headers = Message()
        headers["Authorization"] = "Bearer expected-key"
        headers["Authorization"] = "Bearer expected-key"
        handler.headers = headers
        with patch.object(core, "load_service_secret", return_value="expected-key"):
            self.assertFalse(handler._authorized())

    def test_gateway_auth_returns_distinct_public_and_internal_scopes(self):
        handler = object.__new__(web2api.GatewayHandler)

        def secret(name, required=False):
            self.assertFalse(required)
            return {"web2api": "public-key", "gateway_internal": "internal-key"}.get(name)

        with patch.object(core, "load_service_secret", side_effect=secret):
            for supplied, expected_scope in (
                ("public-key", "public"),
                ("internal-key", "internal"),
                ("未知密钥", None),
            ):
                with self.subTest(supplied=supplied):
                    headers = Message()
                    headers["Authorization"] = f"Bearer {supplied}"
                    handler.headers = headers
                    self.assertEqual(handler._access_scope(), expected_scope)

    def test_missing_internal_secret_never_promotes_public_key(self):
        handler = object.__new__(web2api.GatewayHandler)
        headers = Message()
        headers["X-API-Key"] = "public-key"
        handler.headers = headers

        def secret(name, required=False):
            self.assertFalse(required)
            return "public-key" if name == "web2api" else None

        with patch.object(core, "load_service_secret", side_effect=secret):
            self.assertEqual(handler._access_scope(), "public")

    def test_gateway_rejects_ambiguous_or_non_ascii_auth_without_500(self):
        handler = object.__new__(web2api.GatewayHandler)
        both = Message()
        both["Authorization"] = "Bearer internal-key"
        both["X-API-Key"] = "public-key"
        handler.headers = both
        with patch.object(core, "load_service_secret") as load_secret:
            self.assertIsNone(handler._access_scope())
        load_secret.assert_not_called()

        non_ascii = Message()
        non_ascii["Authorization"] = "Bearer 密钥"
        handler.headers = non_ascii
        with patch.object(
            core,
            "load_service_secret",
            side_effect=lambda name: "public-key" if name == "web2api" else "internal-key",
        ):
            self.assertIsNone(handler._access_scope())

    def test_public_status_hides_nonmember_identity_quota_and_error_detail(self):
        manager = web2api.Web2APIManager()
        manager.last_account = {"id": "private-import", "label": "Private Person"}
        manager.last_quota = {"remainingPercent": 42}
        manager.last_error = "账号 Private Person 不可用"
        settings = {
            "accounts": [{"id": "private-import", "label": "Private Person"}],
            "web2api": {
                **core._default_web2api_settings(),
                "accountIds": ["public-pool"],
                "activeForCodex": True,
                "activeAccountId": "private-import",
            },
        }
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "service_secret_configured", return_value=True),
        ):
            public = manager.status(access_scope="public")
            internal = manager.status(access_scope="internal")
        self.assertIsNone(public["activeAccountId"])
        self.assertIsNone(public["activeAccount"])
        self.assertIsNone(public["lastAccount"])
        self.assertIsNone(public["lastQuota"])
        self.assertNotIn("Private Person", public["lastError"])
        self.assertEqual(internal["activeAccountId"], "private-import")
        self.assertEqual(internal["lastAccount"]["id"], "private-import")

    def test_http_handler_passes_authenticated_scope_to_models_and_execute(self):
        calls = []

        class Manager:
            def models(self, *, access_scope):
                calls.append(("models", access_scope))
                return {"data": []}

            def execute(self, path, payload, *, access_scope):
                calls.append(("execute", path, payload["model"], access_scope))
                return {
                    "body": b"{}",
                    "status": 200,
                    "contentType": "application/json",
                    "headers": {},
                }

        class Server:
            server_address = ("127.0.0.1", 48888)
            manager = Manager()

            @staticmethod
            def upstream_slot():
                return nullcontext()

        handler = object.__new__(web2api.GatewayHandler)
        handler.server = Server()
        handler._validate_http_request = lambda **_kwargs: True
        handler._require_auth = lambda: "internal"
        handler._json = lambda *_args, **_kwargs: None
        handler._send = lambda *_args, **_kwargs: None
        handler._error = self.fail

        handler.path = "/v1/models"
        handler.do_GET()

        payload = json.dumps({"model": "private-model", "input": "hello"}).encode()
        headers = Message()
        headers["Content-Length"] = str(len(payload))
        handler.headers = headers
        handler.rfile = io.BytesIO(payload)
        handler.path = "/v1/responses"
        handler.do_POST()

        self.assertEqual(
            calls,
            [
                ("models", "internal"),
                ("execute", "/v1/responses", "private-model", "internal"),
            ],
        )

    def test_usage_parser_accepts_responses_chat_and_fragmented_sse(self):
        self.assertEqual(
            web2api._normalized_usage(
                {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
            ),
            {
                "inputTokens": 7,
                "cachedInputTokens": 0,
                "cacheWriteTokens": 0,
                "outputTokens": 3,
                "reasoningOutputTokens": 0,
                "totalTokens": 10,
            },
        )
        self.assertEqual(
            web2api._normalized_usage({"prompt_tokens": 2, "completion_tokens": 5}),
            {
                "inputTokens": 2,
                "cachedInputTokens": 0,
                "cacheWriteTokens": 0,
                "outputTokens": 5,
                "reasoningOutputTokens": 0,
                "totalTokens": 7,
            },
        )
        detailed = web2api._normalized_usage(
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 70, "cache_write_tokens": 9},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 12},
                "total_tokens": 120,
            }
        )
        self.assertEqual(detailed["cachedInputTokens"], 70)
        self.assertEqual(detailed["cacheWriteTokens"], 9)
        self.assertEqual(detailed["reasoningOutputTokens"], 12)
        self.assertEqual(detailed["totalTokens"], 120)
        self.assertIsNone(web2api._normalized_usage({}))

        capture = web2api._SSEUsageCapture()
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            },
        }
        encoded = f"data: {json.dumps(terminal)}\r\n\r\ndata: [DONE]\r\n\r\n".encode()
        for chunk in (encoded[:9], encoded[9:31], encoded[31:73], encoded[73:]):
            capture.feed(chunk)
        capture.finish()
        self.assertEqual(capture.usage["totalTokens"], 15)
        self.assertTrue(capture.done)
        self.assertFalse(capture.failed)

    def test_store_batches_flushes_atomically_and_resets(self):
        store = web2api.UsageStatsStore(self.stats_path)
        route = {
            "source": "account_pool",
            "sourceKind": "account",
            "sourceRecordId": "account-1",
            "accountId": "account-1",
            "providerId": "",
            "requestedModel": "gpt-test",
            "routedModel": "gpt-test",
            "requestClassification": "unclassified",
            "configuredForSubagents": False,
        }
        store.record(route, {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5})
        store.record(route, None, failed=True)
        self.assertFalse(self.stats_path.exists())
        pending = store.snapshot()
        self.assertEqual(pending["totals"]["requestCount"], 2)
        self.assertEqual(pending["totals"]["failureCount"], 1)
        self.assertEqual(pending["totals"]["usageMissingCount"], 1)
        self.assertEqual(pending["totals"]["totalTokens"], 5)
        self.assertTrue(pending["pendingFlush"])
        self.assertEqual(len(pending["recentRequests"]), 2)
        self.assertGreaterEqual(
            pending["recentRequests"][0]["timestamp"],
            pending["recentRequests"][1]["timestamp"],
        )
        self.assertEqual(pending["byAgentRole"][0]["agentRole"], "unclassified")
        self.assertEqual(pending["dailyTotals"][0]["totalTokens"], 5)
        self.assertEqual(
            pending["dailyTotals"][0]["byAgentRole"]["unclassified"]["requestCount"],
            2,
        )

        self.assertTrue(store.flush(force=True))
        self.assertTrue(self.stats_path.is_file())
        persisted = web2api.UsageStatsStore(self.stats_path).snapshot()
        self.assertEqual(persisted["byAccount"][0]["accountId"], "account-1")
        self.assertEqual(persisted["byModel"][0]["routedModel"], "gpt-test")
        self.assertEqual(persisted["totals"]["totalTokens"], 5)
        self.assertFalse(any(self.stats_path.parent.glob("*.tmp")))

        reset = store.reset()
        self.assertEqual(reset["totals"]["requestCount"], 0)
        self.assertEqual(reset["days"], {})

    def test_usage_day_rolls_over_at_beijing_midnight(self):
        self.assertEqual(
            web2api._beijing_usage_day("2026-08-12T15:59:59Z"),
            "2026-08-12",
        )
        self.assertEqual(
            web2api._beijing_usage_day("2026-08-12T16:00:00Z"),
            "2026-08-13",
        )
        document = web2api.UsageStatsStore._safe_document(
            {
                "days": {},
                "recentRequests": [
                    {
                        "timestamp": "2026-08-12T16:00:00Z",
                        "requestedModel": "gpt-test",
                    }
                ],
            }
        )
        self.assertEqual(document["recentRequests"][0]["date"], "2026-08-13")
        self.assertEqual(document["timeZone"], "Asia/Shanghai")
        self.assertEqual(document["dayBoundary"], "00:00")

    def test_start_closes_server_and_clears_state_when_thread_start_fails(self):
        manager = web2api.Web2APIManager()
        settings = {"web2api": core._default_web2api_settings()}

        class FakeServer:
            def __init__(self):
                self.closed = False
                self.shutdown_called = False

            def serve_forever(self, **_kwargs):
                return None

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.closed = True

        class FailingThread:
            joined = False

            def start(self):
                raise RuntimeError("thread unavailable")

            def join(self, timeout=None):
                self.joined = timeout

        server = FakeServer()
        thread = FailingThread()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(web2api, "GatewayServer", return_value=server),
            patch.object(web2api.threading, "Thread", return_value=thread),
            patch.object(core, "now_iso", return_value="2026-08-12T12:00:00+08:00"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.start()

        self.assertTrue(server.closed)
        self.assertFalse(server.shutdown_called)
        self.assertFalse(thread.joined)
        self.assertIsNone(manager.server)
        self.assertIsNone(manager.thread)
        self.assertIsNone(manager.started_at)
        self.assertEqual(manager.last_error, "本地 API 启动未完成（RuntimeError）")
        self.assertNotIn("thread unavailable", manager.last_error)

    def test_start_shuts_down_thread_and_clears_state_when_settings_save_fails(self):
        manager = web2api.Web2APIManager()
        settings = {"web2api": core._default_web2api_settings()}

        class FakeServer:
            def __init__(self):
                self.closed = False
                self.shutdown_called = False

            def serve_forever(self, **_kwargs):
                return None

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.closed = True

        class StartedThread:
            def __init__(self):
                self.started = False
                self.join_timeout = None

            def start(self):
                self.started = True

            def join(self, timeout=None):
                self.join_timeout = timeout

        server = FakeServer()
        thread = StartedThread()
        with (
            patch.object(core, "load_settings", return_value=settings),
            patch.object(core, "service_secret_configured", return_value=True),
            patch.object(core, "save_settings", side_effect=OSError("settings unavailable")),
            patch.object(web2api, "GatewayServer", return_value=server),
            patch.object(web2api.threading, "Thread", return_value=thread),
            patch.object(core, "now_iso", return_value="2026-08-12T12:00:00+08:00"),
        ):
            with self.assertRaisesRegex(OSError, "settings unavailable"):
                manager.start()

        self.assertTrue(thread.started)
        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.closed)
        self.assertEqual(thread.join_timeout, 3)
        self.assertIsNone(manager.server)
        self.assertIsNone(manager.thread)
        self.assertIsNone(manager.started_at)
        self.assertEqual(manager.last_error, "本地 API 启动未完成（OSError）")
        self.assertNotIn("settings unavailable", manager.last_error)

    def test_stream_records_actual_account_and_terminal_usage(self):
        manager = web2api.Web2APIManager()
        manager.usage_stats = web2api.UsageStatsStore(self.stats_path)
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 13, "output_tokens": 8, "total_tokens": 21},
            },
        }
        body = f"data: {json.dumps(terminal)}\n\ndata: [DONE]\n\n".encode()
        response = FakeResponse([body[:17], body[17:]])
        account = {"id": "actual-account"}
        settings = {"subagentRouting": {"routes": {}}}
        with (
            patch.object(core, "resolve_model_route", return_value=None),
            patch.object(core, "load_settings", return_value=settings),
            patch.object(manager, "_open_upstream", return_value=(response, account, {})),
        ):
            stream = manager.stream(
                "/v1/responses",
                {"model": "gpt-test", "input": "hello", "stream": True},
            )
            self.assertEqual(b"".join(stream["chunks"]), body)

        snapshot = manager.usage_snapshot()
        self.assertEqual(snapshot["totals"]["requestCount"], 1)
        self.assertEqual(snapshot["totals"]["totalTokens"], 21)
        route = next(iter(next(iter(snapshot["days"].values()))["routes"].values()))
        self.assertEqual(route["accountId"], "actual-account")
        self.assertEqual(route["source"], "account_pool")
        self.assertEqual(route["requestClassification"], "unclassified")
        self.assertFalse(route["configuredForSubagents"])
        manager.usage_stats.flush(force=True)

    def test_configured_subagent_route_is_labeled_without_claiming_request_identity(self):
        manager = web2api.Web2APIManager()
        route = {
            "key": "account:one/gpt-test",
            "id": "gpt-test",
            "sourceKind": "account",
            "sourceRecordId": "one",
        }
        settings = {
            "subagentRouting": {
                "routes": {"simple": {"models": ["account:one/gpt-test"]}}
            }
        }
        with patch.object(core, "load_settings", return_value=settings):
            context = manager._usage_route_context(
                {"model": "gpt-test"},
                "gpt-test",
                route=route,
                account={"id": "one"},
            )
        self.assertTrue(context["configuredForSubagents"])
        self.assertEqual(context["requestClassification"], "configured_subagent_model")
        self.assertNotEqual(context["requestClassification"], "explicit_subagent")
        self.assertEqual(context["agentRole"], "unclassified")

    def test_explicit_gateway_metadata_classifies_main_and_subagent_usage(self):
        manager = web2api.Web2APIManager()
        manager.usage_stats = web2api.UsageStatsStore(self.stats_path)
        main = manager._usage_route_context(
            {"metadata": {"agent_manager_source": "main_agent"}},
            "gpt-main",
            account={"id": "account-main"},
        )
        child = manager._usage_route_context(
            {"metadata": {"agent_manager_source": "subagent"}},
            "gpt-child",
            account={"id": "account-child"},
        )
        manager.usage_stats.record(main, {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7})
        manager.usage_stats.record(child, {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4})
        snapshot = manager.usage_snapshot()
        by_role = {item["agentRole"]: item for item in snapshot["byAgentRole"]}
        self.assertEqual(by_role["mainAgent"]["totalTokens"], 7)
        self.assertEqual(by_role["subagent"]["totalTokens"], 4)
        daily_roles = snapshot["dailyTotals"][0]["byAgentRole"]
        self.assertEqual(daily_roles["mainAgent"]["requestCount"], 1)
        self.assertEqual(daily_roles["subagent"]["requestCount"], 1)

    def test_private_managed_model_alias_is_explicit_subagent_evidence(self):
        manager = web2api.Web2APIManager()
        context = manager._usage_route_context(
            {"model": "cam-agent-hard-1-model-deadbeef"},
            "cam-agent-hard-1-model-deadbeef",
            route={
                "key": "account:child::model",
                "id": "model",
                "sourceKind": "account",
                "sourceRecordId": "child",
                "subagentAlias": True,
                "subagentLevel": "hard",
            },
            account={"id": "child"},
        )
        self.assertEqual(context["requestClassification"], "explicit_subagent")
        self.assertEqual(context["agentRole"], "subagent")

    def test_schema_one_usage_history_remains_readable(self):
        day = "2026-08-10"
        route = {
            "source": "account_pool",
            "sourceKind": "account",
            "sourceRecordId": "account-legacy",
            "accountId": "account-legacy",
            "providerId": "",
            "requestedModel": "gpt-legacy",
            "routedModel": "gpt-legacy",
            "routeKey": "legacy",
            "requestClassification": "explicit_main_agent",
            "configuredForSubagents": False,
            "requestCount": 1,
            "failureCount": 0,
            "usageReportedCount": 1,
            "usageMissingCount": 0,
            "inputTokens": 2,
            "outputTokens": 1,
            "totalTokens": 3,
        }
        self.stats_path.parent.mkdir(parents=True)
        self.stats_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "updatedAt": "2026-08-10T00:00:00+00:00",
                    "totals": {},
                    "days": {day: {"totals": {}, "routes": {"legacy": route}}},
                }
            ),
            encoding="utf-8",
        )
        snapshot = web2api.UsageStatsStore(self.stats_path).snapshot()
        self.assertEqual(snapshot["schemaVersion"], web2api.USAGE_STATS_SCHEMA_VERSION)
        self.assertEqual(snapshot["totals"]["totalTokens"], 3)
        self.assertEqual(snapshot["byAgentRole"][0]["agentRole"], "mainAgent")
        self.assertEqual(snapshot["dailyTotals"][0]["date"], day)

    def test_interrupted_stream_counts_failure_without_estimating_tokens(self):
        manager = web2api.Web2APIManager()
        manager.usage_stats = web2api.UsageStatsStore(self.stats_path)
        context = manager._usage_route_context({}, "gpt-test", account={"id": "account-2"})

        def broken_chunks():
            yield b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"x\"}\n\n"
            raise OSError("connection lost")

        with self.assertRaises(OSError):
            list(manager._tracked_chunks(broken_chunks(), context))
        snapshot = manager.usage_snapshot()
        self.assertEqual(snapshot["totals"]["requestCount"], 1)
        self.assertEqual(snapshot["totals"]["failureCount"], 1)
        self.assertEqual(snapshot["totals"]["usageMissingCount"], 1)
        self.assertEqual(snapshot["totals"]["totalTokens"], 0)
        manager.usage_stats.flush(force=True)

    def test_codex_session_usage_uses_official_role_and_deduplicates_repeated_state(self):
        codex_home = Path(self.temp.name) / "codex-home"
        state_dir = Path(self.temp.name) / "state"
        session_file = codex_home / "sessions" / "2026" / "08" / "rollout-test.jsonl"
        session_file.parent.mkdir(parents=True)
        events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "type": "session_meta",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "subagent",
                    "source": {"subagent": {"thread_spawn": {}}},
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-11T01:00:01Z",
                "type": "turn_context",
                "payload": {"type": "turn_context", "model": "gpt-test"},
            },
        ]
        token_event = {
            "timestamp": "2026-08-11T01:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 6,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 14,
                    },
                    "total_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 6,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 2,
                        "total_tokens": 14,
                    },
                },
            },
        }
        events.extend([token_event, token_event])
        session_file.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
        with patch.object(core, "CODEX_HOME", codex_home), patch.object(core, "STATE_DIR", state_dir):
            snapshot = web2api.codex_session_usage_snapshot()
        self.assertEqual(snapshot["totals"]["requestCount"], 1)
        self.assertEqual(snapshot["totals"]["totalTokens"], 14)
        self.assertEqual(snapshot["totals"]["cachedInputTokens"], 6)
        self.assertEqual(snapshot["totals"]["reasoningOutputTokens"], 2)
        self.assertEqual(snapshot["records"][0]["agentRole"], "subagent")
        self.assertEqual(snapshot["records"][0]["model"], "gpt-test")

    def test_account_attribution_combines_direct_main_and_actual_gateway_account(self):
        gateway = {
            "updatedAt": "2026-08-11T02:00:00Z",
            "days": {
                "2026-08-11": {
                    "routes": {
                        "child": {
                            "accountId": "account-child",
                            "providerId": "",
                            "routedModel": "gpt-child",
                            "agentRole": "subagent",
                            "requestCount": 1,
                            "usageReportedCount": 1,
                            "inputTokens": 7,
                            "outputTokens": 3,
                            "totalTokens": 10,
                            "lastSeenAt": "2026-08-11T01:05:00Z",
                        }
                    }
                }
            },
            "recentRequests": [],
        }
        sessions = {
            "updatedAt": "2026-08-11T02:00:01Z",
            "records": [
                {
                    "date": "2026-08-11",
                    "provider": "openai",
                    "accountId": "account-main",
                    "model": "gpt-main",
                    "agentRole": "mainAgent",
                    "requestCount": 1,
                    "usageReportedCount": 1,
                    "inputTokens": 11,
                    "outputTokens": 4,
                    "totalTokens": 15,
                    "lastSeenAt": "2026-08-11T01:06:00Z",
                },
                {
                    "date": "2026-08-11",
                    "provider": "cam_aggregate",
                    "accountId": "account-main",
                    "model": "gpt-child",
                    "agentRole": "subagent",
                    "requestCount": 1,
                    "usageReportedCount": 1,
                    "totalTokens": 10,
                },
            ],
        }
        result = web2api.account_attribution_snapshot(gateway, sessions)
        self.assertEqual(result["totals"]["totalTokens"], 25)
        self.assertEqual(
            {item.get("accountId") for item in result["records"]},
            {"account-main", "account-child"},
        )
        self.assertEqual(result["recentRequests"][0]["accountId"], "account-main")

    def test_direct_main_session_uses_known_activation_timeline(self):
        session_file = Path(self.temp.name) / "rollout-main.jsonl"
        events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "user",
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-11T01:00:01Z",
                "payload": {"type": "turn_context", "model": "gpt-main"},
            },
            {
                "timestamp": "2026-08-11T01:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                            "total_tokens": 7,
                        },
                        "total_token_usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                            "total_tokens": 7,
                        },
                    },
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
        parsed = web2api._parse_codex_session_usage_file(
            session_file,
            [(web2api._timestamp_epoch("2026-08-11T00:59:00Z"), "account-main")],
        )
        records = parsed["records"]
        self.assertEqual(records[0]["accountId"], "account-main")
        self.assertEqual(records[0]["lastSeenAt"], "2026-08-11T01:00:02Z")
        self.assertEqual(parsed["recentRequests"][0]["accountId"], "account-main")

    def test_codex_session_usage_incrementally_parses_only_appended_events(self):
        codex_home = Path(self.temp.name) / "codex-home"
        state_dir = Path(self.temp.name) / "state"
        session_file = codex_home / "sessions" / "2026" / "08" / "rollout-incremental.jsonl"
        session_file.parent.mkdir(parents=True)
        initial_events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "user",
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-11T01:00:01Z",
                "payload": {"type": "turn_context", "model": "gpt-main"},
            },
            {
                "timestamp": "2026-08-11T01:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                        "total_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                    },
                },
            },
        ]
        initial_text = "\n".join(json.dumps(item) for item in initial_events)
        session_file.write_text(initial_text, encoding="utf-8")
        with patch.object(core, "CODEX_HOME", codex_home), patch.object(core, "STATE_DIR", state_dir):
            first = web2api.codex_session_usage_snapshot()
            old_size = session_file.stat().st_size
            appended = {
                "timestamp": "2026-08-11T01:00:03Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 6, "output_tokens": 2, "total_tokens": 8},
                        "total_token_usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
                    },
                },
            }
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write("\n" + json.dumps(appended))
            with patch.object(
                web2api,
                "_parse_codex_session_usage_file",
                wraps=web2api._parse_codex_session_usage_file,
            ) as parse_usage:
                second = web2api.codex_session_usage_snapshot()

        self.assertEqual(first["totals"]["requestCount"], 1)
        self.assertEqual(second["totals"]["requestCount"], 2)
        self.assertEqual(second["totals"]["totalTokens"], 15)
        self.assertEqual(parse_usage.call_count, 1)
        self.assertEqual(parse_usage.call_args.kwargs["start_offset"], old_size)

    def test_codex_session_usage_migrates_schema_six_without_double_counting(self):
        codex_home = Path(self.temp.name) / "codex-home"
        state_dir = Path(self.temp.name) / "state"
        sessions_root = codex_home / "sessions"
        session_file = sessions_root / "2026" / "08" / "rollout-migrate.jsonl"
        session_file.parent.mkdir(parents=True)
        base_events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "user",
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-11T01:00:01Z",
                "payload": {"type": "turn_context", "model": "gpt-main"},
            },
            {
                "timestamp": "2026-08-11T01:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                        "total_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                    },
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(item) for item in base_events) + "\n", encoding="utf-8")
        old_size = session_file.stat().st_size
        appended = {
            "timestamp": "2026-08-11T01:00:03Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 6, "output_tokens": 2, "total_tokens": 8},
                    "total_token_usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
                },
            },
        }
        with session_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(appended))
        relative = session_file.relative_to(sessions_root).as_posix()
        cache_key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]
        cache_path = state_dir / web2api.CODEX_SESSION_USAGE_CACHE_FILE
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 6,
                    "files": {
                        cache_key: {
                            "size": old_size,
                            "mtimeNs": 0,
                            "records": [
                                {
                                    "date": "2026-08-11",
                                    "source": "codex_session",
                                    "sourceKind": "codex_session",
                                    "originator": "Codex Desktop",
                                    "provider": "openai",
                                    "accountId": "",
                                    "model": "gpt-main",
                                    "routedModel": "gpt-main",
                                    "agentRole": "mainAgent",
                                    "requestClassification": "explicit_main_agent",
                                    "firstSeenAt": "2026-08-11T01:00:02Z",
                                    "lastSeenAt": "2026-08-11T01:00:02Z",
                                    "requestCount": 1,
                                    "failureCount": 0,
                                    "usageReportedCount": 1,
                                    "usageMissingCount": 0,
                                    "inputTokens": 5,
                                    "cachedInputTokens": 0,
                                    "cacheWriteTokens": 0,
                                    "outputTokens": 2,
                                    "reasoningOutputTokens": 0,
                                    "totalTokens": 7,
                                }
                            ],
                            "recentRequests": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch.object(core, "CODEX_HOME", codex_home), patch.object(core, "STATE_DIR", state_dir):
            snapshot = web2api.codex_session_usage_snapshot()

        self.assertEqual(snapshot["totals"]["requestCount"], 2)
        self.assertEqual(snapshot["totals"]["totalTokens"], 15)
        migrated = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schemaVersion"], web2api.CODEX_SESSION_USAGE_CACHE_SCHEMA)
        self.assertEqual(migrated["files"][cache_key]["processedOffset"], session_file.stat().st_size)

    def test_codex_session_parser_skips_oversized_unrelated_jsonl_line(self):
        session_file = Path(self.temp.name) / "rollout-large-line.jsonl"
        events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "subagent",
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {"timestamp": "2026-08-11T01:00:01Z", "payload": {"type": "turn_context", "model": "gpt-main"}},
            {"type": "response_item", "payload": {"type": "tool_output", "text": "x" * 8_000}},
            {
                "timestamp": "2026-08-11T01:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                        "total_token_usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                    },
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
        with patch.object(web2api, "CODEX_SESSION_MAX_JSONL_LINE_BYTES", 512):
            parsed = web2api._parse_codex_session_usage_file(session_file)
        self.assertEqual(parsed["records"][0]["totalTokens"], 7)
        self.assertEqual(parsed["parserState"]["model"], "gpt-main")
        self.assertEqual(parsed["offset"], session_file.stat().st_size)

    def test_codex_session_usage_reports_pending_bytes_at_increment_scan_cap(self):
        codex_home = Path(self.temp.name) / "codex-home"
        state_dir = Path(self.temp.name) / "state"
        session_file = codex_home / "sessions" / "2026" / "08" / "rollout-pending.jsonl"
        session_file.parent.mkdir(parents=True)
        events = [
            {
                "timestamp": "2026-08-11T01:00:00Z",
                "payload": {
                    "type": "session_meta",
                    "thread_source": "user",
                    "originator": "Codex Desktop",
                    "model_provider": "openai",
                },
            },
            {"timestamp": "2026-08-11T01:00:01Z", "payload": {"type": "turn_context", "model": "gpt-main"}},
        ]
        session_file.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
        with patch.object(core, "CODEX_HOME", codex_home), patch.object(core, "STATE_DIR", state_dir):
            web2api.codex_session_usage_snapshot()
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write("\n" + json.dumps({"payload": {"type": "tool_output", "text": "x" * 4_000}}))
            with patch.object(web2api, "CODEX_SESSION_INCREMENT_MAX_BYTES", 256):
                snapshot = web2api.codex_session_usage_snapshot()
        self.assertEqual(snapshot["coverage"]["partialFiles"], 1)
        self.assertGreater(snapshot["coverage"]["pendingBytes"], 0)
        self.assertEqual(snapshot["coverage"]["boundedScanBytesPerFile"], 256)

    def test_codex_session_role_fallback_does_not_mistake_user_fork_for_subagent(self):
        self.assertEqual(
            web2api._codex_session_role(
                {"thread_source": "user", "forked_from_id": "parent-thread"}
            ),
            "mainAgent",
        )
        self.assertEqual(
            web2api._codex_session_role(
                {
                    "parent_thread_id": "parent-thread",
                    "agent_role": "cam_hard_1",
                }
            ),
            "subagent",
        )
        self.assertEqual(
            web2api._codex_session_role(
                {
                    "forked_from_id": "parent-thread",
                    "agent_role": "cam_hard_1",
                }
            ),
            "subagent",
        )

    def test_usage_accepts_official_cache_write_input_token_field(self):
        usage = web2api._normalized_usage(
            {
                "input_tokens": 20,
                "output_tokens": 3,
                "total_tokens": 23,
                "cache_write_input_tokens": 7,
            }
        )
        self.assertEqual(usage["cacheWriteTokens"], 7)


if __name__ == "__main__":
    unittest.main()
