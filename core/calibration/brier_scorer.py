"""
Brier score calculator for PolyEdge v5.

Brier score measures probabilistic forecast accuracy:
  BS = (1/N) * sum((predicted_prob - actual_outcome)^2)

Lower is better.  A random forecaster scores 0.25 on binary events.
Our target: BS < 0.25 (better than random), warning at >= 0.25.

The module reads from the calibration table and computes:
  - Overall Brier score across all settled predictions
  - Per-category breakdown
  - Rolling 30-day score (most recent performance)
  - Calibration curve data (binned by predicted probability)

Schema reference (calibration table)
--------------------------------------
  predicted_prob     REAL   — model probability at time of trade
  actual_outcome     INTEGER — 1 = YES, 0 = NO; NULL until settled
  brier_contribution REAL   — (predicted_prob - actual_outcome)^2

Usage::

    scorer = BrierScorer()
    report = await scorer.compute()
    print(report.overall_brier, report.n_settled)
"""

import dataclasses
import logging
import math
from datetime import datetime, timedelta, timezone

from config import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class CalibrationBin:
    """One bin in the calibration curve."""
    predicted_low:  float   # lower bound of bin (e.g. 0.40)
    predicted_high: float   # upper bound (e.g. 0.50)
    mean_predicted: float   # average predicted prob in this bin
    mean_actual:    float   # fraction of YES outcomes in this bin
    n:              int     # number of predictions in this bin
    overconfident:  bool    # True when mean_predicted > mean_actual + 0.10


@dataclasses.dataclass(slots=True)
class BrierReport:
    """Full Brier score report."""
    overall_brier:      float           # mean Brier score, all time
    rolling_30d_brier:  float | None    # last 30 days; None if < 5 rows
    n_settled:          int             # total settled predictions used
    n_rolling:          int             # predictions in rolling window
    by_category:        dict[str, float]  # category → brier score
    calibration_curve:  list[CalibrationBin]
    better_than_random: bool            # overall_brier < 0.25
    computed_at:        str             # UTC ISO-8601


# ---------------------------------------------------------------------------
# BrierScorer
# ---------------------------------------------------------------------------

class BrierScorer:
    """Reads calibration table and computes Brier scores.

    Usage::

        scorer = BrierScorer()
        report = await scorer.compute()
    """

    _BIN_EDGES = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]

    async def compute(self) -> BrierReport:
        """Query DB and return a full BrierReport."""
        rows = await self._fetch_settled()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if not rows:
            return BrierReport(
                overall_brier=0.0,
                rolling_30d_brier=None,
                n_settled=0,
                n_rolling=0,
                by_category={},
                calibration_curve=[],
                better_than_random=True,   # vacuously true
                computed_at=now_str,
            )

        overall   = _mean_brier(rows)
        by_cat    = _brier_by_category(rows)
        curve     = _calibration_curve(rows, self._BIN_EDGES)
        rolling   = _rolling_brier(rows, days=30)
        n_rolling = _count_rolling(rows, days=30)

        return BrierReport(
            overall_brier=round(overall, 6),
            rolling_30d_brier=round(rolling, 6) if rolling is not None else None,
            n_settled=len(rows),
            n_rolling=n_rolling,
            by_category={k: round(v, 6) for k, v in by_cat.items()},
            calibration_curve=curve,
            better_than_random=overall < C.BRIER_THRESHOLD,
            computed_at=now_str,
        )

    async def compute_for_category(self, category: str) -> float | None:
        """Return Brier score for a single category, or None if < 5 rows."""
        rows = await self._fetch_settled(category=category)
        if len(rows) < 5:
            return None
        return round(_mean_brier(rows), 6)

    # ------------------------------------------------------------------
    # DB fetch
    # ------------------------------------------------------------------

    async def _fetch_settled(
        self, category: str | None = None
    ) -> list[dict]:
        """Return list of dicts with keys: predicted_prob, actual_outcome,
        brier_contribution, settled_at, category."""
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                if category:
                    cursor = await db.execute(
                        """
                        SELECT c.predicted_prob,
                               c.actual_outcome,
                               c.brier_contribution,
                               c.settled_at,
                               m.category
                        FROM   calibration c
                        JOIN   markets     m ON c.ticker = m.ticker
                        WHERE  c.actual_outcome IS NOT NULL
                        AND    m.category = ?
                        ORDER  BY c.settled_at
                        """,
                        (category,),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT c.predicted_prob,
                               c.actual_outcome,
                               c.brier_contribution,
                               c.settled_at,
                               m.category
                        FROM   calibration c
                        JOIN   markets     m ON c.ticker = m.ticker
                        WHERE  c.actual_outcome IS NOT NULL
                        ORDER  BY c.settled_at
                        """
                    )
                raw = await cursor.fetchall()
        except Exception as exc:
            logger.warning("brier_db_fetch_failed  error=%s", exc)
            return []

        return [
            {
                "predicted_prob":    float(r["predicted_prob"]),
                "actual_outcome":    int(r["actual_outcome"]),
                "brier_contribution": float(r["brier_contribution"])
                    if r["brier_contribution"] is not None
                    else (float(r["predicted_prob"]) - int(r["actual_outcome"])) ** 2,
                "settled_at":  r["settled_at"] or "",
                "category":    r["category"]   or "unknown",
            }
            for r in raw
        ]


# ---------------------------------------------------------------------------
# Pure computation helpers
# ---------------------------------------------------------------------------

def brier_score(predicted: float, actual: int) -> float:
    """Compute Brier contribution for one prediction."""
    return (predicted - actual) ** 2


def _mean_brier(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return sum(r["brier_contribution"] for r in rows) / len(rows)


def _brier_by_category(rows: list[dict]) -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for r in rows:
        cat = r["category"]
        groups.setdefault(cat, []).append(r["brier_contribution"])
    return {cat: sum(v) / len(v) for cat, v in groups.items()}


def _rolling_brier(rows: list[dict], days: int = 30) -> float | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    recent = [r for r in rows if r["settled_at"] >= cutoff]
    if len(recent) < 5:
        return None
    return _mean_brier(recent)


def _count_rolling(rows: list[dict], days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return sum(1 for r in rows if r["settled_at"] >= cutoff)


def _calibration_curve(
    rows: list[dict],
    bin_edges: list[float],
) -> list[CalibrationBin]:
    """Bin predictions by predicted_prob and compute mean actual per bin."""
    bins: list[CalibrationBin] = []

    for i in range(len(bin_edges) - 1):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        bucket = [
            r for r in rows
            if lo <= r["predicted_prob"] < hi
        ]
        if not bucket:
            continue
        mean_pred   = sum(r["predicted_prob"] for r in bucket) / len(bucket)
        mean_actual = sum(r["actual_outcome"]  for r in bucket) / len(bucket)
        bins.append(CalibrationBin(
            predicted_low=lo,
            predicted_high=hi,
            mean_predicted=round(mean_pred, 4),
            mean_actual=round(mean_actual, 4),
            n=len(bucket),
            overconfident=mean_pred > mean_actual + 0.10,
        ))

    return bins
