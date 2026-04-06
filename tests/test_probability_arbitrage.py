"""Tests for core/strategies/probability_arbitrage.py"""

import unittest
from unittest.mock import MagicMock

from core.strategies.probability_arbitrage import Signal, StrategyA, _tier_float
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_market(ticker="TEST-01", mid_price=0.50):
    m = MagicMock()
    m.ticker     = ticker
    m.title      = "Test market"
    m.category   = "economics"
    m.mid_price  = mid_price
    # ScannedMarket uses mid_price; StrategyA also accepts market_price attr
    # Don't set market_price so it falls back to mid_price
    del m.market_price  # ensure AttributeError, falls back to mid_price
    return m


def _make_estimate(
    action_eligible=True,
    action="buy_yes",
    divergence_pp=15.0,
    fee_drag_pp=0.44,
    final_prob=0.65,
    market_price=0.50,
    confidence_tier="high",
    n=60,
):
    e = MagicMock()
    e.action_eligible = action_eligible
    e.action          = action
    e.divergence_pp   = divergence_pp
    e.fee_drag_pp     = fee_drag_pp
    e.final_prob      = final_prob
    e.market_price    = market_price
    e.confidence_tier = confidence_tier
    e.n               = n
    return e


# ---------------------------------------------------------------------------
# _tier_float
# ---------------------------------------------------------------------------

class TestTierFloat(unittest.TestCase):
    def test_high(self):
        self.assertAlmostEqual(_tier_float("high"), 0.80)

    def test_medium(self):
        self.assertAlmostEqual(_tier_float("medium"), 0.55)

    def test_low(self):
        self.assertAlmostEqual(_tier_float("low"), 0.30)

    def test_unknown_defaults_low(self):
        self.assertAlmostEqual(_tier_float("unknown"), 0.30)


# ---------------------------------------------------------------------------
# StrategyA.evaluate
# ---------------------------------------------------------------------------

class TestStrategyA(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyA()

    def test_returns_signal_when_eligible(self):
        market   = _make_market()
        estimate = _make_estimate()
        signal   = self.strategy.evaluate(market, estimate)
        self.assertIsInstance(signal, Signal)

    def test_returns_none_when_not_eligible(self):
        market   = _make_market()
        estimate = _make_estimate(action_eligible=False)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertIsNone(signal)

    def test_returns_none_when_net_edge_zero(self):
        # divergence_pp = fee_drag_pp → net = 0
        market   = _make_market()
        estimate = _make_estimate(divergence_pp=2.0, fee_drag_pp=2.0)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertIsNone(signal)

    def test_returns_none_when_net_edge_negative(self):
        market   = _make_market()
        estimate = _make_estimate(divergence_pp=1.0, fee_drag_pp=5.0)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertIsNone(signal)

    def test_buy_yes_direction(self):
        market   = _make_market()
        estimate = _make_estimate(action="buy_yes", divergence_pp=15.0,
                                   final_prob=0.65, market_price=0.50)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertEqual(signal.direction, "buy_yes")

    def test_buy_no_direction(self):
        market   = _make_market()
        estimate = _make_estimate(action="buy_no", divergence_pp=-15.0,
                                   final_prob=0.35, market_price=0.50)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertEqual(signal.direction, "buy_no")

    def test_target_price_for_buy_yes(self):
        market   = _make_market()
        estimate = _make_estimate(action="buy_yes", final_prob=0.65)
        signal   = self.strategy.evaluate(market, estimate)
        # target = final_prob - CONVERGENCE_EXIT
        expected = round(0.65 - C.CONVERGENCE_EXIT, 4)
        self.assertAlmostEqual(signal.target_price, expected, places=4)

    def test_target_price_for_buy_no(self):
        market   = _make_market()
        estimate = _make_estimate(action="buy_no", divergence_pp=-15.0,
                                   final_prob=0.35, market_price=0.50)
        signal   = self.strategy.evaluate(market, estimate)
        # target = (1 - final_prob) + CONVERGENCE_EXIT
        expected = round((1 - 0.35) + C.CONVERGENCE_EXIT, 4)
        self.assertAlmostEqual(signal.target_price, expected, places=4)

    def test_target_price_clamped_to_valid_range(self):
        # final_prob very low → buy_no target could go out of range
        market   = _make_market()
        estimate = _make_estimate(action="buy_no", divergence_pp=-50.0,
                                   final_prob=0.01, market_price=0.50)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertLessEqual(signal.target_price, 0.99)
        self.assertGreaterEqual(signal.target_price, 0.01)

    def test_confidence_mapped_from_tier(self):
        market   = _make_market()
        estimate = _make_estimate(confidence_tier="medium")
        signal   = self.strategy.evaluate(market, estimate)
        self.assertAlmostEqual(signal.confidence, 0.55)
        self.assertEqual(signal.confidence_label, "medium")

    def test_net_edge_computed_correctly(self):
        market   = _make_market()
        estimate = _make_estimate(divergence_pp=15.0, fee_drag_pp=2.0)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertAlmostEqual(signal.net_edge_pp, 13.0, places=2)

    def test_strategy_name(self):
        market   = _make_market()
        estimate = _make_estimate()
        signal   = self.strategy.evaluate(market, estimate)
        self.assertEqual(signal.strategy, "probability_arbitrage")

    def test_signal_has_signal_time(self):
        market   = _make_market()
        estimate = _make_estimate()
        signal   = self.strategy.evaluate(market, estimate)
        self.assertRegex(signal.signal_time, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_signal_ticker_matches_market(self):
        market   = _make_market(ticker="ECON-JOBS-MAY")
        estimate = _make_estimate()
        signal   = self.strategy.evaluate(market, estimate)
        self.assertEqual(signal.ticker, "ECON-JOBS-MAY")

    def test_entry_price_uses_mid_price_fallback(self):
        market   = _make_market(mid_price=0.62)
        estimate = _make_estimate(final_prob=0.75)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertAlmostEqual(signal.entry_price, 0.62, places=2)

    def test_market_with_market_price_attr(self):
        market            = MagicMock()
        market.ticker     = "TEST"
        market.title      = "Test"
        market.category   = "economics"
        market.mid_price  = 0.50
        market.market_price = 0.55   # explicit attribute
        estimate = _make_estimate(final_prob=0.70)
        signal   = self.strategy.evaluate(market, estimate)
        self.assertAlmostEqual(signal.entry_price, 0.55, places=2)


if __name__ == "__main__":
    unittest.main()
