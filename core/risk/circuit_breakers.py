"""
Circuit breakers for PolyEdge v5.

Three drawdown levels (from CLAUDE.md)
---------------------------------------
  DAILY   — 5%   loss of bankroll in a calendar day  → pause 24 hours
  WEEKLY  — 10%  loss of bankroll in a rolling week  → pause 72 hours
  MONTHLY — 15%  loss of bankroll in a rolling month → enter review mode

Kill switch
-----------
  bankroll <= $300  → halt all trading, close all positions

Each check is stateless: the caller passes in current P&L figures and the
circuit breaker computes whether a threshold is breached.  The breaker also
persists an alert row to the DB and fires a Slack critical alert.

State: paused_until
-------------------
The breaker maintains an in-memory `paused_until` timestamp.  The caller
should query `is_paused()` before each trade attempt.  State is intentionally
in-memory (not persisted) — a restart always starts fresh, which is safe
because the paused_until check is advisory and the kill switch is absolute.

Usage::

    cb = CircuitBreaker()
    cb.update(bankroll=480.0, daily_pnl=-26.0, weekly_pnl=-26.0,
              monthly_pnl=-26.0, open_positions=3)

    if cb.is_paused():
        return   # trading blocked

    if cb.kill_switch_active():
        # close all positions immediately
"""

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pause durations
# ---------------------------------------------------------------------------

_PAUSE_DAILY_H:   int = 24
_PAUSE_WEEKLY_H:  int = 72
_PAUSE_MONTHLY_H: int = 24 * 30   # review mode — very long pause


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class BreakerResult:
    """Outcome of a single CircuitBreaker.update() call."""
    triggered:      bool
    trigger_name:   str     # "" when not triggered
    loss_pct:       float   # actual loss fraction that triggered
    threshold_pct:  float   # threshold that was breached
    paused_until:   datetime | None
    kill_switch:    bool


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Monitors P&L and halts trading when drawdown limits are exceeded.

    Thread-safe: uses a simple mutex around state mutations so it can be
    called from async tasks without race conditions.
    """

    def __init__(
        self,
        slack_alerter: Any | None = None,   # SlackAlerter or None
    ) -> None:
        self._alerter      = slack_alerter
        self._paused_until: datetime | None = None
        self._kill_active:  bool = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public read API (safe to call without lock — atomic reads)
    # ------------------------------------------------------------------

    def is_paused(self) -> bool:
        """True if trading is currently paused (circuit breaker or kill switch)."""
        if self._kill_active:
            return True
        if self._paused_until is None:
            return False
        return datetime.now(timezone.utc) < self._paused_until

    def kill_switch_active(self) -> bool:
        return self._kill_active

    def paused_until(self) -> datetime | None:
        return self._paused_until

    # ------------------------------------------------------------------
    # Main update — call once per trade cycle
    # ------------------------------------------------------------------

    async def update(
        self,
        bankroll:       float,
        daily_pnl:      float,
        weekly_pnl:     float,
        monthly_pnl:    float,
        open_positions: int,
        starting_bankroll: float | None = None,
    ) -> BreakerResult:
        """Check all thresholds and pause if any is breached.

        Args:
            bankroll:          Current bankroll in dollars.
            daily_pnl:         Net P&L today in dollars (negative = loss).
            weekly_pnl:        Net P&L this rolling 7-day period.
            monthly_pnl:       Net P&L this rolling 30-day period.
            open_positions:    Number of currently open positions.
            starting_bankroll: Reference bankroll for % calculation.
                               Defaults to bankroll + |pnl| if not provided.
        Returns:
            BreakerResult — check .triggered and .kill_switch.
        """
        async with self._lock:
            return await self._check(
                bankroll, daily_pnl, weekly_pnl, monthly_pnl,
                open_positions, starting_bankroll,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _check(
        self,
        bankroll:       float,
        daily_pnl:      float,
        weekly_pnl:     float,
        monthly_pnl:    float,
        open_positions: int,
        starting_bankroll: float | None,
    ) -> BreakerResult:

        now = datetime.now(timezone.utc)

        # --- Kill switch (absolute, irreversible in this session) ------
        if bankroll <= C.KILL_SWITCH:
            if not self._kill_active:
                self._kill_active  = True
                self._paused_until = datetime(9999, 12, 31, tzinfo=timezone.utc)
                logger.critical(
                    "kill_switch_triggered  bankroll=$%.2f  threshold=$%.2f",
                    bankroll, C.KILL_SWITCH,
                )
                await self._fire_slack_kill(bankroll, open_positions)
                await self._persist_alert(
                    "critical", "circuit_breaker",
                    f"Kill switch: bankroll ${bankroll:.2f} <= ${C.KILL_SWITCH:.2f}",
                )
            return BreakerResult(
                triggered=True, trigger_name="kill_switch",
                loss_pct=0.0, threshold_pct=0.0,
                paused_until=self._paused_until, kill_switch=True,
            )

        # --- Already paused — no need to re-trigger --------------------
        if self._paused_until and now < self._paused_until:
            return BreakerResult(
                triggered=False, trigger_name="",
                loss_pct=0.0, threshold_pct=0.0,
                paused_until=self._paused_until, kill_switch=False,
            )

        # --- Compute reference bankroll --------------------------------
        ref = starting_bankroll if starting_bankroll else max(1.0, bankroll)

        # --- Check drawdown tiers (most severe first) ------------------
        checks = [
            ("monthly_loss_limit", monthly_pnl, C.MONTHLY_LOSS_LIMIT,
             timedelta(hours=_PAUSE_MONTHLY_H)),
            ("weekly_loss_limit",  weekly_pnl,  C.WEEKLY_LOSS_LIMIT,
             timedelta(hours=_PAUSE_WEEKLY_H)),
            ("daily_loss_limit",   daily_pnl,   C.DAILY_LOSS_LIMIT,
             timedelta(hours=_PAUSE_DAILY_H)),
        ]

        for name, pnl, threshold, pause_dur in checks:
            if pnl >= 0:
                continue   # profit — no concern
            loss_pct = abs(pnl) / ref
            if loss_pct > threshold:
                resume_at = now + pause_dur
                self._paused_until = resume_at
                resume_str = resume_at.strftime("%Y-%m-%dT%H:%MZ")

                logger.warning(
                    "circuit_breaker_triggered  name=%s  loss=%.2f%%  "
                    "threshold=%.0f%%  resume=%s",
                    name, loss_pct * 100, threshold * 100, resume_str,
                )

                await self._fire_slack_breaker(
                    name, loss_pct, bankroll, open_positions, resume_str
                )
                await self._persist_alert(
                    "critical", "circuit_breaker",
                    f"{name}: loss {loss_pct*100:.1f}% > {threshold*100:.0f}%; "
                    f"resume {resume_str}",
                )

                return BreakerResult(
                    triggered=True, trigger_name=name,
                    loss_pct=loss_pct, threshold_pct=threshold,
                    paused_until=resume_at, kill_switch=False,
                )

        # --- All clear -------------------------------------------------
        return BreakerResult(
            triggered=False, trigger_name="",
            loss_pct=0.0, threshold_pct=0.0,
            paused_until=None, kill_switch=False,
        )

    # ------------------------------------------------------------------
    # Slack helpers (fire-and-forget; silenced on failure)
    # ------------------------------------------------------------------

    async def _fire_slack_breaker(
        self,
        name:           str,
        loss_pct:       float,
        bankroll:       float,
        open_positions: int,
        resume_str:     str,
    ) -> None:
        if self._alerter is None:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._alerter.circuit_breaker(
                    trigger=name,
                    loss_pct=loss_pct,
                    bankroll=bankroll,
                    open_positions=open_positions,
                    resume_in=resume_str,
                ),
            )
        except Exception as exc:
            logger.warning("slack_breaker_alert_failed  error=%s", exc)

    async def _fire_slack_kill(
        self,
        bankroll:       float,
        open_positions: int,
    ) -> None:
        if self._alerter is None:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._alerter.kill_switch(
                    bankroll=bankroll,
                    open_positions=open_positions,
                ),
            )
        except Exception as exc:
            logger.warning("slack_kill_alert_failed  error=%s", exc)

    # ------------------------------------------------------------------
    # DB persistence (silenced on failure)
    # ------------------------------------------------------------------

    async def _persist_alert(
        self, level: str, category: str, message: str
    ) -> None:
        try:
            from persistence.database import get_connection
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            async with get_connection() as db:
                await db.execute(
                    "INSERT INTO alerts (level, category, message, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (level, category, message, ts),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("circuit_breaker_persist_failed  error=%s", exc)

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------

    def reset_pause(self) -> None:
        """Clear the pause timer (operator override — use with care)."""
        self._paused_until = None
        logger.info("circuit_breaker_pause_cleared_manually")

    def force_kill(self) -> None:
        """Manually activate the kill switch."""
        self._kill_active  = True
        self._paused_until = datetime(9999, 12, 31, tzinfo=timezone.utc)
        logger.critical("kill_switch_force_activated_by_operator")
