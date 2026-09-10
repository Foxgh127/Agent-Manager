from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.gateway.service as web2api


class QuotaLiveCountersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / 'codex' / 'sessions'
        self.sessions.mkdir(parents=True)
        self.state = self.root / 'state'
        self.start = datetime(2026, 9, 8, 3, tzinfo=timezone.utc)
        self.clock = self.start
        owner = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.clock.astimezone(tz) if tz else owner.clock.replace(tzinfo=None)
        self.activations = [{'timestamp': (self.start - timedelta(days=1)).isoformat(), 'accountId': 'a'}]
        for obj, name, value in [
            (core, 'STATE_DIR', self.state), (core, 'CODEX_HOME', self.root / 'codex'),
            (web2api, 'datetime', Clock),
            (core, 'account_activation_timeline', lambda: self.activations),
        ]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def meta(self):
        return {'timestamp': (self.start - timedelta(hours=1)).isoformat(),
                'type': 'session_meta', 'payload': {'type': 'session_meta',
                'thread_source': 'user', 'model_provider': 'openai'}}

    def event(self, seconds, total=100, cumulative=None):
        return {'timestamp': (self.start + timedelta(seconds=seconds)).isoformat(),
                'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'last_token_usage': {'input_tokens': total - 20, 'output_tokens': 20,
                                         'cached_input_tokens': 10, 'reasoning_output_tokens': 5,
                                         'total_tokens': total},
                    'total_token_usage': {'input_tokens': (cumulative or total) - 20,
                                         'output_tokens': 20, 'total_tokens': cumulative or total},
                }}}

    def write(self, name, events, append=False):
        path = self.sessions / name
        with path.open('a' if append else 'w', encoding='utf-8') as handle:
            for event in events:
                handle.write(json.dumps(event) + '\n')
        return path

    def scan(self, seconds=0):
        self.clock = self.start + timedelta(seconds=seconds)
        return web2api.codex_session_usage_snapshot()

    def test_baseline_excludes_history_and_append_counts_once(self):
        self.write('one.jsonl', [self.meta(), self.event(-10)])
        first = self.scan()
        live = first['liveCoverage']
        self.assertTrue(live['complete'])
        self.assertEqual(live['accounts'], {'a': 0})
        self.write('one.jsonl', [self.event(5, 200, 300)], append=True)
        second = self.scan(10)['liveCoverage']
        self.assertEqual(second['accounts'], {'a': 200})
        self.assertEqual(second['epoch'], live['epoch'])
        third = self.scan(20)['liveCoverage']
        self.assertEqual(third['accounts'], {'a': 200})
        self.assertEqual(third['epoch'], live['epoch'])
        self.write('one.jsonl', [self.event(5, 200, 300)], append=True)
        self.assertEqual(self.scan(30)['liveCoverage']['accounts'], {'a': 200})

    def test_reopening_same_account_keeps_live_generation_and_totals(self):
        self.write('one.jsonl', [self.meta(), self.event(-10)])
        first = self.scan()['liveCoverage']
        self.write('one.jsonl', [self.event(5, cumulative=200)], append=True)
        second = self.scan(10)['liveCoverage']
        self.activations.append({'timestamp': (self.start + timedelta(seconds=11)).isoformat(), 'accountId': 'a'})
        reopened = self.scan(20)['liveCoverage']
        self.assertEqual(reopened['epoch'], first['epoch'])
        self.assertEqual(reopened['accounts'], second['accounts'])
        self.assertGreater(reopened['accounts']['a'], 0)

    def test_permanent_partial_history_does_not_block_future_intervals(self):
        filler = {'type': 'response_item', 'payload': {'text': 'x' * 4000}}
        self.write('large.jsonl', [self.meta(), filler, self.event(-10)])
        with patch.object(web2api, 'CODEX_SESSION_BOOTSTRAP_MAX_BYTES', 1200):
            first = self.scan()
            self.assertEqual(first['coverage']['partialFiles'], 1)
            self.assertTrue(first['liveCoverage']['complete'])
            self.write('large.jsonl', [self.event(5, 200, 300)], append=True)
            second = self.scan(10)
        self.assertEqual(second['coverage']['partialFiles'], 1)
        self.assertTrue(second['liveCoverage']['complete'])
        self.assertEqual(second['liveCoverage']['accounts'], {'a': 200})
        self.assertEqual(second['liveCoverage']['epoch'], first['liveCoverage']['epoch'])

    def test_new_full_file_filters_backfill_and_counts_post_start_events(self):
        self.write('one.jsonl', [self.meta()])
        first = self.scan()['liveCoverage']
        self.write('import.jsonl', [self.meta(), self.event(-5, 999), self.event(5, 200, 1199)])
        second = self.scan(10)['liveCoverage']
        self.assertEqual(second['epoch'], first['epoch'])
        self.assertEqual(second['accounts'], {'a': 200})
        self.write('old.jsonl', [self.meta(), self.event(-100, 500)])
        third = self.scan(20)['liveCoverage']
        self.assertEqual(third['accounts'], {'a': 200})
        self.assertEqual(third['epoch'], first['epoch'])

    def test_existing_file_old_timestamp_backfill_is_not_live_usage(self):
        self.write('one.jsonl', [self.meta(), self.event(-10)])
        self.scan()
        self.write('one.jsonl', [self.event(-5, 999, 1099)], append=True)
        self.assertEqual(self.scan(10)['liveCoverage']['accounts'], {'a': 0})

    def test_truncate_rewrite_remove_and_activation_change_reset_epoch(self):
        path = self.write('one.jsonl', [self.meta(), self.event(-10)])
        first = self.scan()['liveCoverage']
        self.write('one.jsonl', [self.meta()])
        second = self.scan(10)['liveCoverage']
        self.assertNotEqual(first['epoch'], second['epoch'])
        self.write('one.jsonl', [self.meta(), self.event(15)], append=True)
        third = self.scan(20)['liveCoverage']
        self.assertEqual(third['accounts']['a'], 100)
        self.activations.append({'timestamp': (self.start + timedelta(seconds=21)).isoformat(), 'accountId': 'b'})
        fourth = self.scan(30)['liveCoverage']
        self.assertNotEqual(fourth['epoch'], third['epoch'])
        self.assertEqual(fourth['accounts'], {'a': 0, 'b': 0})
        path.unlink()
        fifth = self.scan(40)['liveCoverage']
        self.assertNotEqual(fifth['epoch'], fourth['epoch'])

    def test_same_size_rewrite_detected_by_resume_fingerprint(self):
        path = self.write('one.jsonl', [self.meta(), self.event(-10)])
        first = self.scan()['liveCoverage']
        content = path.read_text()
        path.write_text(content.replace('100', '200'), encoding='utf-8')
        second = self.scan(10)['liveCoverage']
        self.assertNotEqual(first['epoch'], second['epoch'])

    def test_new_partial_bootstrap_starts_new_baseline(self):
        self.write('one.jsonl', [self.meta()])
        first = self.scan()['liveCoverage']
        filler = {'type': 'response_item', 'payload': {'text': 'x' * 4000}}
        self.write('new-large.jsonl', [self.meta(), filler, self.event(5)])
        with patch.object(web2api, 'CODEX_SESSION_BOOTSTRAP_MAX_BYTES', 1200):
            second = self.scan(10)['liveCoverage']
        self.assertNotEqual(first['epoch'], second['epoch'])
        self.assertEqual(second['accounts'], {'a': 0})

    def test_pending_json_not_complete_then_catches_up_without_double_count(self):
        path = self.write('one.jsonl', [self.meta(), self.event(-10)])
        first = self.scan()['liveCoverage']
        line = json.dumps(self.event(5, 200, 300))
        with path.open('a', encoding='utf-8') as handle:
            handle.write(line[:70])
        second = self.scan(10)['liveCoverage']
        self.assertFalse(second['complete'])
        with path.open('a', encoding='utf-8') as handle:
            handle.write(line[70:] + '\n')
        third = self.scan(20)['liveCoverage']
        self.assertTrue(third['complete'])
        self.assertEqual(third['epoch'], first['epoch'])
        self.assertEqual(third['accounts'], {'a': 200})

    def test_append_during_parser_open_respects_stat_end_boundary(self):
        path = self.write('one.jsonl', [self.meta(), self.event(-10)])
        self.scan()
        self.write('one.jsonl', [self.event(5, 200, 300)], append=True)
        boundary = path.stat().st_size
        original = web2api._parse_codex_session_usage_file
        def race(file, *args, **kwargs):
            self.assertEqual(kwargs['end_offset'], boundary)
            self.write('one.jsonl', [self.event(6, 300, 600)], append=True)
            return original(file, *args, **kwargs)
        with patch.object(web2api, '_parse_codex_session_usage_file', side_effect=race):
            second = self.scan(10)['liveCoverage']
        self.assertFalse(second['complete'])
        self.assertEqual(second['accounts'], {'a': 200})
        stored = json.loads((self.state / web2api.CODEX_SESSION_USAGE_CACHE_FILE).read_text())
        entry = next(iter(stored['files'].values()))
        self.assertEqual(entry['processedOffset'], boundary)
        self.assertEqual(entry['parserState']['previousCumulative'][-1], 300)
        third = self.scan(20)['liveCoverage']
        self.assertTrue(third['complete'])
        self.assertEqual(third['accounts'], {'a': 500})

    def test_live_totals_not_limited_by_recent_event_cap(self):
        self.write('one.jsonl', [self.meta()])
        self.scan()
        self.write('one.jsonl', [self.event(i, 100, i * 100) for i in range(1, 11)], append=True)
        with patch.object(web2api, 'CODEX_SESSION_MAX_RECENT_PER_FILE', 2):
            result = self.scan(20)
        self.assertEqual(len(result['recentRequests']), 2)
        self.assertEqual(result['liveCoverage']['accounts'], {'a': 1000})

    def test_cache_write_failure_is_not_certified_complete(self):
        self.write('one.jsonl', [self.meta()])
        with patch.object(core, 'atomic_write_bytes', side_effect=OSError('test failure')):
            result = self.scan()['liveCoverage']
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'cache_persistence_failed')

    def test_unchanged_live_read_does_not_rewrite_historical_index(self):
        self.write('one.jsonl', [self.meta()])
        self.scan()
        with patch.object(core, 'atomic_write_bytes', wraps=core.atomic_write_bytes) as writer:
            result = self.scan(10)
        writer.assert_not_called()
        self.assertEqual(result['liveCoverage']['observedAt'], self.clock.isoformat())

    def test_gateway_generation_survives_flush_restart_and_changes_on_reset(self):
        path = self.state / 'gateway.json'
        store = web2api.UsageStatsStore(path)
        first = store.snapshot()
        generation = first['counterGeneration']
        self.assertTrue(first['coverage']['counterGenerationValid'])
        self.assertEqual(store.snapshot()['counterGeneration'], generation)
        store.record({'accountId': 'a', 'sourceKind': 'account'},
                     {'inputTokens': 80, 'outputTokens': 20, 'totalTokens': 100})
        self.addCleanup(lambda: store.flush(force=True))
        pending = store.snapshot()
        self.assertEqual(pending['counterGeneration'], generation)
        self.assertEqual(pending['byAccount'][0]['totalTokens'], 100)
        store.flush(force=True)
        restarted = web2api.UsageStatsStore(path)
        self.assertEqual(restarted.snapshot()['counterGeneration'], generation)
        self.assertNotEqual(restarted.reset()['counterGeneration'], generation)

    def test_gateway_generation_migration_is_persisted_once_and_missing_usage_exposed(self):
        path = self.state / 'gateway.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schemaVersion': 1, 'days': {}}))
        store = web2api.UsageStatsStore(path)
        migrated = store.snapshot()
        self.assertEqual(migrated['counterGeneration'], json.loads(path.read_text())['counterGeneration'])
        self.assertEqual(migrated['counterGeneration'], web2api.UsageStatsStore(path).snapshot()['counterGeneration'])
        store.record({'accountId': 'a', 'sourceKind': 'account'}, None)
        self.addCleanup(lambda: store.flush(force=True))
        self.assertEqual(store.snapshot()['coverage']['usageMissingRequests'], 1)

    def test_gateway_restored_older_generation_changes_scope(self):
        path = self.state / 'gateway.json'
        store = web2api.UsageStatsStore(path)
        first = store.snapshot()['counterGeneration']
        backup = path.read_bytes()
        second = store.reset()['counterGeneration']
        self.assertNotEqual(first, second)
        path.write_bytes(backup)
        self.assertEqual(store.snapshot()['counterGeneration'], first)


if __name__ == '__main__':
    unittest.main()
