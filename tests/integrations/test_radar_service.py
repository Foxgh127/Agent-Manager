import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import urllib.error

import agent_manager.integrations.radar as radar


def summary_bytes(version=1):
    return json.dumps(
        {
            "service": "codex-reset-radar",
            "schema_version": "2.0",
            "status": "watching",
            "monitored_at": f"2026-08-0{version}T10:00:00Z",
            "timezone": "UTC",
            "window_open": False,
            "window": {"open": False, "label": "weekly", "closed_at": "2026-08-10T00:00:00Z", "message": "closed"},
            "prediction": {"confidence": 0.8},
            "recommended_action": "wait",
            "tibo_presence": {"seen": True},
            "model_iq": {
                "updated_at": f"2026-08-0{version}T09:00:00Z",
                "latest": {"model": "gpt-5", "score": 101 + version},
                "comparisons": {
                    "gpt_56_sol_high": {
                        "label": "GPT-5.6 Sol high",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "high",
                        "latest": {
                            "score": 103.6,
                            "valid_tasks": 112,
                            "average_cost_usd": 5.02,
                            "average_task_time_human": "20分钟",
                        },
                    }
                },
                "recent_days": [{"date": "2026-08-01", "score": 101}],
                "quota_check": {"status": "ok"},
                "quota_calibration": {"confidence": 0.9},
                "quota_radar": {
                    "updated_at": f"2026-08-0{version}T08:00:00Z",
                    "date": "2026-08-01",
                    "rows": [{"tier": "pro", "seven_d": 1000, "basis": "estimated"}],
                    "five_hour_policy": {"label": "5h"},
                    "seven_day_policy": {"label": "7d"},
                    "trend": [{"date": "2026-07-31", "value": 4}],
                    "history": [{"date": "2026-07-30", "value": 3}],
                    "source": "public",
                },
            },
        }
    ).encode()


def metrics_bytes(version=1):
    return json.dumps(
        {
            "source_updated_at": f"2026-08-0{version}T11:00:00Z",
            "runs_24h_total": 100,
            "runs_48h_total": 200,
            "runs_total": 1000,
            "points": [
                {
                    "model": "gpt-5.6-sol",
                    "effort": "ultra",
                    "weighted_total": 420,
                    "runs_total": 1000,
                    "runs_24h": 84,
                    "iq": 106.1,
                    "average_price_usd": 21.7,
                    "average_minutes": 52.4,
                    "source_updated_at": f"2026-08-0{version}T11:00:00Z",
                },
                {
                    "model": "experimental",
                    "effort": "high",
                    "weighted_total": 1,
                    "runs_total": 1,
                    "runs_24h": 1,
                    "iq": 150,
                },
            ],
            "metrics": {"best_score": 123 + version},
            "item_count": 92,
            "configuration_count": 24,
            "cell_count": 2208,
        }
    ).encode()


def visual_metrics_bytes(version=1):
    return json.dumps(
        {
            "schema": 1,
            "type": "visual_spatial_reasoning_summary",
            "source_updated_at": f"2026-08-0{version}T10:30:00Z",
            "runs_24h_total": 40,
            "runs_48h_total": 70,
            "runs_total": 300,
            "points": [
                {
                    "model": "gpt-5.6-sol",
                    "effort": "ultra",
                    "valid_tasks": 80,
                    "benchmark_tasks": 86,
                    "runs_24h": 16,
                    "runs_48h": 20,
                    "runs_total": 80,
                    "iq": 104.0,
                    "average_price_usd": 19.0,
                    "price_samples": 80,
                    "average_minutes": 28.0,
                    "duration_samples": 80,
                    "latest_graded_at": f"2026-08-0{version}T10:30:00Z",
                },
                {
                    "model": "experimental",
                    "effort": "high",
                    "valid_tasks": 80,
                    "iq": 200,
                },
            ],
        }
    ).encode()


def forecast_bytes(score=3):
    return json.dumps(
        {
            "fetchedAt": "2026-08-11T00:30:00Z",
            "nextRefreshAt": "2026-08-11T01:00:00Z",
            "forecast": {
                "score": score,
                "latestResetAt": "2026-08-11T00:28:16Z",
                "hoursSinceReset": 27.05,
                "resetAnnounced": False,
                "breakdown": [
                    {"label": "baseline", "points": 12},
                    {"label": "recent-reset cooldown", "points": -22},
                ],
            },
            "tiboPosts": [],
            "tiboSignal": {
                "vagueposts": [
                    {
                        "guid": "hint-1",
                        "title": "Little surprise for you tomorrow.",
                        "pubDate": "2026-08-11T00:29:00Z",
                        "link": "https://x.com/thsottiaux/status/123456",
                    }
                ]
            },
            "history": [
                {
                    "at": "2026-08-11T00:29:00Z",
                    "fromScore": 3,
                    "toScore": 17,
                    "scoreDelta": 14,
                    "changes": [
                        {
                            "label": "OpenAI team vagueposting",
                            "delta": 14,
                            "from": 0,
                            "to": 14,
                            "details": [
                                {
                                    "action": "Source post",
                                    "kind": "tweet",
                                    "name": "Little surprise for you tomorrow.",
                                    "url": "https://x.com/thsottiaux/status/123456",
                                }
                            ],
                        }
                    ],
                }
            ],
            "resetEvents": [
                {
                    "guid": "reset-1",
                    "title": "Usage limits have been reset for all paid ChatGPT Work and Codex users.",
                    "pubDate": "2026-08-11T00:28:16Z",
                    "persistedResetStatus": "completed",
                    "link": "https://x.com/thsottiaux/status/reset-1",
                }
            ],
            "sourceErrors": {},
        }
    ).encode()


def status_bytes():
    return json.dumps(
        {
            "incidents": [
                {
                    "id": "incident-1",
                    "name": "Elevated errors on Codex",
                    "status": "resolved",
                    "impact": "minor",
                    "created_at": "2026-08-10T20:00:00Z",
                    "updated_at": "2026-08-10T21:00:00Z",
                    "resolved_at": "2026-08-10T21:00:00Z",
                    "incident_updates": [{"body": "All impacted services have recovered."}],
                }
            ]
        }
    ).encode()


def detail_bytes():
    return json.dumps(
        {
            "benchmark_id": "bench-2026-08",
            "tier_windows_usd": {"pro": [20, 40]},
            "configurations": [
                {"id": f"config-{index}", "model": "gpt-5", "score": index, "secret": "drop"}
                for index in range(25)
            ],
            "items": [
                {"id": f"item-{index}", "title": f"Item {index}", "category": "reasoning", "raw": "drop"}
                for index in range(100)
            ],
            "cells": [
                {
                    "item_id": f"item-{index % 100}",
                    "configuration_id": f"config-{index % 25}",
                    "score": index,
                    "private_blob": "drop",
                }
                for index in range(1600)
            ],
        }
    ).encode()


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Codex Reset Radar</title>
<item><title>Codex limits have been reset</title><link>https://codex-reset-radar.pages.dev/events/open</link>
<guid>evt-1</guid><pubDate>Fri, 10 Jul 2026 05:30:53 GMT</pubDate>
<description><![CDATA[<b>Public</b> reset event]]></description></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


class QueueOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError(f"Unexpected network call: {request.full_url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class RadarParserTests(unittest.TestCase):
    def test_summary_extracts_flat_quota_and_reset_fields(self):
        parsed = radar.parse_public_json(summary_bytes())
        self.assertEqual(parsed["quota"]["tiers"][0]["tier"], "pro")
        self.assertEqual(parsed["quota"]["tiers"][0]["estimated7d"], "$1,000.00")
        self.assertEqual(parsed["quota"]["tiers"][0]["sourceLabel"], "推算")
        self.assertEqual(parsed["quota"]["plans"][0]["window"], "five-hour")
        self.assertEqual(parsed["quota"]["history"][0]["value"], 3)
        self.assertEqual(parsed["quota"]["trendPoints"][0]["value"], 4)
        self.assertFalse(parsed["reset"]["windowOpen"])
        self.assertEqual(parsed["intelligence"]["items"][0]["family"], "Sol")
        self.assertEqual(parsed["intelligence"]["items"][0]["score"], 103.6)

    def test_detail_matrix_is_strictly_bounded_and_allowlisted(self):
        parsed = radar.parse_intelligence_detail(detail_bytes())
        self.assertEqual(len(parsed["configurations"]), 18)
        self.assertEqual(len(parsed["items"]), 80)
        self.assertEqual(len(parsed["cells"]), 1440)
        self.assertNotIn("secret", parsed["configurations"][0])
        self.assertNotIn("private_blob", parsed["cells"][0])

    def test_metrics_keeps_complete_established_matrix_and_filters_probes(self):
        parsed = radar.parse_intelligence_metrics(metrics_bytes())
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["label"], "Sol ultra")
        self.assertEqual(parsed["items"][0]["cost"], "$21.70")
        self.assertEqual(parsed["items"][0]["duration"], "52分钟")

        empty = radar.parse_intelligence_metrics('{"metrics": {}, "points": []}')
        self.assertNotIn("items", empty)
        self.assertNotIn("comparisons", empty)

    def test_composite_metrics_match_public_weighted_mean_and_dynamic_models(self):
        parsed = radar.parse_composite_intelligence_metrics(metrics_bytes(), visual_metrics_bytes())
        point = parsed["items"][0]
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(point["label"], "Sol ultra")
        self.assertAlmostEqual(point["iq"], 105.76, places=2)
        self.assertEqual(point["sampleCount"], 100)
        self.assertEqual(point["cost"], "$21.27")
        self.assertEqual(point["duration"], "48分钟")
        self.assertEqual(parsed["updatedAt"], "2026-08-01T10:30:00Z")

    def test_public_forecast_preserves_transparent_predictor_inputs(self):
        parsed = radar.parse_public_forecast(forecast_bytes())
        self.assertEqual(parsed["predictor"]["score"], 3)
        self.assertEqual(parsed["predictor"]["hoursSinceReset"], 27.05)
        self.assertEqual(
            parsed["predictor"]["breakdown"],
            [
                {"label": "baseline", "points": 12},
                {"label": "recent-reset cooldown", "points": -22},
            ],
        )
        self.assertEqual(parsed["latestSignal"]["signalType"], "vague-hint")
        self.assertEqual(parsed["scoreHistory"][0]["toScore"], 17)
        self.assertEqual(parsed["scoreHistory"][0]["changes"][0]["details"][0]["kind"], "tweet")

    def test_public_post_translation_pairs_are_extracted_without_fake_fallback(self):
        html = b"""
        <ol><li class="reset-tibo-post" data-tibo-post-id="post-1">
          <p class="reset-tibo-post-original" lang="en"><b>English</b>Do not worry, we have compute</p>
          <p class="reset-tibo-post-translation" lang="zh-CN"><b>Chinese</b>\xe5\x88\xab\xe6\x8b\x85\xe5\xbf\x83\xef\xbc\x8c\xe6\x88\x91\xe4\xbb\xac\xe6\x9c\x89\xe7\xae\x97\xe5\x8a\x9b\xe3\x80\x82</p>
        </li></ol>
        """
        pairs = radar.parse_public_html_translations(html)
        self.assertEqual(pairs["post-1"]["translationZh"], "别担心，我们有算力。")
        fallback = radar._auto_translate_public_text("Codex usage limits reset in the next hour")
        self.assertEqual(fallback, "")
        attached = radar._attach_public_translation({"title": "A previously unseen sentence."})
        self.assertIsNone(attached["translationZh"])
        self.assertFalse(attached["translationComplete"])

    def test_machine_translation_response_requires_real_chinese(self):
        translated = radar.parse_machine_translation(
            json.dumps(
                {
                    "responseStatus": 200,
                    "responseData": {"translatedText": "这是一条自动翻译。"},
                }
            ).encode()
        )
        self.assertEqual(translated, "这是一条自动翻译。")
        with self.assertRaises(radar.RadarSchemaError):
            radar.parse_machine_translation(
                b'{"responseStatus":200,"responseData":{"translatedText":"unchanged"}}'
            )

    def test_html_embedded_json_and_text_fallback(self):
        embedded = summary_bytes().decode()
        parsed = radar.parse_public_html(f'<script type="application/json">{embedded}</script>')
        self.assertEqual(parsed["format"], "html-embedded-json")
        degraded = radar.parse_public_html("<h1>Codex Reset Radar</h1><p>Quota radar and reset window are waiting.</p>")
        self.assertTrue(degraded["degraded"])
        self.assertIn("Quota", degraded["quota"]["summary"])

    def test_rss_parser_strips_markup_and_rejects_external_links(self):
        events = radar.parse_public_feed(RSS)
        self.assertEqual(events[0]["description"], "Public reset event")
        self.assertEqual(events[0]["publishedAt"], "2026-07-10T05:30:53Z")

        namespaced = RSS.replace(b"<rss", b'<rss xmlns="urn:test"', 1)
        self.assertEqual(radar.parse_public_feed(namespaced)[0]["title"], events[0]["title"])

    def test_rss_parser_rejects_dtd_and_entities(self):
        malicious = b'''<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<rss><channel><item><title>&secret;</title></item></channel></rss>'''
        with self.assertRaisesRegex(radar.RadarSchemaError, "不是有效的 XML"):
            radar.parse_public_feed(malicious)

    def test_local_merge_never_copies_credentials(self):
        merged = radar.merge_reset_metadata(
            {"events": radar.parse_public_feed(RSS)},
            [
                {
                    "id": "account-1",
                    "name": "Work",
                    "token": "token-secret",
                    "apiKey": "key-secret",
                    "cookie": "cookie-secret",
                    "usage": {
                        "planLabel": "Pro",
                        "weekly": {"resetAt": "2026-08-15T00:00:00Z", "remainingPercent": 55},
                        "resetCredits": {
                            "availableCount": 1,
                            "detailsAvailable": True,
                            "credits": [{"id": "credit-1", "expiresAt": "2026-08-20T00:00:00Z"}],
                        },
                    },
                }
            ],
        )
        encoded = json.dumps(merged)
        self.assertNotIn("token-secret", encoded)
        self.assertNotIn("key-secret", encoded)
        self.assertNotIn("cookie-secret", encoded)
        self.assertEqual(merged["localAccounts"][0]["resetCredits"]["availableCount"], 1)
        self.assertEqual(len(merged["timeline"]), 1)
        self.assertEqual(merged["timeline"][0]["resetTypeLabel"], "全量重置")
        self.assertEqual(merged["localAccounts"][0]["windows"][0]["kind"], "weekly")
        self.assertNotIn("2026-08-15T00:00:00Z", json.dumps(merged["timeline"]))
        self.assertNotIn("2026-08-20T00:00:00Z", json.dumps(merged["timeline"]))

    def test_reset_alert_rules_are_future_only_and_conservative(self):
        now = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
        since = now - timedelta(hours=1)
        a_forecast = {
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
        }
        self.assertEqual(radar.classify_reset_alert(a_forecast, [], since=since, now=now)["level"], "A")
        vague = {
            "predictor": {"score": 95},
            "posts": [{"id": "vague", "title": "reset Monday", "publishedAt": "2026-08-10T23:30:00Z"}],
            "resetEvents": [],
        }
        self.assertIsNone(radar.classify_reset_alert(vague, [], since=since, now=now))
        b_forecast = {
            "predictor": {"score": 75},
            "posts": [
                {
                    "id": "official-signal",
                    "title": "We are investigating Codex usage limits.",
                    "publishedAt": "2026-08-10T23:30:00Z",
                    "url": "https://x.com/thsottiaux/status/official-signal",
                }
            ],
            "resetEvents": [],
        }
        self.assertEqual(radar.classify_reset_alert(b_forecast, [], since=since, now=now)["level"], "B")

        missing_time = {
            "predictor": {"score": 99},
            "posts": [],
            "resetEvents": [{
                "id": "undated",
                "status": "pending",
                "title": "We will reset Codex usage limits within the next 24 hours.",
                "url": "javascript:alert(1)",
            }],
        }
        self.assertIsNone(radar.classify_reset_alert(missing_time, [], since=since, now=now))

    def test_public_links_are_https_allowlisted(self):
        payload = json.loads(forecast_bytes())
        payload["tiboPosts"].append({
            "guid": "unsafe-post",
            "title": "Unsafe source",
            "pubDate": "2026-08-11T00:29:00Z",
            "link": "javascript:alert(1)",
        })
        payload["resetEvents"][0]["link"] = "https://attacker.example/reset"
        parsed = radar.parse_public_forecast(json.dumps(payload))
        self.assertIsNone(parsed["posts"][0]["url"])
        self.assertIsNone(parsed["resetEvents"][0]["url"])

    def test_monitor_schedule_starts_immediately_and_runs_overnight(self):
        before_window = datetime(2026, 8, 10, 23, 30, tzinfo=timezone.utc)  # 07:30 Beijing
        self.assertEqual(radar.next_monitor_time(before_window), before_window)
        after_window = datetime(2026, 8, 11, 15, 30, tzinfo=timezone.utc)  # 23:30 Beijing
        self.assertEqual(radar.next_monitor_time(after_window, after_window.isoformat()), datetime(2026, 8, 11, 16, 30, tzinfo=timezone.utc))


class RadarServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "radar.json"
        self.clock = FakeClock(datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc))

    def tearDown(self):
        self.temp.cleanup()

    def snapshot_responses(self):
        return [
            FakeResponse(summary_bytes(), headers={"ETag": '"summary-1"'}),
            FakeResponse(b"<html></html>"),
            FakeResponse(metrics_bytes(), headers={"ETag": '"metrics-1"'}),
            FakeResponse(visual_metrics_bytes(), headers={"ETag": '"visual-1"'}),
            FakeResponse(RSS, headers={"ETag": '"feed-1"'}),
            FakeResponse(forecast_bytes(), headers={"ETag": '"forecast-1"'}),
            FakeResponse(b'{"incidents": []}'),
        ]

    def test_startup_refreshes_once_and_caches_normalized_data(self):
        opener = QueueOpener(self.snapshot_responses())
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        first = service.get_snapshot(startup=True)
        second = service.get_snapshot(startup=True)
        self.assertEqual(len(opener.calls), 7)
        self.assertEqual(first["intelligence"]["data"]["items"][0]["label"], "Sol ultra")
        self.assertEqual(first["quota"]["data"]["tiers"][0]["estimated7d"], "$1,000.00")
        self.assertEqual(first["reset"]["data"]["events"][0]["source"], "public-rss")
        self.assertEqual(first["reset"]["data"]["resetHistory"][0]["resetTypeLabel"], "全量重置")
        self.assertTrue(second["intelligence"]["cached"])
        cached_text = self.cache.read_text(encoding="utf-8")
        self.assertNotIn("private_blob", cached_text)

    def test_missing_headline_translation_uses_machine_fallback_once_then_cache(self):
        machine = json.dumps(
            {
                "responseStatus": 200,
                "responseData": {"translatedText": "这是一条此前未出现的公开推文。"},
            }
        ).encode()
        opener = QueueOpener([FakeResponse(machine)])
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        first = {"latestSignal": radar._attach_public_translation({"id": "new-1", "title": "A never seen public post."})}
        service._translate_missing_forecast_posts(first)
        self.assertEqual(first["latestSignal"]["translationSource"], "mymemory")
        self.assertEqual(first["translationStatus"]["translated"], 1)

        second = {"latestSignal": radar._attach_public_translation({"id": "new-1", "title": "A never seen public post."})}
        service._translate_missing_forecast_posts(second)
        self.assertEqual(second["latestSignal"]["translationZh"], "这是一条此前未出现的公开推文。")
        self.assertEqual(len(opener.calls), 1)

    def test_intelligence_refresh_survives_summary_source_failure(self):
        summary_failure = radar.RadarHTTPError("summary unavailable", status=503)
        opener = QueueOpener(
            [summary_failure, FakeResponse(metrics_bytes()), FakeResponse(visual_metrics_bytes())]
        )
        result = radar.RadarService(self.cache, opener=opener, clock=self.clock).get_intelligence(refresh=True)

        self.assertTrue(result["available"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["data"]["items"][0]["label"], "Sol ultra")
        self.assertEqual(result["source"]["url"], radar.PUBLIC_INTELLIGENCE_URL)
        self.assertEqual(len(opener.calls), 3)

    def test_partial_composite_refresh_keeps_previous_complete_result_stale(self):
        warm = QueueOpener(
            [FakeResponse(summary_bytes()), FakeResponse(metrics_bytes()), FakeResponse(visual_metrics_bytes())]
        )
        first = radar.RadarService(self.cache, opener=warm, clock=self.clock).get_intelligence(refresh=True)
        previous_iq = first["data"]["items"][0]["iq"]
        failing = QueueOpener(
            [
                FakeResponse(summary_bytes(2)),
                FakeResponse(metrics_bytes(2)),
                radar.RadarHTTPError("visual unavailable", status=503),
            ]
        )

        result = radar.RadarService(self.cache, opener=failing, clock=self.clock).get_intelligence(refresh=True)

        self.assertTrue(result["available"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["data"]["mode"], "composite-weighted-mean")
        self.assertEqual(result["data"]["items"][0]["iq"], previous_iq)

    def test_radar_rejects_downgrade_or_private_redirect_targets(self):
        handler = radar._SafeRadarRedirectHandler()
        for target in (
            "http://codex-reset-radar.pages.dev/current.json",
            "https://127.0.0.1/current.json",
            "https://attacker.example/current.json",
        ):
            with self.subTest(target=target):
                with self.assertRaises(radar.RadarError):
                    handler.redirect_request(None, None, 302, "redirect", {}, target)

    def test_startup_snapshot_never_refreshes_again_without_manual_request(self):
        opener = QueueOpener(self.snapshot_responses())
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)

        service.get_snapshot(startup=True)
        self.clock.value += timedelta(days=30)
        service.get_snapshot(startup=True)
        service.get_snapshot()

        self.assertEqual(len(opener.calls), 7)

    def test_quota_attempt_is_capped_to_once_per_week(self):
        opener = QueueOpener([FakeResponse(summary_bytes(1)), FakeResponse(b"<html></html>"), FakeResponse(summary_bytes(2)), FakeResponse(b"<html></html>")])
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        first = service.get_quota(refresh=True)
        self.clock.value += timedelta(days=6)
        suppressed = service.get_quota(refresh=True)
        self.clock.value += timedelta(days=2)
        refreshed = service.get_quota(refresh=True)
        self.assertEqual(len(opener.calls), 4)
        self.assertTrue(first["available"])
        self.assertEqual(suppressed["refreshSuppressed"], "weekly-cadence")
        self.assertFalse(refreshed["cached"])

    def test_forced_quota_refresh_bypasses_weekly_background_cadence(self):
        opener = QueueOpener([FakeResponse(summary_bytes(1)), FakeResponse(b"<html></html>"), FakeResponse(summary_bytes(2)), FakeResponse(b"<html></html>")])
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        service.get_quota(refresh=True)
        self.clock.value += timedelta(minutes=5)

        refreshed = service.get_quota(refresh=True, force=True)

        self.assertEqual(len(opener.calls), 4)
        self.assertTrue(refreshed["available"])
        self.assertIsNone(refreshed["refreshSuppressed"])

    def test_etag_304_keeps_last_known_good(self):
        opener = QueueOpener(
            [
                FakeResponse(summary_bytes(), headers={"ETag": '"s1"'}),
                FakeResponse(metrics_bytes(), headers={"ETag": '"m1"'}),
                FakeResponse(visual_metrics_bytes(), headers={"ETag": '"v1"'}),
                FakeResponse(status=304),
                FakeResponse(status=304),
                FakeResponse(status=304),
            ]
        )
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        service.get_intelligence(refresh=True)
        result = service.get_intelligence(refresh=True)
        self.assertTrue(result["cached"])
        self.assertFalse(result["stale"])
        self.assertEqual(opener.calls[3][0].get_header("If-none-match"), '"s1"')

    def test_429_returns_lkg_stale_and_enforces_retry_after(self):
        too_many = urllib.error.HTTPError(
            radar.PUBLIC_SUMMARY_URL,
            429,
            "too many",
            {"Retry-After": "3600"},
            io.BytesIO(),
        )
        warm_opener = QueueOpener(
            [FakeResponse(summary_bytes()), FakeResponse(metrics_bytes()), FakeResponse(visual_metrics_bytes())]
        )
        radar.RadarService(self.cache, opener=warm_opener, clock=self.clock).get_intelligence(refresh=True)
        failing_opener = QueueOpener([too_many])
        service = radar.RadarService(self.cache, opener=failing_opener, clock=self.clock)
        failed = service.get_intelligence(refresh=True)
        suppressed = service.get_intelligence(refresh=True)
        self.assertTrue(failed["available"])
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["nextAllowedAt"], "2026-08-11T01:00:00Z")
        self.assertEqual(suppressed["refreshSuppressed"], "backoff")
        # A summary rate limit no longer prevents the independent metrics
        # endpoint from being attempted in the same refresh.
        self.assertEqual(len(failing_opener.calls), 2)

    def test_corrupt_cache_fails_closed_without_network(self):
        self.cache.write_text("not json", encoding="utf-8")
        opener = QueueOpener([])
        result = radar.RadarService(self.cache, opener=opener, clock=self.clock).get_intelligence()
        self.assertFalse(result["available"])
        self.assertTrue(result["needsManualRefresh"])
        self.assertEqual(opener.calls, [])

    def test_hourly_monitor_refreshes_all_reset_sources_once(self):
        opener = QueueOpener([FakeResponse(summary_bytes()), FakeResponse(b'<html></html>'), FakeResponse(RSS), FakeResponse(forecast_bytes()), FakeResponse(status_bytes())])
        service = radar.RadarService(self.cache, opener=opener, clock=self.clock)
        first = service.run_monitor()
        second = service.run_monitor()
        self.assertTrue(first["attempted"])
        self.assertTrue(first["success"])
        self.assertEqual(first["state"]["lastResult"], "来源数据已过期，暂不发出预测预警")
        self.assertEqual(first["state"]["lastCheckMode"], "scheduled")
        self.assertEqual(first["state"]["nextCheckAt"], "2026-08-11T01:00:00Z")
        self.assertEqual(second["suppressed"], "already-checked")
        self.assertEqual(len(opener.calls), 5)

    def test_cache_health_reports_and_clears_only_corrupt_cache(self):
        self.cache.write_text("not json", encoding="utf-8")
        cache = radar.RadarCache(self.cache)
        before = cache.health()
        result = cache.clear_corrupt()
        self.assertFalse(before["healthy"])
        self.assertTrue(result["changed"])
        self.assertFalse(self.cache.exists())
        self.assertTrue(result["health"]["healthy"])

    def test_cache_save_merges_independent_stale_writer_changes(self):
        cache = radar.RadarCache(self.cache)
        baseline = radar._default_state()
        cache.save(baseline)
        intelligence_writer = json.loads(json.dumps(baseline))
        reset_writer = json.loads(json.dumps(baseline))
        intelligence_writer["sections"]["intelligence"] = {
            "fetchedAt": "2026-08-11T00:00:00Z",
            "data": {"items": [{"id": "sol"}]},
        }
        reset_writer["sections"]["reset"] = {
            "fetchedAt": "2026-08-11T00:01:00Z",
            "data": {"events": [{"id": "reset"}]},
        }

        cache.save(intelligence_writer, previous=baseline)
        cache.save(reset_writer, previous=baseline)

        merged = cache.load()
        self.assertEqual(merged["sections"]["intelligence"]["data"]["items"][0]["id"], "sol")
        self.assertEqual(merged["sections"]["reset"]["data"]["events"][0]["id"], "reset")

    def test_cache_health_does_not_delete_valid_snapshot(self):
        radar.RadarCache(self.cache).save(radar._default_state())
        before = self.cache.read_bytes()
        result = radar.RadarCache(self.cache).clear_corrupt()
        self.assertFalse(result["changed"])
        self.assertEqual(self.cache.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
