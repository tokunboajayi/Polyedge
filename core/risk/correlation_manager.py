"""
Correlation manager for PolyEdge v5.

Tracks open positions by category and direction and blocks new entries when
any of the three concentration limits would be exceeded.

Limits (from CLAUDE.md / constants.py)
---------------------------------------
  MAX_OPEN_POSITIONS      = 5      — total open positions
  MAX_BANKROLL_IN_POSITIONS = 50%  — total dollar exposure / bankroll
  MAX_CORRELATED_EXPOSURE = 30%    — exposure in same category OR direction

A "correlated group" is any (category, direction) pair.  If adding a new
position would push a single category-direction bucket above 30% of bankroll,
the position is blocked.

Position registry
-----------------
The manager holds an in-memory dict of open positions keyed by ticker.
The caller is responsible for calling:
  add_position()    — on trade entry
  remove_position() — on trade exit / settlement

check() returns a CorrelationResult with allowed=True/False and reason.

Usage::

    cm = CorrelationManager()
    cm.add_position(ticker="FED-RATE-MAY", category="economics",
                    direction="buy_yes", dollar_size=25.00)
    result = cm.check(category="economics", direction="buy_yes",
                      dollar_size=20.00, bankroll=480.00)
    if result.allowed:
        cm.add_position(...)
"""

import dataclasses
import logging

from config import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal position record
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class _Position:
    ticker:      str
    category:    str
    direction:   str    # "buy_yes" | "buy_no"
    dollar_size: float


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class CorrelationResult:
    """Result of CorrelationManager.check()."""
    allowed:             bool
    reason:              str
    open_count:          int
    total_exposure:      float   # dollars currently deployed
    exposure_pct:        float   # total_exposure / bankroll
    correlated_exposure: float   # dollars in same category+direction bucket
    correlated_pct:      float   # correlated_exposure / bankroll


# ---------------------------------------------------------------------------
# CorrelationManager
# ---------------------------------------------------------------------------

class CorrelationManager:
    """In-memory registry of open positions with concentration checks.

    Not thread-safe: designed to be used from a single async task (the engine
    loop).  Wrap in asyncio.Lock if concurrent access is needed.
    """

    def __init__(self) -> None:
        self._positions: dict[str, _Position] = {}   # ticker → Position

    # ------------------------------------------------------------------
    # Registry mutations
    # ------------------------------------------------------------------

    def add_position(
        self,
        ticker:      str,
        category:    str,
        direction:   str,
        dollar_size: float,
    ) -> None:
        """Register a new open position."""
        if ticker in self._positions:
            logger.warning(
                "correlation_add_duplicate  ticker=%s  updating_size", ticker
            )
        self._positions[ticker] = _Position(
            ticker=ticker,
            category=category.lower().strip(),
            direction=direction,
            dollar_size=dollar_size,
        )
        logger.debug(
            "position_added  ticker=%s  cat=%s  dir=%s  size=$%.2f  "
            "total_open=%d",
            ticker, category, direction, dollar_size, len(self._positions),
        )

    def remove_position(self, ticker: str) -> bool:
        """Remove a position from the registry.  Returns True if it existed."""
        if ticker in self._positions:
            del self._positions[ticker]
            logger.debug(
                "position_removed  ticker=%s  remaining=%d",
                ticker, len(self._positions),
            )
            return True
        logger.debug("position_remove_miss  ticker=%s", ticker)
        return False

    def update_size(self, ticker: str, new_dollar_size: float) -> None:
        """Update the dollar size of an existing position (partial fill, etc.)."""
        if ticker in self._positions:
            self._positions[ticker] = dataclasses.replace(
                self._positions[ticker], dollar_size=new_dollar_size
            )

    # ------------------------------------------------------------------
    # Check — would adding this position violate any limit?
    # ------------------------------------------------------------------

    def check(
        self,
        category:    str,
        direction:   str,
        dollar_size: float,
        bankroll:    float,
    ) -> CorrelationResult:
        """Check whether a proposed new position is within all limits.

        Args:
            category:    Canonical category of the proposed trade.
            direction:   "buy_yes" | "buy_no"
            dollar_size: Proposed position size in dollars.
            bankroll:    Current available bankroll.

        Returns:
            CorrelationResult with allowed=True only if all limits pass.
        """
        category = category.lower().strip()
        ref = max(1.0, bankroll)

        open_count = len(self._positions)
        total_exp  = sum(p.dollar_size for p in self._positions.values())

        # Correlated exposure: same (category, direction) bucket
        bucket_exp = sum(
            p.dollar_size
            for p in self._positions.values()
            if p.category == category and p.direction == direction
        )

        # Projected values after adding the new position
        new_total   = total_exp  + dollar_size
        new_bucket  = bucket_exp + dollar_size
        new_count   = open_count + 1

        total_pct    = new_total  / ref
        bucket_pct   = new_bucket / ref

        # --- Gate 1: max open positions --------------------------------
        if new_count > C.MAX_OPEN_POSITIONS:
            return self._result(
                False,
                f"max_open_positions: {new_count}>{C.MAX_OPEN_POSITIONS}",
                open_count, total_exp, total_exp / ref, bucket_exp, bucket_exp / ref,
            )

        # --- Gate 2: total bankroll exposure ---------------------------
        if total_pct > C.MAX_BANKROLL_IN_POSITIONS:
            return self._result(
                False,
                f"total_exposure: {total_pct:.1%}>{C.MAX_BANKROLL_IN_POSITIONS:.0%}",
                open_count, total_exp, total_exp / ref, bucket_exp, bucket_exp / ref,
            )

        # --- Gate 3: correlated (category+direction) exposure ----------
        if bucket_pct > C.MAX_CORRELATED_EXPOSURE:
            return self._result(
                False,
                f"correlated_exposure {category}/{direction}: "
                f"{bucket_pct:.1%}>{C.MAX_CORRELATED_EXPOSURE:.0%}",
                open_count, total_exp, total_exp / ref, bucket_exp, bucket_exp / ref,
            )

        logger.debug(
            "correlation_check_pass  cat=%s  dir=%s  size=$%.2f  "
            "total=%.1f%%  bucket=%.1f%%  open=%d",
            category, direction, dollar_size,
            total_pct * 100, bucket_pct * 100, new_count,
        )

        return self._result(
            True, "all_limits_ok",
            open_count, total_exp, total_exp / ref, bucket_exp, bucket_exp / ref,
        )

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def open_count(self) -> int:
        return len(self._positions)

    def total_exposure(self) -> float:
        return sum(p.dollar_size for p in self._positions.values())

    def exposure_by_category(self) -> dict[str, float]:
        """Return {category: total_dollars} for all open positions."""
        result: dict[str, float] = {}
        for p in self._positions.values():
            result[p.category] = result.get(p.category, 0.0) + p.dollar_size
        return result

    def exposure_by_bucket(self) -> dict[tuple[str, str], float]:
        """Return {(category, direction): total_dollars} for all buckets."""
        result: dict[tuple[str, str], float] = {}
        for p in self._positions.values():
            key = (p.category, p.direction)
            result[key] = result.get(key, 0.0) + p.dollar_size
        return result

    def tickers(self) -> list[str]:
        return list(self._positions.keys())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        allowed:      bool,
        reason:       str,
        open_count:   int,
        total_exp:    float,
        exp_pct:      float,
        corr_exp:     float,
        corr_pct:     float,
    ) -> CorrelationResult:
        if not allowed:
            logger.debug("correlation_blocked  reason=%s", reason)
        return CorrelationResult(
            allowed=allowed,
            reason=reason,
            open_count=open_count,
            total_exposure=round(total_exp, 2),
            exposure_pct=round(exp_pct, 4),
            correlated_exposure=round(corr_exp, 2),
            correlated_pct=round(corr_pct, 4),
        )
