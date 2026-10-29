"""Stage 2 tests: run `python -m unittest test_price_estimator.py -v`.
Does not require a running database. Uses the SAME 10k synthetic transactions.
"""
import json
import unittest
from datetime import date
from pathlib import Path
from price_estimator import (OfflineRepository, estimate, percentile,
                             variant_limit_compatible, canonical_key)


class ValuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(__file__).parent / 'data'
        cls.repo = OfflineRepository(cls.data_dir)
        cls.fixtures = json.loads((cls.data_dir / 'demo_targets.json').read_text())
        cls.as_of = date(2026, 10, 28)

    def run_fixture(self, name):
        f = self.fixtures[name]
        return estimate(self.repo, f['card'], f['target_grade_bucket'], self.as_of, name)

    def test_level_1(self):
        result = self.run_fixture('L1_EXACT')
        self.assertEqual(result.method, 'L1_EXACT')
        self.assertGreaterEqual(result.comparable_count, 3)
        self.assertEqual(result.estimate_type, 'MARKET_ESTIMATE')
        self.assertEqual(result.confidence, 'HIGH')

    def test_level_2(self):
        result = self.run_fixture('L2_GRADE_FALLBACK')
        self.assertEqual(result.method, 'L2_GRADE_ADJUSTED')
        self.assertEqual(result.grade_reference, 'PSA_10')
        self.assertGreaterEqual(result.grade_ratio_pairs, 5)
        self.assertGreater(result.grade_multiplier, 0)
        self.assertLess(result.grade_multiplier, 1)

    def test_level_3(self):
        result = self.run_fixture('L3_VARIANT_FALLBACK')
        self.assertEqual(result.method, 'L3_SIMILAR_VARIANTS')
        self.assertEqual(result.estimate_type, 'INDICATIVE_SIMILAR_CARD_BENCHMARK')
        self.assertEqual(result.confidence, 'LOW')

    def test_level_4(self):
        result = self.run_fixture('L4_COHORT_FALLBACK')
        self.assertEqual(result.method, 'L4_OTHER_PLAYERS')
        self.assertEqual(result.estimate_type, 'CATEGORY_BENCHMARK_NOT_CARD_VALUE')
        self.assertEqual(result.confidence, 'VERY_LOW')

    def test_unknown_card(self):
        card = dict(self.fixtures['L1_EXACT']['card'])
        card['card_number'] = 'NOT-A-REAL-CARD'
        result = estimate(self.repo, card, 'PSA_9', self.as_of)
        self.assertEqual(result.method, 'UNKNOWN_CARD')
        self.assertIsNone(result.estimated_price_usd)

    def test_old_data_has_no_comps(self):
        f = self.fixtures['L1_EXACT']
        result = estimate(self.repo, f['card'], 'PSA_9', date(2028, 1, 1))
        self.assertEqual(result.method, 'NO_COMPS')

    def test_identity_change_changes_key(self):
        card = self.fixtures['L1_EXACT']['card']
        changed = dict(card, parallel_code='ORANGE')
        self.assertNotEqual(canonical_key(card), canonical_key(changed))

    def test_rarity_matching_rule(self):
        self.assertTrue(variant_limit_compatible(99, 150))
        self.assertTrue(variant_limit_compatible(99, 25))
        self.assertFalse(variant_limit_compatible(99, None))
        self.assertFalse(variant_limit_compatible(99, 10))

    def test_percentile(self):
        self.assertEqual(percentile([10,20,30,40], .5), 25)

if __name__ == '__main__':
    unittest.main()
