from agent_manager.integrations.reset_history import verified_reset_history, SOURCE
from agent_manager.integrations.radar import RadarService


def test_reviewed_occurrences_do_not_invent_timezone_or_third_card():
    records = verified_reset_history()
    assert [item["occurredAt"] for item in records] == ["2026-09-07", "2026-09-04", "2026-09-03"]
    assert sum(item["resetType"] == "reset-card" for item in records) == 2
    assert all(item["url"] == SOURCE and item["publishedAt"] is None
               and item["occurrenceTimezone"] is None for item in records)


def test_offline_reset_section_includes_verified_dates_not_cache_claims():
    service = RadarService.__new__(RadarService)
    service._state = {"sections": {"reset": {"data": {"verifiedResetHistory": [{"bad": True}]}}}}
    records = service._section("reset")["data"]["verifiedResetHistory"]
    assert records == verified_reset_history()
    records.clear()
    assert len(service._section("reset")["data"]["verifiedResetHistory"]) == 3
