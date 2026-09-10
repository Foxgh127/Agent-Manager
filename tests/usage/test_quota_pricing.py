import unittest

from agent_manager.usage.pricing import equivalent, snapshot_equivalent


class QuotaPricingTests(unittest.TestCase):
    def row(self, **changes):
        return {'model': 'gpt-6-astra', 'inputTokens': 1_000_000,
                'cachedInputTokens': 800_000, 'outputTokens': 100_000,
                'reasoningOutputTokens': 70_000, **changes}

    def test_split_prices_exclude_double_counted_cache_and_reasoning(self):
        result = equivalent([self.row()])
        self.assertEqual(result['knownUsd'], 7.8)  # 200K*10 + 800K*1 + 100K*50 / 1M
        self.assertEqual(result['pricedTokens'], 1_100_000)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['breakdown'][0]['uncachedInputUsd'], 2)
        self.assertFalse(result['actualBalance'])

    def test_actual_models_are_priced_independently(self):
        result = equivalent([self.row(), self.row(model='gpt-5.6-sol')])
        self.assertEqual(result['knownUsd'], 10.92)
        self.assertEqual(len(result['breakdown']), 2)

    def test_unknown_models_and_aliases_are_not_guessed(self):
        result = equivalent([self.row(model='gpt-6-custom'), self.row()])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['knownUsd'], 7.8)
        self.assertEqual(result['unpricedTokens'], 1_100_000)
        self.assertIsNone(equivalent([self.row(model='gpt-6')])['knownUsd'])

    def test_missing_invalid_nonfinite_and_subset_overflow_are_unknown(self):
        for changes in ({'inputTokens': None}, {'cachedInputTokens': 1_100_000},
                        {'outputTokens': float('inf')}, {'outputTokens': True},
                        {'usageMissingCount': 1}, {'cachedInputTokens': -1}):
            with self.subTest(changes=changes):
                result = equivalent([self.row(**changes)])
                self.assertEqual(result['status'], 'unknown')
                self.assertIsNone(result['knownUsd'])
        self.assertIsNone(equivalent([])['knownUsd'])

    def test_retained_scope_excludes_other_accounts_and_gateway_log_duplicates(self):
        native = self.row(accountId='a', provider='openai', agentRole='mainAgent', date='2026-09-09')
        snapshot = {'days': {'2026-09-10': {'routes': {'one': self.row(accountId='a'),
                                                     'other': self.row(accountId='b')}}},
                    'codexSessions': {'records': [native, {**native, 'provider': 'agent_manager'},
                                                 {**native, 'agentRole': 'subagent'}],
                                      'coverage': {'partialFiles': 1}}}
        result = snapshot_equivalent('a', snapshot)
        self.assertEqual(result['knownUsd'], 15.6)
        self.assertEqual(result['firstDate'], '2026-09-09')
        self.assertEqual(result['lastDate'], '2026-09-10')
        self.assertFalse(result['sourceHistoryComplete'])


if __name__ == '__main__':
    unittest.main()
