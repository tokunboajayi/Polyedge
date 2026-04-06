"""
Orderbook Confirmation Signal for PolyEdge v5.

Role
----
A final pre-trade gate that inspects the live orderbook snapshot for a market
and returns a confirm/reject boolean with a detailed reason.  If confirmed,
it also provides a refined entry price and estimated slippage.

Three checks (all must pass to confirm)
----------------------------------------
1. **Depth check** — the top-N levels of the relevant side must have
   sufficient combined size to absorb the intended order without excessive
   impact.  Threshold: depth_dollars >= MIN_DEPTH_DOLLARS (default $200).

2. **Spoofing detection** — reject if a single level accounts for >= 70% of
   total visible depth on one side while the spread is at its widest.  Such
   concentration suggests a spoof order that will be pulled before fill.

3. **Slippage estimate** — simulate walking the book for the intended
   contract count and check that estimated slippage <= MAX_EXIT_SLIPPAGE (3%).

Input: OrderbookSnapshot
------------------------
The Kalshi WebSocket delivers orderbook data as:
  {
    "yes": [[price_cents, size], ...],   # sorted best-first (desc for bids)
    "no":  [[price_cents, size], ...]    # sorted best-first
  }
This module expects the same structure (as produced by kalshi_websocket.py).

Output: ConfirmResult
---------------------
  confirmed       bool
  reason          str    one-line explanation
  entry_price     float  refined entry in dollars (best ask for direction)
  est_slippage    float  0.0–1.0 fraction of order cost
  depth_dollars   float  total visible depth on the relevant side
  spoof_flag      bool   True if spoofing pattern detected
"""

import dataclasses
import logging

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_DEPTH_DOLLARS:   float = 200.0   # minimum liquidity depth to confirm
SPOOF_CONCENTRATION: float = 0.70    # single-level share that triggers flag
TOP_N_LEVELS:        int   = 5       # levels to inspect for depth/slippage


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class ConfirmResult:
    """Output of OrderbookConfirm.check()."""
    confirmed:     bool
    reason:        str
    entry_price:   float   # dollars — best available price for the direction
    est_slippage:  float   # fraction (0.0–1.0) of order cost
    depth_dollars: float   # total visible dollar depth on relevant side
    spoof_flag:    bool    # True if concentration > SPOOF_CONCENTRATION


# ---------------------------------------------------------------------------
# OrderbookConfirm
# ---------------------------------------------------------------------------

class OrderbookConfirm:
    """Validates an orderbook before executing a signal.

    Usage::

        confirm = OrderbookConfirm()
        result  = confirm.check(
            direction="buy_yes",
            orderbook={"yes": [[62, 300], [61, 500]], "no": [[38, 200]]},
            num_contracts=10,
        )
        if result.confirmed:
            entry = result.entry_price
    """

    def check(
        self,
        direction:     str,             # "buy_yes" | "buy_no"
        orderbook:     dict,            # {"yes": [[cents, size], ...], "no": [...]}
        num_contracts: int = 5,
        max_slippage:  float | None = None,
    ) -> ConfirmResult:
        """Run all three orderbook checks.

        Args:
            direction:     Trade direction from the signal.
            orderbook:     Raw orderbook dict from WebSocket/REST.
            num_contracts: Intended order size in contracts.
            max_slippage:  Override for slippage threshold (default from C).

        Returns:
            ConfirmResult — confirmed=True only if all checks pass.
        """
        if max_slippage is None:
            max_slippage = C.MAX_EXIT_SLIPPAGE

        # --- Select the relevant side -----------------------------------
        # buy_yes → we need ask liquidity on the YES side
        # buy_no  → we need ask liquidity on the NO side
        side_key = "yes" if direction == "buy_yes" else "no"
        levels   = list(orderbook.get(side_key, []))

        if not levels:
            return ConfirmResult(
                confirmed=False,
                reason=f"empty_{side_key}_orderbook",
                entry_price=0.0,
                est_slippage=1.0,
                depth_dollars=0.0,
                spoof_flag=False,
            )

        # Normalise: levels may be [cents, size] or [price_float, size]
        levels = _normalise_levels(levels)

        # Best ask = lowest ask price (first level if sorted ascending)
        # Kalshi WebSocket sends asks sorted ascending (best first for asks)
        # We assume the caller passes the ask side sorted best-first (ascending).
        best_ask_dollars = levels[0][0]  # already normalised to dollars

        # --- Check 1: Depth --------------------------------------------
        depth_dollars, spoof_flag = _check_depth(levels, best_ask_dollars)

        if depth_dollars < MIN_DEPTH_DOLLARS:
            logger.debug(
                "ob_confirm_reject  direction=%s  reason=insufficient_depth  "
                "depth=$%.2f  threshold=$%.2f",
                direction, depth_dollars, MIN_DEPTH_DOLLARS,
            )
            return ConfirmResult(
                confirmed=False,
                reason=f"insufficient_depth=${depth_dollars:.0f}<${MIN_DEPTH_DOLLARS:.0f}",
                entry_price=best_ask_dollars,
                est_slippage=1.0,
                depth_dollars=depth_dollars,
                spoof_flag=spoof_flag,
            )

        # --- Check 2: Spoofing -----------------------------------------
        if spoof_flag:
            logger.info(
                "ob_confirm_reject  direction=%s  reason=spoof_detected  "
                "concentration>=%.0f%%",
                direction, SPOOF_CONCENTRATION * 100,
            )
            return ConfirmResult(
                confirmed=False,
                reason=f"spoof_detected_concentration>={SPOOF_CONCENTRATION:.0%}",
                entry_price=best_ask_dollars,
                est_slippage=1.0,
                depth_dollars=depth_dollars,
                spoof_flag=True,
            )

        # --- Check 3: Slippage estimate --------------------------------
        est_slippage = _estimate_slippage(levels, num_contracts, best_ask_dollars)

        if est_slippage > max_slippage:
            logger.debug(
                "ob_confirm_reject  direction=%s  reason=high_slippage  "
                "slippage=%.2f%%  max=%.2f%%",
                direction, est_slippage * 100, max_slippage * 100,
            )
            return ConfirmResult(
                confirmed=False,
                reason=(
                    f"slippage={est_slippage:.2%}>{max_slippage:.2%}"
                ),
                entry_price=best_ask_dollars,
                est_slippage=est_slippage,
                depth_dollars=depth_dollars,
                spoof_flag=False,
            )

        # --- All checks passed -----------------------------------------
        logger.info(
            "ob_confirm_accept  direction=%s  entry=%.3f  slippage=%.2f%%  "
            "depth=$%.0f",
            direction, best_ask_dollars, est_slippage * 100, depth_dollars,
        )
        return ConfirmResult(
            confirmed=True,
            reason="all_checks_passed",
            entry_price=best_ask_dollars,
            est_slippage=est_slippage,
            depth_dollars=depth_dollars,
            spoof_flag=False,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _normalise_levels(levels: list) -> list[tuple[float, int]]:
    """Convert [[price, size], ...] to [(price_dollars, size), ...].

    Handles both integer cents (>1) and float dollars (0.01–0.99).
    Sorts ascending by price (best ask first).
    """
    normalised = []
    for level in levels[:TOP_N_LEVELS * 2]:  # look at extra levels for spoof check
        if not level or len(level) < 2:
            continue
        price_raw = level[0]
        size      = int(level[1])
        if size <= 0:
            continue
        # Detect unit: if >1 assume cents, else assume dollars
        price_dollars = price_raw / 100.0 if price_raw > 1 else float(price_raw)
        normalised.append((round(price_dollars, 4), size))

    # Sort ascending by price
    normalised.sort(key=lambda x: x[0])
    return normalised


def _check_depth(
    levels: list[tuple[float, int]],
    best_ask: float,
) -> tuple[float, bool]:
    """Compute total dollar depth and detect spoofing.

    Returns (depth_dollars, spoof_flag).
    depth_dollars = sum of (price * size) for top TOP_N_LEVELS levels.
    spoof_flag = True if one level holds >= SPOOF_CONCENTRATION of total depth.
    """
    top = levels[:TOP_N_LEVELS]
    level_values = [price * size for price, size in top]
    total_depth  = sum(level_values)

    if total_depth == 0:
        return 0.0, False

    max_level = max(level_values)
    spoof_flag = (max_level / total_depth) >= SPOOF_CONCENTRATION

    return round(total_depth, 2), spoof_flag


def _estimate_slippage(
    levels:        list[tuple[float, int]],
    num_contracts: int,
    best_ask:      float,
) -> float:
    """Simulate walking the orderbook for num_contracts.

    Returns estimated slippage as a fraction of the best-ask cost.
    Uses volume-weighted average fill price vs best ask.

    If there is not enough book depth for num_contracts, returns 1.0 (worst case).
    """
    if best_ask <= 0 or num_contracts <= 0:
        return 0.0

    remaining  = num_contracts
    total_cost = 0.0

    for price, size in levels:
        if remaining <= 0:
            break
        fill = min(remaining, size)
        total_cost += fill * price
        remaining  -= fill

    if remaining > 0:
        # Could not fill full order — slippage is worst-case
        return 1.0

    avg_fill = total_cost / num_contracts
    slippage = abs(avg_fill - best_ask) / best_ask if best_ask > 0 else 0.0
    return round(slippage, 6)
