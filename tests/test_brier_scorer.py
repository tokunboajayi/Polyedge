"""Tests for core/calibration/brier_scorer.py"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from core.calibration.brier_scorer import (
    BrierReport,
    BrierScorer,
    CalibrationBin,
    _brier_by_category,
    _calibration_curve,
    _count_rolling,
    _mean_brier,
    _rolling_brier,
    brier_score,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestBrierScore(unittest.TestCase):
    def test_perfect_yes(self):
        self.assertAlmostEqual(brier_score(1.0, 1), 0.0)

    def test_perfect_no(self):
        self.assertAlmostEqual(brier_score(0.0, 0), 0.0)

    def test_worst_yes(self):
        self.assertAlmostEqual(brier_score(0.0, 1), 1.0)

    def test_worst_no(self):
        self.assertAlmostEqual(brier_score(1.0, 0), 1.0)

    def test_random(self):
        # 50/50 on binary → 0.25
        self.assertAlmostEqual(brier_score(0.5, 1), 0.25)
        self.assertAlmostEqual(brier_score(0.5, 0), 0.25)

    def test_moderate_error(self):
        # (0.7 - 1)^2 = 0.09
        self.assertAlmostEqual(brier_score(0.7, 1), 0.09)


class TestMeanBrier(unittest.TestCase):
    def _rows(self, *briers):
        return [{"brier_contribution": b} for b in briers]

    def test_single_row(self):
        self.assertAlmostEqual(_mean_brier(self._rows(0.09)), 0.09)

    def test_average_of_two(self):
        self.assertAlmostEqual(_mean_brier(self._rows(0.0, 0.25)), 0.125)

    def test_empty_returns_zero(self):
        self.assertAlmostEqual(_mean_brier([]), 0.0)


class TestBrierByCategory(unittest.TestCase):
    def _rows(self, data):
        return [{"brier_contribution": b, "category": c} for b, c in data]

    def test_groups_by_category(self):
        rows = self._rows([(0.1, "econ"), (0.2, "econ"), (0.3, "politics")])
        result = _brier_by_category(rows)
        self.assertAlmostEqual(result["econ"], 0.15)
        self.assertAlmostEqual(result["politics"], 0.30)


class TestRollingBrier(unittest.TestCase):
    def _ts(self, days_ago):
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _rows(self, *specs):
        """specs = [(brier, days_ago), ...]"""
        return [
            {"brier_contribution": b, "settled_at": self._ts(d),
             "actual_outcome": 1, "predicted_prob": 0.5}
            for b, d in specs
        ]

    def test_recent_rows_included(self):
        rows = self._rows(*([(0.1, i) for i in range(10)]))
        result = _rolling_brier(rows, days=30)
        self.assertIsNotNone(result)

    def test_old_rows_excluded(self):
        # 10 rows from 40 days ago
        rows = self._rows(*([(0.5, 40)] * 10))
        result = _rolling_brier(rows, days=30)
        self.assertIsNone(result)  # < 5 rows in window

    def test_returns_none_when_too_few(self):
        rows = self._rows((0.1, 1), (0.2, 2))  # only 2 rows
        result = _rolling_brier(rows, days=30)
        self.assertIsNone(result)

    def test_count_rolling(self):
        # days 0..39 ago; days 0..29 are within the 30-day window = 30 rows
        # but day 30 is exactly at the boundary — cutoff is < 30 days ago
        # so rows at days 0-29 pass (30 rows), day 30 does not
        rows = self._rows(*([(0.1, i) for i in range(40)]))
        count = _count_rolling(rows, days=30)
        # days 0–29 = 30 rows within the window (day 30 is at/past the boundary)
        self.assertGreaterEqual(count, 29)
        self.assertLessEqual(count, 31)


class TestCalibrationCurve(unittest.TestCase):
    def _rows(self, *specs):
        """specs = [(predicted, actual), ...]"""
        return [
            {
                "predicted_prob": p,
                "actual_outcome": a,
                "brier_contribution": (p - a) ** 2,
                "settled_at": "2026-01-01T00:00:00Z",
                "category": "econ",
            }
            for p, a in specs
        ]

    def test_bins_populated(self):
        from core.calibration.brier_scorer import BrierScorer
        edges = BrierScorer._BIN_EDGES
        rows = self._rows((0.45, 1), (0.45, 0), (0.65, 1), (0.65, 1))
        bins = _calibration_curve(rows, edges)
        self.assertGreater(len(bins), 0)

    def test_overconfident_flagged(self):
        edges = [0.0, 0.5, 1.01]
        # mean_predicted = 0.45, mean_actual = 0.0 → overconfident
        rows = self._rows((0.45, 0), (0.45, 0), (0.45, 0))
        bins = _calibration_curve(rows, edges)
        self.assertTrue(bins[0].overconfident)

    def test_well_calibrated_not_flagged(self):
        edges = [0.0, 0.6, 1.01]
        rows = self._rows((0.50, 1), (0.50, 0))  # mean_pred=0.50, mean_actual=0.50
        bins = _calibration_curve(rows, edges)
        self.assertFalse(bins[0].overconfident)

    def test_empty_bin_skipped(self):
        edges = [0.0, 0.5, 1.01]
        rows = self._rows((0.70, 1))
        bins = _calibration_curve(rows, edges)
        # Only the 0.5-1.01 bin has data
        self.assertEqual(len(bins), 1)
        self.assertAlmostEqual(bins[0].predicted_low, 0.5)


# ---------------------------------------------------------------------------
# BrierScorer.compute (DB mocked)
# ---------------------------------------------------------------------------

class TestBrierScorerCompute(unittest.IsolatedAsyncioTestCase):

    def _make_rows(self, n=10, predicted=0.70, actual=1):
        from datetime import timedelta
        base = datetime.now(timezone.utc) - timedelta(days=5)
        return [
            {
                "predicted_prob":     predicted,
                "actual_outcome":     actual,
                "brier_contribution": (predicted - actual) ** 2,
                "settled_at":         (base + timedelta(hours=i)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "category": "economics",
            }
            for i in range(n)
        ]

    async def test_no_data_returns_empty_report(self):
        scorer = BrierScorer()
        scorer._fetch_settled = AsyncMock(return_value=[])
        report = await scorer.compute()
        self.assertIsInstance(report, BrierReport)
        self.assertEqual(report.n_settled, 0)
        self.assertAlmostEqual(report.overall_brier, 0.0)

    async def test_computes_overall_brier(self):
        scorer = BrierScorer()
        rows = self._make_rows(n=10, predicted=0.70, actual=1)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        report = await scorer.compute()
        expected = (0.70 - 1) ** 2  # 0.09
        self.assertAlmostEqual(report.overall_brier, expected, places=4)

    async def test_n_settled_correct(self):
        scorer = BrierScorer()
        rows = self._make_rows(n=15)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        report = await scorer.compute()
        self.assertEqual(report.n_settled, 15)

    async def test_by_category_populated(self):
        scorer = BrierScorer()
        rows = self._make_rows(n=5)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        report = await scorer.compute()
        self.assertIn("economics", report.by_category)

    async def test_better_than_random_when_low_brier(self):
        scorer = BrierScorer()
        # Perfect predictions → brier = 0
        rows = self._make_rows(n=10, predicted=1.0, actual=1)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        report = await scorer.compute()
        self.assertTrue(report.better_than_random)

    async def test_not_better_than_random_when_high_brier(self):
        scorer = BrierScorer()
        # Terrible predictions → brier = 1.0
        rows = self._make_rows(n=10, predicted=0.0, actual=1)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        report = await scorer.compute()
        self.assertFalse(report.better_than_random)

    async def test_compute_for_category_returns_none_when_few(self):
        scorer = BrierScorer()
        scorer._fetch_settled = AsyncMock(return_value=[])
        result = await scorer.compute_for_category("economics")
        self.assertIsNone(result)

    async def test_compute_for_category_returns_score(self):
        scorer = BrierScorer()
        rows = self._make_rows(n=8)
        scorer._fetch_settled = AsyncMock(return_value=rows)
        result = await scorer.compute_for_category("economics")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, (0.70 - 1) ** 2, places=4)


if __name__ == "__main__":
    unittest.main()
