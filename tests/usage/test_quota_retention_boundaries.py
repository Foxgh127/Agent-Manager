import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
import agent_manager.usage.estimation as estimator
from agent_manager.usage.pricing import PRICE_VERSION


class QuotaRetentionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(core, 'STATE_DIR', Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start = 1_800_000_000

    def account(self, step):
        return {'id': 'a', 'fingerprint': 'principal', 'plan': 'pro', 'usage': {
            'updatedAt': self.start + step * 300,
            'weekly': {'remainingPercent': 90 - step * 3,
                       'resetAt': self.start + 604800, 'windowMinutes': 10080}}}

    def observe(self, step, *, unpriced=5000, invalid=1, priced_delta=30000):
        price = {'status': 'partial', 'priceVersion': PRICE_VERSION, 'sourceHistoryComplete': True,
                 'knownUsd': 1 + step * .3, 'pricedTokens': 100000 + step * priced_delta,
                 'unpricedTokens': unpriced, 'invalidRowCount': invalid,
                 'unknownModels': ['old-unknown-model']}
        return estimator.observe(self.account(step), {'accountId': 'a',
            'observedAt': self.start + step * 300, 'cumulativeTokens': 105000 + step * 30000,
            'coverageComplete': True, 'coverageEpoch': 'epoch', 'apiEquivalent': price},
            now=self.start + step * 300)

    def test_old_unknown_history_allows_three_new_known_usd_intervals(self):
        for step in range(4):
            result = self.observe(step)
        self.assertEqual(result['usdSampleCount'], 3)
        self.assertEqual(result['estimatedTotalUsd']['estimate'], 10)
        warm = estimator.read_summary(self.account(3), now=self.start + 900)
        self.assertEqual(warm['apiEquivalent']['status'], 'partial')
        self.assertEqual(warm['apiEquivalent']['unpricedTokens'], 5000)
        self.assertEqual(warm['apiEquivalent']['unknownModels'], ['old-unknown-model'])

    def test_unknown_or_invalid_usage_changes_do_not_price_that_interval(self):
        for field in ('unpriced', 'invalid'):
            with self.subTest(field=field):
                if estimator._path().exists():
                    estimator._path().unlink()
                for step in range(3):
                    self.observe(step)
                result = self.observe(3, **{field: 6000 if field == 'unpriced' else 2})
                self.assertEqual(result['usdSampleCount'], 2)
                self.assertNotIn('estimatedTotalUsd', result)
                # Decreases (backfill/reclassification/retention) also break
                # this dollar interval, even if total known tokens match.
                result = self.observe(4)
                self.assertEqual(result['usdSampleCount'], 2)

    def test_partial_history_still_requires_exact_priced_token_delta(self):
        for step in range(3):
            self.observe(step)
        result = self.observe(3, priced_delta=29999)
        self.assertEqual(result['usdSampleCount'], 2)
        self.assertNotIn('estimatedTotalUsd', result)

    def test_size_pressure_prunes_archives_before_accounts(self):
        for step in range(4):
            self.observe(step)
        states = estimator._load(estimator._path())
        identity = estimator._identity(self.account(3))
        active = states[identity]
        other = copy.deepcopy(active)
        other['lastAt'] -= 1000
        other_id = estimator._hash('other')
        states[other_id] = other
        base_size = len(json.dumps({'schemaVersion': estimator.SCHEMA, 'accounts': states},
                                   separators=(',', ':')).encode())
        for state in states.values():
            state['history'] = [{'lastAt': self.start - index * 1000,
                                 'samples': copy.deepcopy(state['samples']) * 16} for index in range(8)]
        with patch.object(estimator, 'MAX_FILE_BYTES', base_size + 300):
            estimator._save(estimator._path(), states, keep_identity=identity)
            self.assertLessEqual(estimator._path().stat().st_size, estimator.MAX_FILE_BYTES)
            saved = estimator._load(estimator._path())
            self.assertEqual(set(saved), {identity, other_id})
            self.assertEqual(len(saved[identity]['samples']), 3)
            self.assertTrue(saved[identity]['prunedHistoryCount'])
            result = self.observe(4)
            self.assertEqual(result['sampleCount'], 4)
            self.assertLessEqual(estimator._path().stat().st_size, estimator.MAX_FILE_BYTES)

    def test_size_pressure_lru_preserves_written_account_and_next_sample(self):
        for step in range(4):
            self.observe(step)
        states = estimator._load(estimator._path())
        identity = estimator._identity(self.account(3))
        base_size = estimator._path().stat().st_size
        for index in range(3):
            other = copy.deepcopy(states[identity])
            other['lastAt'] += 10000 + index
            states[estimator._hash(index)] = other
        with patch.object(estimator, 'MAX_FILE_BYTES', base_size + 400):
            estimator._save(estimator._path(), states, keep_identity=identity)
            self.assertLessEqual(estimator._path().stat().st_size, estimator.MAX_FILE_BYTES)
            self.assertEqual(set(estimator._load(estimator._path())), {identity})
            result = self.observe(4)
            self.assertEqual(result['sampleCount'], 4)
            self.assertLessEqual(estimator._path().stat().st_size, estimator.MAX_FILE_BYTES)
            self.assertEqual(estimator.read_summary(self.account(4), now=self.start + 1200)['sampleCount'], 4)

    def test_partial_without_unknown_counters_cannot_certify_usd_interval(self):
        self.observe(0)
        states = estimator._load(estimator._path())
        identity = estimator._identity(self.account(0))
        states[identity]['anchorPricing'].pop('invalidRowCount')
        estimator._save(estimator._path(), states, keep_identity=identity)
        self.observe(1)
        state = estimator._load(estimator._path())[identity]
        self.assertNotIn('usd', state['samples'][0])


if __name__ == '__main__':
    unittest.main()
