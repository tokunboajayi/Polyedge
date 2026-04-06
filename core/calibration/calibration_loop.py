"""
Calibration loop for PolyEdge v5.

Purpose
-------
After every trade settles, the calibration loop:
  1. Writes the actual outcome + brier_contribution to the calibration table.
  2. Counts total settled predictions.
  3. Once >= 30 are settled, computes a calibration correction factor:
       factor = mean(actual_outcome) / mean(predicted_prob)
     Interpretation: if factor < 1.0 we are systematically over-predicting;
     if > 1.0 we are under-predicting.
  4. Checks factor against the CALIBRATION_FACTOR_RANGE (0.7, 1.3):
     - Inside range → log info.
     - Outside range → fire Slack warning + persist alert.

Correction factor usage
-----------------------
The factor is NOT applied automatically — it is surfaced to the operator and
to the probability model for optional adjustment.  The engine calls
get_correction_factor() to read the latest factor before sizing a trade.

CalibrationState (in-memory singleton)
---------------------------------------
Tracks:
  resolved_count   int     — total settled predictions seen this session
  correction_factor float  — most recently computed factor
  last_computed_at  str    — UTC ISO-8601 when factor was last updated

Usage::

    loop = CalibrationLoop()
    await loop.record_outcome(trade_id=42, ticker="FED-RATE-MAY",
                               predicted_prob=0.72, actual_outcome=1)
    state = loop.get_state()
    factor = loop.get_correction_factor()
"""

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any

from config import constants as C
from core.calibration.brier_scorer import BrierScorer, brier_score

logger = logging.getLogger(__name__)

# Minimum settled trades before we trust the correction factor
_MIN_TRADES_FOR_FACTOR: int = 30


# ---------------------------------------------------------------------------
# CalibrationState
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CalibrationState:
    """Current calibration status."""
    resolved_count:    int
    correction_factor: float   # 1.0 = well-calibrated
    last_computed_at:  str     # UTC ISO-8601; "" if not yet computed
    within_range:      bool    # True when factor in CALIBRATION_FACTOR_RANGE
    brier_score:       float   # most recent overall Brier score
    phase:             str     # "warming_up" | "calibrating" | "calibrated"


# ---------------------------------------------------------------------------
# CalibrationLoop
# ---------------------------------------------------------------------------

class CalibrationLoop:
    """Records trade outcomes and maintains a correction factor.

    Usage::

        loop = CalibrationLoop()
        await loop.record_outcome(
            trade_id=1, ticker="FED-RATE-MAY",
            predicted_prob=0.72, actual_outcome=1,
            settled_at="2026-04-10T14:00:00Z",
        )
        factor = loop.get_correction_factor()
    """

    def __init__(self, slack_alerter: Any | None = None) -> None:
        self._alerter  = slack_alerter
        self._scorer   = BrierScorer()
        self._state    = CalibrationState(
            resolved_count=0,
            correction_factor=1.0,
            last_computed_at="",
            within_range=True,
            brier_score=0.0,
            phase="warming_up",
        )
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    async def record_outcome(
        self,
        trade_id:      int,
        ticker:        str,
        predicted_prob: float,
        actual_outcome: int,    # 1 = YES resolved, 0 = NO resolved
        settled_at:    str | None = None,
    ) -> CalibrationState:
        """Persist outcome, recompute factor, and fire alerts if needed.

        Args:
            trade_id:       DB row id of the trade.
            ticker:         Kalshi market ticker.
            predicted_prob: Model probability at time of trade (0.01–0.99).
            actual_outcome: 1 if market resolved YES, 0 if NO.
            settled_at:     UTC ISO-8601 settlement timestamp; defaults to now.

        Returns:
            Updated CalibrationState.
        """
        async with self._lock:
            return await self._process(
                trade_id, ticker, predicted_prob,
                actual_outcome, settled_at,
            )

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_correction_factor(self) -> float:
        """Return the latest correction factor (1.0 when not yet computed)."""
        return self._state.correction_factor

    def get_state(self) -> CalibrationState:
        return dataclasses.replace(self._state)

    def resolved_count(self) -> int:
        return self._state.resolved_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _process(
        self,
        trade_id:      int,
        ticker:        str,
        predicted_prob: float,
        actual_outcome: int,
        settled_at:    str | None,
    ) -> CalibrationState:

        now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        settled_ts = settled_at or now_str
        bc         = round(brier_score(predicted_prob, actual_outcome), 8)

        # 1 — Persist outcome to calibration table
        await self._persist_outcome(
            trade_id, ticker, predicted_prob,
            actual_outcome, bc, settled_ts, now_str,
        )

        # 2 — Update in-memory resolved count
        self._state.resolved_count += 1
        count = self._state.resolved_count

        # 3 — Recompute correction factor once we have enough data
        if count >= _MIN_TRADES_FOR_FACTOR:
            await self._recompute_factor(now_str)
        else:
            logger.info(
                "calibration_warming_up  settled=%d  need=%d",
                count, _MIN_TRADES_FOR_FACTOR,
            )

        return self.get_state()

    async def _recompute_factor(self, now_str: str) -> None:
        """Fetch all settled rows, compute factor, update state, alert if needed."""
        rows = await self._scorer._fetch_settled()

        if not rows:
            return

        mean_pred   = sum(r["predicted_prob"]  for r in rows) / len(rows)
        mean_actual = sum(r["actual_outcome"]   for r in rows) / len(rows)

        if mean_pred <= 0:
            factor = 1.0
        else:
            factor = round(mean_actual / mean_pred, 4)

        brier = round(sum(r["brier_contribution"] for r in rows) / len(rows), 6)

        lo, hi      = C.CALIBRATION_FACTOR_RANGE
        in_range    = lo <= factor <= hi
        phase       = _phase(len(rows))

        old_factor  = self._state.correction_factor
        drift       = round(abs(factor - old_factor), 4)

        self._state = CalibrationState(
            resolved_count=len(rows),
            correction_factor=factor,
            last_computed_at=now_str,
            within_range=in_range,
            brier_score=brier,
            phase=phase,
        )

        logger.info(
            "calibration_updated  n=%d  factor=%.4f  brier=%.4f  "
            "in_range=%s  drift=%.4f  phase=%s",
            len(rows), factor, brier, in_range, drift, phase,
        )

        # 4 — Alert if factor drifted outside range
        if not in_range:
            lo_pct  = f"{lo:.0%}"
            hi_pct  = f"{hi:.0%}"
            message = (
                f"Calibration factor {factor:.3f} outside range "
                f"[{lo_pct}, {hi_pct}]. n={len(rows)} brier={brier:.4f}"
            )
            await self._fire_alert(factor, len(rows), brier, now_str)
            await self._persist_system_alert(
                "warning", "calibration", message
            )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _persist_outcome(
        self,
        trade_id:      int,
        ticker:        str,
        predicted_prob: float,
        actual_outcome: int,
        brier_contrib: float,
        settled_at:    str,
        now_str:       str,
    ) -> None:
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                # Upsert: if a row exists for this trade_id update it,
                # otherwise insert.
                existing = await (await db.execute(
                    "SELECT id FROM calibration WHERE trade_id = ?", (trade_id,)
                )).fetchone()

                if existing:
                    await db.execute(
                        """UPDATE calibration
                           SET actual_outcome     = ?,
                               brier_contribution = ?,
                               settled_at         = ?
                           WHERE trade_id = ?""",
                        (actual_outcome, brier_contrib, settled_at, trade_id),
                    )
                else:
                    await db.execute(
                        """INSERT INTO calibration
                           (trade_id, ticker, predicted_prob, actual_outcome,
                            brier_contribution, recorded_at, settled_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (trade_id, ticker, predicted_prob, actual_outcome,
                         brier_contrib, now_str, settled_at),
                    )
                await db.commit()
        except Exception as exc:
            logger.warning("calibration_persist_failed  error=%s", exc)

    async def _persist_system_alert(
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
            logger.warning("calibration_alert_persist_failed  error=%s", exc)

    # ------------------------------------------------------------------
    # Slack
    # ------------------------------------------------------------------

    async def _fire_alert(
        self,
        factor:  float,
        n:       int,
        brier:   float,
        now_str: str,
    ) -> None:
        if self._alerter is None:
            return
        lo, hi = C.CALIBRATION_FACTOR_RANGE
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._alerter.edge_metric_warning(
                    metric="calibration_factor",
                    value=factor,
                    threshold=f"[{lo:.1f}, {hi:.1f}]",
                    window_days=0,
                    bankroll=0.0,
                    open_positions=0,
                ),
            )
        except Exception as exc:
            logger.warning("calibration_slack_alert_failed  error=%s", exc)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _phase(n: int) -> str:
    if n < _MIN_TRADES_FOR_FACTOR:
        return "warming_up"
    if n < 60:
        return "calibrating"
    return "calibrated"
