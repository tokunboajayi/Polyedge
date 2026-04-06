"""
Base-rate probability models per Kalshi market category.

Design
------
Each Kalshi category (economics, politics, regulatory, macro, tech) has a
ProbabilityModel that combines three inputs:

  1. **Historical base rate** — queried from the calibration table (settled
     rows) grouped by category.  Falls back to seeded priors if insufficient
     data (<50 rows).

  2. **Claude adjustment** — the DecisionResult.predicted_probability from
     claude_analyzer.decision_mode(), used directly when confidence is
     "high" or "medium".  Discarded on "low" confidence or API failure.

  3. **Bayesian blend** — final_prob = weight_base * base_rate +
     weight_claude * claude_prob, normalised to [0, 1].  Weights are
     determined by confidence tier and the number of historical precedents:

       high confidence + >=50 n:   35% base / 65% Claude
       medium confidence + >=50 n: 50% base / 50% Claude
       low confidence OR n < 50:   80% base / 20% Claude (very conservative)

Strategy A threshold check
--------------------------
`exceeds_divergence_threshold(final_prob, market_price)` returns True when:
  abs(final_prob - market_price) > C.DIVERGENCE_THRESHOLD / 100
  AND exceeds round-trip maker fee drag at that price (fee-adjusted edge > 0)

This is the main signal gate for Strategy A (probability arbitrage).

Seeded priors
-------------
Hardcoded conservative base-rate priors used when the live DB has < 50 settled
markets for a category.  Derived from general knowledge of Kalshi market
history and kept intentionally conservative (all near 0.50) to avoid
over-trading before sufficient calibration data accumulates.

Public API
----------
  model = ProbabilityModel()
  est   = await model.estimate(category, market_price, claude_result)
  # est.final_prob, est.base_rate, est.n, est.confidence_tier, est.action_eligible
"""

import dataclasses
import logging
from typing import Any

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seeded priors (fallback when DB has < MIN_HISTORICAL_PRECEDENTS settled rows)
# ---------------------------------------------------------------------------

_SEEDED_PRIORS: dict[str, float] = {
    "economics":  0.48,   # Fed/CPI decisions — slightly below 0.50 (hawkish bias)
    "politics":   0.50,   # Binary political outcomes — genuinely 50/50
    "regulatory": 0.45,   # Regulatory actions — slightly below 0.50 (inaction bias)
    "macro":      0.50,   # Macro indicators — neutral prior
    "tech":       0.52,   # Tech metrics — slightly above 0.50 (growth bias)
    "weather":    0.50,   # Weather events — neutral
    "sports":     0.50,   # Sports — neutral
    "culture":    0.50,   # Culture / entertainment — neutral
    "other":      0.50,   # Catch-all
}

_DEFAULT_PRIOR: float = 0.50


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class ProbabilityEstimate:
    """Result of ProbabilityModel.estimate()."""
    category:         str
    base_rate:        float       # raw historical (or seeded) base rate
    n:                int         # number of settled calibration rows for category
    claude_prob:      float | None  # from DecisionResult, or None if unavailable
    confidence_tier:  str         # "high" | "medium" | "low"
    weight_base:      float       # fraction of final_prob from base_rate
    weight_claude:    float       # fraction of final_prob from claude_prob
    final_prob:       float       # blended probability estimate
    market_price:     float       # current market YES price (dollars)
    divergence_pp:    float       # final_prob * 100 - market_price * 100
    fee_drag_pp:      float       # round-trip fee as percentage points
    action_eligible:  bool        # True when divergence > threshold AND > fee drag
    action:           str         # "buy_yes" | "buy_no" | "hold"


# ---------------------------------------------------------------------------
# ProbabilityModel
# ---------------------------------------------------------------------------

class ProbabilityModel:
    """Category-level base-rate model with Bayesian blend and DB lookup.

    Usage::

        model = ProbabilityModel()
        est = await model.estimate(
            category="economics",
            market_price=0.58,
            claude_result=decision_result,   # DecisionResult or None
        )
        if est.action_eligible:
            print(est.action, est.final_prob)
    """

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def estimate(
        self,
        category:     str,
        market_price: float,
        claude_result: Any | None = None,  # DecisionResult | None
    ) -> ProbabilityEstimate:
        """Compute a blended probability estimate for one market.

        Args:
            category:     Normalised Kalshi category string.
            market_price: Current YES ask price in dollars (0.01–0.99).
            claude_result: DecisionResult from ClaudeAnalyzer, or None.

        Returns:
            ProbabilityEstimate with all fields populated.
        """
        category = category.lower().strip()

        # 1 — Historical base rate from DB (with seeded fallback)
        base_rate, n = await self._load_base_rate(category)

        # 2 — Claude probability + tier
        claude_prob, tier = self._parse_claude(claude_result, n)

        # 3 — Blend
        w_base, w_claude = _blend_weights(tier, n)
        if claude_prob is not None:
            final_prob = round(w_base * base_rate + w_claude * claude_prob, 4)
        else:
            final_prob = base_rate

        final_prob = max(0.01, min(0.99, final_prob))

        # 4 — Divergence & fee drag
        divergence_pp = round((final_prob - market_price) * 100, 2)
        fee_drag_pp   = round(
            C.round_trip_fee(market_price, 1, is_maker=True) * 100, 4
        )

        # 5 — Action eligibility
        action_eligible, action = _evaluate_action(
            divergence_pp, fee_drag_pp, final_prob, market_price
        )

        logger.debug(
            "prob_estimate  cat=%s  base=%.3f  n=%d  claude=%s  tier=%s  "
            "final=%.3f  price=%.2f  div=%.1fpp  eligible=%s  action=%s",
            category, base_rate, n,
            f"{claude_prob:.3f}" if claude_prob is not None else "None",
            tier, final_prob, market_price, divergence_pp,
            action_eligible, action,
        )

        return ProbabilityEstimate(
            category=category,
            base_rate=base_rate,
            n=n,
            claude_prob=claude_prob,
            confidence_tier=tier,
            weight_base=w_base,
            weight_claude=w_claude,
            final_prob=final_prob,
            market_price=market_price,
            divergence_pp=divergence_pp,
            fee_drag_pp=fee_drag_pp,
            action_eligible=action_eligible,
            action=action,
        )

    # ------------------------------------------------------------------
    # DB query
    # ------------------------------------------------------------------

    async def _load_base_rate(self, category: str) -> tuple[float, int]:
        """Query calibration table for settled outcomes in this category.

        Returns (base_rate, n).  Falls back to seeded prior if n < MIN_PRECEDENTS
        or if the DB is unavailable.
        """
        try:
            from persistence.database import get_connection
            async with get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN c.actual_outcome = 1 THEN 1 ELSE 0 END)
                    FROM   calibration c
                    JOIN   markets     m ON c.ticker = m.ticker
                    WHERE  m.category        = ?
                    AND    c.actual_outcome  IS NOT NULL
                    """,
                    (category,),
                )
                row = await cursor.fetchone()
        except Exception as exc:
            logger.warning("base_rate_db_error  cat=%s  error=%s", category, exc)
            return _seeded_prior(category), 0

        if row is None or row[0] == 0:
            return _seeded_prior(category), 0

        n, yes_count = int(row[0]), int(row[1] or 0)

        if n < C.MIN_HISTORICAL_PRECEDENTS:
            # Blend seeded prior with observed data proportionally
            seeded = _seeded_prior(category)
            observed = yes_count / n
            blend_weight = n / C.MIN_HISTORICAL_PRECEDENTS
            blended = round(
                (1 - blend_weight) * seeded + blend_weight * observed, 4
            )
            logger.debug(
                "base_rate_sparse  cat=%s  n=%d  seeded=%.3f  observed=%.3f  blended=%.3f",
                category, n, seeded, observed, blended,
            )
            return blended, n

        base_rate = round(yes_count / n, 4)
        return base_rate, n

    # ------------------------------------------------------------------
    # Claude parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_claude(
        result: Any | None,
        n: int,
    ) -> tuple[float | None, str]:
        """Extract (claude_prob, tier) from a DecisionResult.

        Returns (None, "low") if result is unavailable, cached, or low confidence.
        The 'n' argument is checked: if we have insufficient data, we downgrade
        "high" confidence to "medium".
        """
        if result is None:
            return None, "low"

        # DecisionResult duck-type: needs predicted_probability, confidence, cached
        confidence = getattr(result, "confidence", "low")
        cached     = getattr(result, "cached", True)
        prob       = getattr(result, "predicted_probability", None)

        if cached or prob is None or confidence == "low":
            return None, "low"

        # Downgrade if insufficient historical data
        if n < C.MIN_HISTORICAL_PRECEDENTS and confidence == "high":
            confidence = "medium"

        return float(prob), confidence


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _seeded_prior(category: str) -> float:
    return _SEEDED_PRIORS.get(category, _DEFAULT_PRIOR)


def _blend_weights(tier: str, n: int) -> tuple[float, float]:
    """Return (weight_base, weight_claude) for the given confidence tier and n."""
    if tier == "high" and n >= C.MIN_HISTORICAL_PRECEDENTS:
        return 0.35, 0.65
    if tier == "medium" and n >= C.MIN_HISTORICAL_PRECEDENTS:
        return 0.50, 0.50
    # low confidence, no claude, or insufficient data
    return 0.80, 0.20


def _evaluate_action(
    divergence_pp: float,
    fee_drag_pp:   float,
    final_prob:    float,
    market_price:  float,
) -> tuple[bool, str]:
    """Return (eligible, action) based on divergence vs threshold and fee drag.

    Eligibility requires:
      1. |divergence_pp| > DIVERGENCE_THRESHOLD  (default 10 pp)
      2. |divergence_pp| > fee_drag_pp  (fee-adjusted edge must be positive)
    """
    threshold = C.DIVERGENCE_THRESHOLD
    abs_div   = abs(divergence_pp)

    if abs_div <= threshold or abs_div <= fee_drag_pp:
        return False, "hold"

    action = "buy_yes" if final_prob > market_price else "buy_no"
    return True, action


def exceeds_divergence_threshold(
    final_prob:   float,
    market_price: float,
    is_maker:     bool = True,
) -> bool:
    """Standalone helper for Strategy A gate check.

    Returns True when the model-to-market divergence exceeds both
    DIVERGENCE_THRESHOLD and the round-trip maker fee drag.

    Args:
        final_prob:   Blended probability estimate (0.01–0.99).
        market_price: Current YES ask price (0.01–0.99).
        is_maker:     Whether we will use limit orders (default True).
    """
    divergence_pp = abs((final_prob - market_price) * 100)
    fee_drag_pp   = C.round_trip_fee(market_price, 1, is_maker=is_maker) * 100
    return (
        divergence_pp > C.DIVERGENCE_THRESHOLD
        and divergence_pp > fee_drag_pp
    )
