import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import radar_service as radar
from test_radar_service import FakeClock, FakeResponse, QueueOpener, metrics_bytes, visual_metrics_bytes


def alert_forecast():
    return {
        "predictor": {"score": 20},
        "posts": [],
        "resetEvents": [
            {
                "id": "future-explicit",
                "status": "pending",
                "title": "We will reset Codex usage limits within the next 24 hours.",
                "publishedAt": "2026-08-10T23:30:00Z",
                "effectiveAt": "2026-08-11T12:00:00Z",
                "resetAt": "2026-08-11T12:00:00Z",
                "url": "https://x.com/thsottiaux/status/future-explicit",
            }
        ],
        "resetHistory": [],
    }


class RadarAuditV8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "radar.json"
        self.clock = FakeClock(datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc))

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_success_still_honors_summary_retry_after(self):
        limited = urllib.error.HTTPError(
            radar.PUBLIC_SUMMARY_URL,
            429,
            "too many",
            {"Retry-After": "3600"},
            io.BytesIO(),
        )
        opener = QueueOpener(
            [limited, FakeResponse(metrics_bytes()), FakeResponse(visual_metrics_bytes())]
        )
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)

        fresh_components = service.get_intelligence(refresh=True)
        suppressed = service.get_intelligence(refresh=True)

        self.assertTrue(fresh_components["available"])
        self.assertEqual(fresh_components["nextAllowedAt"], "2026-08-11T01:00:00Z")
        self.assertEqual(suppressed["refreshSuppressed"], "backoff")
        self.assertEqual(len(opener.calls), 3)

    def test_nonraising_429_response_also_honors_retry_after(self):
        opener = QueueOpener(
            [FakeResponse(status=429, headers={"Retry-After": "7200"})]
        )
        result = radar.RadarService(self.cache, opener=opener, clock=self.clock).get_quota(
            refresh=True,
            force=True,
        )
        self.assertEqual(result["nextAllowedAt"], "2026-08-11T02:00:00Z")
        self.assertTrue(result["stale"])

    def test_304_without_matching_cached_section_is_failure(self):
        state = radar._default_state()
        state["validators"]["summary"] = {"etag": '"summary"'}
        radar.RadarCache(self.cache).save(state)
        opener = QueueOpener([FakeResponse(status=304)])

        result = radar.RadarService(self.cache, opener=opener, clock=self.clock).get_quota(
            refresh=True,
            force=True,
        )

        self.assertFalse(result["available"])
        self.assertTrue(result["stale"])
        self.assertIn("304", result["error"])

    def test_two_stale_monitor_instances_emit_one_new_alert(self):
        first = radar.RadarService(self.cache, clock=self.clock)
        second = radar.RadarService(self.cache, clock=self.clock)
        forecast = alert_forecast()
        with (
            patch.object(first, "_fetch_forecast", return_value=(copy.deepcopy(forecast), False)),
            patch.object(first, "_fetch_status_incidents", return_value=([], False)),
            patch.object(first, "_translate_missing_forecast_posts", side_effect=lambda value: value),
            patch.object(second, "_fetch_forecast", return_value=(copy.deepcopy(forecast), False)) as second_fetch,
            patch.object(second, "_fetch_status_incidents", return_value=([], False)),
            patch.object(second, "_translate_missing_forecast_posts", side_effect=lambda value: value),
        ):
            first_result = first.run_monitor()
            second_result = second.run_monitor()

        self.assertTrue(first_result["newAlert"])
        self.assertFalse(second_result["newAlert"])
        self.assertEqual(second_result["suppressed"], "already-checked")
        second_fetch.assert_not_called()

    def test_effective_reset_time_uses_nearest_year_at_new_year_boundary(self):
        feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
<title>Codex limits have been reset</title>
<guid>new-year</guid>
<pubDate>Thu, 31 Dec 2026 15:55:00 GMT</pubDate>
<description>\xe6\x9d\x83\xe7\x9b\x8a\xe7\x9c\x9f\xe5\xae\x9e\xe7\x94\x9f\xe6\x95\x88\xef\xbc\x9a01\xe6\x9c\x8801\xe6\x97\xa5 09:00</description>
</item></channel></rss>"""
        parsed = radar.parse_public_feed(feed)
        self.assertEqual(parsed[0]["publishedAt"], "2026-12-31T15:55:00Z")
        self.assertEqual(parsed[0]["resetAt"], "2027-01-01T01:00:00Z")

    def test_monitor_cache_contains_no_notification_delivery_side_effects(self):
        service = radar.RadarService(self.cache, clock=self.clock)
        with (
            patch.object(service, "_fetch_forecast", return_value=(alert_forecast(), False)),
            patch.object(service, "_fetch_status_incidents", return_value=([], False)),
            patch.object(service, "_translate_missing_forecast_posts", side_effect=lambda value: value),
        ):
            result = service.run_monitor()
        encoded = json.dumps(radar.RadarCache(self.cache).load(), ensure_ascii=False)
        self.assertTrue(result["newAlert"])
        self.assertNotIn("notificationSent", encoded)
        self.assertNotIn("email", encoded.casefold())

    def test_terminal_or_completed_signals_never_raise_reset_alert(self):
        now = self.clock.value
        since = datetime(2026, 8, 10, 23, 0, tzinfo=timezone.utc)
        cancelled = alert_forecast()
        cancelled["predictor"]["score"] = 99
        cancelled["resetEvents"][0]["status"] = "cancelled"
        completed_post = {
            "predictor": {"score": 99},
            "resetEvents": [],
            "posts": [
                {
                    "id": "completed",
                    "title": "Codex usage limits have been reset.",
                    "publishedAt": "2026-08-10T23:30:00Z",
                }
            ],
        }
        self.assertIsNone(radar.classify_reset_alert(cancelled, [], since=since, now=now))
        self.assertIsNone(radar.classify_reset_alert(completed_post, [], since=since, now=now))

    def test_status_304_without_monitor_cache_is_not_treated_as_empty_authority(self):
        service = radar.RadarService(self.cache, clock=self.clock)
        with (
            patch.object(service, "_fetch_forecast", return_value=({"predictor": {}, "posts": [], "resetEvents": []}, False)),
            patch.object(service, "_fetch_status_incidents", return_value=(None, True)),
            patch.object(service, "_translate_missing_forecast_posts", side_effect=lambda value: value),
        ):
            result = service.run_monitor()
        self.assertFalse(result["success"])
        self.assertIn("304", result["state"]["lastError"])

    def test_corrupt_future_cadence_and_failure_count_fail_open_for_refresh(self):
        state = radar._default_state()
        state["failureCount"] = "not-an-integer"
        state["nextAllowedAt"] = "2099-01-01T00:00:00Z"
        state["quotaLastAttemptAt"] = "2099-01-01T00:00:00Z"
        radar.RadarCache(self.cache).save(state)
        opener = QueueOpener([FakeResponse(status=500)])
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)

        result = service.get_quota(refresh=True)

        self.assertTrue(result["refreshAttempted"])
        self.assertTrue(result["stale"])
        self.assertRegex(result["nextAllowedAt"], r"^2026-08-11T00:0[5-9]:")


if __name__ == "__main__":
    unittest.main()
