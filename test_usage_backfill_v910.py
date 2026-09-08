from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_manager_core as core
import web2api_service as web2api


class UsageBackfillTests(unittest.TestCase):
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
            (web2api, 'datetime', Clock), (web2api, 'CODEX_SESSION_BOOTSTRAP_MAX_BYTES', 1600),
            (web2api, 'CODEX_SESSION_MAX_JSONL_LINE_BYTES', 1024),
            (web2api, 'CODEX_SESSION_BACKFILL_CHUNK_BYTES', 2048),
            (web2api, 'CODEX_SESSION_BACKFILL_PAUSE_SECONDS', 0.001),
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

    def seed(self, name='one.jsonl'):
        filler = {'type': 'response_item', 'payload': {'text': 'x' * 4500}}
        return self.write(name, [self.meta(), self.event(-30), filler,
                                self.event(-20, 100, 200), self.event(-20, 100, 200),
                                self.event(-10, 100, 300)])

    def scan(self, seconds=0):
        self.clock = self.start + timedelta(seconds=seconds)
        return web2api.codex_session_usage_snapshot()

    def document(self):
        return json.loads((self.state / web2api.CODEX_SESSION_USAGE_CACHE_FILE).read_text())

    def finish(self, limit=30):
        results = []
        for _ in range(limit):
            result = web2api.codex_session_usage_backfill_step()
            results.append(result)
            if not result.get('pendingFiles'):
                break
        self.assertLess(len(results), limit, results)
        self.assertNotIn(results[-1]['status'], {'blocked', 'retry'})
        return results

    def test_legacy_permanent_gap_is_filled_exactly_and_source_is_unchanged(self):
        path = self.seed()
        original = path.read_bytes()
        first = self.scan()
        self.assertEqual(first['coverage']['partialFiles'], 1)
        initial_live = first['liveCoverage']
        pages = self.finish()
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(page.get('processedBytes', 0) <= 2048 for page in pages))
        result = self.scan(10)
        self.assertEqual(result['coverage']['partialFiles'], 0)
        self.assertEqual(result['coverage']['backfill']['status'], 'complete')
        self.assertEqual(result['totals']['totalTokens'], 300)
        self.assertEqual(result['totals']['requestCount'], 3)
        self.assertEqual(result['liveCoverage']['epoch'], initial_live['epoch'])
        self.assertEqual(result['liveCoverage']['accounts'], {'a': 0})
        self.assertEqual(path.read_bytes(), original)

    def test_shadow_progress_survives_snapshot_refresh_and_restart(self):
        self.seed()
        self.scan()
        step = web2api.codex_session_usage_backfill_step()
        self.assertEqual(step['status'], 'progressed')
        shadow = next(iter(self.document()['files'].values()))['historyBackfill']
        old_offset = shadow['offset']
        self.scan(10)
        saved = next(iter(self.document()['files'].values()))['historyBackfill']
        self.assertEqual(saved['offset'], old_offset)
        self.finish()
        self.assertEqual(self.scan(20)['totals']['totalTokens'], 300)

    def test_live_appends_continue_during_backfill_without_live_epoch_reset(self):
        self.seed()
        baseline = self.scan()['liveCoverage']
        web2api.codex_session_usage_backfill_step()
        self.write('one.jsonl', [self.event(5, 200, 500)], append=True)
        live = self.scan(10)['liveCoverage']
        self.assertEqual(live['accounts'], {'a': 200})
        self.finish()
        completed = self.scan(20)
        self.assertEqual(completed['totals']['totalTokens'], 500)
        self.assertEqual(completed['liveCoverage']['accounts'], {'a': 200})
        self.assertEqual(completed['liveCoverage']['epoch'], baseline['epoch'])
        self.write('one.jsonl', [self.event(25, 100, 600)], append=True)
        self.assertEqual(self.scan(30)['liveCoverage']['accounts'], {'a': 300})

    def test_truncation_discards_shadow_and_starts_clean_source(self):
        self.seed()
        self.scan()
        web2api.codex_session_usage_backfill_step()
        self.write('one.jsonl', [self.meta(), self.event(5, 200, 200)])
        changed = self.scan(10)
        entry = next(iter(self.document()['files'].values()))
        self.assertNotIn('historyBackfill', entry)
        self.assertFalse(entry['partialHistory'])
        self.assertEqual(changed['totals']['totalTokens'], 200)

    def test_attribution_change_does_not_commit_wrong_account_history(self):
        self.seed()
        self.scan()
        web2api.codex_session_usage_backfill_step()
        self.activations[0]['accountId'] = 'b'
        self.assertEqual(web2api.codex_session_usage_backfill_step()['reason'], 'attribution_changed')
        self.scan(10)
        self.finish()
        result = self.scan(20)
        self.assertEqual({record['accountId'] for record in result['records']}, {'b'})

    def test_parse_outside_lock_and_concurrent_append_rejects_page(self):
        self.seed()
        self.scan()
        original = web2api._parse_codex_session_usage_file
        ran = []
        def race(*args, **kwargs):
            def take_lock():
                with web2api._CODEX_SESSION_USAGE_LOCK:
                    ran.append(True)
            thread = threading.Thread(target=take_lock)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive(), 'historical parsing must not hold snapshot lock')
            parsed = original(*args, **kwargs)
            self.write('one.jsonl', [self.event(5, 200, 500)], append=True)
            return parsed
        with patch.object(web2api, '_parse_codex_session_usage_file', side_effect=race):
            result = web2api.codex_session_usage_backfill_step()
        self.assertEqual(result['reason'], 'source_changed_during_parse')
        self.assertEqual(ran, [True])
        self.assertNotIn('historyBackfill', next(iter(self.document()['files'].values())))

    def test_multiple_files_round_robin_progress_and_repeated_completion_no_duplication(self):
        self.seed('one.jsonl')
        self.seed('two.jsonl')
        self.scan()
        web2api.codex_session_usage_backfill_step()
        web2api.codex_session_usage_backfill_step()
        entries = list(self.document()['files'].values())
        self.assertTrue(all(entry.get('historyBackfill') for entry in entries))
        self.finish()
        self.assertEqual(self.scan(10)['totals']['totalTokens'], 600)
        self.assertEqual(web2api.codex_session_usage_backfill_step()['status'], 'idle')
        self.assertEqual(self.scan(20)['totals']['totalTokens'], 600)

    def test_failed_cache_write_preserves_old_counters_and_resume_cursor(self):
        self.seed()
        self.scan()
        before = self.document()
        with patch.object(core, 'atomic_write_bytes', side_effect=OSError('full disk')):
            result = web2api.codex_session_usage_backfill_step()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.document(), before)

    def test_background_worker_is_coalesced_and_finishes_all_pages(self):
        self.seed()
        self.scan()
        entered, release = threading.Event(), threading.Event()
        original = web2api.codex_session_usage_backfill_step
        def delayed(**kwargs):
            entered.set()
            release.wait(2)
            return original(**kwargs)
        with patch.object(web2api, 'codex_session_usage_backfill_step', side_effect=delayed):
            self.assertTrue(web2api.request_codex_session_usage_backfill())
            self.assertTrue(entered.wait(2))
            self.assertFalse(web2api.request_codex_session_usage_backfill())
            with web2api._CODEX_SESSION_BACKFILL_LOCK:
                worker = next(iter(web2api._CODEX_SESSION_BACKFILL_WORKERS.values()))
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.scan(10)['coverage']['partialFiles'], 0)

    def test_shutdown_cancels_uncommitted_page_and_requires_explicit_restart(self):
        self.seed()
        self.scan()
        before = self.document()
        entered, release = threading.Event(), threading.Event()
        original = web2api._parse_codex_session_usage_file
        def delayed(*args, **kwargs):
            parsed = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return parsed
        with patch.object(web2api, '_parse_codex_session_usage_file', side_effect=delayed):
            self.assertTrue(web2api.request_codex_session_usage_backfill())
            self.assertTrue(entered.wait(2))
            with web2api._CODEX_SESSION_BACKFILL_LOCK:
                worker = next(iter(web2api._CODEX_SESSION_BACKFILL_WORKERS.values()))
            self.assertFalse(web2api.stop_codex_session_usage_backfill(timeout_seconds=0.01))
            self.assertFalse(web2api.request_codex_session_usage_backfill())
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.document(), before)
        self.assertTrue(web2api.stop_codex_session_usage_backfill())
        self.assertTrue(web2api.request_codex_session_usage_backfill(restart=True))
        with web2api._CODEX_SESSION_BACKFILL_LOCK:
            worker = next(iter(web2api._CODEX_SESSION_BACKFILL_WORKERS.values()))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.scan(10)['coverage']['partialFiles'], 0)

    def test_commit_honors_cache_file_lock_and_retains_checkpoint_on_contention(self):
        self.seed()
        self.scan()
        before = self.document()
        cache_path = self.state / web2api.CODEX_SESSION_USAGE_CACHE_FILE
        with web2api._exclusive_usage_file_lock(cache_path.with_suffix(cache_path.suffix + '.lock')):
            result = web2api.codex_session_usage_backfill_step()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.document(), before)


if __name__ == '__main__':
    unittest.main()
