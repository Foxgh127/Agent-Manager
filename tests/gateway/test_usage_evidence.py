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


def test_usage_snapshot_exposes_actual_model_and_route_evidence(tmp_path):
    store = gateway.UsageStatsStore(tmp_path / "usage.json")
    store.record(
        enrich_context(
            {
                "source": "account_pool",
                "sourceKind": "account",
                "accountId": "account-1",
                "requestedModel": "alias",
                "routedModel": "gpt-6-astra",
            },
            {"model": "gpt-6-astra", "system_fingerprint": "fp_a", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ),
        {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
    )
    store.record(
        enrich_context(
            {
                "source": "account_pool",
                "sourceKind": "account",
                "accountId": "account-1",
                "requestedModel": "alias",
                "routedModel": "gpt-6-astra",
            },
            {"model": "gpt-5.6-luna", "system_fingerprint": "fp_b", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ),
        {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
    )
    snapshot = store.snapshot()
    assert snapshot["byActualModel"]
    assert snapshot["routingEvidence"]["consistentRequests"] == 1
    assert snapshot["routingEvidence"]["mismatchRequests"] == 1
    assert snapshot["routingEvidence"]["fingerprintedRequests"] == 2
    assert snapshot["routingEvidence"]["fingerprintChanges"] == 1


def test_reference_candidates_survive_ledger_reload_and_account_attribution(tmp_path):
    path = tmp_path / "usage.json"
    store = gateway.UsageStatsStore(path)
    try:
        for official in (True, False):
            context = {"sourceKind": "account" if official else "provider", "requestedModel": "alias",
                       "routedModel": "native-model" if official else "alias",
                       "modelIdentityAuthority": "official_direct" if official else "unknown",
                       "accountId": "a" if official else "", "providerId": "" if official else "p"}
            store.record(enrich_context(context, {"object": "response", "model": "native-model" if official else "alias",
                         "system_fingerprint": "fp_reference"}), {"inputTokens": 4, "outputTokens": 2})
        assert store.flush(force=True)
        snapshot = gateway.UsageStatsStore(path).snapshot()
        attribution = gateway.account_attribution_snapshot(snapshot, None)
        row = next(item for item in attribution["recentRequests"] if item.get("providerId"))
        assert row["actualModel"] == "alias"  # preserve declaration, do not rewrite billing
        assert row["modelIdentity"]["status"] == "candidate"
        assert row["modelIdentity"]["candidates"] == ["native-model"]
        assert row["modelIdentity"]["referenceCount"] == 1
    finally:
        if store.flush_timer:
            store.flush_timer.cancel()
