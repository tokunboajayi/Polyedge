"""Tests for core/signals/orderbook_confirm.py"""

import unittest

from core.signals.orderbook_confirm import (
    ConfirmResult,
    MIN_DEPTH_DOLLARS,
    SPOOF_CONCENTRATION,
    OrderbookConfirm,
    _check_depth,
    _estimate_slippage,
    _normalise_levels,
)
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _book(yes_levels=None, no_levels=None):
    return {
        "yes": yes_levels or [],
        "no":  no_levels  or [],
    }


def _deep_book(price_cents=60, side="yes", levels=5, size_per_level=500):
    """Build a realistic deep orderbook for one side."""
    return {
        side: [
            [price_cents + i, size_per_level]
            for i in range(levels)
        ]
    }


# ---------------------------------------------------------------------------
# _normalise_levels
# ---------------------------------------------------------------------------

class TestNormaliseLevels(unittest.TestCase):
    def test_cents_converted_to_dollars(self):
        levels = _normalise_levels([[60, 100], [62, 200]])
        self.assertAlmostEqual(levels[0][0], 0.60, places=4)
        self.assertAlmostEqual(levels[1][0], 0.62, places=4)

    def test_dollar_input_unchanged(self):
        levels = _normalise_levels([[0.60, 100]])
        self.assertAlmostEqual(levels[0][0], 0.60, places=4)

    def test_sorted_ascending_by_price(self):
        levels = _normalise_levels([[65, 100], [60, 200], [62, 150]])
        prices = [l[0] for l in levels]
        self.assertEqual(prices, sorted(prices))

    def test_zero_size_filtered_out(self):
        levels = _normalise_levels([[60, 0], [62, 100]])
        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0][0], 0.62)

    def test_empty_input(self):
        self.assertEqual(_normalise_levels([]), [])


# ---------------------------------------------------------------------------
# _check_depth
# ---------------------------------------------------------------------------

class TestCheckDepth(unittest.TestCase):
    def test_basic_depth_calculation(self):
        # (0.60*300) + (0.61*200) + (0.62*100) = 180 + 122 + 62 = 364
        levels = [(0.60, 300), (0.61, 200), (0.62, 100)]
        depth, spoof = _check_depth(levels, 0.60)
        self.assertAlmostEqual(depth, 364.0, places=1)
        self.assertFalse(spoof)

    def test_spoof_detected_when_one_level_dominates(self):
        # Level 1 has 10000 contracts, everything else tiny
        levels = [(0.60, 10000), (0.61, 10), (0.62, 10)]
        depth, spoof = _check_depth(levels, 0.60)
        self.assertTrue(spoof)

    def test_no_spoof_when_distributed(self):
        levels = [(0.60, 200), (0.61, 180), (0.62, 200)]
        _, spoof = _check_depth(levels, 0.60)
        self.assertFalse(spoof)

    def test_empty_levels_returns_zero(self):
        depth, spoof = _check_depth([], 0.60)
        self.assertAlmostEqual(depth, 0.0)
        self.assertFalse(spoof)


# ---------------------------------------------------------------------------
# _estimate_slippage
# ---------------------------------------------------------------------------

class TestEstimateSlippage(unittest.TestCase):
    def test_zero_slippage_all_at_best_price(self):
        # All contracts available at best ask
        levels   = [(0.60, 1000)]
        slippage = _estimate_slippage(levels, 10, 0.60)
        self.assertAlmostEqual(slippage, 0.0, places=6)

    def test_positive_slippage_when_walking_book(self):
        # 5 at 0.60, 5 at 0.65 → avg = (5*0.60 + 5*0.65)/10 = 0.625
        # slippage vs best = (0.625 - 0.60)/0.60 = 4.17%
        levels   = [(0.60, 5), (0.65, 500)]
        slippage = _estimate_slippage(levels, 10, 0.60)
        expected = (0.625 - 0.60) / 0.60
        self.assertAlmostEqual(slippage, expected, places=4)

    def test_returns_1_when_insufficient_depth(self):
        levels   = [(0.60, 3)]
        slippage = _estimate_slippage(levels, 10, 0.60)
        self.assertAlmostEqual(slippage, 1.0)

    def test_zero_contracts_returns_zero(self):
        levels   = [(0.60, 100)]
        slippage = _estimate_slippage(levels, 0, 0.60)
        self.assertAlmostEqual(slippage, 0.0)


# ---------------------------------------------------------------------------
# OrderbookConfirm.check
# ---------------------------------------------------------------------------

class TestOrderbookConfirm(unittest.TestCase):
    def setUp(self):
        self.confirm = OrderbookConfirm()

    def _good_book(self, direction="buy_yes", price_cents=60):
        side = "yes" if direction == "buy_yes" else "no"
        return {side: [[price_cents + i, 500] for i in range(5)]}

    def test_confirms_good_orderbook(self):
        result = self.confirm.check("buy_yes", self._good_book("buy_yes"), num_contracts=5)
        self.assertTrue(result.confirmed)
        self.assertEqual(result.reason, "all_checks_passed")

    def test_confirms_buy_no(self):
        result = self.confirm.check("buy_no", self._good_book("buy_no", 40), num_contracts=5)
        self.assertTrue(result.confirmed)

    def test_rejects_empty_orderbook(self):
        result = self.confirm.check("buy_yes", _book(), num_contracts=5)
        self.assertFalse(result.confirmed)
        self.assertIn("empty", result.reason)

    def test_rejects_insufficient_depth(self):
        # Only 1 contract at 0.60 = $0.60 depth — way below $200 threshold
        result = self.confirm.check(
            "buy_yes",
            {"yes": [[60, 1]]},
            num_contracts=1,
        )
        self.assertFalse(result.confirmed)
        self.assertIn("insufficient_depth", result.reason)

    def test_rejects_spoof_orderbook(self):
        # One massive level, rest tiny
        book   = {"yes": [[60, 100000], [61, 5], [62, 5]]}
        result = self.confirm.check("buy_yes", book, num_contracts=5)
        self.assertFalse(result.confirmed)
        self.assertIn("spoof", result.reason)

    def test_rejects_high_slippage(self):
        # 5 levels of 100 contracts spread widely — passes depth ($395) and
        # spoof check (max level 24%). Order size 500 must walk all 5 levels:
        # avg fill = (60+70+80+90+95)/5 = 79c; slippage = (79-60)/60 = 31.7%
        book   = {"yes": [[60, 100], [70, 100], [80, 100], [90, 100], [95, 100]]}
        result = self.confirm.check(
            "buy_yes", book, num_contracts=500,
            max_slippage=0.03
        )
        self.assertFalse(result.confirmed)
        self.assertIn("slippage", result.reason)

    def test_returns_confirm_result_type(self):
        result = self.confirm.check("buy_yes", self._good_book(), num_contracts=5)
        self.assertIsInstance(result, ConfirmResult)

    def test_entry_price_is_best_ask(self):
        # Best ask = 60c = $0.60
        book   = {"yes": [[60, 500], [61, 500], [62, 500]]}
        result = self.confirm.check("buy_yes", book, num_contracts=5)
        self.assertAlmostEqual(result.entry_price, 0.60, places=4)

    def test_slippage_zero_for_small_order_in_deep_book(self):
        # Two large levels at the same best price — passes spoof check (50%/50%)
        # and all 5 contracts fill at best ask → zero slippage
        book   = {"yes": [[60, 5000], [61, 5000]]}
        result = self.confirm.check("buy_yes", book, num_contracts=5)
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.est_slippage, 0.0, places=4)

    def test_spoof_flag_set_in_result(self):
        book = {"yes": [[60, 100000], [61, 5], [62, 5]]}
        result = self.confirm.check("buy_yes", book, num_contracts=1)
        self.assertTrue(result.spoof_flag)

    def test_depth_dollars_reported_correctly(self):
        # 5 levels of 500 contracts at ~$0.60
        book   = {"yes": [[60 + i, 500] for i in range(5)]}
        result = self.confirm.check("buy_yes", book, num_contracts=5)
        # depth is sum of top 5 levels' price*size
        self.assertGreater(result.depth_dollars, MIN_DEPTH_DOLLARS)

    def test_custom_max_slippage_respected(self):
        # With a very tight slippage limit (0.1%), a walked book should fail
        book = {"yes": [[60, 2], [70, 1000]]}
        result = self.confirm.check(
            "buy_yes", book, num_contracts=5, max_slippage=0.001
        )
        self.assertFalse(result.confirmed)

    def test_no_book_on_wrong_side_rejects(self):
        # buy_yes but only no-side has data
        book = {"yes": [], "no": [[40, 500]]}
        result = self.confirm.check("buy_yes", book, num_contracts=5)
        self.assertFalse(result.confirmed)


if __name__ == "__main__":
    unittest.main()
