"""Tests for core/risk/circuit_breakers.py"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from core.risk.circuit_breakers import BreakerResult, CircuitBreaker
from config import constants as C


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestCircuitBreakerUpdate(unittest.IsolatedAsyncioTestCase):

    def _make_cb(self):
        cb = CircuitBreaker(slack_alerter=None)
        # Silence DB persistence
        cb._persist_alert = AsyncMock()
        return cb

    async def test_no_trigger_when_profitable(self):
        cb = self._make_cb()
        result = await cb.update(
            bankroll=500.0, daily_pnl=10.0, weekly_pnl=20.0,
            monthly_pnl=30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertFalse(result.triggered)
        self.assertFalse(result.kill_switch)
        self.assertFalse(cb.is_paused())

    async def test_daily_loss_limit_triggered(self):
        cb = self._make_cb()
        # 5% of $500 = $25; lose $30 (6%)
        result = await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=2,
            starting_bankroll=500.0,
        )
        self.assertTrue(result.triggered)
        self.assertEqual(result.trigger_name, "daily_loss_limit")
        self.assertTrue(cb.is_paused())

    async def test_weekly_loss_limit_triggered(self):
        cb = self._make_cb()
        # 10% of $500 = $50; lose $60 (12%)
        result = await cb.update(
            bankroll=440.0, daily_pnl=-5.0, weekly_pnl=-60.0,
            monthly_pnl=-60.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertTrue(result.triggered)
        self.assertEqual(result.trigger_name, "weekly_loss_limit")

    async def test_monthly_loss_limit_triggered(self):
        cb = self._make_cb()
        # 15% of $500 = $75; lose $80 (16%)
        result = await cb.update(
            bankroll=420.0, daily_pnl=-5.0, weekly_pnl=-20.0,
            monthly_pnl=-80.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertTrue(result.triggered)
        self.assertEqual(result.trigger_name, "monthly_loss_limit")

    async def test_monthly_breaker_takes_priority_over_daily(self):
        # Monthly is checked first; daily is also breached but monthly fires
        cb = self._make_cb()
        result = await cb.update(
            bankroll=400.0, daily_pnl=-30.0, weekly_pnl=-55.0,
            monthly_pnl=-80.0, open_positions=2,
            starting_bankroll=500.0,
        )
        self.assertEqual(result.trigger_name, "monthly_loss_limit")

    async def test_kill_switch_at_300(self):
        cb = self._make_cb()
        cb._fire_slack_kill = AsyncMock()
        result = await cb.update(
            bankroll=C.KILL_SWITCH, daily_pnl=0.0, weekly_pnl=0.0,
            monthly_pnl=0.0, open_positions=3,
            starting_bankroll=500.0,
        )
        self.assertTrue(result.kill_switch)
        self.assertTrue(cb.kill_switch_active())

    async def test_kill_switch_below_300(self):
        cb = self._make_cb()
        cb._fire_slack_kill = AsyncMock()
        result = await cb.update(
            bankroll=299.99, daily_pnl=0.0, weekly_pnl=0.0,
            monthly_pnl=0.0, open_positions=0,
        )
        self.assertTrue(result.kill_switch)

    async def test_already_paused_returns_not_triggered(self):
        cb = self._make_cb()
        # First call triggers
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        # Second call — already paused
        result = await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertFalse(result.triggered)

    async def test_is_paused_false_initially(self):
        cb = self._make_cb()
        self.assertFalse(cb.is_paused())

    async def test_is_paused_true_after_trigger(self):
        cb = self._make_cb()
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertTrue(cb.is_paused())

    async def test_reset_pause_clears_timer(self):
        cb = self._make_cb()
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        self.assertTrue(cb.is_paused())
        cb.reset_pause()
        self.assertFalse(cb.is_paused())

    async def test_force_kill(self):
        cb = self._make_cb()
        cb.force_kill()
        self.assertTrue(cb.kill_switch_active())
        self.assertTrue(cb.is_paused())

    async def test_persist_alert_called_on_trigger(self):
        cb = self._make_cb()
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        cb._persist_alert.assert_called_once()

    async def test_slack_alerter_called_on_trigger(self):
        mock_alerter = MagicMock()
        mock_alerter.circuit_breaker = MagicMock(return_value=True)
        cb = CircuitBreaker(slack_alerter=mock_alerter)
        cb._persist_alert = AsyncMock()
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        # Slack call is async via run_in_executor; just verify no crash
        self.assertTrue(cb.is_paused())

    async def test_paused_until_is_roughly_24h_for_daily(self):
        cb = self._make_cb()
        before = datetime.now(timezone.utc)
        await cb.update(
            bankroll=470.0, daily_pnl=-30.0, weekly_pnl=-30.0,
            monthly_pnl=-30.0, open_positions=1,
            starting_bankroll=500.0,
        )
        after = datetime.now(timezone.utc)
        paused = cb.paused_until()
        self.assertIsNotNone(paused)
        # Should be roughly 24 hours from now
        delta = paused - before
        self.assertGreater(delta.total_seconds(), 23 * 3600)
        self.assertLess(delta.total_seconds(), 25 * 3600)


if __name__ == "__main__":
    unittest.main()
