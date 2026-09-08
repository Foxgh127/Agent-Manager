import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.gateway.service as usage


def event(timestamp, event_type, payload):
    return json.dumps(
        {"timestamp": timestamp, "type": event_type, "payload": {"type": event_type, **payload}},
        separators=(",", ":"),
    ).encode() + b"\n"


def token_event(timestamp, last_total, cumulative_total):
    return event(
        timestamp,
        "token_count",
        {
            "info": {
                "last_token_usage": {
                    "input_tokens": last_total - 2,
                    "cached_input_tokens": 1,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                    "total_tokens": last_total,
                },
                "total_token_usage": {
                    "input_tokens": cumulative_total - 2,
                    "cached_input_tokens": 1,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                    "total_tokens": cumulative_total,
                },
            }
        },
    )


class UsageAuditV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "codex"
        self.state_dir = self.root / "state"
        self.sessions = self.codex_home / "sessions/2026/09"
        self.sessions.mkdir(parents=True)
        self.state_dir.mkdir()
        self.path = self.sessions / "rollout-test.jsonl"
        self.patches = [
            patch.object(core, "CODEX_HOME", self.codex_home),
            patch.object(core, "STATE_DIR", self.state_dir),
        ]
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.temp.cleanup()

    def rollout(self, model="gpt-a", token_total=5, cumulative_total=5):
        return b"".join(
            [
                event(
                    "2026-09-05T01:00:00Z",
                    "session_meta",
                    {"thread_source": "user", "model_provider": "openai", "originator": "Codex"},
                ),
                event("2026-09-05T01:00:01Z", "turn_context", {"model": model}),
                token_event("2026-09-05T01:00:02Z", token_total, cumulative_total),
            ]
        )

    def test_activation_timeline_change_reattributes_unchanged_rollout(self):
        self.path.write_bytes(self.rollout())
        first_timeline = [{"timestamp": "2026-09-05T00:00:00Z", "accountId": "account-a"}]
        second_timeline = [{"timestamp": "2026-09-05T00:00:00Z", "accountId": "account-b"}]
        with patch.object(core, "account_activation_timeline", return_value=first_timeline):
            first = usage.codex_session_usage_snapshot()
        with patch.object(core, "account_activation_timeline", return_value=second_timeline):
            second = usage.codex_session_usage_snapshot()
        self.assertEqual(first["records"][0]["accountId"], "account-a")
        self.assertEqual(second["records"][0]["accountId"], "account-b")

    def test_same_size_same_mtime_replacement_invalidates_rollout_cache(self):
        first_bytes = self.rollout(model="gpt-a")
        second_bytes = self.rollout(model="gpt-b")
        self.assertEqual(len(first_bytes), len(second_bytes))
        self.path.write_bytes(first_bytes)
        stamp = self.path.stat().st_mtime_ns
        with patch.object(core, "account_activation_timeline", return_value=[]):
            first = usage.codex_session_usage_snapshot()
            self.path.write_bytes(second_bytes)
            os.utime(self.path, ns=(stamp, stamp))
            self.assertEqual(self.path.stat().st_mtime_ns, stamp)
            second = usage.codex_session_usage_snapshot()
        self.assertEqual(first["records"][0]["model"], "gpt-a")
        self.assertEqual(second["records"][0]["model"], "gpt-b")

    def test_increment_page_boundary_retries_complete_token_line(self):
        self.path.write_bytes(self.rollout())
        with (
            patch.object(core, "account_activation_timeline", return_value=[]),
            patch.object(usage, "CODEX_SESSION_INCREMENT_MAX_BYTES", 1_024),
        ):
            first = usage.codex_session_usage_snapshot()
            padding = json.dumps(
                {"type": "response_item", "payload": {"blob": "x" * 850}},
                separators=(",", ":"),
            ).encode() + b"\n"
            self.path.write_bytes(
                self.path.read_bytes()
                + padding
                + token_event("2026-09-05T01:01:02Z", 7, 12)
            )
            partial = usage.codex_session_usage_snapshot()
            completed = usage.codex_session_usage_snapshot()
        self.assertEqual(first["totals"]["requestCount"], 1)
        self.assertGreater(partial["coverage"]["pendingBytes"], 0)
        self.assertEqual(completed["totals"]["requestCount"], 2)
        self.assertEqual(completed["coverage"]["pendingBytes"], 0)

    def test_corrupt_or_alias_date_keys_are_not_exported(self):
        route = {
            "accountId": "account-a",
            "requestCount": 1,
            "usageReportedCount": 1,
            "totalTokens": 5,
        }
        document = usage.UsageStatsStore._safe_document(
            {
                "days": {
                    "2026-09-05": {"routes": {"valid": route}},
                    "9999-99-99": {"routes": {"invalid": route}},
                    "2026-09-05-extra": {"routes": {"alias": route}},
                }
            }
        )
        self.assertEqual(list(document["days"]), ["2026-09-05"])
        self.assertEqual(document["totals"]["requestCount"], 1)

    def test_invalid_routes_do_not_consume_the_bounded_export_budget(self):
        routes = {f"invalid-{index}": None for index in range(usage.USAGE_STATS_MAX_ROUTES_PER_DAY)}
        routes["valid"] = {
            "accountId": "account-a",
            "requestCount": 1,
            "usageReportedCount": 1,
            "totalTokens": 5,
        }
        document = usage.UsageStatsStore._safe_document(
            {"days": {"2026-09-05": {"routes": routes}}}
        )
        self.assertEqual(document["totals"]["requestCount"], 1)
        self.assertEqual(len(document["days"]["2026-09-05"]["routes"]), 1)

    def test_usage_subsets_are_bounded_without_inflating_total(self):
        normalized = usage._normalized_usage(
            {
                "input_tokens": 10,
                "cached_input_tokens": 20,
                "output_tokens": 5,
                "reasoning_output_tokens": 9,
                "total_tokens": 15,
            }
        )
        self.assertEqual(normalized["cachedInputTokens"], 10)
        self.assertEqual(normalized["reasoningOutputTokens"], 5)
        self.assertEqual(normalized["totalTokens"], 15)
        persisted = usage.UsageStatsStore._safe_document(
            {
                "days": {
                    "2026-09-05": {
                        "routes": {
                            "route": {
                                "inputTokens": 10,
                                "cachedInputTokens": 20,
                                "outputTokens": 5,
                                "reasoningOutputTokens": 9,
                                "totalTokens": 15,
                            }
                        }
                    }
                }
            }
        )
        counters = next(iter(persisted["days"]["2026-09-05"]["routes"].values()))
        self.assertEqual(counters["cachedInputTokens"], 10)
        self.assertEqual(counters["reasoningOutputTokens"], 5)

    def test_account_attribution_does_not_mutate_gateway_export(self):
        gateway_request = {
            "timestamp": "2026-09-05T01:00:02Z",
            "date": "2026-09-05",
            "accountId": "account-a",
            "requestedModel": "gpt-a",
            "routedModel": "gpt-a",
            "agentRole": "unclassified",
            "requestClassification": "unclassified",
            "requestCount": 1,
            "usageReportedCount": 1,
            "inputTokens": 3,
            "outputTokens": 2,
            "totalTokens": 5,
        }
        gateway = {"days": {}, "recentRequests": [gateway_request]}
        sessions = {
            "records": [],
            "recentRequests": [
                {
                    "timestamp": "2026-09-05T01:00:02Z",
                    "model": "gpt-a",
                    "agentRole": "subagent",
                    "inputTokens": 3,
                    "outputTokens": 2,
                    "totalTokens": 5,
                }
            ],
        }
        before = json.loads(json.dumps(gateway))
        attributed = usage.account_attribution_snapshot(gateway, sessions)
        self.assertEqual(gateway, before)
        self.assertEqual(attributed["recentRequests"][0]["agentRole"], "subagent")

    def test_simultaneous_flushes_do_not_duplicate_pending_delta(self):
        store = usage.UsageStatsStore(self.state_dir / "usage.json")
        with patch.object(store, "_schedule_flush_locked"):
            store.record({"accountId": "account-a", "routedModel": "gpt-a"}, {"totalTokens": 5})
        barrier = threading.Barrier(3)
        results = []

        def flush():
            barrier.wait()
            results.append(store.flush(force=True))

        threads = [threading.Thread(target=flush) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["totals"]["requestCount"], 1)
        self.assertEqual(snapshot["totals"]["totalTokens"], 5)
        self.assertEqual(sorted(results), [False, True])

    def test_corrupt_stats_file_recovers_with_new_valid_export(self):
        path = self.state_dir / "usage.json"
        path.write_bytes(b"{not-json")
        store = usage.UsageStatsStore(path)
        with patch.object(store, "_schedule_flush_locked"):
            store.record({"accountId": "account-a"}, None, failed=True)
        self.assertTrue(store.flush(force=True))
        exported = store.snapshot()
        self.assertEqual(exported["totals"]["requestCount"], 1)
        self.assertEqual(exported["totals"]["failureCount"], 1)
        json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
