import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_manager_core as core
import quota_estimation as estimate


class QuotaEstimationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patch = patch.object(core, 'STATE_DIR', Path(self.tmp.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.start = 1_800_000_000
        self.reset = self.start + 6 * 86400

    def account(self, step, remaining, **updates):
        result = {'id': 'account-one', 'fingerprint': 'principal-one', 'plan': 'pro',
                  'accessToken': 'SECRET-MUST-NOT-BE-STORED',
                  'usage': {'updatedAt': self.start + step * 300, 'planRaw': 'pro',
                            'weekly': {'remainingPercent': remaining, 'resetAt': self.reset,
                                       'windowMinutes': 10080}}}
        result.update(updates)
        return result

    def local(self, step, tokens, **updates):
        result = {'accountId': 'account-one', 'observedAt': self.start + step * 300,
                  'cumulativeTokens': tokens, 'coverageComplete': True,
                  'coverageEpoch': 'generation-one', 'usageMissingCount': 0}
        result.update(updates)
        return result

    def observe(self, step, remaining, tokens, account=None, local=None):
        return estimate.observe(account or self.account(step, remaining),
                                local if local is not None else self.local(step, tokens),
                                now=self.start + step * 300)

    def calibrated(self):
        for i in range(6):
            result = self.observe(i, 90 - 3 * i, 1_000_000 + 30_000 * i)
        return result

    def test_first_sample_and_no_percentage_delta_do_not_fabricate(self):
        result = self.observe(0, 90, 100_000)
        self.assertEqual(result['status'], 'pending')
        self.assertIsNone(result['estimatedTotalTokens'])
        for i in range(1, 5):
            result = self.observe(i, 90, 100_000 + i * 1000)
        self.assertEqual(result['sampleCount'], 0)
        self.assertIsNone(result['estimatedTotalTokens'])
        self.assertEqual(result['localObservedTokens'], 4000)

    def test_pending_becomes_calibrated_constant_workload_in_k_units(self):
        for i in range(4):
            result = self.observe(i, 90 - i * 3, 1_000_000 + i * 30_000)
            self.assertEqual(result['status'], 'pending' if i < 3 else 'calibrated')
        self.assertEqual(result['estimatedTotalTokens']['estimate'], 1_000_000)
        self.assertEqual(result['estimatedTotalTokens']['lower'], 750_000)
        self.assertEqual(result['estimatedTotalTokens']['upper'], 1_500_000)
        self.assertEqual(result['estimatedUsedTokens']['estimate'], 190_000)
        self.assertEqual(result['estimatedRemainingTokens']['estimate'], 810_000)
        self.assertEqual(result['localObservedTokens'], 90_000)
        self.assertEqual(result['localCumulativeTokens'], 1_090_000)
        self.assertEqual(result['tokensPerK'], 1000)
        self.assertFalse(result['officialTokenLimit'])

    def test_one_percent_quantization_accumulates_nonoverlapping_samples(self):
        for i in range(7):
            result = self.observe(i, 100 - i, i * 10_000)
        self.assertEqual(result['sampleCount'], 3)
        self.assertEqual(result['estimatedTotalTokens']['estimate'], 1_000_000)
        self.assertEqual(result['estimatedTotalTokens']['lower'], 666_666)
        self.assertEqual(result['estimatedTotalTokens']['upper'], 2_000_000)
        state = next(iter(json.loads(estimate._path().read_text())['accounts'].values()))
        for before, after in zip(state['samples'], state['samples'][1:]):
            self.assertEqual(before['endAt'], after['startAt'])

    def test_moderate_workload_variation_has_range(self):
        tokens = 0
        self.observe(0, 95, tokens)
        for i, delta in enumerate([25_000, 30_000, 35_000, 28_000, 32_000], 1):
            tokens += delta
            result = self.observe(i, 95 - 3 * i, tokens)
        budget = result['estimatedTotalTokens']
        self.assertLess(budget['lower'], 1_000_000)
        self.assertGreater(budget['upper'], 1_000_000)
        self.assertEqual(result['confidence'], 'medium')

    def test_extreme_mixed_model_interval_is_robustly_rejected(self):
        tokens = 0
        self.observe(0, 95, tokens)
        for i, delta in enumerate([30_000, 30_000, 900_000, 30_000, 30_000], 1):
            tokens += delta
            result = self.observe(i, 95 - 3 * i, tokens)
        self.assertEqual(result['estimatedTotalTokens']['estimate'], 1_000_000)
        self.assertEqual(result['rejectedSampleCount'], 1)
        self.assertEqual(result['confidence'], 'low')

    def test_quota_regain_in_same_window_resets(self):
        self.calibrated()
        result = self.observe(6, 95, 1_200_000)
        self.assertEqual(result['reason'], 'quota_regained')
        self.assertEqual(result['sampleCount'], 0)
        self.assertIsNone(result['estimatedTotalTokens'])

    def test_new_window_and_paid_plan_do_not_reuse_calibration(self):
        for change in ('reset', 'plan', 'subscription'):
            with self.subTest(change=change):
                self.calibrated()
                account = self.account(6, 72)
                if change == 'reset':
                    account['usage']['weekly']['resetAt'] += 7 * 86400
                elif change == 'plan':
                    account['usage']['planRaw'] = 'plus'
                else:
                    account['subscriptionStartedAt'] = '2026-09-08T00:00:00Z'
                result = self.observe(6, 72, 1_180_000, account=account)
                self.assertEqual(result['reason'], 'quota_window_or_plan_changed')
                self.assertIsNone(result['estimatedTotalTokens'])
                estimate._path().unlink()

    def test_identity_is_principal_and_account_scoped(self):
        self.calibrated()
        different_principal = self.account(6, 72, fingerprint='other-principal')
        result = self.observe(6, 72, 1_180_000, account=different_principal)
        self.assertEqual(result['sampleCount'], 0)
        wrong_local = self.local(7, 1_200_000, accountId='other-account')
        result = self.observe(7, 69, 1_200_000, local=wrong_local)
        self.assertEqual(result['reason'], 'local_account_mismatch')

    def test_partial_statistics_break_continuity(self):
        self.calibrated()
        result = self.observe(6, 72, 1_180_000,
                              local=self.local(6, 1_180_000, coverageComplete=False))
        self.assertEqual(result['reason'], 'local_coverage_incomplete')
        result = self.observe(7, 69, 1_210_000)
        self.assertEqual(result['sampleCount'], 0)

    def test_counter_regression_and_generation_change_reset(self):
        self.calibrated()
        result = self.observe(6, 72, 10)
        self.assertEqual(result['reason'], 'local_counter_decreased')
        result = self.observe(7, 69, 40_000,
                              local=self.local(7, 40_000, coverageEpoch='backfilled'))
        self.assertEqual(result['reason'], 'local_counter_generation_changed')

    def test_stale_missing_expired_and_nonfinite_official_observations_ignored(self):
        for field, value, expected in [
            ('stale', True, 'official_quota_stale'),
            ('remainingPercent', None, 'official_quota_unavailable'),
            ('remainingPercent', float('nan'), 'official_quota_unavailable'),
            ('resetAt', self.start - 1, 'official_quota_stale'),
            ('windowMinutes', None, 'official_quota_unavailable'),
        ]:
            with self.subTest(field=field, value=value):
                account = self.account(0, 90)
                account['usage']['weekly'][field] = value
                result = self.observe(0, 90, 0, account=account)
                self.assertEqual(result['reason'], expected)
                self.assertIsNone(result['estimatedTotalTokens'])
        account = self.account(0, 90)
        result = estimate.observe(account, self.local(0, 0), now=self.start + 1000)
        self.assertEqual(result['reason'], 'official_quota_stale')

    def test_alignment_uses_sample_time_not_last_event_time(self):
        result = self.observe(1, 90, 100, local=self.local(0, 100))
        self.assertEqual(result['reason'], 'observation_time_mismatch')

    def test_duplicate_and_out_of_order_do_not_add_samples(self):
        self.observe(0, 90, 0)
        self.observe(1, 87, 30_000)
        result = self.observe(1, 87, 30_000)
        self.assertEqual(result['sampleCount'], 1)
        result = self.observe(0, 90, 0)
        self.assertEqual(result['reason'], 'duplicate_or_out_of_order_quota')
        state = next(iter(json.loads(estimate._path().read_text())['accounts'].values()))
        self.assertEqual(len(state['samples']), 1)

    def test_duplicate_calibrated_observation_preserves_summary_without_new_sample(self):
        before = self.calibrated()
        after = self.observe(5, 75, 1_150_000)
        self.assertEqual(before, after)

    def test_read_summary_is_read_only_and_preserves_calibration(self):
        before = self.calibrated()
        path = estimate._path()
        raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(3):
            result = estimate.read_summary(self.account(5, 75), now=self.start + 1500)
            self.assertEqual(result['estimatedTotalTokens'], before['estimatedTotalTokens'])
            self.assertEqual(result['sampleCount'], 5)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_read_summary_projects_fresh_same_window_without_training(self):
        self.calibrated()
        result = estimate.read_summary(self.account(6, 72), now=self.start + 1800)
        self.assertEqual(result['status'], 'calibrated')
        self.assertEqual(result['estimatedRemainingTokens']['estimate'], 720_000)
        self.assertEqual(result['sampleCount'], 5)
        self.assertTrue(result['quotaProjectionOnly'])
        self.assertEqual(result['calibrationObservedAt'], self.start + 1500)
        self.assertEqual(result['quotaObservedAt'], self.start + 1800)

    def test_read_summary_hides_stale_regained_or_new_plan_estimates(self):
        self.calibrated()
        result = estimate.read_summary(self.account(6, 99), now=self.start + 1800)
        self.assertEqual(result['reason'], 'quota_regained')
        self.assertEqual(result['localCumulativeTokens'], 1_150_000)
        changed = self.account(6, 72)
        changed['usage']['planRaw'] = 'plus'
        result = estimate.read_summary(changed, now=self.start + 1800)
        self.assertEqual(result['reason'], 'quota_window_or_plan_changed')
        result = estimate.read_summary(self.account(5, 75), now=self.start + 3000)
        self.assertEqual(result['reason'], 'official_quota_stale')
        self.assertIsNone(result['estimatedTotalTokens'])

    def test_partial_observation_exposes_and_persists_measured_totals_immediately(self):
        local = self.local(0, 123_456, coverageComplete=False)
        result = self.observe(0, 90, 123_456, local=local)
        self.assertEqual(result['reason'], 'local_coverage_incomplete')
        self.assertEqual(result['localCumulativeTokens'], 123_456)
        self.assertEqual(result['localCumulativeKTokens'], 123.456)
        self.assertFalse(result['localCoverageComplete'])
        result = estimate.read_summary(self.account(0, 90), now=self.start)
        self.assertEqual(result['localCumulativeTokens'], 123_456)
        self.assertIsNone(result['estimatedTotalTokens'])
        result = self.observe(1, 87, 153_456)
        self.assertEqual(result['sampleCount'], 0)
        self.assertEqual(result['localObservedTokens'], 0)

    def test_unavailable_quota_still_exposes_local_measured_tokens(self):
        account = self.account(0, None)
        result = self.observe(0, None, 1234, account=account)
        self.assertEqual(result['localCumulativeTokens'], 1234)
        result = estimate.read_summary(account, now=self.start)
        self.assertEqual(result['reason'], 'official_quota_unavailable')
        self.assertEqual(result['localCumulativeTokens'], 1234)

    def test_read_without_store_does_not_create_files(self):
        result = estimate.read_summary(self.account(0, 90), now=self.start)
        self.assertEqual(result['reason'], 'awaiting_paired_observations')
        self.assertFalse(estimate._path().exists())

    def test_remote_decline_without_local_activity_invalidates(self):
        self.calibrated()
        result = self.observe(6, 72, 1_150_000)
        self.assertEqual(result['reason'], 'quota_drop_without_local_usage')
        self.assertIsNone(result['estimatedTotalTokens'])

    def test_percentage_clamp_and_unknown_plan(self):
        self.assertEqual(self.observe(0, 999, 0)['remainingPercent'], 100)
        self.assertEqual(self.observe(1, -12, 100)['remainingPercent'], 0)
        account = self.account(2, 80, plan='unknown')
        account['usage']['planRaw'] = 'unknown'
        self.assertEqual(self.observe(2, 80, 100, account=account)['reason'], 'plan_unavailable')

    def test_preparation_uses_only_attributed_totals_not_subset_tokens(self):
        snapshot = {'records': [
            {'accountId': 'a', 'totalTokens': 120, 'inputTokens': 100,
             'cachedInputTokens': 80, 'outputTokens': 20, 'reasoningOutputTokens': 10},
            {'accountId': 'a', 'totalTokens': 30},
            {'accountId': 'b', 'totalTokens': 999, 'usageMissingCount': 1},
            {'providerId': 'external', 'totalTokens': 1234},
        ]}
        result = estimate.prepare_observations(snapshot, observed_at=self.start,
                                               coverage_complete=True, coverage_epoch='one')
        self.assertEqual(set(result), {'a', 'b'})
        self.assertEqual(result['a']['cumulativeTokens'], 150)
        self.assertTrue(result['a']['coverageComplete'])
        self.assertFalse(result['b']['coverageComplete'])
        result = estimate.prepare_observations(snapshot, observed_at=self.start)
        self.assertFalse(result['a']['coverageComplete'])
        snapshot['coverage'] = {'partialFiles': 1}
        result = estimate.prepare_observations(snapshot, observed_at=self.start,
                                               coverage_complete=True, coverage_epoch='one')
        self.assertFalse(result['a']['coverageComplete'])

    def test_dynamic_directory_bounded_store_and_no_secrets(self):
        self.calibrated()
        text = estimate._path().read_text()
        for sensitive in ['SECRET-MUST-NOT-BE-STORED', 'principal-one', 'account-one', 'pro']:
            self.assertNotIn(sensitive, text)
        self.assertLess(len(text), estimate.MAX_FILE_BYTES)
        with tempfile.TemporaryDirectory() as other:
            with patch.object(core, 'STATE_DIR', Path(other)):
                result = self.observe(7, 69, 1_210_000)
                self.assertEqual(result['sampleCount'], 0)
                self.assertTrue(estimate._path().exists())

    def test_corrupt_file_recovers_without_failure(self):
        estimate._path().write_text('{bad-json', encoding='utf-8')
        result = self.observe(0, 90, 1000)
        self.assertEqual(result['reason'], 'insufficient_intervals')
        self.assertEqual(json.loads(estimate._path().read_text())['schemaVersion'], 1)

    def test_persistence_survives_independent_calls_and_bounds_account_count(self):
        with patch.object(estimate, 'MAX_ACCOUNTS', 3):
            for i in range(7):
                account = self.account(i, 90, id=f'account-{i}')
                local = self.local(i, 0, accountId=f'account-{i}')
                self.observe(i, 90, 0, account=account, local=local)
        stored = json.loads(estimate._path().read_text())
        self.assertEqual(len(stored['accounts']), 3)


    def test_pro_and_pro20x_aliases_share_one_calibration_scope(self):
        self.calibrated()
        account = self.account(6, 71)
        account["usage"]["planRaw"] = "pro20x"
        result = estimate.read_summary(account, now=self.start + 6 * 300)
        self.assertEqual(result["status"], "calibrated")
        account["usage"]["planRaw"] = "pro5x"
        self.assertEqual(estimate.read_summary(account, now=self.start + 6 * 300)["reason"], "quota_window_or_plan_changed")


if __name__ == '__main__':
    unittest.main()
