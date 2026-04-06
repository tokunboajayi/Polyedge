"""
Fractional Kelly position sizer for PolyEdge v5.

Kelly criterion
---------------
Full Kelly fraction = (p * b - q) / b
  p = probability of winning (model probability, adjusted for fees)
  q = 1 - p
  b = net odds (payout per $1 risked)

For a binary contract at price P (dollars):
  Win:  profit = (1 - P) per contract dollar risked
  Loss: lose P per contract dollar risked
  b = (1 - P) / P
  Kelly = p - q / b = p - q * P / (1 - P)

Fee adjustment
--------------
Round-trip maker fees reduce the effective win probability:
  fee_drag_per_dollar = round_trip_fee(P, 1, is_maker=True)
  p_adjusted = p - fee_drag_per_dollar / (1 - P)   (fee eats into winnings)
  q_adjusted = 1 - p_adjusted

Phase multipliers (applied to raw Kelly fraction)
--------------------------------------------------
  KELLY_MICRO_LIVE   = 0.15x  — micro-live phase (< 30 resolved trades)
  KELLY_CALIBRATION  = 0.25x  — calibration phase (30–? resolved trades)
  KELLY_FULL         = 0.35x  — fully calibrated

Hard limits (override Kelly if larger)
---------------------------------------
  MAX_POSITION_PCT   = 5% of bankroll per position
  MIN_TRADE_SIZE     = $5 minimum
  MAX_TRADE_SIZE_MICRO_LIVE = $10 cap during micro-live

Sizing output
-------------
PositionSize.num_contracts is the integer contract count.
Contracts are $1 par each; price P means each contract costs P dollars.
  num_contracts = floor(dollar_size / entry_price)

Returns PositionSize(0, ..., eligible=False) when sizing falls below minimum
or Kelly fraction is non-positive after fee adjustment.
"""

import dataclasses
import logging
import math

from config import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class PositionSize:
    """Result of PositionSizer.size()."""
    num_contracts:    int     # 0 when not eligible
    dollar_size:      float   # num_contracts * entry_price
    kelly_fraction:   float   # raw Kelly fraction (before phase multiplier)
    adj_kelly:        float   # after phase multiplier and hard caps
    phase_multiplier: float   # 0.15 / 0.25 / 0.35
    phase:            str     # "micro_live" | "calibration" | "full"
    entry_price:      float   # dollars (0.01–0.99)
    fee_drag:         float   # round-trip fee in dollars per contract
    eligible:         bool    # False → do not trade
    reason:           str     # one-line explanation


# ---------------------------------------------------------------------------
# PositionSizer
# ---------------------------------------------------------------------------

class PositionSizer:
    """Fractional Kelly sizer with fee adjustment and phase-based multipliers.

    Usage::

        sizer = PositionSizer()
        size  = sizer.size(
            model_prob=0.72,
            entry_price=0.60,
            bankroll=487.50,
            resolved_trade_count=35,   # drives phase selection
        )
        if size.eligible:
            num_contracts = size.num_contracts
    """

    def size(
        self,
        model_prob:           float,
        entry_price:          float,
        bankroll:             float,
        resolved_trade_count: int,
        is_maker:             bool = True,
    ) -> PositionSize:
        """Compute contract count using fractional Kelly + fee adjustment.

        Args:
            model_prob:           Blended model probability (0.01–0.99).
            entry_price:          Limit price we intend to pay (dollars).
            bankroll:             Current available bankroll (dollars).
            resolved_trade_count: Total settled trades so far (drives phase).
            is_maker:             True for limit orders (default).

        Returns:
            PositionSize — check .eligible before trading.
        """
        phase, multiplier = _select_phase(resolved_trade_count)

        # --- Fee drag -------------------------------------------------
        fee_drag = C.round_trip_fee(entry_price, 1, is_maker=is_maker)

        # --- Kelly probability after fees -----------------------------
        # Win profit per contract = (1 - entry_price) - fee_drag
        # Loss per contract       = entry_price + fee_drag  (approximate)
        win_profit  = (1.0 - entry_price) - fee_drag
        loss_amount = entry_price + fee_drag

        if win_profit <= 0:
            return _ineligible(
                entry_price, fee_drag, phase, multiplier,
                f"win_profit_negative={win_profit:.4f}",
            )

        # Odds b = win_profit / loss_amount
        b = win_profit / loss_amount
        q = 1.0 - model_prob

        kelly_raw = (model_prob * b - q) / b   # full Kelly fraction

        if kelly_raw <= 0:
            return _ineligible(
                entry_price, fee_drag, phase, multiplier,
                f"negative_kelly={kelly_raw:.4f}",
            )

        # --- Apply phase multiplier ----------------------------------
        kelly_adj = kelly_raw * multiplier

        # --- Hard caps -----------------------------------------------
        # Cap at MAX_POSITION_PCT of bankroll
        max_dollar = bankroll * C.MAX_POSITION_PCT
        # Micro-live: additional hard cap
        if phase == "micro_live":
            max_dollar = min(max_dollar, C.MAX_TRADE_SIZE_MICRO_LIVE)

        dollar_size = min(kelly_adj * bankroll, max_dollar)

        if dollar_size < C.MIN_TRADE_SIZE:
            return _ineligible(
                entry_price, fee_drag, phase, multiplier,
                f"dollar_size=${dollar_size:.2f}<min=${C.MIN_TRADE_SIZE}",
            )

        # --- Convert to contracts ------------------------------------
        num_contracts = math.floor(dollar_size / entry_price)

        if num_contracts < 1:
            return _ineligible(
                entry_price, fee_drag, phase, multiplier,
                "num_contracts=0",
            )

        actual_dollar = num_contracts * entry_price

        logger.info(
            "position_size  phase=%s  model_prob=%.3f  price=%.2f  "
            "kelly=%.4f  adj=%.4f  contracts=%d  dollars=$%.2f  bankroll=$%.2f",
            phase, model_prob, entry_price,
            kelly_raw, kelly_adj, num_contracts, actual_dollar, bankroll,
        )

        return PositionSize(
            num_contracts=num_contracts,
            dollar_size=round(actual_dollar, 2),
            kelly_fraction=round(kelly_raw, 6),
            adj_kelly=round(kelly_adj, 6),
            phase_multiplier=multiplier,
            phase=phase,
            entry_price=entry_price,
            fee_drag=round(fee_drag, 6),
            eligible=True,
            reason=f"kelly={kelly_raw:.4f} x {multiplier} => {kelly_adj:.4f}",
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _select_phase(resolved_count: int) -> tuple[str, float]:
    """Return (phase_name, kelly_multiplier) based on resolved trade count."""
    if resolved_count < 30:
        return "micro_live", C.KELLY_MICRO_LIVE
    if resolved_count < 60:
        return "calibration", C.KELLY_CALIBRATION
    return "full", C.KELLY_FULL


def _ineligible(
    entry_price:  float,
    fee_drag:     float,
    phase:        str,
    multiplier:   float,
    reason:       str,
) -> PositionSize:
    logger.debug("position_ineligible  reason=%s", reason)
    return PositionSize(
        num_contracts=0,
        dollar_size=0.0,
        kelly_fraction=0.0,
        adj_kelly=0.0,
        phase_multiplier=multiplier,
        phase=phase,
        entry_price=entry_price,
        fee_drag=fee_drag,
        eligible=False,
        reason=reason,
    )


def kelly_fraction(
    model_prob:  float,
    entry_price: float,
    is_maker:    bool = True,
) -> float:
    """Standalone Kelly fraction (0.0 when non-positive).

    Useful for quick eligibility checks without full sizing context.
    """
    fee_drag   = C.round_trip_fee(entry_price, 1, is_maker=is_maker)
    win_profit = (1.0 - entry_price) - fee_drag
    if win_profit <= 0:
        return 0.0
    loss_amount = entry_price + fee_drag
    b = win_profit / loss_amount
    q = 1.0 - model_prob
    raw = (model_prob * b - q) / b
    return max(0.0, raw)
