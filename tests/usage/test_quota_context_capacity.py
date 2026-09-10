import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_manager.core as core
from agent_manager.usage import estimation as estimator
from agent_manager.usage.capacity import compare_capacity, interval_reference
from agent_manager.usage.pricing import equivalent, normalize_context_tier, snapshot_equivalent, PRICE_VERSION


def row(**changes):
    return dict(actualModel='gpt-5.6-sol', modelEvidence='actual', model='gpt-5.6-sol',
                inputTokens=100000, cachedInputTokens=20000, cacheWriteTokens=10000,
                outputTokens=10000, reasoningOutputTokens=5000, requestCount=5,
                usageEvidenceVersion=2, serviceTier='default', contextTier='short',
                cacheWriteEvidence='known', inputOutputEvidence='known', cachedInputEvidence='known',
                reasoningEvidence='known') | changes


class ContextPricingTests(unittest.TestCase):
    def test_272k_boundary_and_user_examples(self):
        self.assertEqual(normalize_context_tier(272000), 'short')
        self.assertEqual(normalize_context_tier(272001), 'long')
        self.assertEqual(normalize_context_tier(None), 'unknown')
        for model, cost in [('gpt-5.6-luna', .0544), ('gpt-5.6-sol', 1.088)]:
            r = row(actualModel=model, inputTokens=272000, cachedInputTokens=0, cacheWriteTokens=0, outputTokens=0)
            self.assertAlmostEqual(equivalent([r])['knownUsd'], cost)
            r.update(inputTokens=272001, contextTier='long', outputTokens=1000)
            expected = 272001 * (0.4 if 'luna' in model else 8) / 1e6 + (1.8 if 'luna' in model else 30) / 1000
            self.assertAlmostEqual(equivalent([r])['knownUsd'], expected)

    def test_long_full_request_fast_cache_write_and_reasoning_subsets(self):
        result = equivalent([row(contextTier='long', serviceTier='priority')])
        # Entire input split: 70k ordinary*16 +20k read*1.6 +10k write*20; 10k output*60.
        self.assertAlmostEqual(result['knownUsd'], 1.952)
        self.assertEqual(result['pricedTokens'], 110000)
        self.assertAlmostEqual(result['breakdown'][0]['cacheWriteUsd'], .2)
        mixed = equivalent([row(), row(actualModel='gpt-6-astra', contextTier='long')])
        self.assertAlmostEqual(mixed['knownUsd'], .538 + 2.44)

    def test_old_native_and_missing_fields_are_not_short_default_zero(self):
        native = row(provider='openai', accountId='a', agentRole='mainAgent')
        for key in ('actualModel', 'serviceTier', 'contextTier', 'cacheWriteEvidence', 'cachedInputEvidence'):
            native.pop(key, None)
        result = snapshot_equivalent('a', {'codexSessions': {'records': [native]}})
        self.assertIsNone(result['knownUsd'])
        self.assertIsNotNone(result['conditionalReferenceUsd']['lower'])
        for change in ({'serviceTier': 'ultrafast'}, {'actualModel': 'unknown-model'},
                       {'inputOutputEvidence': 'unknown'}):
            result = equivalent([row(**change)])
            self.assertIsNone(result['conditionalReferenceUsd']['lower'])
        self.assertIsNone(equivalent([row(cacheWriteTokens=90000)])['knownUsd'])

    def test_conditional_bounds_are_additive_and_repricing_cannot_create_interval(self):
        a = equivalent([row(serviceTier='unknown', cacheWriteEvidence='unknown')])
        b = equivalent([row(serviceTier='unknown', cacheWriteEvidence='unknown', inputTokens=200000,
                            cachedInputTokens=40000, cacheWriteTokens=20000, outputTokens=20000)])
        for p in (a, b): p['sourceHistoryComplete'] = True
        interval = interval_reference(a, b, 110000)
        self.assertIsNotNone(interval)
        self.assertLess(interval['lower'], interval['upper'])
        self.assertIsNone(interval_reference(a, b, 110001))
        altered = copy.deepcopy(b)
        key = next(iter(altered['conditionalReferenceUsd']['counters']))
        altered['conditionalReferenceUsd']['counters'][key]['upper'] = 0
        self.assertIsNone(interval_reference(a, altered, 110000))
        different = equivalent([row(serviceTier='default')]) | {'sourceHistoryComplete': True}
        self.assertIsNone(interval_reference(a, different, 110000))

    def test_gateway_capture_partition_persistence_and_pricing_contract(self):
        from agent_manager.gateway.service import UsageStatsStore, _SSEUsageCapture
        from agent_manager.usage.request_metadata import enrich_context
        with tempfile.TemporaryDirectory() as tmp:
            store = UsageStatsStore(Path(tmp) / 'usage.json')
            with patch.object(store, '_schedule_flush_locked'):
                for input_count, tier, write in [(272000, 'default', 1000), (272001, 'priority', 2000)]:
                    capture = _SSEUsageCapture()
                    capture.observe_event({'type': 'response.created', 'response': {'model': 'gpt-5.6-sol', 'service_tier': tier}})
                    completed = {'type': 'response.completed', 'response': {'usage': {
                        'input_tokens': input_count, 'output_tokens': 1000,
                        'input_tokens_details': {'cached_tokens': 100000, 'cache_write_tokens': write},
                        'output_tokens_details': {'reasoning_tokens': 500}}}}
                    capture.observe_event(completed)
                    capture.observe_event(completed)  # Capture replaces usage, does not add final frames.
                    store.record(enrich_context({'accountId': 'a', 'routedModel': 'gpt-5.6-sol'}, capture), capture.usage)
            store.flush(force=True)
            snapshot = UsageStatsStore(store.path).snapshot()
            routes = [r for day in snapshot['days'].values() for r in day['routes'].values()]
            self.assertEqual(len(routes), 2)
            self.assertEqual({r['contextTier'] for r in routes}, {'short', 'long'})
            self.assertEqual(snapshot['totals']['requestCount'], 2)
            self.assertEqual(snapshot['totals']['cacheWriteTokens'], 3000)
            price = snapshot_equivalent('a', snapshot)
            self.assertEqual(price['status'], 'available')
            expected = (171000*4 + 100000*.4 + 1000*5 + 1000*20 +
                        170001*16 + 100000*1.6 + 2000*20 + 1000*60) / 1e6
            self.assertAlmostEqual(price['knownUsd'], expected)


class CapacityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(core, 'STATE_DIR', Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        self.start = 1800000000
        self.cumulative = 1

    def account(self, cycle, step, drop=3, plan='pro'):
        return {'id': 'a', 'fingerprint': 'one', 'plan': plan, 'calibrationWorkloadScope': 'stable-config',
                'usage': {'updatedAt': self.start + cycle * 604800 + step * 300, 'planRaw': plan,
                          'weekly': {'remainingPercent': 95 - step * drop,
                                     'resetAt': self.start + (cycle + 1) * 604800, 'windowMinutes': 10080}}}

    def local(self, account, **changes):
        n = self.cumulative
        r = row(inputTokens=100000*n, cachedInputTokens=20000*n, cacheWriteTokens=10000*n,
                outputTokens=10000*n, reasoningOutputTokens=5000*n, requestCount=5*n, **changes)
        price = equivalent([r]) | {'sourceHistoryComplete': True}
        return {'accountId': 'a', 'observedAt': account['usage']['updatedAt'], 'cumulativeTokens': 110000*n,
                'coverageComplete': True, 'coverageEpoch': 'same-generation', 'apiEquivalent': price}

    def cycle(self, cycle, *, drop=3, plan='pro', **changes):
        for step in range(6):
            account = self.account(cycle, step, drop, plan)
            result = estimator.observe(account, self.local(account, **changes), now=account['usage']['updatedAt'])
            self.cumulative += 1
        return result

    def test_same_workload_decline_compares_two_distinct_prior_windows_and_restarts(self):
        self.cycle(0)
        self.cycle(1)
        result = self.cycle(2, drop=9)
        compare = result['capacityComparison']
        self.assertEqual(compare['status'], 'comparable_capacity_decline_signal')
        self.assertEqual(compare['comparableWindowCount'], 2)
        self.assertLess(compare['changePercent']['upper'], 0)
        self.assertFalse(compare['officialReductionProven'])
        code = '''import json,sys
from pathlib import Path
import agent_manager.core as core
from agent_manager.usage.estimation import read_summary,observe
core.STATE_DIR=Path(sys.argv[1])
a,l=json.loads(sys.argv[2]),json.loads(sys.argv[3])
print(json.dumps({'before':read_summary(a,now=a['usage']['updatedAt']), 'after':observe(a,l,now=a['usage']['updatedAt'])}))'''
        account = self.account(2, 6, 9)
        child = subprocess.run([sys.executable, '-c', code, self.tmp.name, json.dumps(account), json.dumps(self.local(account))],
                               capture_output=True, text=True, check=True, timeout=20,
                               env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src')))
        loaded = json.loads(child.stdout)
        self.assertEqual(loaded['before']['capacityComparison']['status'], compare['status'])
        self.assertEqual(loaded['after']['sampleCount'], 6)

    def test_changed_plan_model_cache_or_incomplete_reasoning_blocks_comparison(self):
        self.cycle(0); self.cycle(1); self.cycle(2)
        states = estimator._load(estimator._path())
        state = next(iter(states.values()))
        self.assertEqual(compare_capacity(state)['status'], 'comparable_no_clear_decline')
        for mismatch in ('plan', 'model', 'cache', 'reasoning'):
            with self.subTest(mismatch=mismatch):
                altered = copy.deepcopy(state)
                if mismatch == 'plan': altered['comparisonScope'] = 'different'
                for sample in altered['samples']:
                    if mismatch == 'reasoning': sample.pop('workload', None)
                    if mismatch == 'cache': next(iter(sample['workload'].values()))['cached'] = 0
                    if mismatch == 'model':
                        sample['workload']['other-model|default|short'] = sample['workload'].pop(next(iter(sample['workload'])))
                self.assertEqual(compare_capacity(altered)['status'], 'insufficient_comparable_evidence')

    def test_same_reset_segments_cannot_impersonate_two_historical_windows(self):
        self.cycle(0); self.cycle(1, drop=9)
        state = next(iter(estimator._load(estimator._path()).values()))
        state['history'] *= 2
        compared = compare_capacity(state)
        self.assertEqual(compared['comparableWindowCount'], 1)
        self.assertEqual(compared['status'], 'insufficient_comparable_evidence')

    def test_partial_conditional_tokens_unknown_activity_and_generation_break_do_not_bridge(self):
        self.cycle(0, serviceTier='unknown')
        account = self.account(0, 6)
        local = self.local(account, serviceTier='unknown')
        local['coverageEpoch'] = 'rebuilt'
        result = estimator.observe(account, local, now=account['usage']['updatedAt'])
        self.assertEqual(result['sampleCount'], 5)
        state = next(iter(estimator._load(estimator._path()).values()))
        self.assertEqual(state['anchorTokens'], local['cumulativeTokens'])
        self.cumulative += 1
        account = self.account(0, 7)
        local = self.local(account, serviceTier='unknown')
        local['coverageEpoch'] = 'rebuilt'
        local['apiEquivalent']['conditionalReferenceUsd']['unpricedFingerprint'] = 'unknown-activity'
        result = estimator.observe(account, local, now=account['usage']['updatedAt'])
        self.assertEqual(result['conditionalTotalUsd']['sampleCount'], 5)
        state = next(iter(estimator._load(estimator._path()).values()))
        self.assertNotIn('conditionalUsd', state['samples'][-1])

    def test_conditional_total_requires_full_intervals_and_never_flags_capacity(self):
        result = self.cycle(0, serviceTier='unknown', cacheWriteEvidence='unknown')
        self.assertIsNone(result.get('estimatedTotalUsd'))
        self.assertGreater(result['conditionalTotalUsd']['lower'], 0)
        self.assertGreater(result['conditionalTotalUsd']['upper'], result['conditionalTotalUsd']['lower'])
        self.assertEqual(result['capacityComparison']['status'], 'insufficient_comparable_evidence')
        self.assertNotIn('estimate', result['conditionalTotalUsd'])

    def test_schema1_keeps_old_samples_but_cannot_use_old_usd_for_new_price_or_comparison(self):
        self.cycle(0)
        value = json.loads(estimator._path().read_text(encoding='utf-8'))
        value['schemaVersion'] = 1
        state = next(iter(value['accounts'].values()))
        state.pop('comparisonScope')
        for sample in state['samples']:
            sample['priceVersion'] = 'openai-standard-short-2026-09-10'
            sample.pop('workload', None); sample.pop('conditionalUsd', None)
        estimator._path().write_text(json.dumps(value), encoding='utf-8')
        account = self.account(0, 5)
        result = estimator.read_summary(account, now=account['usage']['updatedAt'])
        self.assertEqual(result['sampleCount'], 5)
        self.assertTrue(result['legacyEvidenceRetained'])
        self.assertIsNone(result.get('estimatedTotalUsd'))
        self.assertEqual(result['capacityComparison']['status'], 'insufficient_comparable_evidence')
        self.assertLess(estimator._path().stat().st_size, estimator.MAX_FILE_BYTES)


if __name__ == '__main__':
    unittest.main()
