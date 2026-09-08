from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import radar_service as radar


class RadarPredictionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'radar.json'
        self.now = datetime(2026, 9, 8, 13, tzinfo=timezone.utc)
        self.service = radar.RadarService(self.path, clock=lambda: self.now)

    def forecast(self, score=100, checked=None, events=None):
        return {'checkedAt': checked or radar._iso(self.now),
                'predictor': {'score': score, 'latestResetAt': '2026-08-31T02:34:27Z'},
                'posts': [], 'resetEvents': [], 'resetHistory': events or []}

    def event(self, event_id='123456', kind='full-reset', occurrence=None):
        return {'id': event_id, 'url': f'https://x.com/thsottiaux/status/{event_id}',
                'title': 'Usage limits have been reset for all paid Codex users.',
                'publishedAt': '2026-08-31T02:34:27Z', 'status': 'completed',
                'resetType': kind, **radar._occurrence_metadata(occurrence)}

    def refresh(self, forecast):
        with patch.object(self.service, '_fetch_summary', return_value=(None, 'json', False)), \
             patch.object(self.service, '_fetch_events', return_value=([], False)), \
             patch.object(self.service, '_fetch_forecast', return_value=(forecast, False)), \
             patch.object(self.service, '_translate_missing_forecast_posts'), \
             patch.object(self.service, '_translate_reset_payload'):
            return self.service.get_reset_radar(refresh=True)

    def test_fresh_100_score_is_prediction_not_confirmation(self):
        alert = radar.classify_reset_alert(self.forecast(), [], since=self.now - timedelta(hours=1), now=self.now)
        self.assertEqual(alert['level'], 'P')
        self.assertEqual(alert['kind'], 'community-prediction')
        self.assertIn('不是重置发生概率', alert['evidence'])
        self.assertIn('未确认', alert['window'])
        self.assertEqual(alert['expiresAt'], '2026-09-08T19:00:00Z')

    def test_expired_missing_or_future_source_time_does_not_alert(self):
        for stamp in ('2026-09-07T13:00:00Z', '2026-09-09T13:00:00Z', 'invalid'):
            with self.subTest(stamp=stamp):
                self.assertIsNone(radar.classify_reset_alert(self.forecast(checked=stamp), [], since=self.now - timedelta(days=1), now=self.now))

    def test_manual_refresh_updates_check_and_deduplicates_prediction_across_restart(self):
        first = self.refresh(self.forecast())
        self.assertTrue(first['newAlert'])
        self.assertEqual(first['data']['monitor']['lastRunAt'], radar._iso(self.now))
        self.assertEqual(first['data']['monitor']['lastCheckMode'], 'manual-refresh')
        self.assertEqual(first['data']['monitor']['lastResult'], '预测预警')
        self.service = radar.RadarService(self.path, clock=lambda: self.now)
        self.now += timedelta(minutes=10)
        second = self.refresh(self.forecast())
        self.assertFalse(second['newAlert'])
        self.assertEqual(first['alert']['signature'], second['alert']['signature'])

    def test_history_survives_remote_eviction_and_does_not_duplicate(self):
        self.refresh(self.forecast(events=[self.event()]))
        self.now += timedelta(hours=1)
        again = self.refresh(self.forecast(events=[self.event()]))
        self.assertEqual(len(again['data']['resetHistory']), 1)
        self.assertEqual(again['data']['resetHistory'][0]['discoveredAt'], '2026-09-08T13:00:00Z')
        self.service = radar.RadarService(self.path, clock=lambda: self.now)
        later = self.refresh(self.forecast(events=[]))
        self.assertEqual(len(later['data']['resetHistory']), 1)
        self.assertEqual(later['data']['resetHistory'][0]['occurrencePrecision'], 'unknown')
        self.assertIsNone(later['data']['resetHistory'][0]['occurredAt'])

    def test_card_and_hard_reset_are_distinct_events_even_same_post(self):
        result = self.refresh(self.forecast(events=[self.event(), self.event(kind='reset-card')]))
        self.assertEqual(len(result['data']['resetHistory']), 2)
        self.assertEqual({e['resetType'] for e in result['data']['resetHistory']}, {'full-reset', 'reset-card'})

    def test_occurrence_precision_is_explicit_and_ambiguous_timezone_not_assumed(self):
        self.assertEqual(radar._occurrence_metadata('2026-09-08')['occurrencePrecision'], 'date')
        self.assertEqual(radar._occurrence_metadata('2026-09-08T14:20+08:00')['occurrencePrecision'], 'minute')
        self.assertEqual(radar._occurrence_metadata('2026-09-08T14:20:31+08:00')['occurrencePrecision'], 'second')
        self.assertIsNone(radar._occurrence_metadata('2026-09-08T14:20')['occurredAt'])

    def test_forecast_publication_is_not_occurrence_and_pending_schedule_is_not_history(self):
        payload = {'forecast': {'score': 100}, 'resetEvents': [
            {'guid': 'done', 'title': 'Codex limits have been reset', 'pubDate': '2026-09-08T12:34:56Z', 'persistedResetStatus': 'completed'},
            {'guid': 'pending', 'title': 'Will reset tomorrow', 'pubDate': '2026-09-08T12:34:56Z', 'effectiveAt': '2026-09-09T00:00:00Z', 'persistedResetStatus': 'pending'},
        ]}
        parsed = radar.parse_public_forecast(json.dumps(payload))
        self.assertEqual(len(parsed['resetHistory']), 1)
        self.assertIsNone(parsed['resetHistory'][0]['occurredAt'])
        self.assertEqual(parsed['resetHistory'][0]['publishedAt'], '2026-09-08T12:34:56Z')

    def test_rss_fallback_does_not_copy_publication_to_effective_time(self):
        xml = '<rss><channel><item><title>Codex limits have been reset</title><pubDate>Tue, 08 Sep 2026 12:34:56 GMT</pubDate><description>Confirmed in public post.</description></item></channel></rss>'
        event = radar.parse_public_feed(xml)[0]
        self.assertIsNone(event['occurredAt'])
        self.assertIsNone(event['resetAt'])
        self.assertEqual(event['publishedAt'], '2026-09-08T12:34:56Z')

    def test_local_weekly_windows_do_not_enter_public_ledger(self):
        self.refresh(self.forecast(events=[]))
        result = self.service.get_reset_radar(accounts=[{'id': 'a', 'usage': {'weekly': {'resetAt': '2026-09-08T13:00:00Z', 'remainingPercent': 100}}}])
        self.assertEqual(result['data']['resetHistory'], [])
        self.assertEqual(self.service._state['resetHistoryLedger'], {})

    def test_failed_manual_check_records_attempt_without_faking_success_time(self):
        self.refresh(self.forecast())
        self.now += timedelta(hours=1)
        with patch.object(self.service, '_fetch_summary', return_value=(None, 'json', False)), \
             patch.object(self.service, '_fetch_events', return_value=([], False)), \
             patch.object(self.service, '_fetch_forecast', side_effect=radar.RadarError('source unavailable')), \
             patch.object(self.service, '_translate_missing_forecast_posts'), \
             patch.object(self.service, '_translate_reset_payload'):
            result = self.service.get_reset_radar(refresh=True)
        monitor = result['data']['monitor']
        self.assertEqual(monitor['lastRunAt'], '2026-09-08T14:00:00Z')
        self.assertEqual(monitor['lastSuccessAt'], '2026-09-08T13:00:00Z')
        self.assertEqual(monitor['lastError'], 'source unavailable')

    def test_more_precise_evidence_upgrades_existing_event_without_changing_discovery(self):
        self.refresh(self.forecast(events=[self.event()]))
        self.now += timedelta(hours=1)
        result = self.refresh(self.forecast(events=[self.event(occurrence='2026-08-31T10:30+08:00')]))
        self.assertEqual(len(result['data']['resetHistory']), 1)
        event = result['data']['resetHistory'][0]
        self.assertEqual(event['occurrencePrecision'], 'minute')
        self.assertEqual(event['discoveredAt'], '2026-09-08T13:00:00Z')


if __name__ == '__main__':
    unittest.main()
