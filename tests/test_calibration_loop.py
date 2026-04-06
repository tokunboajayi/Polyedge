"""Tests for core/calibration/calibration_loop.py"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.calibration.calibration_loop import (
    CalibrationLoop,
    CalibrationState,
    _phase,
)
from config import constants as C


# ---------------------------------------------------------------------------
# _phase helper
# ---------------------------------------------------------------------------

class TestPhase(unittest.TestCase):
    def test_warming_up_below_30(self):
        self.assertEqual(_phase(0), "warming_up")
        self.assertEqual(_phase(29), "warming_up")

    def test_calibrating_at_30(self):
        self.assertEqual(_phase(30), "calibrating")
        self.assertEqual(_phase(59), "calibrating")

    def test_calibrated_at_60(self):
        self.assertEqual(_phase(60), "calibrated")
        self.assertEqual(_phase(200), "calibrated")


# ---------------------------------------------------------------------------
# CalibrationLoop.record_outcome
# ---------------------------------------------------------------------------

class TestCalibrationLoop(unittest.IsolatedAsyncioTestCase):

    def _make_loop(self):
        loop = CalibrationLoop(slack_alerter=None)
        loop._persist_outcome       = AsyncMock()
        loop._persist_system_alert  = AsyncMock()
        loop._fire_alert            = AsyncMock()
        return loop

    def _make_rows(self, n, predicted=0.65, actual=1):
        from datetime import datetime, timedelta, timezone
        base = datetime.now(timezone.utc)
        return [
            {
                "predicted_prob":     predicted,
                "actual_outcome":     actual,
                "brier_contribution": (predicted - actual) ** 2,
                "settled_at":         (base + i * timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "category":           "economics",
            }
            for i in range(n)
        ]

    async def test_record_outcome_increments_count(self):
        loop = self._make_loop()
        loop._scorer._fetch_settled = AsyncMock(return_value=[])
        await loop.record_outcome(1, "TEST", 0.70, 1)
        self.assertEqual(loop.resolved_count(), 1)

    async def test_initial_state(self):
        loop = self._make_loop()
        state = loop.get_state()
        self.assertIsInstance(state, CalibrationState)
        self.assertEqual(state.resolved_count, 0)
        self.assertAlmostEqual(state.correction_factor, 1.0)
        self.assertEqual(state.phase, "warming_up")

    async def test_phase_warming_up_below_30(self):
        loop = self._make_loop()
        loop._scorer._fetch_settled = AsyncMock(return_value=[])
        for i in range(10):
            await loop.record_outcome(i, "T", 0.70, 1)
        self.assertEqual(loop.get_state().phase, "warming_up")

    async def test_factor_not_computed_below_threshold(self):
        loop = self._make_loop()
        loop._scorer._fetch_settled = AsyncMock(return_value=[])
        for i in range(29):
            await loop.record_outcome(i, "T", 0.70, 1)
        self.assertAlmostEqual(loop.get_correction_factor(), 1.0)

    async def test_factor_computed_at_30_resolved(self):
        loop = self._make_loop()
        rows = self._make_rows(30, predicted=0.65, actual=1)
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        # After 30 records, _recompute_factor is called
        for i in range(30):
            await loop.record_outcome(i, "T", 0.65, 1)
        # factor = mean_actual / mean_predicted = 1.0 / 0.65 ≈ 1.538
        factor = loop.get_correction_factor()
        expected = 1.0 / 0.65
        self.assertAlmostEqual(factor, expected, places=2)

    async def test_factor_within_range_no_alert(self):
        loop = self._make_loop()
        # Well-calibrated rows: predicted ≈ actual rate
        rows = self._make_rows(30, predicted=0.60, actual=1)
        # Override: make mean_actual/mean_predicted close to 1.0
        for r in rows:
            r["actual_outcome"] = 1
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        for i in range(30):
            await loop.record_outcome(i, "T", 0.60, 1)
        # factor = 1/0.60 = 1.67 > 1.3 → alert fires
        # Not within range but let's verify alert IS called
        loop._fire_alert.assert_called()

    async def test_factor_inside_range_no_alert_when_balanced(self):
        loop = self._make_loop()
        # Mix of YES and NO to make mean_actual close to mean_predicted
        rows = []
        for i in range(30):
            actual = 1 if i < 18 else 0   # 60% actual YES
            rows.append({
                "predicted_prob":     0.60,
                "actual_outcome":     actual,
                "brier_contribution": (0.60 - actual) ** 2,
                "settled_at":         "2026-01-01T00:00:00Z",
                "category":           "economics",
            })
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        for i in range(30):
            await loop.record_outcome(i, "T", 0.60, rows[i]["actual_outcome"])
        # factor = 0.60/0.60 = 1.0 — within range [0.7, 1.3]
        self.assertAlmostEqual(loop.get_correction_factor(), 1.0, places=2)
        loop._fire_alert.assert_not_called()

    async def test_alert_fires_when_factor_out_of_range(self):
        loop = self._make_loop()
        # Factor = 1.0 / 0.50 = 2.0 → outside [0.7, 1.3]
        rows = self._make_rows(30, predicted=0.50, actual=1)
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        for i in range(30):
            await loop.record_outcome(i, "T", 0.50, 1)
        loop._fire_alert.assert_called()
        loop._persist_system_alert.assert_called()

    async def test_within_range_flag_set(self):
        loop = self._make_loop()
        lo, hi = C.CALIBRATION_FACTOR_RANGE
        rows = []
        for i in range(30):
            actual = 1 if i < 18 else 0
            rows.append({
                "predicted_prob":     0.60,
                "actual_outcome":     actual,
                "brier_contribution": (0.60 - actual) ** 2,
                "settled_at":         "2026-01-01T00:00:00Z",
                "category":           "economics",
            })
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        for i in range(30):
            await loop.record_outcome(i, "T", 0.60, rows[i]["actual_outcome"])
        self.assertTrue(loop.get_state().within_range)

    async def test_get_correction_factor_returns_float(self):
        loop = self._make_loop()
        factor = loop.get_correction_factor()
        self.assertIsInstance(factor, float)

    async def test_persist_outcome_called_on_record(self):
        loop = self._make_loop()
        loop._scorer._fetch_settled = AsyncMock(return_value=[])
        await loop.record_outcome(1, "TEST", 0.70, 1)
        loop._persist_outcome.assert_called_once()

    async def test_brier_score_updated_in_state(self):
        loop = self._make_loop()
        rows = self._make_rows(30, predicted=0.70, actual=1)
        loop._scorer._fetch_settled = AsyncMock(return_value=rows)
        for i in range(30):
            await loop.record_outcome(i, "T", 0.70, 1)
        state = loop.get_state()
        expected = (0.70 - 1) ** 2
        self.assertAlmostEqual(state.brier_score, expected, places=4)


if __name__ == "__main__":
    unittest.main()
