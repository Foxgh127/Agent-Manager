from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_manager_core as core
import claude_manager_service as claude
import radar_service as radar
import relay_portal_service as relay
import web2api_service as web2api
import test_claude_manager_service as claude_fixtures


def sse(*events):
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


class GatewayRegressions(unittest.TestCase):
    def test_multiple_tool_calls_keep_contiguous_indices_and_matching_deltas(self):
        events = []
        for i in range(2):
            events.extend([
                {"type": "response.output_item.added", "item": {"type": "function_call", "id": f"item-{i}", "call_id": f"call-{i}", "name": "tool"}},
                {"type": "response.function_call_arguments.delta", "item_id": f"item-{i}", "delta": "{}"},
            ])
        events.append({"type": "response.completed", "response": {"status": "completed"}})
        chunks = web2api._sse_events(web2api._chat_sse(sse(*events), "test"))
        indices = [event["choices"][0]["delta"]["tool_calls"][0]["index"] for event in chunks if event["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(indices, [0, 0, 1, 1])
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_truncated_chat_stream_does_not_emit_fake_done(self):
        body = sse({"type": "response.output_text.delta", "delta": "partial"})
        iterator = web2api.Web2APIManager._chat_stream_chunks(io.BytesIO(body), "test")
        self.assertIn(b"partial", next(iterator))
        with self.assertRaisesRegex(web2api.GatewayError, "提前结束"):
            next(iterator)

    def test_incomplete_response_preserves_text_and_length_reason(self):
        response = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": "partial"}
        body = sse({"type": "response.incomplete", "response": response})
        parsed = web2api._completed_response(body)
        self.assertEqual(parsed, response)
        self.assertEqual(web2api._chat_completion(parsed, "test")["choices"][0]["finish_reason"], "length")
        self.assertEqual(web2api._sse_events(web2api._chat_sse(body, "test"))[-1]["choices"][0]["finish_reason"], "length")

    def test_malformed_tool_call_is_a_client_error(self):
        with self.assertRaises(web2api.GatewayError) as error:
            web2api._chat_input({"messages": [{"role": "assistant", "tool_calls": [None]}]})
        self.assertEqual(error.exception.status, 400)

    def test_unterminated_usage_event_is_counted(self):
        body = sse({"type": "response.completed", "response": {"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}}).rstrip()
        usage, failed = web2api._inspect_usage_body(body)
        self.assertEqual(usage["totalTokens"], 6)
        self.assertFalse(failed)

    def test_clean_eof_without_terminal_is_recorded_as_failure(self):
        manager = object.__new__(web2api.Web2APIManager)
        with patch.object(manager, "_record_usage") as record:
            list(manager._tracked_chunks([sse({"type": "response.output_text.delta", "delta": "partial"})], {}))
        self.assertTrue(record.call_args.kwargs["failed"])


class ClaudeConfigRegressions(unittest.TestCase):
    setUp = claude_fixtures.ClaudeManagerServiceTests.setUp
    tearDown = claude_fixtures.ClaudeManagerServiceTests.tearDown
    save_demo = claude_fixtures.ClaudeManagerServiceTests.save_demo

    def test_corrupt_existing_config_is_preserved_during_apply(self):
        self.save_demo()
        paths = claude.configuration_paths()
        for content in ('{"unfinished":', '[]'):
            paths["normalConfig"].write_text(content, encoding="utf-8")
            with self.assertRaises(core.ManagerError):
                claude.apply_profile("work_api")
            self.assertEqual(paths["normalConfig"].read_text(encoding="utf-8"), content)
            self.assertFalse(claude.TRANSACTION_FILE.exists())

    def test_bom_config_preserves_existing_preferences(self):
        path = claude.configuration_paths()["normalConfig"]
        path.write_text('{"keep": "preference"}', encoding="utf-8-sig")
        claude._write_mode(path, "3p")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["keep"], "preference")

    def test_invalid_profile_list_reports_validation_instead_of_crashing(self):
        core.atomic_write_json(claude.REGISTRY_FILE, {"profiles": [None]})
        with self.assertRaises(core.ManagerError):
            claude.list_profiles()

    def test_api_key_with_control_character_is_rejected_before_storage(self):
        with self.assertRaises(core.ManagerError):
            claude.save_profile({"name": "test", "baseUrl": "https://api.example.test", "apiKey": "bad\r\nkey"})
        self.assertFalse(claude.SECRETS_FILE.exists())


class RelayGenerationRegressions(unittest.TestCase):
    def service(self):
        service = relay.RelayPortalService()
        service._session = {**service._empty_state(), "sessionId": "old", "portalUrl": "https://relay.example.test"}
        service.window = SimpleNamespace(get_current_url=lambda: "https://relay.example.test")
        return service

    def test_old_scan_cannot_publish_secrets_into_replacement_session(self):
        service = self.service()
        def evaluate(*_args):
            service._session = {**service._empty_state(), "sessionId": "new", "status": "waiting_login"}
            return {}
        with patch.object(service, "_evaluate", side_effect=evaluate), \
                patch.object(relay, "normalize_probe_result", return_value=({"adapter": "new-api", "adapterLabel": "Test", "keys": []}, {"old-key": "secret"})), \
                patch.object(relay, "_dashboard_session_from_probe", return_value={}):
            with self.assertRaisesRegex(core.ManagerError, "已被替换"):
                service.scan("old")
        self.assertEqual(service._session["status"], "waiting_login")
        self.assertEqual(service._secrets, {})

    def test_replaced_session_cannot_have_new_window_closed_by_old_import(self):
        service = self.service()
        destroyed = []
        service.window = SimpleNamespace(destroy=lambda: destroyed.append(True))
        service._release_window(session_id="different")
        self.assertEqual(destroyed, [])
        self.assertIsNotNone(service.window)


class RadarRetryRegressions(unittest.TestCase):
    def test_oversized_retry_after_is_bounded_before_timedelta_conversion(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(radar._retry_after("9" * 200, now), now + radar.RETRY_AFTER_MAX)


if __name__ == "__main__":
    unittest.main()
