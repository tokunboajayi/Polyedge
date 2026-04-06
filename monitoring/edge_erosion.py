"""
Edge erosion monitor for PolyEdge v5.

Five metrics tracked on a rolling 30-day window
-------------------------------------------------
1. win_rate        — fraction of closed trades with pnl > 0
2. sharpe_ratio    — mean daily pnl / stddev daily pnl (annualised)
3. trade_frequency — average settled trades per calendar day
4. avg_edge        — mean (entry_price - exit_price for NO / exit_price - entry for YES)
                     as a fraction of entry price; approximates model-to-market edge
5. brier_score     — mean Brier contribution from calibration table (settled rows)

Threshold matrix (from constants.py)
--------------------------------------
Metric          Healthy          Warning          Critical (auto-pause)
win_rate        >= 55%           >= 50%           < 50%
sharpe_ratio    >= 1.0           >= 0.5           < 0.5
trade_frequency 3–8 /day         1–15 /day        < 1 or > 15
avg_edge        >= 5%            >= 3%            < 3%
brier_score     <= 0.20          <= 0.25          > 0.25

Status: "healthy" | "warning" | "critical"
Overall status = worst of all five individual statuses.

Auto-pause
----------
Any metric reaching "critical" triggers auto-pause. EdgeErosionMonitor returns
pause=True in the snapshot; the engine must honour this.

Daily snapshot
--------------
run_daily_snapshot() computes all five metrics, persists one row to
edge_metrics (upserted by snapshot_date), and fires Slack alerts for any
metric not "healthy". Returns EdgeSnapshot.

Usage::

    monitor = EdgeErosionMonitor(slack_alerter=alerter)
    snap    = await monitor.run_daily_snapshot(
        bankroll=487.50, open_positions=2
    )
    if snap.should_pause:
        engine.pause()
"""

import dataclasses
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from config import constants as C

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class MetricResult:
    name:    str
    value:   float
    status:  str   # "healthy" | "warning" | "critical"
    healthy_threshold: float
    warning_threshold: float


@dataclasses.dataclass(slots=True)
class EdgeSnapshot:
    snapshot_date:   str          # UTC date "YYYY-MM-DD"
    win_rate:        MetricResult
    sharpe_ratio:    MetricResult
    trade_frequency: MetricResult
    avg_edge:        MetricResult
    brier_score:     MetricResult
    overall_status:  str          # worst of five
    trades_in_window: int
    should_pause:    bool
    computed_at:     str          # UTC ISO-8601


# ---------------------------------------------------------------------------
# EdgeErosionMonitor
# ---------------------------------------------------------------------------

class EdgeErosionMonitor:
    """Computes rolling 30-day edge metrics, stores snapshots, fires alerts.

    Usage::

        monitor = EdgeErosionMonitor(slack_alerter=alerter)
        snap = await monitor.run_daily_snapshot(bankroll=487.50, open_positions=2)
    """

    def __init__(self, slack_alerter: Any | None = None) -> None:
        self._alerter = slack_alerter

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_daily_snapshot(
        self,
        bankroll:       float,
        open_positions: int,
        date_str:       str | None = None,   # override for testing
    ) -> EdgeSnapshot:
        """Compute all five metrics, persist, alert, return snapshot."""
        now     = datetime.now(timezone.utc)
        date    = date_str or now.strftime("%Y-%m-%d")
        cutoff  = (now - timedelta(days=_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

        trades = await self._fetch_closed_trades(cutoff)
        brier_rows = await self._fetch_brier_rows(cutoff)

        win_rate_r    = _compute_win_rate(trades)
        sharpe_r      = _compute_sharpe(trades, _WINDOW_DAYS)
        frequency_r   = _compute_frequency(trades, _WINDOW_DAYS)
        avg_edge_r    = _compute_avg_edge(trades)
        brier_r       = _compute_brier(brier_rows)

        metrics   = [win_rate_r, sharpe_r, frequency_r, avg_edge_r, brier_r]
        statuses  = [m.status for m in metrics]
        overall   = _worst_status(statuses)
        should_pause = overall == "critical"

        snap = EdgeSnapshot(
            snapshot_date=date,
            win_rate=win_rate_r,
            sharpe_ratio=sharpe_r,
            trade_frequency=frequency_r,
            avg_edge=avg_edge_r,
            brier_score=brier_r,
            overall_status=overall,
            trades_in_window=len(trades),
            should_pause=should_pause,
            computed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        await self._persist_snapshot(snap)
        await self._send_alerts(snap, bankroll, open_positions)

        logger.info(
            "edge_snapshot  date=%s  overall=%s  pause=%s  trades=%d  "
            "win_rate=%.3f  sharpe=%.2f  freq=%.2f  edge=%.3f  brier=%.3f",
            date, overall, should_pause, len(trades),
            win_rate_r.value, sharpe_r.value, frequency_r.value,
            avg_edge_r.value, brier_r.value,
        )

        return snap

    # ------------------------------------------------------------------
    # DB fetches
    # ------------------------------------------------------------------

    async def _fetch_closed_trades(self, cutoff: str) -> list[dict]:
        """Return closed/settled trades within the rolling window."""
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT entry_price, exit_price, pnl, fees_paid,
                           entry_time, exit_time, side, strategy
                    FROM   trades
                    WHERE  status IN ('closed', 'settled')
                    AND    exit_time >= ?
                    AND    pnl IS NOT NULL
                    """,
                    (cutoff,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("edge_fetch_trades_failed  error=%s", exc)
            return []
        return [dict(r) for r in rows]

    async def _fetch_brier_rows(self, cutoff: str) -> list[dict]:
        """Return settled calibration rows within the rolling window."""
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
                    (cutoff,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("edge_fetch_brier_failed  error=%s", exc)
            return []
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist_snapshot(self, snap: EdgeSnapshot) -> None:
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                await db.execute(
                    """
                    INSERT INTO edge_metrics
                        (snapshot_date, win_rate, sharpe_ratio, trade_frequency,
                         avg_edge, brier_score, trades_in_window, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_date) DO UPDATE SET
                        win_rate        = excluded.win_rate,
                        sharpe_ratio    = excluded.sharpe_ratio,
                        trade_frequency = excluded.trade_frequency,
                        avg_edge        = excluded.avg_edge,
                        brier_score     = excluded.brier_score,
                        trades_in_window= excluded.trades_in_window,
                        status          = excluded.status,
                        created_at      = excluded.created_at
                    """,
                    (
                        snap.snapshot_date,
                        snap.win_rate.value,
                        snap.sharpe_ratio.value,
                        snap.trade_frequency.value,
                        snap.avg_edge.value,
                        snap.brier_score.value,
                        snap.trades_in_window,
                        snap.overall_status,
                        snap.computed_at,
                    ),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("edge_persist_snapshot_failed  error=%s", exc)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def _send_alerts(
        self,
        snap:           EdgeSnapshot,
        bankroll:       float,
        open_positions: int,
    ) -> None:
        if self._alerter is None:
            return

        import asyncio
        for metric in [
            snap.win_rate, snap.sharpe_ratio, snap.trade_frequency,
            snap.avg_edge, snap.brier_score,
        ]:
            if metric.status == "healthy":
                continue
            try:
                threshold = (
                    metric.warning_threshold
                    if metric.status == "warning"
                    else metric.healthy_threshold
                )
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda m=metric, t=threshold: self._alerter.edge_metric_warning(
                        metric=m.name,
                        value=m.value,
                        threshold=t,
                        severity=m.status,
                        bankroll=bankroll,
                        open_positions=open_positions,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "edge_alert_failed  metric=%s  error=%s", metric.name, exc
                )


# ---------------------------------------------------------------------------
# Pure metric computations
# ---------------------------------------------------------------------------

def _compute_win_rate(trades: list[dict]) -> MetricResult:
    if not trades:
        return MetricResult("win_rate", 0.0, "warning",
                            C.EDGE_WIN_RATE_HEALTHY, C.EDGE_WIN_RATE_WARNING)
    wins  = sum(1 for t in trades if t["pnl"] > 0)
    rate  = wins / len(trades)
    status = _win_rate_status(rate)
    return MetricResult("win_rate", round(rate, 4), status,
                        C.EDGE_WIN_RATE_HEALTHY, C.EDGE_WIN_RATE_WARNING)


def _win_rate_status(rate: float) -> str:
    if rate >= C.EDGE_WIN_RATE_HEALTHY:
        return "healthy"
    if rate >= C.EDGE_WIN_RATE_WARNING:
        return "warning"
    return "critical"


def _compute_sharpe(trades: list[dict], window_days: int) -> MetricResult:
    """Daily P&L Sharpe ratio over the window."""
    if len(trades) < 2:
        return MetricResult("sharpe_ratio", 0.0, "warning",
                            C.EDGE_SHARPE_HEALTHY, C.EDGE_SHARPE_WARNING)

    # Bucket P&L by exit date
    daily: dict[str, float] = {}
    for t in trades:
        day = (t.get("exit_time") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0.0) + (t["pnl"] or 0.0)

    if len(daily) < 2:
        return MetricResult("sharpe_ratio", 0.0, "warning",
                            C.EDGE_SHARPE_HEALTHY, C.EDGE_SHARPE_WARNING)

    daily_pnls = list(daily.values())
    mean_pnl   = statistics.mean(daily_pnls)
    std_pnl    = statistics.stdev(daily_pnls)

    if std_pnl == 0:
        sharpe = 0.0
    else:
        # Annualise: multiply daily Sharpe by sqrt(252)
        sharpe = round((mean_pnl / std_pnl) * math.sqrt(252), 4)

    status = _sharpe_status(sharpe)
    return MetricResult("sharpe_ratio", sharpe, status,
                        C.EDGE_SHARPE_HEALTHY, C.EDGE_SHARPE_WARNING)


def _sharpe_status(sharpe: float) -> str:
    if sharpe >= C.EDGE_SHARPE_HEALTHY:
        return "healthy"
    if sharpe >= C.EDGE_SHARPE_WARNING:
        return "warning"
    return "critical"


def _compute_frequency(trades: list[dict], window_days: int) -> MetricResult:
    freq   = len(trades) / max(1, window_days)
    status = _frequency_status(freq)
    return MetricResult("trade_frequency", round(freq, 4), status,
                        C.EDGE_FREQUENCY_MIN_HEALTHY, C.EDGE_FREQUENCY_MIN_WARNING)


def _frequency_status(freq: float) -> str:
    if C.EDGE_FREQUENCY_MIN_HEALTHY <= freq <= C.EDGE_FREQUENCY_MAX_HEALTHY:
        return "healthy"
    if C.EDGE_FREQUENCY_MIN_WARNING <= freq <= C.EDGE_FREQUENCY_MAX_WARNING:
        return "warning"
    return "critical"


def _compute_avg_edge(trades: list[dict]) -> MetricResult:
    """Average realised edge as a fraction of entry price.

    For closed trades: edge = |pnl| / (entry_price * num_contracts) — but we
    don't have num_contracts in the fetch. Use pnl / entry_price as a proxy.
    A positive pnl relative to entry is a proxy for the realised edge.
    """
    if not trades:
        return MetricResult("avg_edge", 0.0, "warning",
                            C.EDGE_AVG_EDGE_HEALTHY, C.EDGE_AVG_EDGE_WARNING)

    edges = []
    for t in trades:
        ep = t.get("entry_price") or 0
        if ep > 0 and t.get("pnl") is not None:
            edges.append(t["pnl"] / ep)

    if not edges:
        return MetricResult("avg_edge", 0.0, "warning",
                            C.EDGE_AVG_EDGE_HEALTHY, C.EDGE_AVG_EDGE_WARNING)

    avg   = round(statistics.mean(edges), 6)
    status = _edge_status(avg)
    return MetricResult("avg_edge", avg, status,
                        C.EDGE_AVG_EDGE_HEALTHY, C.EDGE_AVG_EDGE_WARNING)


def _edge_status(avg: float) -> str:
    if avg >= C.EDGE_AVG_EDGE_HEALTHY:
        return "healthy"
    if avg >= C.EDGE_AVG_EDGE_WARNING:
        return "warning"
    return "critical"


def _compute_brier(brier_rows: list[dict]) -> MetricResult:
    if not brier_rows:
        # No settled predictions yet — treat as healthy (not enough data to penalise)
        return MetricResult("brier_score", 0.0, "healthy",
                            C.EDGE_BRIER_HEALTHY, C.EDGE_BRIER_WARNING)
    score  = round(
        sum(r["brier_contribution"] for r in brier_rows) / len(brier_rows), 6
    )
    status = _brier_status(score)
    return MetricResult("brier_score", score, status,
                        C.EDGE_BRIER_HEALTHY, C.EDGE_BRIER_WARNING)


def _brier_status(score: float) -> str:
    # Lower brier is better; critical above WARNING threshold
    if score <= C.EDGE_BRIER_HEALTHY:
        return "healthy"
    if score <= C.EDGE_BRIER_WARNING:
        return "warning"
    return "critical"


_STATUS_ORDER = {"healthy": 0, "warning": 1, "critical": 2}


def _worst_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _STATUS_ORDER.get(s, 0))
