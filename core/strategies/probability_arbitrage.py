"""
Strategy A — Probability Arbitrage for PolyEdge v5.

Thesis
------
Base-rate models + Claude analysis produce a probability estimate for each
market.  When that estimate diverges from the market-implied price by more
than DIVERGENCE_THRESHOLD (10 pp) AND the edge exceeds round-trip fee drag,
the market is mispriced and a mean-reverting position is justified.

Signal generation (read-only)
------------------------------
StrategyA.evaluate() takes a ScannedMarket and a ProbabilityEstimate and
returns a Signal or None.  It never touches the order book or places trades.

Signal fields
-------------
  ticker              str
  direction           "buy_yes" | "buy_no"
  entry_price         float   target limit price (dollars)
  target_price        float   convergence exit price (dollars)
  stop_price          float   hard stop (not used for limit sizing, advisory)
  confidence          float   0.0 – 1.0  (starts at ProbabilityEstimate tier)
  confidence_label    str     "high" | "medium" | "low"
  edge_pp             float   signed divergence in percentage points
  fee_drag_pp         float   round-trip maker fee as percentage points
  net_edge_pp         float   edge_pp - fee_drag_pp (net expected edge)
  model_prob          float   final_prob from ProbabilityEstimate
  market_price        float   current YES ask price
  reasoning           str     one-line explanation
  strategy            str     "probability_arbitrage"
  signal_time         str     UTC ISO-8601 when signal was generated

Entry price
-----------
For buy_yes: entry = market_price (we join the spread passively)
For buy_no:  entry = 1 - market_price  (buy NO at the complement)

Target price (CONVERGENCE_EXIT = 3% threshold from model)
-----------
buy_yes: target = model_prob - CONVERGENCE_EXIT
buy_no:  target = 1 - model_prob + CONVERGENCE_EXIT  (NO price target)

Stop price (advisory — enforced by circuit breakers, not this module)
-----------
Placed at the entry price minus 2× the fee_drag in the adverse direction.
"""

import dataclasses
import logging
from datetime import datetime, timezone

from config import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Signal:
    """A trading signal produced by Strategy A or B.

    This object is read-only — it describes *what* to do, not *how to execute*.
    The engine layer is responsible for sizing, ordering, and risk checks.
    """
    ticker:           str
    direction:        str     # "buy_yes" | "buy_no"
    entry_price:      float   # dollars (0.01–0.99)
    target_price:     float   # dollars — take-profit level
    stop_price:       float   # dollars — advisory stop
    confidence:       float   # 0.0–1.0
    confidence_label: str     # "high" | "medium" | "low"
    edge_pp:          float   # signed divergence (pp)
    fee_drag_pp:      float   # round-trip fee as pp
    net_edge_pp:      float   # edge_pp - fee_drag_pp
    model_prob:       float   # blended model probability
    market_price:     float   # current YES ask (dollars)
    reasoning:        str     # one-line explanation
    strategy:         str     # "probability_arbitrage" | "mean_reversion"
    signal_time:      str     # UTC ISO-8601


# ---------------------------------------------------------------------------
# Confidence tier → float
# ---------------------------------------------------------------------------

_TIER_TO_FLOAT: dict[str, float] = {
    "high":   0.80,
    "medium": 0.55,
    "low":    0.30,
}


def _tier_float(tier: str) -> float:
    return _TIER_TO_FLOAT.get(tier, 0.30)


# ---------------------------------------------------------------------------
# Strategy A
# ---------------------------------------------------------------------------

class StrategyA:
    """Probability Arbitrage signal generator.

    Usage::

        strategy = StrategyA()
        signal = strategy.evaluate(scanned_market, probability_estimate)
        if signal:
            # pass to news_catalyst and orderbook_confirm before sizing
    """

    def evaluate(self, market, estimate) -> Signal | None:
        """Generate a buy_yes / buy_no signal when divergence exceeds gate.

        Args:
            market:   ScannedMarket from data.market_scanner
            estimate: ProbabilityEstimate from analysis.probability_model

        Returns:
            Signal if the gate is passed, None otherwise.
        """
        # Gate 0 — market must have real pricing (5c–95c); 0-priced markets
        # produce spurious 50pp edge because mid_price falls back to 0.50.
        entry_check = market.mid_price if hasattr(market, "market_price") \
            else market.mid_price
        ask = market.yes_ask  # cents; None or 0 means unpriced
        if not ask or ask <= 0 or not (5 <= ask <= 95):
            logger.debug(
                "strategy_a_skip  ticker=%s  reason=unpriced  yes_ask=%s",
                market.ticker, ask,
            )
            return None

        # Gate 1 — probability model must consider action eligible
        if not estimate.action_eligible:
            logger.debug(
                "strategy_a_skip  ticker=%s  reason=not_eligible  div=%.1fpp",
                market.ticker, estimate.divergence_pp,
            )
            return None

        # Gate 2 — net edge must be positive after fee drag
        net_edge = abs(estimate.divergence_pp) - estimate.fee_drag_pp
        if net_edge <= 0:
            logger.debug(
                "strategy_a_skip  ticker=%s  reason=fee_drag  "
                "div=%.1fpp  fee=%.2fpp",
                market.ticker, estimate.divergence_pp, estimate.fee_drag_pp,
            )
            return None

        direction = estimate.action  # "buy_yes" | "buy_no"

        # Entry: passive limit at current market price
        entry_price = market.market_price if hasattr(market, "market_price") \
            else market.mid_price

        # Target: convergence within CONVERGENCE_EXIT of model probability
        if direction == "buy_yes":
            target_price = round(
                estimate.final_prob - C.CONVERGENCE_EXIT, 4
            )
            stop_price = round(
                entry_price - 2 * estimate.fee_drag_pp / 100, 4
            )
        else:  # buy_no
            # NO price is complement of YES; target convergence from below
            target_price = round(
                (1 - estimate.final_prob) + C.CONVERGENCE_EXIT, 4
            )
            stop_price = round(
                (1 - entry_price) - 2 * estimate.fee_drag_pp / 100, 4
            )

        # Clamp prices to valid range
        target_price = max(0.01, min(0.99, target_price))
        stop_price   = max(0.01, min(0.99, stop_price))

        confidence_label = estimate.confidence_tier
        confidence       = _tier_float(confidence_label)

        reasoning = (
            f"Model={estimate.final_prob:.2%} vs market={entry_price:.2%}; "
            f"divergence={estimate.divergence_pp:+.1f}pp  "
            f"net_edge={net_edge:.1f}pp  "
            f"base_rate_n={estimate.n}"
        )

        signal = Signal(
            ticker=market.ticker,
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            confidence=confidence,
            confidence_label=confidence_label,
            edge_pp=estimate.divergence_pp,
            fee_drag_pp=estimate.fee_drag_pp,
            net_edge_pp=round(net_edge, 2),
            model_prob=estimate.final_prob,
            market_price=entry_price,
            reasoning=reasoning,
            strategy="probability_arbitrage",
            signal_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        logger.info(
            "strategy_a_signal  ticker=%s  direction=%s  entry=%.2f  "
            "target=%.2f  edge=%.1fpp  net=%.1fpp  conf=%s",
            signal.ticker, signal.direction, signal.entry_price,
            signal.target_price, signal.edge_pp, signal.net_edge_pp,
            signal.confidence_label,
        )

        return signal
