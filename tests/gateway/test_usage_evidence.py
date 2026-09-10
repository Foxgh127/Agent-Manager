"""Persisted aggregate migration and per-request billing evidence boundaries."""
from datetime import datetime, timezone
import json

import agent_manager.gateway.service as gateway
from agent_manager.usage.request_metadata import enrich_context


def test_legacy_aggregate_migrates_once_and_new_context_tiers_stay_separate(tmp_path):
    path = tmp_path / "usage.json"
    day = gateway._beijing_usage_day(datetime.now(timezone.utc).isoformat())
    old = {"source": "account_pool", "sourceKind": "account", "accountId": "fixture", "sourceRecordId": "fixture",
           "requestedModel": "gpt-test", "routedModel": "gpt-test", "routeKey": "pool:gpt-test",
           "requestCount": 2, "usageReportedCount": 2, "inputTokens": 100, "cachedInputTokens": 30,
           "outputTokens": 10, "totalTokens": 110}
    path.write_text(json.dumps({"schemaVersion": 5, "counterGeneration": "a" * 64,
        "days": {day: {"routes": {"legacy": old}}}, "recentRequests": []}), encoding="utf-8")
    store = gateway.UsageStatsStore(path)
    try:
        for count in (20, 300000):
            response = {"model": "gpt-test", "service_tier": "priority", "usage": {
                "input_tokens": count, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 4, "output_tokens_details": {"reasoning_tokens": 2}}}
            store.record(enrich_context(old, response), gateway._payload_usage(response))
        assert store.flush(force=True)
        reloaded = gateway.UsageStatsStore(path).snapshot()
        assert reloaded["schemaVersion"] == 6
        assert reloaded["counterGeneration"] == "a" * 64
        assert reloaded["totals"]["requestCount"] == 4
        assert reloaded["totals"]["inputTokens"] == 300120
        assert reloaded["totals"]["outputTokens"] == 18
        routes = list(reloaded["days"][day]["routes"].values())
        assert len(routes) == 3
        assert {row["contextTier"] for row in routes} == {"short", "long", "unknown"}
        legacy = next(row for row in routes if row["usageEvidenceVersion"] == 0)
        assert legacy["requestCount"] == 2 and legacy["cacheWriteEvidence"] == "unknown"
        assert legacy["serviceTier"] == "unknown" and legacy["modelEvidence"] == "requested"
    finally:
        if store.flush_timer:
            store.flush_timer.cancel()
