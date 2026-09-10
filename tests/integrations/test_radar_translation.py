import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import agent_manager.integrations.radar as radar


class _Response:
    headers = {}

    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


class _TranslationOpener:
    def __init__(self, *, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        self.assert_translation_url(request.full_url)
        original = parse_qs(urlparse(request.full_url).query)["q"][0]
        if self.fail_first and len(self.calls) == 1:
            translated = original
        else:
            translated = f"中文译文：{original}"
        return _Response(json.dumps({
            "responseStatus": 200,
            "responseData": {"translatedText": translated},
        }, ensure_ascii=False).encode())

    @staticmethod
    def assert_translation_url(url):
        if not url.startswith(radar.PUBLIC_TRANSLATE_URL + "?"):
            raise AssertionError(f"Unexpected network call: {url}")


class RadarTranslationV9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "radar.json"
        self.now = lambda: datetime(2026, 9, 6, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_english_in_legacy_zh_field_is_not_reported_as_success(self):
        item = radar._attach_public_translation({
            "title": "A new public reset statement.",
            "translationZh": "A new public reset statement.",
        })

        self.assertEqual(item["originalText"], "A new public reset statement.")
        self.assertIsNone(item["translationZh"])
        self.assertEqual(item["translationSource"], "unavailable")
        self.assertEqual(item["translationState"], "pending")
        self.assertFalse(item["translationComplete"])
        self.assertEqual(item["translationFields"]["title"]["state"], "pending")

    def test_forecast_translation_covers_every_public_display_field(self):
        opener = _TranslationOpener()
        service = radar.RadarService(self.cache, opener=opener, clock=self.now)
        forecast = {
            "latestSignal": radar._attach_public_translation({
                "id": "latest",
                "title": "Newest signal headline",
                "context": "Newest signal body",
                "reason": "Newest signal reason",
                "label": "Newest signal label",
                "status": "new signal state",
                "impact": "new signal impact",
            }),
            "posts": [radar._attach_public_translation({
                "id": "post",
                "title": "Post title",
                "context": "Post body",
                "description": "Post description",
            })],
            "resetEvents": [radar._attach_public_translation({
                "id": "event",
                "title": "Reset event title",
                "context": "Reset event body",
                "status": "event state",
                "impact": "event impact",
            })],
            "predictor": {"breakdown": [{
                "label": "Predictor factor",
                "description": "Predictor factor explanation",
            }]},
            "scoreHistory": [{"changes": [{
                "label": "Historical score change",
                "details": [{"action": "Read source", "name": "Historical source text"}],
            }]}],
        }

        service._translate_missing_forecast_posts(forecast)

        records_and_fields = [
            (forecast["latestSignal"], ("title", "context", "reason", "label", "status", "impact")),
            (forecast["posts"][0], ("title", "context", "description")),
            (forecast["resetEvents"][0], ("title", "context", "status", "impact")),
            (forecast["predictor"]["breakdown"][0], ("label", "description")),
            (forecast["scoreHistory"][0]["changes"][0], ("label",)),
            (forecast["scoreHistory"][0]["changes"][0]["details"][0], ("action", "name")),
        ]
        for record, fields in records_and_fields:
            with self.subTest(fields=fields):
                self.assertEqual(record["translationState"], "complete")
                for field in fields:
                    self.assertIn(field, record["translated"])
                    self.assertIn("中文译文", record[f"{field}Zh"])
                    self.assertEqual(record["translationFields"][field]["original"], record[field])
                    self.assertEqual(record["translationFields"][field]["source"], "mymemory")
        self.assertEqual(forecast["translationStatus"]["state"], "complete")
        self.assertEqual(forecast["translationStatus"]["unavailable"], 0)
        self.assertTrue(all(url.startswith(radar.PUBLIC_TRANSLATE_URL) for url, _ in opener.calls))

    def test_failed_translation_is_not_success_cached_and_retries_next_refresh(self):
        opener = _TranslationOpener(fail_first=True)
        service = radar.RadarService(self.cache, opener=opener, clock=self.now)
        item = radar._attach_public_translation({"id": "retry", "title": "Retry this headline"})
        forecast = {"latestSignal": item}

        service._translate_missing_forecast_posts(forecast)
        digest = radar.hashlib.sha256(b"Retry this headline").hexdigest()
        self.assertEqual(item["translationState"], "failed")
        self.assertIsNone(item["translationZh"])
        self.assertNotIn(digest, service._state["machineTranslations"])
        self.assertEqual(forecast["translationStatus"]["state"], "failed")

        service._translate_missing_forecast_posts(forecast)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(item["translationState"], "complete")
        self.assertIn("中文译文", item["translationZh"])
        self.assertIn(digest, service._state["machineTranslations"])

    def test_legacy_english_success_cache_is_ignored_and_replaced(self):
        opener = _TranslationOpener()
        service = radar.RadarService(self.cache, opener=opener, clock=self.now)
        original = "Cached headline requiring repair"
        digest = radar.hashlib.sha256(original.encode()).hexdigest()
        service._state["machineTranslations"][digest] = {
            "translationZh": original,
            "source": "mymemory",
        }

        translated = service._machine_translate_public_text(original)

        self.assertEqual(len(opener.calls), 1)
        self.assertIn("中文译文", translated)
        self.assertEqual(service._state["machineTranslations"][digest]["translationZh"], translated)

    def test_304_refresh_revisits_incomplete_cached_forecast(self):
        translation = json.dumps({
            "responseStatus": 200,
            "responseData": {"translatedText": "缓存中的标题现在已有中文译文。"},
        }, ensure_ascii=False).encode()
        responses = iter([
            _Response(status=304),
            _Response(status=304),
            _Response(status=304),
            _Response(status=304),
            _Response(translation),
        ])
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return next(responses)

        service = radar.RadarService(self.cache, opener=opener, clock=self.now)
        cached_item = radar._attach_public_translation({
            "id": "cached-pending",
            "title": "A cached headline was not translated",
        })
        service._state["sections"]["reset"] = {
            "data": {"forecastSignals": {"latestSignal": cached_item}},
            "fetchedAt": "2026-09-05T00:00:00Z",
            "source": {"url": radar.PUBLIC_FORECAST_URL, "format": "json", "degraded": False},
        }

        with patch.object(service, '_fetch_status_incidents', return_value=([], False)):
            result = service.get_reset_radar(refresh=True)

        repaired = result["data"]["forecastSignals"]["latestSignal"]
        self.assertEqual(repaired["translationState"], "complete")
        self.assertEqual(repaired["titleZh"], "缓存中的标题现在已有中文译文。")
        self.assertEqual(len(calls), 5)
        self.assertTrue(calls[-1].startswith(radar.PUBLIC_TRANSLATE_URL))

    def test_reset_summary_rss_and_status_updates_expose_field_state(self):
        parsed = radar.parse_public_json(json.dumps({
            "service": "codex-reset-radar",
            "schema_version": "2.0",
            "status": "watching",
            "recommended_action": "wait",
            "window": {"open": False, "label": "weekly", "message": "closed"},
            "model_iq": {},
        }))
        reset = parsed["reset"]
        self.assertEqual(reset["statusZh"], "监测中")
        self.assertEqual(reset["recommendedActionZh"], "等待")
        self.assertEqual(reset["window"]["messageZh"], "已关闭")
        self.assertEqual(reset["translationState"], "complete")

        rss = """<?xml version='1.0'?><rss version='2.0'><channel><item>
        <title>Codex limits have been reset</title>
        <link>https://codexradar.com/events/1</link><guid>1</guid>
        <pubDate>Sun, 06 Sep 2026 00:00:00 GMT</pubDate>
        <description>Public reset event</description></item></channel></rss>"""
        event = radar.parse_public_feed(rss)[0]
        self.assertEqual(event["titleZh"], "Codex 额度限制已重置。")
        self.assertEqual(event["descriptionZh"], "公开重置事件")
        self.assertEqual(event["translationState"], "complete")

        incidents = radar.parse_openai_status_incidents(json.dumps({"incidents": [{
            "id": "incident-1",
            "name": "Elevated errors on Codex",
            "status": "resolved",
            "impact": "minor",
            "created_at": "2026-09-06T00:00:00Z",
            "updated_at": "2026-09-06T01:00:00Z",
            "incident_updates": [{"body": "All impacted services have recovered."}],
        }]}))[0]
        self.assertEqual(incidents["nameZh"], "Codex 错误率升高")
        self.assertEqual(incidents["statusZh"], "已解决")
        self.assertEqual(incidents["impactZh"], "轻微")
        self.assertEqual(incidents["latestUpdateZh"], "所有受影响的服务均已恢复。")
        self.assertEqual(incidents["translationState"], "complete")


if __name__ == "__main__":
    unittest.main()
