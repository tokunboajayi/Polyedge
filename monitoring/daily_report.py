"""
Daily and weekly P&L report generator for PolyEdge v5.

Daily report  (sent every midnight UTC via cron / engine scheduler)
---------------------------------------------------------------------
  - Trades executed today (count)
  - Net P&L today
  - P&L month-to-date
  - 30-day rolling win rate
  - 30-day rolling Brier score
  - Claude API cost today
  - Current bankroll and open positions
  → Slack #daily channel via SlackAlerter.daily_summary()

Weekly report  (sent every Monday 00:05 UTC)
---------------------------------------------
  - Trades this week (count)
  - Net P&L this week
  - P&L year-to-date
  - Weekly win rate
  - 30-day Sharpe ratio
  - 30-day average edge
  - Claude API cost this week
  → Slack #weekly channel via SlackAlerter.weekly_summary()

Usage::

    reporter = DailyReporter(slack_alerter=alerter)
    await reporter.send_daily(bankroll=487.50, open_positions=2)
    await reporter.send_weekly(bankroll=487.50, open_positions=2)
"""

import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DailyReporter
# ---------------------------------------------------------------------------

class DailyReporter:
    """Generates and sends daily/weekly P&L summaries.

    Usage::

        reporter = DailyReporter(slack_alerter=alerter)
        await reporter.send_daily(bankroll=487.50, open_positions=2)
    """

    def __init__(self, slack_alerter: Any | None = None) -> None:
        self._alerter = slack_alerter

    # ------------------------------------------------------------------
    # Public: daily
    # ------------------------------------------------------------------

    async def send_daily(
        self,
        bankroll:       float,
        open_positions: int,
        date_str:       str | None = None,   # override for testing: "YYYY-MM-DD"
    ) -> dict:
        """Compute today's metrics and send the daily summary.

        Returns a dict of all computed values (useful for tests / logging).
        """
        now      = datetime.now(timezone.utc)
        today    = date_str or now.strftime("%Y-%m-%d")
        day_start = f"{today}T00:00:00Z"
        month_start = f"{today[:7]}-01T00:00:00Z"
        window_30d  = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        trades_today  = await self._fetch_closed_trades(day_start)
        trades_mtd    = await self._fetch_closed_trades(month_start)
        trades_30d    = await self._fetch_closed_trades(window_30d)
        brier_30d     = await self._fetch_brier_rows(window_30d)
        api_cost_today = await self._fetch_api_cost(day_start)

        pnl_today  = round(sum(t["pnl"] for t in trades_today), 4)
        pnl_mtd    = round(sum(t["pnl"] for t in trades_mtd),   4)
        win_rate   = _win_rate(trades_30d)
        brier      = _mean_brier(brier_30d)

        data = {
            "date":           today,
            "trades_today":   len(trades_today),
            "pnl_today":      pnl_today,
            "pnl_mtd":        pnl_mtd,
            "win_rate_30d":   win_rate,
            "brier_30d":      brier,
            "api_cost_today": api_cost_today,
            "bankroll":       bankroll,
            "open_positions": open_positions,
        }

        logger.info(
            "daily_report  date=%s  trades=%d  pnl=$%.2f  mtd=$%.2f  "
            "win_rate=%.1f%%  brier=%.3f  api_cost=$%.4f",
            today, len(trades_today), pnl_today, pnl_mtd,
            win_rate * 100, brier, api_cost_today,
        )

        if self._alerter is not None:
            try:
                self._alerter.daily_summary(
                    date=today,
                    trades_today=len(trades_today),
                    pnl_today=pnl_today,
                    pnl_mtd=pnl_mtd,
                    win_rate_30d=win_rate,
                    brier_30d=brier,
                    api_cost_today=api_cost_today,
                    bankroll=bankroll,
                    open_positions=open_positions,
                )
            except Exception as exc:
                logger.warning("daily_report_slack_failed  error=%s", exc)

        return data

    # ------------------------------------------------------------------
    # Public: weekly
    # ------------------------------------------------------------------

    async def send_weekly(
        self,
        bankroll:       float,
        open_positions: int,
        week_start_str: str | None = None,   # override: "YYYY-MM-DD" Monday
    ) -> dict:
        """Compute this week's metrics and send the weekly report.

        Returns a dict of all computed values.
        """
        now = datetime.now(timezone.utc)

        if week_start_str:
            week_start_dt = datetime.strptime(week_start_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        else:
            # Roll back to most recent Monday
            days_since_monday = now.weekday()
            week_start_dt = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        week_start  = week_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        year_start  = f"{now.year}-01-01T00:00:00Z"
        window_30d  = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        week_label  = f"W/c {week_start_dt.strftime('%d %b %Y')}"

        trades_week = await self._fetch_closed_trades(week_start)
        trades_ytd  = await self._fetch_closed_trades(year_start)
        trades_30d  = await self._fetch_closed_trades(window_30d)
        api_cost_week = await self._fetch_api_cost(week_start)

        pnl_week   = round(sum(t["pnl"] for t in trades_week), 4)
        pnl_ytd    = round(sum(t["pnl"] for t in trades_ytd),  4)
        win_rate   = _win_rate(trades_week)
        sharpe     = _sharpe_ratio(trades_30d)
        avg_edge   = _avg_edge(trades_30d)

        data = {
            "week_label":    week_label,
            "trades_week":   len(trades_week),
            "pnl_week":      pnl_week,
            "pnl_ytd":       pnl_ytd,
            "win_rate_week": win_rate,
            "sharpe_30d":    sharpe,
            "avg_edge_30d":  avg_edge,
            "api_cost_week": api_cost_week,
            "bankroll":      bankroll,
            "open_positions": open_positions,
        }

        logger.info(
            "weekly_report  week=%s  trades=%d  pnl=$%.2f  ytd=$%.2f  "
            "win_rate=%.1f%%  sharpe=%.2f  avg_edge=%.2f%%  api_cost=$%.4f",
            week_label, len(trades_week), pnl_week, pnl_ytd,
            win_rate * 100, sharpe, avg_edge * 100, api_cost_week,
        )

        if self._alerter is not None:
            try:
                self._alerter.weekly_summary(
                    week_label=week_label,
                    trades_week=len(trades_week),
                    pnl_week=pnl_week,
                    pnl_ytd=pnl_ytd,
                    win_rate_week=win_rate,
                    sharpe_30d=sharpe,
                    avg_edge_30d=avg_edge,
                    api_cost_week=api_cost_week,
                    bankroll=bankroll,
                    open_positions=open_positions,
                )
            except Exception as exc:
                logger.warning("weekly_report_slack_failed  error=%s", exc)

        return data

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _fetch_closed_trades(self, since: str) -> list[dict]:
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT pnl, entry_price, exit_price, entry_time,
                           exit_time, side, strategy, fees_paid
                    FROM   trades
                    WHERE  status IN ('closed', 'settled')
                    AND    pnl IS NOT NULL
                    AND    exit_time >= ?
                    """,
                    (since,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("daily_report_fetch_trades_failed  error=%s", exc)
            return []
        return [dict(r) for r in rows]

    async def _fetch_brier_rows(self, since: str) -> list[dict]:
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT brier_contribution
                    FROM   calibration
                    WHERE  actual_outcome IS NOT NULL
                    AND    brier_contribution IS NOT NULL
                    AND    settled_at >= ?
                    """,
                    (since,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("daily_report_fetch_brier_failed  error=%s", exc)
            return []
        return [dict(r) for r in rows]

    async def _fetch_api_cost(self, since: str) -> float:
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                cursor = await db.execute(
                    "SELECT SUM(cost_usd) FROM api_costs WHERE called_at >= ?",
                    (since,),
                )
                row = await cursor.fetchone()
        except Exception as exc:
            logger.warning("daily_report_fetch_api_cost_failed  error=%s", exc)
            return 0.0
        return round(float(row[0] or 0.0), 6)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    return round(wins / len(trades), 4)


def _mean_brier(brier_rows: list[dict]) -> float:
    if not brier_rows:
        return 0.0
    return round(
        sum(r["brier_contribution"] for r in brier_rows) / len(brier_rows), 6
    )


def _sharpe_ratio(trades: list[dict]) -> float:
    """Annualised Sharpe from daily P&L buckets."""
    if len(trades) < 2:
        return 0.0
    daily: dict[str, float] = {}
    for t in trades:
        day = (t.get("exit_time") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0.0) + (t.get("pnl") or 0.0)
    if len(daily) < 2:
        return 0.0
    vals = list(daily.values())
    mu   = statistics.mean(vals)
    sd   = statistics.stdev(vals)
    if sd == 0:
        return 0.0
    return round((mu / sd) * math.sqrt(252), 4)


def _avg_edge(trades: list[dict]) -> float:
    """Average pnl / entry_price as edge proxy."""
    edges = []
    for t in trades:
        ep  = t.get("entry_price") or 0
        pnl = t.get("pnl")
        if ep > 0 and pnl is not None:
            edges.append(pnl / ep)
    if not edges:
        return 0.0
    return round(statistics.mean(edges), 6)
