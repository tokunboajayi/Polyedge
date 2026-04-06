"""Tests for monitoring/daily_report.py"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from monitoring.daily_report import (
    DailyReporter,
    _avg_edge,
    _mean_brier,
    _sharpe_ratio,
    _win_rate,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _trade(pnl, entry=0.50, days_ago=1):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago))
    return {
        "pnl": pnl,
        "entry_price": entry,
        "exit_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class TestWinRate(unittest.TestCase):
    def test_all_wins(self):
        trades = [_trade(5)] * 10
        self.assertAlmostEqual(_win_rate(trades), 1.0)

    def test_all_losses(self):
        trades = [_trade(-5)] * 10
        self.assertAlmostEqual(_win_rate(trades), 0.0)

    def test_mixed(self):
        trades = [_trade(5)] * 6 + [_trade(-3)] * 4
        self.assertAlmostEqual(_win_rate(trades), 0.60)

    def test_empty(self):
        self.assertAlmostEqual(_win_rate([]), 0.0)


class TestMeanBrier(unittest.TestCase):
    def test_basic(self):
        rows = [{"brier_contribution": 0.09}] * 4
        self.assertAlmostEqual(_mean_brier(rows), 0.09)

    def test_empty(self):
        self.assertAlmostEqual(_mean_brier([]), 0.0)


class TestSharpeRatio(unittest.TestCase):
    def test_empty(self):
        self.assertAlmostEqual(_sharpe_ratio([]), 0.0)

    def test_single(self):
        self.assertAlmostEqual(_sharpe_ratio([_trade(5)]), 0.0)

    def test_two_same_day_one_day(self):
        trades = [_trade(5, days_ago=1), _trade(3, days_ago=1)]
        # All pnl on same day → only 1 day bucket → stdev undefined
        result = _sharpe_ratio(trades)
        self.assertAlmostEqual(result, 0.0)

    def test_positive_sharpe_multi_day(self):
        trades = [_trade(5, days_ago=i) for i in range(1, 4)] + \
                 [_trade(3, days_ago=i) for i in range(4, 7)]
        result = _sharpe_ratio(trades)
        self.assertIsInstance(result, float)


class TestAvgEdge(unittest.TestCase):
    def test_positive_edge(self):
        trades = [_trade(pnl=0.10, entry=0.50)] * 5
        self.assertAlmostEqual(_avg_edge(trades), 0.20, places=4)

    def test_negative_edge(self):
        trades = [_trade(pnl=-0.05, entry=0.50)] * 5
        self.assertAlmostEqual(_avg_edge(trades), -0.10, places=4)

    def test_empty(self):
        self.assertAlmostEqual(_avg_edge([]), 0.0)


# ---------------------------------------------------------------------------
# DailyReporter.send_daily
# ---------------------------------------------------------------------------

class TestSendDaily(unittest.IsolatedAsyncioTestCase):

    def _make_reporter(self, alerter=None):
        r = DailyReporter(slack_alerter=alerter)
        r._fetch_closed_trades = AsyncMock(return_value=[])
        r._fetch_brier_rows    = AsyncMock(return_value=[])
        r._fetch_api_cost      = AsyncMock(return_value=0.05)
        return r

    async def test_returns_dict(self):
        r = self._make_reporter()
        data = await r.send_daily(bankroll=500.0, open_positions=2)
        self.assertIsInstance(data, dict)

    async def test_contains_required_keys(self):
        r = self._make_reporter()
        data = await r.send_daily(bankroll=500.0, open_positions=2)
        for key in ("date", "trades_today", "pnl_today", "pnl_mtd",
                    "win_rate_30d", "brier_30d", "api_cost_today"):
            self.assertIn(key, data)

    async def test_pnl_today_sums_closed_trades(self):
        r = self._make_reporter()
        trades = [{"pnl": 5.0, "entry_price": 0.5, "exit_time": "2026-04-05T12:00:00Z"},
                  {"pnl": 3.0, "entry_price": 0.5, "exit_time": "2026-04-05T13:00:00Z"}]
        r._fetch_closed_trades = AsyncMock(return_value=trades)
        data = await r.send_daily(bankroll=500.0, open_positions=2,
                                   date_str="2026-04-05")
        self.assertAlmostEqual(data["pnl_today"], 8.0)

    async def test_date_override_used(self):
        r = self._make_reporter()
        data = await r.send_daily(bankroll=500.0, open_positions=2,
                                   date_str="2026-04-05")
        self.assertEqual(data["date"], "2026-04-05")

    async def test_slack_daily_summary_called(self):
        mock_alerter = MagicMock()
        r = self._make_reporter(alerter=mock_alerter)
        await r.send_daily(bankroll=500.0, open_positions=2)
        mock_alerter.daily_summary.assert_called_once()

    async def test_no_crash_without_alerter(self):
        r = self._make_reporter(alerter=None)
        data = await r.send_daily(bankroll=500.0, open_positions=2)
        self.assertIsInstance(data, dict)

    async def test_api_cost_included(self):
        r = self._make_reporter()
        r._fetch_api_cost = AsyncMock(return_value=0.042)
        data = await r.send_daily(bankroll=500.0, open_positions=2)
        self.assertAlmostEqual(data["api_cost_today"], 0.042)

    async def test_bankroll_in_result(self):
        r = self._make_reporter()
        data = await r.send_daily(bankroll=487.50, open_positions=3)
        self.assertAlmostEqual(data["bankroll"], 487.50)


# ---------------------------------------------------------------------------
# DailyReporter.send_weekly
# ---------------------------------------------------------------------------

class TestSendWeekly(unittest.IsolatedAsyncioTestCase):

    def _make_reporter(self, alerter=None):
        r = DailyReporter(slack_alerter=alerter)
        r._fetch_closed_trades = AsyncMock(return_value=[])
        r._fetch_api_cost      = AsyncMock(return_value=0.20)
        return r

    async def test_returns_dict(self):
        r = self._make_reporter()
        data = await r.send_weekly(bankroll=500.0, open_positions=2)
        self.assertIsInstance(data, dict)

    async def test_contains_required_keys(self):
        r = self._make_reporter()
        data = await r.send_weekly(bankroll=500.0, open_positions=2)
        for key in ("week_label", "trades_week", "pnl_week", "pnl_ytd",
                    "win_rate_week", "sharpe_30d", "avg_edge_30d", "api_cost_week"):
            self.assertIn(key, data)

    async def test_pnl_week_sums_correctly(self):
        r = self._make_reporter()
        trades = [{"pnl": 10.0, "entry_price": 0.5, "exit_time": "2026-04-01T10:00:00Z"},
                  {"pnl": -3.0, "entry_price": 0.5, "exit_time": "2026-04-02T10:00:00Z"}]
        r._fetch_closed_trades = AsyncMock(return_value=trades)
        data = await r.send_weekly(bankroll=500.0, open_positions=2,
                                    week_start_str="2026-03-30")
        self.assertAlmostEqual(data["pnl_week"], 7.0)

    async def test_week_start_override(self):
        r = self._make_reporter()
        data = await r.send_weekly(bankroll=500.0, open_positions=2,
                                    week_start_str="2026-03-30")
        self.assertIn("30 Mar 2026", data["week_label"])

    async def test_slack_weekly_summary_called(self):
        mock_alerter = MagicMock()
        r = self._make_reporter(alerter=mock_alerter)
        await r.send_weekly(bankroll=500.0, open_positions=2)
        mock_alerter.weekly_summary.assert_called_once()

    async def test_no_crash_without_alerter(self):
        r = self._make_reporter(alerter=None)
        data = await r.send_weekly(bankroll=500.0, open_positions=2)
        self.assertIsInstance(data, dict)

    async def test_slack_args_include_week_label(self):
        mock_alerter = MagicMock()
        r = self._make_reporter(alerter=mock_alerter)
        await r.send_weekly(bankroll=500.0, open_positions=2,
                             week_start_str="2026-03-30")
        call_kwargs = mock_alerter.weekly_summary.call_args.kwargs
        self.assertIn("30 Mar 2026", call_kwargs["week_label"])


if __name__ == "__main__":
    unittest.main()
