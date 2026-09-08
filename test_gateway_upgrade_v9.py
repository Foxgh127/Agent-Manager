import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import web2api_service as web2api


def sse(*events):
    return b"".join(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
        for event in events
    )


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


class CapturingWriter:
    def __init__(self, failure=None):
        self.failure = failure
        self.body = bytearray()

    def write(self, value):
        if self.failure is not None:
            raise self.failure
        self.body.extend(value)
        return len(value)

    def flush(self):
        return None


class GatewayUpgradeV9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(core, "STATE_DIR", Path(self.temp.name) / "state")
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    def _handler(self, manager, writer):
        handler = object.__new__(web2api.GatewayHandler)
        handler.server = type("Server", (), {"manager": manager})()
        handler.path = "/v1/responses"
        handler.wfile = writer
        handler.close_connection = False
        handler.sent_status = []
        handler.send_response = handler.sent_status.append
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        return handler

    def _tracked_response_chunks(self, manager, response):
        capture = web2api._SSEUsageCapture()
        return manager._tracked_chunks(
            manager._response_stream_chunks(response, capture),
            {},
            capture,
            observe_output=False,
        )

    def test_chat_custom_tool_request_and_history_round_trip(self):
        payload = {
            "model": "gpt-test",
            "tools": [
                {
                    "type": "freeform",
                    "custom": {
                        "name": "exec",
                        "description": "Run a command",
                        "format": {"type": "text"},
                    },
                }
            ],
            "tool_choice": {"type": "custom", "custom": {"name": "exec"}},
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-exec",
                            "type": "function",
                            "function": {
                                "name": "exec",
                                "arguments": json.dumps({"input": "echo 你好"}, ensure_ascii=False),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-exec", "content": "完成"},
            ],
        }

        converted = web2api._chat_input(payload)

        self.assertEqual(
            converted["tools"],
            [
                {
                    "type": "custom",
                    "name": "exec",
                    "description": "Run a command",
                    "format": {"type": "text"},
                }
            ],
        )
        self.assertEqual(converted["tool_choice"], {"type": "custom", "name": "exec"})
        custom_call = next(item for item in converted["input"] if item.get("type") == "custom_tool_call")
        custom_output = next(item for item in converted["input"] if item.get("type") == "custom_tool_call_output")
        self.assertEqual(custom_call["input"], "echo 你好")
        self.assertEqual(custom_output, {"type": "custom_tool_call_output", "call_id": "call-exec", "output": "完成"})

        responses_payload = {
            "model": "gpt-test",
            "input": [{"type": "custom_tool_call_output", "call_id": "call-exec", "output": "完成"}],
            "tools": [{"type": "custom", "name": "exec", "format": {"type": "text"}}],
        }
        direct = web2api._responses_input(responses_payload)
        self.assertEqual(direct["input"], responses_payload["input"])
        self.assertEqual(direct["tools"], responses_payload["tools"])

    def test_chat_rejects_tool_types_it_cannot_round_trip(self):
        with self.assertRaisesRegex(web2api.GatewayError, "/v1/responses") as raised:
            web2api._chat_input(
                {
                    "messages": [{"role": "user", "content": "search"}],
                    "tools": [{"type": "mcp", "server_label": "private"}],
                }
            )
        self.assertEqual(raised.exception.status, 400)

        with self.assertRaises(web2api.GatewayError) as missing_tools:
            web2api._chat_input(
                {
                    "messages": [{"role": "user", "content": "run"}],
                    "tools": [],
                    "tool_choice": {"type": "function", "function": {"name": "run"}},
                }
            )
        self.assertEqual(missing_tools.exception.status, 400)

    def test_completed_response_uses_authoritative_done_custom_item(self):
        completed_item = {
            "type": "custom_tool_call",
            "id": "item-exec",
            "call_id": "call-exec",
            "name": "exec",
            "input": "dir C:\\\\Temp",
        }
        body = sse(
            {"type": "response.output_item.done", "output_index": 0, "item": completed_item},
            {
                "type": "response.completed",
                "response": {"id": "resp-custom", "status": "completed", "model": "gpt-test", "output": []},
            },
        )

        response = web2api._completed_response(body)
        completion = web2api._chat_completion(response, "gpt-test")
        call = completion["choices"][0]["message"]["tool_calls"][0]

        self.assertEqual(response["output"], [completed_item])
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["x_openai_tool_type"], "custom")
        self.assertEqual(call["function"]["name"], "exec")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"input": "dir C:\\\\Temp"})
        self.assertEqual(completion["choices"][0]["finish_reason"], "tool_calls")

    def test_streamed_custom_tools_emit_once_at_done_with_late_names(self):
        body = sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "custom_tool_call", "id": "item-a", "call_id": "call-a"},
            },
            {"type": "response.custom_tool_call_input.delta", "item_id": "item-a", "delta": "echo "},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "custom_tool_call", "id": "item-b", "call_id": "call-b", "name": "python"},
            },
            {"type": "response.custom_tool_call_input.delta", "item_id": "item-b", "delta": "print(1)"},
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "item-a",
                "call_id": "call-a",
                "name": "exec",
                "input": "echo hello",
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "custom_tool_call",
                    "id": "item-b",
                    "call_id": "call-b",
                    "name": "python",
                    "input": "print(1)",
                },
            },
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        )

        events = web2api._sse_events(web2api._chat_sse(body, "gpt-test"))
        calls = [
            event["choices"][0]["delta"]["tool_calls"][0]
            for event in events
            if event["choices"][0]["delta"].get("tool_calls")
        ]

        self.assertEqual([call["index"] for call in calls], [0, 1])
        self.assertEqual([call["function"]["name"] for call in calls], ["exec", "python"])
        self.assertEqual(
            [json.loads(call["function"]["arguments"])["input"] for call in calls],
            ["echo hello", "print(1)"],
        )
        self.assertTrue(all(call["x_openai_tool_type"] == "custom" for call in calls))
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_downstream_disconnect_closes_upstream_and_counts_cancellation_once(self):
        manager = web2api.Web2APIManager()
        response = FakeResponse(
            [sse({"type": "response.output_text.delta", "delta": "started"})]
        )
        handler = self._handler(manager, CapturingWriter(BrokenPipeError("client closed")))

        with patch.object(manager, "_record_usage") as record:
            handler._stream(
                self._tracked_response_chunks(manager, response),
                "text/event-stream; charset=utf-8",
            )

        self.assertTrue(response.closed)
        self.assertEqual(manager.client_cancelled_count, 1)
        self.assertIsNone(manager.last_error)
        record.assert_called_once()
        self.assertFalse(record.call_args.kwargs["failed"])

    def test_preoutput_truncation_is_an_http_error_and_closes_upstream(self):
        manager = web2api.Web2APIManager()
        response = FakeResponse(
            [sse({"type": "response.created", "response": {"id": "resp-created"}})]
        )
        handler = self._handler(manager, CapturingWriter())

        with (
            patch.object(manager, "_record_usage") as record,
            self.assertRaisesRegex(web2api.GatewayError, "终态"),
        ):
            handler._stream(
                self._tracked_response_chunks(manager, response),
                "text/event-stream; charset=utf-8",
            )

        self.assertEqual(handler.sent_status, [])
        self.assertTrue(response.closed)
        record.assert_called_once()
        self.assertTrue(record.call_args.kwargs["failed"])

    def test_postcommit_truncation_emits_typed_failure_without_done_or_replay(self):
        manager = web2api.Web2APIManager()
        response = FakeResponse(
            [sse({"type": "response.output_text.delta", "delta": "partial"})]
        )
        writer = CapturingWriter()
        handler = self._handler(manager, writer)

        with patch.object(manager, "_record_usage") as record:
            handler._stream(
                self._tracked_response_chunks(manager, response),
                "text/event-stream; charset=utf-8",
            )

        events = web2api._sse_events(bytes(writer.body))
        self.assertEqual(handler.sent_status, [200])
        self.assertEqual(events[0]["type"], "response.output_text.delta")
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn(b"data: [DONE]", writer.body)
        self.assertTrue(response.closed)
        record.assert_called_once()
        self.assertTrue(record.call_args.kwargs["failed"])

    def test_responses_stream_requires_terminal_but_has_no_total_byte_cap(self):
        payload = "x" * 1024
        response = FakeResponse(
            [
                sse({"type": "response.output_text.delta", "delta": payload}),
                sse({"type": "response.completed", "response": {"status": "completed"}}),
            ]
        )
        capture = web2api._SSEUsageCapture()

        with patch.object(web2api, "MAX_RESPONSE_BYTES", 16):
            body = b"".join(web2api.Web2APIManager._response_stream_chunks(response, capture))

        self.assertIn(payload.encode(), body)
        self.assertTrue(capture.response_terminal)
        self.assertTrue(response.closed)

    def test_failed_terminal_is_never_converted_to_a_successful_chat_completion(self):
        body = sse(
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {"message": "rate limit exceeded", "code": "rate_limit_exceeded"},
                },
            }
        )

        with self.assertRaisesRegex(web2api.GatewayError, "rate limit"):
            web2api._completed_response(body)

    def test_expired_account_remains_candidate_for_official_refresh_transaction(self):
        manager = web2api.Web2APIManager()
        account = {
            "id": "expired-but-refreshable",
            "label": "Refreshable",
            "authMode": "chatgpt",
            "sourceType": "codex_auth",
            "codexCompatible": True,
            "quotaOnly": False,
            "models": ["gpt-test"],
            "tokenExpiresAt": "2000-01-01T00:00:00Z",
        }
        settings = {
            "accounts": [account],
            "web2api": {"accountIds": [account["id"]], "routing": "ordered"},
        }

        with patch.object(core, "load_settings", return_value=settings):
            candidates = manager._accounts(requested_model="gpt-test")

        self.assertEqual([item["id"] for item in candidates], [account["id"]])

    def test_upstream_uses_account_refresh_transaction_and_workspace_identity(self):
        manager = web2api.Web2APIManager()
        account = {"id": "refreshable", "label": "Refreshable"}
        response = FakeResponse(
            [sse({"type": "response.completed", "response": {"status": "completed"}})],
            headers={"Content-Type": "text/event-stream"},
        )
        requests = []

        def open_request(request, **_kwargs):
            requests.append(request)
            return response

        with (
            patch.object(manager, "_accounts", return_value=[account]),
            patch.object(
                core,
                "_account_chatgpt_credentials",
                return_value={"accessToken": "renewed-token", "accountId": "workspace-boundary"},
            ) as credentials,
            patch.object(core, "_load_account_snapshot") as legacy_snapshot,
            patch.object(core, "_open_same_origin_request", side_effect=open_request),
        ):
            opened, routed_account, _headers = manager._open_upstream(
                {"model": "gpt-test", "input": "hello"}
            )

        self.assertIs(opened, response)
        self.assertIs(routed_account, account)
        credentials.assert_called_once_with("refreshable")
        legacy_snapshot.assert_not_called()
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer renewed-token")
        self.assertEqual(requests[0].get_header("Chatgpt-account-id"), "workspace-boundary")

    def test_refresh_failure_can_fall_through_before_any_inference_request(self):
        manager = web2api.Web2APIManager()
        accounts = [
            {"id": "expired", "label": "Expired"},
            {"id": "healthy", "label": "Healthy"},
        ]
        response = FakeResponse(
            [sse({"type": "response.completed", "response": {"status": "completed"}})]
        )

        def credentials(account_id):
            if account_id == "expired":
                raise core.ManagerError("refresh unavailable")
            return {"accessToken": "healthy-token", "accountId": "healthy-workspace"}

        with (
            patch.object(manager, "_accounts", return_value=accounts),
            patch.object(core, "_account_chatgpt_credentials", side_effect=credentials) as refreshed,
            patch.object(core, "_open_same_origin_request", return_value=response) as opened,
        ):
            _response, account, _headers = manager._open_upstream(
                {"model": "gpt-test", "input": "hello"}
            )

        self.assertEqual(account["id"], "healthy")
        self.assertEqual(refreshed.call_count, 2)
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
