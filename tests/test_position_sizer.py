"""Tests for core/risk/position_sizer.py"""

import unittest

from core.risk.position_sizer import (
    PositionSize,
    PositionSizer,
    _select_phase,
    kelly_fraction,
)
from config import constants as C


class TestSelectPhase(unittest.TestCase):
    def test_micro_live_below_30(self):
        phase, mult = _select_phase(0)
        self.assertEqual(phase, "micro_live")
        self.assertAlmostEqual(mult, C.KELLY_MICRO_LIVE)

    def test_micro_live_at_29(self):
        phase, _ = _select_phase(29)
        self.assertEqual(phase, "micro_live")

    def test_calibration_at_30(self):
        phase, mult = _select_phase(30)
        self.assertEqual(phase, "calibration")
        self.assertAlmostEqual(mult, C.KELLY_CALIBRATION)

    def test_calibration_at_59(self):
        phase, _ = _select_phase(59)
        self.assertEqual(phase, "calibration")

    def test_full_at_60(self):
        phase, mult = _select_phase(60)
        self.assertEqual(phase, "full")
        self.assertAlmostEqual(mult, C.KELLY_FULL)

    def test_full_at_large(self):
        phase, _ = _select_phase(500)
        self.assertEqual(phase, "full")


class TestKellyFraction(unittest.TestCase):
    def test_positive_edge(self):
        # model 70%, price 50% — clear positive Kelly
        frac = kelly_fraction(0.70, 0.50)
        self.assertGreater(frac, 0.0)

    def test_no_edge_at_fair_price(self):
        # model = price exactly → zero Kelly (after fees may be negative)
        frac = kelly_fraction(0.50, 0.50)
        self.assertAlmostEqual(frac, 0.0, places=2)

    def test_negative_model_prob_returns_zero(self):
        frac = kelly_fraction(0.30, 0.60)
        self.assertAlmostEqual(frac, 0.0)

    def test_high_price_fee_drag(self):
        # price near 0.99 → huge fee drag, nearly zero profit
        frac = kelly_fraction(0.99, 0.99)
        self.assertAlmostEqual(frac, 0.0, places=1)


class TestPositionSizer(unittest.TestCase):
    def setUp(self):
        self.sizer = PositionSizer()

    def _size(self, model_prob=0.70, entry=0.50, bankroll=500.0, resolved=35):
        return self.sizer.size(model_prob, entry, bankroll, resolved)

    def test_returns_position_size(self):
        result = self._size()
        self.assertIsInstance(result, PositionSize)

    def test_eligible_with_good_edge(self):
        result = self._size(model_prob=0.70, entry=0.50, bankroll=500.0)
        self.assertTrue(result.eligible)
        self.assertGreater(result.num_contracts, 0)

    def test_ineligible_no_edge(self):
        result = self._size(model_prob=0.50, entry=0.50)
        self.assertFalse(result.eligible)
        self.assertEqual(result.num_contracts, 0)

    def test_ineligible_negative_edge(self):
        result = self._size(model_prob=0.30, entry=0.70)
        self.assertFalse(result.eligible)

    def test_capped_at_max_position_pct(self):
        # Large bankroll + large edge → should cap at 5% of bankroll
        result = self.sizer.size(0.95, 0.10, bankroll=10_000.0, resolved_trade_count=60)
        max_dollars = 10_000.0 * C.MAX_POSITION_PCT
        self.assertLessEqual(result.dollar_size, max_dollars + 0.01)

    def test_micro_live_capped_at_10(self):
        result = self.sizer.size(0.85, 0.30, bankroll=500.0, resolved_trade_count=5)
        self.assertLessEqual(result.dollar_size, C.MAX_TRADE_SIZE_MICRO_LIVE + 0.01)

    def test_minimum_trade_size_enforced(self):
        # Very small bankroll → should be ineligible
        result = self.sizer.size(0.70, 0.50, bankroll=5.0, resolved_trade_count=35)
        # $5 bankroll × 5% = $0.25 max → below $5 minimum
        self.assertFalse(result.eligible)

    def test_phase_multiplier_applied(self):
        s_micro = self.sizer.size(0.70, 0.50, bankroll=500.0, resolved_trade_count=5)
        s_full  = self.sizer.size(0.70, 0.50, bankroll=500.0, resolved_trade_count=100)
        # Full Kelly should produce larger position than micro-live
        if s_micro.eligible and s_full.eligible:
            self.assertLessEqual(s_micro.adj_kelly, s_full.adj_kelly)

    def test_dollar_size_matches_contracts_times_price(self):
        result = self._size(entry=0.60)
        if result.eligible:
            expected = result.num_contracts * 0.60
            self.assertAlmostEqual(result.dollar_size, expected, places=2)

    def test_phase_is_calibration_at_35(self):
        result = self._size(resolved=35)
        self.assertEqual(result.phase, "calibration")

    def test_phase_is_full_at_60(self):
        result = self._size(resolved=60)
        self.assertEqual(result.phase, "full")

    def test_fee_drag_reported(self):
        result = self._size()
        self.assertGreater(result.fee_drag, 0.0)

    def test_kelly_fraction_in_result(self):
        result = self._size(model_prob=0.70, entry=0.50)
        if result.eligible:
            self.assertGreater(result.kelly_fraction, 0.0)

    def test_ineligible_result_has_zero_contracts(self):
        result = self._size(model_prob=0.40, entry=0.60)
        self.assertFalse(result.eligible)
        self.assertEqual(result.num_contracts, 0)
        self.assertAlmostEqual(result.dollar_size, 0.0)


if __name__ == "__main__":
    unittest.main()
