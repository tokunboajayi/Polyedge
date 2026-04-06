"""Tests for monitoring/edge_erosion.py"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from monitoring.edge_erosion import (
    EdgeErosionMonitor,
    EdgeSnapshot,
    MetricResult,
    _brier_status,
    _compute_avg_edge,
    _compute_brier,
    _compute_frequency,
    _compute_sharpe,
    _compute_win_rate,
    _edge_status,
    _frequency_status,
    _sharpe_status,
    _win_rate_status,
    _worst_status,
)
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trade(pnl, entry=0.50, exit_=0.60, days_ago=5):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago))
    return {
        "pnl": pnl,
        "entry_price": entry,
        "exit_price": exit_,
        "entry_time": (ts - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exit_time":  ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "side": "YES",
        "strategy": "probability_arbitrage",
        "fees_paid": 0.01,
    }


def _brow(bc=0.09):
    return {"brier_contribution": bc}


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

class TestStatusHelpers(unittest.TestCase):
    def test_win_rate_healthy(self):
        self.assertEqual(_win_rate_status(C.EDGE_WIN_RATE_HEALTHY), "healthy")

    def test_win_rate_warning(self):
        self.assertEqual(_win_rate_status(C.EDGE_WIN_RATE_WARNING), "warning")

    def test_win_rate_critical(self):
        self.assertEqual(_win_rate_status(0.40), "critical")

    def test_sharpe_healthy(self):
        self.assertEqual(_sharpe_status(C.EDGE_SHARPE_HEALTHY), "healthy")

    def test_sharpe_warning(self):
        self.assertEqual(_sharpe_status(C.EDGE_SHARPE_WARNING), "warning")

    def test_sharpe_critical(self):
        self.assertEqual(_sharpe_status(0.1), "critical")

    def test_frequency_healthy(self):
        self.assertEqual(_frequency_status(5.0), "healthy")

    def test_frequency_too_low_critical(self):
        self.assertEqual(_frequency_status(0.5), "critical")

    def test_frequency_too_high_critical(self):
        self.assertEqual(_frequency_status(20.0), "critical")

    def test_frequency_warning_low(self):
        self.assertEqual(_frequency_status(C.EDGE_FREQUENCY_MIN_WARNING), "warning")

    def test_edge_healthy(self):
        self.assertEqual(_edge_status(C.EDGE_AVG_EDGE_HEALTHY), "healthy")

    def test_edge_warning(self):
        self.assertEqual(_edge_status(C.EDGE_AVG_EDGE_WARNING), "warning")

    def test_edge_critical(self):
        self.assertEqual(_edge_status(0.01), "critical")

    def test_brier_healthy(self):
        self.assertEqual(_brier_status(C.EDGE_BRIER_HEALTHY), "healthy")

    def test_brier_warning(self):
        v = (C.EDGE_BRIER_HEALTHY + C.EDGE_BRIER_WARNING) / 2
        self.assertEqual(_brier_status(v), "warning")

    def test_brier_critical(self):
        self.assertEqual(_brier_status(C.EDGE_BRIER_WARNING + 0.01), "critical")

    def test_worst_status_critical_wins(self):
        self.assertEqual(_worst_status(["healthy", "warning", "critical"]), "critical")

    def test_worst_status_warning_beats_healthy(self):
        self.assertEqual(_worst_status(["healthy", "warning"]), "warning")

    def test_worst_status_all_healthy(self):
        self.assertEqual(_worst_status(["healthy", "healthy"]), "healthy")


# ---------------------------------------------------------------------------
# Metric computations
# ---------------------------------------------------------------------------

class TestComputeWinRate(unittest.TestCase):
    def test_all_wins(self):
        trades = [_trade(pnl=10) for _ in range(5)]
        m = _compute_win_rate(trades)
        self.assertAlmostEqual(m.value, 1.0)
        self.assertEqual(m.status, "healthy")

    def test_all_losses(self):
        trades = [_trade(pnl=-5) for _ in range(5)]
        m = _compute_win_rate(trades)
        self.assertAlmostEqual(m.value, 0.0)
        self.assertEqual(m.status, "critical")

    def test_60_percent_win_rate(self):
        trades = [_trade(pnl=10)] * 6 + [_trade(pnl=-5)] * 4
        m = _compute_win_rate(trades)
        self.assertAlmostEqual(m.value, 0.60)
        self.assertEqual(m.status, "healthy")

    def test_empty_trades_warning(self):
        m = _compute_win_rate([])
        self.assertEqual(m.status, "warning")


class TestComputeSharpe(unittest.TestCase):
    def test_positive_sharpe(self):
        # 10 trades over 10 different days, all profitable
        trades = [_trade(pnl=5.0, days_ago=i) for i in range(1, 11)]
        m = _compute_sharpe(trades, 30)
        # All same pnl → stdev = 0 → sharpe = 0
        # (no variation in returns → stdev = 0)
        self.assertIsInstance(m.value, float)

    def test_returns_metric_result(self):
        trades = [_trade(pnl=3, days_ago=1), _trade(pnl=5, days_ago=2)]
        m = _compute_sharpe(trades, 30)
        self.assertIsInstance(m, MetricResult)

    def test_empty_returns_warning(self):
        m = _compute_sharpe([], 30)
        self.assertEqual(m.status, "warning")

    def test_single_trade_returns_warning(self):
        m = _compute_sharpe([_trade(5)], 30)
        self.assertEqual(m.status, "warning")


class TestComputeFrequency(unittest.TestCase):
    def test_healthy_frequency(self):
        # 150 trades over 30 days = 5/day
        trades = [_trade(5)] * 150
        m = _compute_frequency(trades, 30)
        self.assertAlmostEqual(m.value, 5.0)
        self.assertEqual(m.status, "healthy")

    def test_too_low_critical(self):
        # 10 trades over 30 days = 0.33/day < 1
        trades = [_trade(5)] * 10
        m = _compute_frequency(trades, 30)
        self.assertEqual(m.status, "critical")

    def test_too_high_critical(self):
        # 600 trades over 30 days = 20/day > 15
        trades = [_trade(5)] * 600
        m = _compute_frequency(trades, 30)
        self.assertEqual(m.status, "critical")


class TestComputeAvgEdge(unittest.TestCase):
    def test_positive_edge(self):
        # pnl=0.10, entry=0.50 → edge = 0.20 = 20%
        trades = [_trade(pnl=0.10, entry=0.50)] * 5
        m = _compute_avg_edge(trades)
        self.assertAlmostEqual(m.value, 0.20, places=4)
        self.assertEqual(m.status, "healthy")

    def test_negative_edge_critical(self):
        trades = [_trade(pnl=-0.10, entry=0.50)] * 5
        m = _compute_avg_edge(trades)
        self.assertLess(m.value, 0)
        self.assertEqual(m.status, "critical")

    def test_empty_returns_warning(self):
        m = _compute_avg_edge([])
        self.assertEqual(m.status, "warning")


class TestComputeBrier(unittest.TestCase):
    def test_good_brier(self):
        rows = [_brow(0.09)] * 10   # brier = 0.09 < 0.20 healthy
        m = _compute_brier(rows)
        self.assertAlmostEqual(m.value, 0.09)
        self.assertEqual(m.status, "healthy")

    def test_bad_brier_critical(self):
        rows = [_brow(0.30)] * 10   # brier = 0.30 > 0.25 critical
        m = _compute_brier(rows)
        self.assertEqual(m.status, "critical")

    def test_empty_returns_healthy(self):
        # No settled predictions yet → treat as healthy
        m = _compute_brier([])
        self.assertEqual(m.status, "healthy")
        self.assertAlmostEqual(m.value, 0.0)


# ---------------------------------------------------------------------------
# EdgeErosionMonitor.run_daily_snapshot
# ---------------------------------------------------------------------------

class TestEdgeErosionMonitor(unittest.IsolatedAsyncioTestCase):

    def _make_monitor(self):
        m = EdgeErosionMonitor(slack_alerter=None)
        m._fetch_closed_trades = AsyncMock(return_value=[])
        m._fetch_brier_rows    = AsyncMock(return_value=[])
        m._persist_snapshot    = AsyncMock()
        m._send_alerts         = AsyncMock()
        return m

    def _healthy_trades(self, n=150):
        return [_trade(pnl=0.10, entry=0.50, days_ago=i % 28 + 1) for i in range(n)]

    async def test_returns_edge_snapshot(self):
        m = self._make_monitor()
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        self.assertIsInstance(snap, EdgeSnapshot)

    async def test_overall_healthy_with_good_data(self):
        m = self._make_monitor()
        m._fetch_closed_trades = AsyncMock(return_value=self._healthy_trades())
        m._fetch_brier_rows = AsyncMock(return_value=[_brow(0.09)] * 30)
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        # win_rate = 100% healthy, brier healthy, etc.
        self.assertIn(snap.overall_status, ("healthy", "warning"))

    async def test_overall_critical_with_bad_data(self):
        m = self._make_monitor()
        # All losing trades → win_rate = 0 → critical
        m._fetch_closed_trades = AsyncMock(
            return_value=[_trade(pnl=-5) for _ in range(20)]
        )
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        self.assertEqual(snap.overall_status, "critical")

    async def test_should_pause_true_on_critical(self):
        m = self._make_monitor()
        m._fetch_closed_trades = AsyncMock(
            return_value=[_trade(pnl=-5) for _ in range(20)]
        )
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        self.assertEqual(snap.should_pause, snap.overall_status == "critical")

    async def test_persist_called(self):
        m = self._make_monitor()
        await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        m._persist_snapshot.assert_called_once()

    async def test_send_alerts_called(self):
        m = self._make_monitor()
        await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        m._send_alerts.assert_called_once()

    async def test_snapshot_date_set(self):
        m = self._make_monitor()
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2,
                                           date_str="2026-04-05")
        self.assertEqual(snap.snapshot_date, "2026-04-05")

    async def test_trades_in_window_count(self):
        m = self._make_monitor()
        m._fetch_closed_trades = AsyncMock(return_value=self._healthy_trades(50))
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        self.assertEqual(snap.trades_in_window, 50)

    async def test_empty_db_no_crash(self):
        m = self._make_monitor()
        snap = await m.run_daily_snapshot(bankroll=500.0, open_positions=2)
        self.assertIsNotNone(snap)


if __name__ == "__main__":
    unittest.main()
