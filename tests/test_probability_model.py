"""
Tests for analysis/probability_model.py
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from analysis.probability_model import (
    ProbabilityEstimate,
    ProbabilityModel,
    _blend_weights,
    _evaluate_action,
    _seeded_prior,
    exceeds_divergence_threshold,
)
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claude_result(prob=0.70, confidence="high", cached=False):
    r = MagicMock()
    r.predicted_probability = prob
    r.confidence = confidence
    r.cached = cached
    return r


# ---------------------------------------------------------------------------
# _seeded_prior
# ---------------------------------------------------------------------------

class TestSeededPrior(unittest.TestCase):
    def test_known_categories(self):
        self.assertAlmostEqual(_seeded_prior("economics"), 0.48)
        self.assertAlmostEqual(_seeded_prior("politics"), 0.50)
        self.assertAlmostEqual(_seeded_prior("regulatory"), 0.45)
        self.assertAlmostEqual(_seeded_prior("tech"), 0.52)

    def test_unknown_category_defaults_to_0_50(self):
        self.assertAlmostEqual(_seeded_prior("unicorn"), 0.50)


# ---------------------------------------------------------------------------
# _blend_weights
# ---------------------------------------------------------------------------

class TestBlendWeights(unittest.TestCase):
    def test_high_confidence_sufficient_n(self):
        w_base, w_claude = _blend_weights("high", C.MIN_HISTORICAL_PRECEDENTS)
        self.assertAlmostEqual(w_base, 0.35)
        self.assertAlmostEqual(w_claude, 0.65)
        self.assertAlmostEqual(w_base + w_claude, 1.0)

    def test_medium_confidence_sufficient_n(self):
        w_base, w_claude = _blend_weights("medium", C.MIN_HISTORICAL_PRECEDENTS)
        self.assertAlmostEqual(w_base, 0.50)
        self.assertAlmostEqual(w_claude, 0.50)

    def test_low_confidence(self):
        w_base, w_claude = _blend_weights("low", 200)
        self.assertAlmostEqual(w_base, 0.80)
        self.assertAlmostEqual(w_claude, 0.20)

    def test_high_confidence_insufficient_n(self):
        """_blend_weights also guards on n: high+n<50 falls to conservative."""
        w_base, w_claude = _blend_weights("high", 5)
        # n=5 < 50, so the high-confidence branch is not taken
        self.assertAlmostEqual(w_base, 0.80)
        self.assertAlmostEqual(w_claude, 0.20)

    def test_medium_confidence_insufficient_n(self):
        w_base, w_claude = _blend_weights("medium", 0)
        # 0 < 50 — no high-path match, falls through to conservative
        self.assertAlmostEqual(w_base, 0.80)
        self.assertAlmostEqual(w_claude, 0.20)


# ---------------------------------------------------------------------------
# _evaluate_action
# ---------------------------------------------------------------------------

class TestEvaluateAction(unittest.TestCase):
    def _fee(self, price=0.50):
        return C.round_trip_fee(price, 1, is_maker=True) * 100

    def test_eligible_buy_yes(self):
        # divergence = 15pp, threshold = 10pp
        eligible, action = _evaluate_action(15.0, self._fee(), 0.65, 0.50)
        self.assertTrue(eligible)
        self.assertEqual(action, "buy_yes")

    def test_eligible_buy_no(self):
        eligible, action = _evaluate_action(-15.0, self._fee(), 0.35, 0.50)
        self.assertTrue(eligible)
        self.assertEqual(action, "buy_no")

    def test_not_eligible_below_threshold(self):
        eligible, action = _evaluate_action(5.0, self._fee(), 0.55, 0.50)
        self.assertFalse(eligible)
        self.assertEqual(action, "hold")

    def test_not_eligible_below_fee_drag(self):
        # Force fee_drag_pp to be larger than divergence
        eligible, action = _evaluate_action(12.0, 20.0, 0.62, 0.50)
        self.assertFalse(eligible)
        self.assertEqual(action, "hold")

    def test_exactly_at_threshold_not_eligible(self):
        # Equal to threshold is NOT eligible (strictly >)
        eligible, action = _evaluate_action(
            C.DIVERGENCE_THRESHOLD, self._fee(), 0.60, 0.50
        )
        self.assertFalse(eligible)


# ---------------------------------------------------------------------------
# exceeds_divergence_threshold
# ---------------------------------------------------------------------------

class TestExceedsDivergenceThreshold(unittest.TestCase):
    def test_eligible_case(self):
        # 70% model vs 50% market = 20pp divergence > 10pp threshold
        self.assertTrue(exceeds_divergence_threshold(0.70, 0.50))

    def test_ineligible_small_divergence(self):
        # 55% vs 50% = 5pp < 10pp
        self.assertFalse(exceeds_divergence_threshold(0.55, 0.50))

    def test_edge_just_above_threshold(self):
        # 61% vs 50% = 11pp > 10pp
        self.assertTrue(exceeds_divergence_threshold(0.61, 0.50))

    def test_taker_fee_higher(self):
        # Taker fee is 4x maker — should still pass with 20pp gap
        self.assertTrue(
            exceeds_divergence_threshold(0.70, 0.50, is_maker=False)
        )

    def test_symmetric_buy_no(self):
        self.assertTrue(exceeds_divergence_threshold(0.30, 0.50))


# ---------------------------------------------------------------------------
# ProbabilityModel._parse_claude
# ---------------------------------------------------------------------------

class TestParseClaudeResult(unittest.TestCase):
    def test_high_confidence_not_cached(self):
        r = _make_claude_result(prob=0.72, confidence="high", cached=False)
        prob, tier = ProbabilityModel._parse_claude(r, 60)
        self.assertAlmostEqual(prob, 0.72)
        self.assertEqual(tier, "high")

    def test_cached_result_ignored(self):
        r = _make_claude_result(prob=0.80, confidence="high", cached=True)
        prob, tier = ProbabilityModel._parse_claude(r, 60)
        self.assertIsNone(prob)
        self.assertEqual(tier, "low")

    def test_low_confidence_ignored(self):
        r = _make_claude_result(prob=0.80, confidence="low", cached=False)
        prob, tier = ProbabilityModel._parse_claude(r, 60)
        self.assertIsNone(prob)
        self.assertEqual(tier, "low")

    def test_none_result(self):
        prob, tier = ProbabilityModel._parse_claude(None, 60)
        self.assertIsNone(prob)
        self.assertEqual(tier, "low")

    def test_high_confidence_downgraded_when_n_insufficient(self):
        r = _make_claude_result(prob=0.75, confidence="high", cached=False)
        prob, tier = ProbabilityModel._parse_claude(r, 10)  # n < 50
        self.assertAlmostEqual(prob, 0.75)
        self.assertEqual(tier, "medium")  # downgraded from high

    def test_medium_confidence_not_downgraded(self):
        r = _make_claude_result(prob=0.65, confidence="medium", cached=False)
        prob, tier = ProbabilityModel._parse_claude(r, 10)
        self.assertAlmostEqual(prob, 0.65)
        self.assertEqual(tier, "medium")  # medium stays medium


# ---------------------------------------------------------------------------
# ProbabilityModel.estimate — integrated (DB mocked)
# ---------------------------------------------------------------------------

class TestProbabilityModelEstimate(unittest.IsolatedAsyncioTestCase):

    async def _estimate(self, category="economics", market_price=0.50,
                        claude_result=None, db_rows=None):
        """Helper: run estimate() with a mocked DB."""
        if db_rows is None:
            # Default: no calibration data
            db_rows = (0, 0)

        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=db_rows)

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        model = ProbabilityModel()
        with patch("persistence.database.get_connection", return_value=mock_db):
            return await model.estimate(category, market_price, claude_result)

    async def test_no_data_uses_seeded_prior(self):
        est = await self._estimate(category="economics", market_price=0.50,
                                    db_rows=(0, 0))
        self.assertIsInstance(est, ProbabilityEstimate)
        self.assertEqual(est.n, 0)
        # With no claude and n=0, final_prob == seeded prior for economics
        self.assertAlmostEqual(est.final_prob, 0.48, places=2)

    async def test_sufficient_data_with_high_claude(self):
        # 60 settled markets, 36 YES (60% base rate)
        claude = _make_claude_result(prob=0.72, confidence="high", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.50,
            claude_result=claude,
            db_rows=(60, 36),  # n=60, yes=36 → 0.60
        )
        self.assertEqual(est.n, 60)
        self.assertAlmostEqual(est.base_rate, 0.60, places=2)
        # high confidence: 35% base + 65% claude = 0.35*0.60 + 0.65*0.72
        expected = round(0.35 * 0.60 + 0.65 * 0.72, 4)
        self.assertAlmostEqual(est.final_prob, expected, places=3)

    async def test_action_eligible_when_divergence_large(self):
        # final_prob ~0.68 vs market 0.50 → 18pp divergence
        claude = _make_claude_result(prob=0.72, confidence="high", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.50,
            claude_result=claude,
            db_rows=(60, 36),
        )
        self.assertTrue(est.action_eligible)
        self.assertEqual(est.action, "buy_yes")

    async def test_action_hold_when_small_divergence(self):
        # claude and base both near market price
        claude = _make_claude_result(prob=0.53, confidence="medium", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.52,
            claude_result=claude,
            db_rows=(60, 30),  # base ~0.50
        )
        # divergence will be small, should be hold
        self.assertFalse(est.action_eligible)
        self.assertEqual(est.action, "hold")

    async def test_buy_no_when_model_below_price(self):
        # Model well below market price
        claude = _make_claude_result(prob=0.28, confidence="high", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.60,
            claude_result=claude,
            db_rows=(60, 18),  # base = 0.30
        )
        # 35%*0.30 + 65%*0.28 = 0.287 vs 0.60 → ~-31pp
        self.assertTrue(est.action_eligible)
        self.assertEqual(est.action, "buy_no")

    async def test_final_prob_clamped_to_valid_range(self):
        # Extreme claude value should be clamped to 0.99
        claude = _make_claude_result(prob=0.99, confidence="high", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.50,
            claude_result=claude,
            db_rows=(60, 60),  # 100% base rate
        )
        self.assertLessEqual(est.final_prob, 0.99)
        self.assertGreaterEqual(est.final_prob, 0.01)

    async def test_db_error_falls_back_to_seeded_prior(self):
        model = ProbabilityModel()
        with patch("persistence.database.get_connection",
                   side_effect=Exception("DB down")):
            est = await model.estimate("economics", 0.50, None)

        self.assertEqual(est.n, 0)
        self.assertAlmostEqual(est.base_rate, 0.48, places=2)

    async def test_sparse_data_blends_prior_with_observed(self):
        # 10 settled rows, 7 YES → observed = 0.70
        est = await self._estimate(
            category="economics",
            market_price=0.50,
            claude_result=None,
            db_rows=(10, 7),  # n=10 < 50 threshold
        )
        self.assertEqual(est.n, 10)
        # blend_weight = 10/50 = 0.20; seeded=0.48, observed=0.70
        expected_base = round((1 - 0.20) * 0.48 + 0.20 * 0.70, 4)
        self.assertAlmostEqual(est.base_rate, expected_base, places=3)

    async def test_category_normalised_to_lowercase(self):
        est = await self._estimate(category="Economics", market_price=0.50)
        self.assertEqual(est.category, "economics")

    async def test_divergence_pp_computed_correctly(self):
        claude = _make_claude_result(prob=0.72, confidence="high", cached=False)
        est = await self._estimate(
            category="economics",
            market_price=0.50,
            claude_result=claude,
            db_rows=(60, 36),
        )
        expected_div = round((est.final_prob - 0.50) * 100, 2)
        self.assertAlmostEqual(est.divergence_pp, expected_div, places=2)

    async def test_fee_drag_positive(self):
        est = await self._estimate(category="economics", market_price=0.50)
        self.assertGreater(est.fee_drag_pp, 0)


if __name__ == "__main__":
    unittest.main()
