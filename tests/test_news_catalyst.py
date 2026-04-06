"""Tests for core/signals/news_catalyst.py"""

import unittest
from unittest.mock import MagicMock

from core.signals.news_catalyst import CatalystResult, NewsCatalyst, _is_aligned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(direction="buy_yes", confidence=0.55, strategy="probability_arbitrage"):
    s = MagicMock()
    s.confidence = confidence
    s.direction  = direction
    s.strategy   = strategy
    s.ticker     = "TEST-01"
    return s


def _make_scan(urgency="monitor", impact="positive", score=6.0, cached=False):
    r = MagicMock()
    r.urgency         = urgency
    r.news_impact     = impact
    r.relevance_score = score
    r.cached          = cached
    r.reasoning       = ""
    return r


# ---------------------------------------------------------------------------
# _is_aligned
# ---------------------------------------------------------------------------

class TestIsAligned(unittest.TestCase):
    def test_buy_yes_positive_aligned(self):
        self.assertTrue(_is_aligned("buy_yes", "positive"))

    def test_buy_yes_negative_not_aligned(self):
        self.assertFalse(_is_aligned("buy_yes", "negative"))

    def test_buy_no_negative_aligned(self):
        self.assertTrue(_is_aligned("buy_no", "negative"))

    def test_buy_no_positive_not_aligned(self):
        self.assertFalse(_is_aligned("buy_no", "positive"))

    def test_neutral_unrecognised(self):
        self.assertFalse(_is_aligned("buy_yes", "neutral"))


# ---------------------------------------------------------------------------
# NewsCatalyst.adjust
# ---------------------------------------------------------------------------

class TestNewsCatalyst(unittest.TestCase):
    def setUp(self):
        self.catalyst = NewsCatalyst()

    def test_none_scan_reduces_confidence(self):
        signal = _make_signal(confidence=0.55)
        result = self.catalyst.adjust(signal, None)
        self.assertLess(result.adjusted_confidence, 0.55)
        self.assertAlmostEqual(result.adjustment_pp, -0.10, places=4)

    def test_cached_scan_reduces_confidence(self):
        signal = _make_signal(confidence=0.55)
        scan   = _make_scan(cached=True)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.10, places=4)

    def test_low_relevance_score_reduces_confidence(self):
        signal = _make_signal(confidence=0.55)
        scan   = _make_scan(score=2.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.15, places=4)

    def test_neutral_impact_no_change(self):
        signal = _make_signal(confidence=0.55)
        scan   = _make_scan(impact="neutral")
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, 0.0, places=4)
        self.assertAlmostEqual(result.adjusted_confidence, 0.55, places=4)

    def test_immediate_positive_aligned_buy_yes_plus_20(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="immediate", impact="positive", score=6.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, 0.20, places=4)

    def test_immediate_negative_misaligned_buy_yes_minus_20(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="immediate", impact="negative", score=6.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.20, places=4)

    def test_monitor_positive_aligned_plus_10(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="positive", score=6.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, 0.10, places=4)

    def test_monitor_negative_misaligned_minus_10(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="negative", score=6.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.10, places=4)

    def test_ignore_urgency_minus_15(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="ignore", impact="positive", score=6.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.15, places=4)

    def test_high_score_overrides_to_20pp(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="positive", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, 0.20, places=4)

    def test_adjusted_confidence_clamped_at_max(self):
        signal = _make_signal(confidence=0.90)
        scan   = _make_scan(urgency="immediate", impact="positive", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertLessEqual(result.adjusted_confidence, 0.95)

    def test_adjusted_confidence_clamped_at_min(self):
        signal = _make_signal(confidence=0.10)
        scan   = _make_scan(urgency="immediate", impact="negative", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertGreaterEqual(result.adjusted_confidence, 0.05)

    def test_veto_when_confidence_drops_below_threshold(self):
        # Start at 0.20, drop by 0.20 → 0.00, should veto
        signal = _make_signal(direction="buy_yes", confidence=0.20)
        scan   = _make_scan(urgency="immediate", impact="negative", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertTrue(result.veto)

    def test_no_veto_when_confidence_stays_above_threshold(self):
        signal = _make_signal(direction="buy_yes", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="positive")
        result = self.catalyst.adjust(signal, scan)
        self.assertFalse(result.veto)

    def test_mean_reversion_veto_on_justified_spike(self):
        # Strategy B, immediate urgency, high score, NOT aligned (news supports the spike)
        # buy_no fade + positive news (YES-supporting) → not aligned → veto
        signal = _make_signal(direction="buy_no", confidence=0.55,
                               strategy="mean_reversion")
        scan   = _make_scan(urgency="immediate", impact="positive", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        self.assertTrue(result.veto)

    def test_strategy_a_no_veto_on_same_conditions(self):
        # Same conditions but strategy_a — should NOT veto (justified-spike check is B-only)
        signal = _make_signal(direction="buy_no", confidence=0.55,
                               strategy="probability_arbitrage")
        scan   = _make_scan(urgency="immediate", impact="positive", score=9.0)
        result = self.catalyst.adjust(signal, scan)
        # confidence drops but veto depends on final confidence level
        # With conf=0.55 and adj=-0.20 → 0.35 > 0.15 threshold, no veto
        self.assertFalse(result.veto)

    def test_buy_no_positive_news_reduces_confidence(self):
        # buy_no + positive news → not aligned → reduces confidence
        signal = _make_signal(direction="buy_no", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="positive")
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, -0.10, places=4)

    def test_buy_no_negative_news_increases_confidence(self):
        # buy_no + negative news → aligned → increases confidence
        signal = _make_signal(direction="buy_no", confidence=0.55)
        scan   = _make_scan(urgency="monitor", impact="negative")
        result = self.catalyst.adjust(signal, scan)
        self.assertAlmostEqual(result.adjustment_pp, 0.10, places=4)

    def test_result_is_catalyst_result_instance(self):
        signal = _make_signal()
        result = self.catalyst.adjust(signal, _make_scan())
        self.assertIsInstance(result, CatalystResult)


if __name__ == "__main__":
    unittest.main()
