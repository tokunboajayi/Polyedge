"""Tests for core/strategies/mean_reversion.py"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from core.strategies.mean_reversion import (
    PricePoint,
    SpikeInfo,
    StrategyB,
    _float_to_tier,
    detect_spike,
    is_official_action,
)
from core.strategies.probability_arbitrage import Signal
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)


def _pts(*prices, minutes_apart=5) -> list[PricePoint]:
    """Build a list of PricePoints spaced minutes_apart apart."""
    result = []
    for i, p in enumerate(prices):
        ts = _NOW - timedelta(minutes=(len(prices) - 1 - i) * minutes_apart)
        result.append(PricePoint(price=p, timestamp=ts))
    return result


def _make_market(ticker="TEST-01", title="Will X happen?", category="economics"):
    m = MagicMock()
    m.ticker   = ticker
    m.title    = title
    m.category = category
    m.mid_price = 0.50
    return m


def _make_scan(urgency="monitor", score=4.0, cached=False, impact="neutral"):
    r = MagicMock()
    r.urgency         = urgency
    r.relevance_score = score
    r.cached          = cached
    r.news_impact     = impact
    r.reasoning       = ""
    return r


# ---------------------------------------------------------------------------
# detect_spike
# ---------------------------------------------------------------------------

class TestDetectSpike(unittest.TestCase):
    def test_no_spike_with_small_move(self):
        pts = _pts(0.50, 0.52, 0.54)
        self.assertIsNone(detect_spike(pts))

    def test_spike_up_detected(self):
        # 0.40 → 0.70 = 75% move in window
        pts = _pts(0.40, 0.45, 0.70)
        spike = detect_spike(pts)
        self.assertIsNotNone(spike)
        self.assertEqual(spike.direction, "up")
        self.assertGreaterEqual(spike.spike_magnitude, C.SPIKE_THRESHOLD)

    def test_spike_down_detected(self):
        pts = _pts(0.70, 0.65, 0.40)
        spike = detect_spike(pts)
        self.assertIsNotNone(spike)
        self.assertEqual(spike.direction, "down")

    def test_single_point_returns_none(self):
        pts = _pts(0.50)
        self.assertIsNone(detect_spike(pts))

    def test_empty_list_returns_none(self):
        self.assertIsNone(detect_spike([]))

    def test_exactly_at_threshold_not_spike(self):
        # 15% move exactly = SPIKE_THRESHOLD; requires strictly >=
        base = 0.50
        current = round(base * (1 + C.SPIKE_THRESHOLD), 4)
        pts = _pts(base, current)
        spike = detect_spike(pts)
        # spike_magnitude == SPIKE_THRESHOLD; < C.SPIKE_THRESHOLD → None
        # Actually SPIKE_THRESHOLD=0.15 and magnitude=(current-base)/base = 0.15 exactly
        # The code says < C.SPIKE_THRESHOLD, so exactly 0.15 is NOT a spike
        self.assertIsNone(spike)

    def test_spike_above_threshold(self):
        # 20% move > 15%
        pts = _pts(0.50, 0.60)
        spike = detect_spike(pts)
        self.assertIsNotNone(spike)

    def test_outside_window_not_used_as_baseline(self):
        # Point > 1 hour ago should not be the baseline
        old_ts   = _NOW - timedelta(hours=2)
        new_ts   = _NOW - timedelta(minutes=10)
        now_ts   = _NOW
        pts = [
            PricePoint(price=0.10, timestamp=old_ts),  # outside window
            PricePoint(price=0.50, timestamp=new_ts),  # inside window, this is baseline
            PricePoint(price=0.55, timestamp=now_ts),  # 10% move — not a spike
        ]
        spike = detect_spike(pts)
        self.assertIsNone(spike)


# ---------------------------------------------------------------------------
# is_official_action
# ---------------------------------------------------------------------------

class TestIsOfficialAction(unittest.TestCase):
    def test_critical_reg_alert_triggers(self):
        alert = MagicMock()
        alert.tier = "CRITICAL"
        result = is_official_action("some market", "economics", None, [alert])
        self.assertTrue(result)

    def test_warning_alert_does_not_trigger(self):
        alert = MagicMock()
        alert.tier = "WARNING"
        result = is_official_action("some market", "economics", None, [alert])
        self.assertFalse(result)

    def test_official_keyword_in_title(self):
        result = is_official_action(
            "Supreme Court ruling on case", "politics", None, None
        )
        self.assertTrue(result)

    def test_official_keyword_in_reasoning(self):
        scan = MagicMock()
        scan.reasoning = "This follows an executive order signed today."
        result = is_official_action("random title", "politics", scan, None)
        self.assertTrue(result)

    def test_no_official_action_signals(self):
        result = is_official_action(
            "Will CPI be above 3%?", "economics", None, None
        )
        self.assertFalse(result)

    def test_none_reg_alerts(self):
        # Should not crash on None
        result = is_official_action("test", "economics", None, None)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# StrategyB.evaluate
# ---------------------------------------------------------------------------

class TestStrategyB(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyB()

    def _spike_history(self, direction="up"):
        if direction == "up":
            return _pts(0.40, 0.42, 0.70)   # 75% spike up
        return _pts(0.70, 0.68, 0.40)        # 43% spike down

    def test_returns_signal_on_spike(self):
        market = _make_market()
        signal = self.strategy.evaluate(market, self._spike_history())
        self.assertIsInstance(signal, Signal)

    def test_returns_none_when_no_spike(self):
        market = _make_market()
        signal = self.strategy.evaluate(market, _pts(0.50, 0.51, 0.52))
        self.assertIsNone(signal)

    def test_returns_none_on_official_action(self):
        market = _make_market(title="Supreme Court ruling on election")
        signal = self.strategy.evaluate(market, self._spike_history())
        self.assertIsNone(signal)

    def test_returns_none_when_justified_move(self):
        # High urgency + high score → justified, should be skipped
        scan   = _make_scan(urgency="immediate", score=9.0, cached=False)
        market = _make_market()
        signal = self.strategy.evaluate(market, self._spike_history(), scan_result=scan)
        self.assertIsNone(signal)

    def test_buy_no_on_upward_spike(self):
        market = _make_market()
        signal = self.strategy.evaluate(market, self._spike_history("up"))
        self.assertEqual(signal.direction, "buy_no")

    def test_buy_yes_on_downward_spike(self):
        market = _make_market()
        signal = self.strategy.evaluate(market, self._spike_history("down"))
        self.assertEqual(signal.direction, "buy_yes")

    def test_target_is_50_percent_reversion_up_spike(self):
        pts    = _pts(0.40, 0.70)   # baseline=0.40, current=0.70, move=0.30
        market = _make_market()
        signal = self.strategy.evaluate(market, pts)
        # spike up → buy_no; target_yes = 0.70 - 0.50*0.30 = 0.55
        # target_price (NO) = 1 - 0.55 = 0.45
        self.assertAlmostEqual(signal.target_price, 0.45, places=2)

    def test_target_is_50_percent_reversion_down_spike(self):
        pts    = _pts(0.70, 0.40)   # baseline=0.70, current=0.40, move=-0.30
        market = _make_market()
        signal = self.strategy.evaluate(market, pts)
        # spike down → buy_yes; target = 0.40 + 0.50*0.30 = 0.55
        self.assertAlmostEqual(signal.target_price, 0.55, places=2)

    def test_stop_is_25_percent_further_adverse_up_spike(self):
        pts    = _pts(0.40, 0.70)   # move = 0.30
        market = _make_market()
        signal = self.strategy.evaluate(market, pts)
        # stop_yes = 0.70 + 0.25*0.30 = 0.775; stop_price(NO) = 1 - 0.775 = 0.225
        self.assertAlmostEqual(signal.stop_price, round(1 - 0.775, 4), places=3)

    def test_strategy_name(self):
        market = _make_market()
        signal = self.strategy.evaluate(market, self._spike_history())
        self.assertEqual(signal.strategy, "mean_reversion")

    def test_confidence_high_for_very_large_spike(self):
        # 75% spike > 25% threshold → high confidence
        market = _make_market()
        signal = self.strategy.evaluate(market, _pts(0.40, 0.70))
        self.assertEqual(signal.confidence_label, "high")

    def test_confidence_medium_for_moderate_spike(self):
        # 20% spike (SPIKE_THRESHOLD <= x < 25%)
        pts    = _pts(0.50, 0.60)  # 20% spike
        market = _make_market()
        signal = self.strategy.evaluate(market, pts)
        self.assertEqual(signal.confidence_label, "medium")

    def test_no_crash_with_empty_reg_alerts(self):
        market = _make_market()
        signal = self.strategy.evaluate(
            market, self._spike_history(), reg_alerts=[]
        )
        self.assertIsInstance(signal, Signal)

    def test_confidence_reduced_when_scan_unavailable(self):
        market = _make_market()
        # No scan_result provided — confidence should be reduced
        sig_no_scan = self.strategy.evaluate(market, _pts(0.40, 0.70), scan_result=None)
        sig_with_scan = self.strategy.evaluate(
            market, _pts(0.40, 0.70),
            scan_result=_make_scan(urgency="monitor", score=4.0, cached=False)
        )
        self.assertLessEqual(sig_no_scan.confidence, sig_with_scan.confidence)


# ---------------------------------------------------------------------------
# _float_to_tier
# ---------------------------------------------------------------------------

class TestFloatToTier(unittest.TestCase):
    def test_high(self):
        self.assertEqual(_float_to_tier(0.80), "high")

    def test_medium(self):
        self.assertEqual(_float_to_tier(0.55), "medium")

    def test_low(self):
        self.assertEqual(_float_to_tier(0.20), "low")

    def test_boundary_high(self):
        self.assertEqual(_float_to_tier(0.70), "high")

    def test_boundary_medium(self):
        self.assertEqual(_float_to_tier(0.45), "medium")


if __name__ == "__main__":
    unittest.main()
