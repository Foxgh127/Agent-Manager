"""Regressions derived from public responses captured 2026-09-10."""
import json
from pathlib import Path

from agent_manager.integrations import radar
from tests.integrations.test_radar_service import FakeResponse, QueueOpener, summary_bytes


FIXTURES = Path(__file__).parents[1] / "fixtures" / "radar_20260910"


def cards():
    return (FIXTURES / "cards.html").read_bytes()


def test_live_fixture_includes_all_astra_efforts_and_matches_source_formula():
    software = json.loads((FIXTURES / "software.json").read_text(encoding="utf-8"))
    visual = json.loads((FIXTURES / "visual.json").read_text(encoding="utf-8"))
    result = radar.parse_composite_intelligence_metrics(json.dumps(software), json.dumps(visual))
    assert len(result["items"]) == 25
    assert {p["effort"] for p in result["items"] if p["model"] == "gpt-6-astra"} == {"low", "medium", "high", "xhigh", "max", "ultra"}
    left, right = software["points"][0], visual["points"][0]
    point = next(p for p in result["items"] if p["id"] == "gpt-6-astra/low")
    expected = (left["iq"] * left["total"] + right["iq"] * right["valid_tasks"]) / (left["total"] + right["valid_tasks"])
    assert point["score"] == round(expected, 2)
    assert result["mode"] == "composite-weighted-mean"


def test_new_model_and_effort_are_not_dropped_and_missing_effort_is_safe():
    payload = {"points": [{"model": "future-model", "effort": "adaptive", "iq": 110, "valid_tasks": 5},
                          {"model": "missing-effort", "iq": 100}]}
    result = radar.parse_composite_intelligence_metrics(json.dumps(payload), json.dumps(payload))
    assert [p["id"] for p in result["items"]] == ["future-model/adaptive"]
    assert result["items"][0]["cost"] is None


def test_live_html_has_usd_measurements_and_preserves_source_time_text():
    quota = radar.parse_public_html_cards(cards())["quota"]
    assert [(p["model"], p["amountUsd"]) for p in quota["tiers"]] == [("Luna", 1145.10), ("Sol", 1919.83)]
    assert quota["sourceUpdatedText"] == "8月25日12:36更新"
    assert quota["updatedAt"] is None
    assert all(p["window"] is None and "estimated7d" not in p for p in quota["tiers"])


def test_history_retains_conflicting_monthly_counts_without_fabricating_dates(tmp_path):
    parsed = radar.parse_public_html_cards(cards())
    assert parsed["monthlyHistory"][0]["resetCardCount"] == 3
    assert parsed["monthlyHistory"][0]["noteCount"] == 2
    service = radar.RadarService(tmp_path / "cache.json")
    service._retain_reset_history(parsed, radar.datetime(2026, 9, 10, tzinfo=radar.timezone.utc))
    service._retain_reset_history(parsed, radar.datetime(2026, 9, 11, tzinfo=radar.timezone.utc))
    assert len(parsed["resetHistory"]) == 2
    direct = next(p for p in parsed["resetHistory"] if p["resetType"] == "full-reset")
    card = next(p for p in parsed["resetHistory"] if p["resetType"] == "reset-card")
    assert direct["occurredAt"] == "2026-09-08" and direct["occurrencePrecision"] == "date"
    assert card["occurredAt"] == "2026-09" and card["occurrencePrecision"] == "month"
    assert card["count"] == 3 and card["noteCount"] == 2 and card["discrepancy"] is True
    assert card["verification"] == "conflicting_source_summary"
    assert direct["publishedAt"] is None and card["publishedAt"] is None


def test_forecast_does_not_promote_scheduled_or_synthetic_activation_time():
    raw = {"forecast": {}, "resetEvents": [
        {"guid": "1", "pubDate": "2026-09-07T19:24:57Z", "title": "We will do a global reset of the usage for all paid subscriptions", "persistedResetStatus": "pending"},
        {"guid": "2", "pubDate": "2026-08-31T02:34:27Z", "activationAt": "2026-08-31T02:34:27Z", "title": "We have now reset usage", "persistedResetStatus": "completed"}]}
    result = radar.parse_public_forecast(json.dumps(raw))
    assert [p["id"] for p in result["resetHistory"]] == ["2"]
    assert result["resetHistory"][0]["occurredAt"] is None


def test_legacy_cache_invalidates_data_but_keeps_historical_evidence(tmp_path):
    path = tmp_path / "cache.json"
    state = radar._default_state()
    state.update(schemaVersion=4, resetHistoryLedger={"past": {"eventId": "past"}}, quotaLastAttemptAt="2026-09-10T00:00:00Z")
    state["validators"] = {"summary": {"etag": "stale"}}
    path.write_text(json.dumps(state), encoding="utf-8")
    loaded = radar.RadarCache(path).load()
    assert loaded["resetHistoryLedger"] == state["resetHistoryLedger"]
    assert loaded["schemaVersion"] == 5 and loaded["validators"] == {}
    assert loaded["quotaLastAttemptAt"] is None
    assert radar.RadarCache(path).health()["healthy"]


def test_quota_refresh_prefers_current_html_and_keeps_stale_on_source_failure(tmp_path):
    opener = QueueOpener([FakeResponse(summary_bytes()), FakeResponse(cards()),
                          FakeResponse(summary_bytes(2)), radar.RadarHTTPError("offline", status=503)])
    service = radar.RadarService(tmp_path / "cache.json", opener=opener)
    fresh = service.get_quota(refresh=True, force=True)
    assert fresh["data"]["tiers"][0]["amountUsd"] == 1145.1
    assert fresh["source"]["url"] == radar.PUBLIC_HTML_URL
    stale = service.get_quota(refresh=True, force=True)
    assert stale["stale"] and stale["data"]["tiers"] == fresh["data"]["tiers"]


def test_soft_resets_and_credit_aliases_have_distinct_identities():
    assert radar._reset_type("Codex soft reset applied") == "soft-reset"
    assert radar._reset_type("banked resets have been granted") == "reset-card"
    event = {"title": "Usage limits have been reset", "completed": True,
             "description": "Global weekly quota replenished", "url": "https://x.com/thsottiaux/status/123"}
    first = radar._history_record(event, "2026-09-10T00:00:00Z")
    second = radar._history_record({**event, "id": "mirror-id"}, "2026-09-11T00:00:00Z")
    assert first["eventId"] == second["eventId"]
