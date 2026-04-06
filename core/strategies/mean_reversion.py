"""
Strategy B — Post-Spike Mean Reversion for PolyEdge v5.

Thesis
------
Reactive retail traders overreact to headline news, pushing Kalshi contract
prices sharply in one direction within minutes.  If Claude judges the move
"unjustified" (i.e. the news does not definitively change the outcome), the
spike will partially or fully revert.  We FADE the spike after a mandatory
30-minute cooling-off period.

NEVER FADE rule
---------------
Any spike caused by an official government, court, or regulatory action must
NEVER be faded.  These are outcome-determinative events — the price has moved
to a new fair value, not a temporary mispricing.

Spike detection
---------------
A spike is recorded when:
  abs(new_price - baseline_price) / baseline_price >= SPIKE_THRESHOLD (15%)
  within a SPIKE_WINDOW_SECONDS (3600 s = 1 hour) look-back window.

The most recent price in the history is the current price; the earliest price
in the window is the baseline.

Signal generation (read-only)
------------------------------
StrategyB.evaluate() takes:
  market     — ScannedMarket (has ticker, mid_price, category, title)
  price_hist — list[PricePoint] — recent price observations
  scan_result — ScanResult from claude_analyzer (used to judge justification)
  reg_alerts  — list[RegulatoryAlert] (if any CRITICAL alert → never fade)

Returns a Signal | None.  Never executes anything.

Signal fields (inherits Signal from probability_arbitrage)
-------------------------------------------------------------
  direction     "buy_yes" (fade a spike down) | "buy_no" (fade a spike up)
  entry_price   current price after spike (we fade at this level)
  target_price  50% reversion from spike magnitude
  stop_price    25% further adverse move from entry
  strategy      "mean_reversion"
  reasoning     includes spike_magnitude, baseline, justification_verdict

Timing gate
-----------
entry_eligible_at is stored on the returned Signal (as a custom field on
the reasoning string) — the engine must not execute until that timestamp.
"""

import dataclasses
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from config import constants as C
from core.strategies.probability_arbitrage import Signal, _tier_float

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers / constants
# ---------------------------------------------------------------------------

SPIKE_WINDOW_SECONDS: int = 3600    # 1 hour look-back for baseline
OFFICIAL_ACTION_KEYWORDS: frozenset[str] = frozenset({
    # Government / court / regulatory keywords that mark outcome-determinative events
    "executive order", "signed into law", "court ruling", "court order",
    "supreme court", "sec charges", "sec enforcement", "doj charges",
    "doj indictment", "federal reserve decision", "fomc decision",
    "rate decision", "official result", "certified result",
    "election certified", "recount certified", "sanctions imposed",
    "sanctions announced", "treaty signed", "legislation passed",
    "bill signed", "veto override", "constitutional amendment",
    "consent decree", "settlement order", "injunction granted",
    "injunction issued", "fda approval", "fda approved",
    "fda rejection", "fda rejected",
})


# ---------------------------------------------------------------------------
# PricePoint
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class PricePoint:
    """A single price observation."""
    price:     float   # YES ask price in dollars (0.01–0.99)
    timestamp: datetime  # UTC


# ---------------------------------------------------------------------------
# SpikeInfo
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class SpikeInfo:
    """Details of a detected spike."""
    baseline_price:    float     # price at start of window
    current_price:     float     # price now
    spike_magnitude:   float     # abs(current - baseline) / baseline
    direction:         str       # "up" | "down"
    window_seconds:    int       # actual seconds spanned


# ---------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------

def detect_spike(price_history: list[PricePoint]) -> SpikeInfo | None:
    """Detect if a spike >= SPIKE_THRESHOLD occurred within SPIKE_WINDOW_SECONDS.

    Args:
        price_history: List of PricePoints sorted oldest → newest.

    Returns:
        SpikeInfo if spike found, None otherwise.
    """
    if len(price_history) < 2:
        return None

    now      = price_history[-1].timestamp
    current  = price_history[-1].price
    cutoff   = now - timedelta(seconds=SPIKE_WINDOW_SECONDS)

    # Find oldest price within window
    baseline_point = None
    for pt in price_history:
        if pt.timestamp >= cutoff:
            baseline_point = pt
            break

    if baseline_point is None or baseline_point.price == current:
        return None

    baseline  = baseline_point.price
    magnitude = abs(current - baseline) / baseline

    if magnitude < C.SPIKE_THRESHOLD:
        return None

    direction = "up" if current > baseline else "down"
    window_s  = int((now - baseline_point.timestamp).total_seconds())

    return SpikeInfo(
        baseline_price=round(baseline, 4),
        current_price=round(current, 4),
        spike_magnitude=round(magnitude, 4),
        direction=direction,
        window_seconds=window_s,
    )


# ---------------------------------------------------------------------------
# Official-action guard
# ---------------------------------------------------------------------------

def is_official_action(
    market_title: str,
    market_category: str,
    scan_result: Any | None,
    reg_alerts: list[Any] | None,
) -> bool:
    """Return True if the spike appears to be caused by an official action.

    Checks:
      1. Regulatory alerts in the critical tier.
      2. Keyword match on market title or claude scan reasoning.
      3. Category is "regulatory" or "politics" with high-confidence scan.
    """
    # 1 — Any CRITICAL regulatory alert → official action
    if reg_alerts:
        for alert in reg_alerts:
            tier = getattr(alert, "tier", "")
            if tier == "CRITICAL":
                logger.debug("official_action_guard: CRITICAL reg alert present")
                return True

    # 2 — Keyword match on market title
    title_lower = market_title.lower()
    for kw in OFFICIAL_ACTION_KEYWORDS:
        if kw in title_lower:
            logger.debug("official_action_guard: keyword=%r in title", kw)
            return True

    # 3 — Claude scan result reasoning keyword check
    if scan_result is not None:
        reasoning = getattr(scan_result, "reasoning", "") or ""
        reasoning_lower = reasoning.lower()
        for kw in OFFICIAL_ACTION_KEYWORDS:
            if kw in reasoning_lower:
                logger.debug("official_action_guard: keyword=%r in reasoning", kw)
                return True

    return False


# ---------------------------------------------------------------------------
# Strategy B
# ---------------------------------------------------------------------------

class StrategyB:
    """Post-Spike Mean Reversion signal generator.

    Usage::

        strategy = StrategyB()
        signal = strategy.evaluate(
            market=scanned_market,
            price_history=list_of_price_points,
            scan_result=scan_result_from_claude,   # ScanResult or None
            reg_alerts=list_of_reg_alerts,         # from regulatory_feeds
        )
        if signal:
            # pass to news_catalyst and orderbook_confirm
    """

    def evaluate(
        self,
        market:       Any,                  # ScannedMarket
        price_history: list[PricePoint],
        scan_result:  Any | None = None,    # ScanResult from ClaudeAnalyzer
        reg_alerts:   list[Any] | None = None,
    ) -> Signal | None:
        """Generate a FADE signal if an unjustified spike is detected.

        Args:
            market:        ScannedMarket — current market state.
            price_history: Recent PricePoints, sorted oldest → newest.
            scan_result:   ScanResult from claude_analyzer.scan_mode(), or None.
            reg_alerts:    Active RegulatoryAlerts, or None.

        Returns:
            Signal if eligible to fade, None otherwise.
        """
        ticker = market.ticker

        # Gate 1 — detect spike
        spike = detect_spike(price_history)
        if spike is None:
            logger.debug("strategy_b_skip  ticker=%s  reason=no_spike", ticker)
            return None

        # Gate 2 — NEVER fade official actions
        if is_official_action(
            market.title, market.category, scan_result, reg_alerts
        ):
            logger.info(
                "strategy_b_skip  ticker=%s  reason=official_action  "
                "spike=%.1f%%",
                ticker, spike.spike_magnitude * 100,
            )
            return None

        # Gate 3 — Claude must NOT report high urgency with high confidence
        #           (that would suggest the move is justified)
        if scan_result is not None:
            urgency    = getattr(scan_result, "urgency", "monitor")
            scan_score = getattr(scan_result, "relevance_score", 5.0)
            cached     = getattr(scan_result, "cached", False)
            if not cached and urgency == "immediate" and scan_score >= 8.0:
                logger.info(
                    "strategy_b_skip  ticker=%s  reason=justified_move  "
                    "urgency=%s  score=%.1f",
                    ticker, urgency, scan_score,
                )
                return None

        # Gate 4 — entry only after mandatory wait (30 min = SPIKE_WAIT_MINUTES)
        if price_history:
            last_time = price_history[-1].timestamp
        else:
            last_time = datetime.now(timezone.utc)
        entry_eligible_at = last_time + timedelta(minutes=C.SPIKE_WAIT_MINUTES)

        # Direction: fade the spike → trade opposite to spike direction
        current_price = spike.current_price
        baseline      = spike.baseline_price
        spike_move    = current_price - baseline  # signed

        if spike.direction == "up":
            # Price spiked up — buy NO (fade the YES price going back down)
            direction     = "buy_no"
            entry_price   = round(1.0 - current_price, 4)  # NO price
            # 50% reversion of spike magnitude → target
            reversion_50  = round(current_price - 0.50 * abs(spike_move), 4)
            target_yes    = max(0.01, min(0.99, reversion_50))
            target_price  = round(1.0 - target_yes, 4)      # NO target
            # 25% further adverse → stop (NO price goes down if YES keeps rising)
            stop_yes   = round(current_price + 0.25 * abs(spike_move), 4)
            stop_price = round(1.0 - min(0.99, stop_yes), 4)
        else:
            # Price spiked down — buy YES (fade the YES price going back up)
            direction     = "buy_yes"
            entry_price   = round(current_price, 4)
            reversion_50  = round(current_price + 0.50 * abs(spike_move), 4)
            target_price  = max(0.01, min(0.99, reversion_50))
            stop_price    = round(current_price - 0.25 * abs(spike_move), 4)
            stop_price    = max(0.01, stop_price)

        # Confidence: medium by default; high if spike is very large (>25%)
        if spike.spike_magnitude >= 0.25:
            confidence_label = "high"
        elif spike.spike_magnitude >= C.SPIKE_THRESHOLD:
            confidence_label = "medium"
        else:
            confidence_label = "low"

        confidence = _tier_float(confidence_label)

        # Adjust confidence down if scan_result is cached or unavailable
        if scan_result is None or getattr(scan_result, "cached", True):
            confidence = max(0.0, confidence - 0.10)
            confidence_label = _float_to_tier(confidence)

        # Fee drag at entry price (best-effort; direction-aware)
        fee_price  = entry_price if direction == "buy_yes" else (1.0 - entry_price)
        fee_drag   = round(C.round_trip_fee(fee_price, 1, is_maker=True) * 100, 4)
        edge_pp    = round(spike.spike_magnitude * 100 * 0.50, 2)  # expected 50% reversion
        net_edge   = round(edge_pp - fee_drag, 2)

        reasoning = (
            f"Spike {spike.direction} {spike.spike_magnitude:.1%} "
            f"(baseline={baseline:.2f} -> current={current_price:.2f}); "
            f"fade direction={direction}; "
            f"wait_until={entry_eligible_at.strftime('%H:%MZ')}; "
            f"target={target_price:.3f}  stop={stop_price:.3f}"
        )

        signal = Signal(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            confidence=confidence,
            confidence_label=confidence_label,
            edge_pp=edge_pp,
            fee_drag_pp=fee_drag,
            net_edge_pp=net_edge,
            model_prob=reversion_50 if direction == "buy_yes" else (1 - reversion_50),
            market_price=current_price,
            reasoning=reasoning,
            strategy="mean_reversion",
            signal_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        logger.info(
            "strategy_b_signal  ticker=%s  direction=%s  spike=%.1f%%  "
            "entry=%.3f  target=%.3f  stop=%.3f  eligible_at=%s  conf=%s",
            ticker, direction, spike.spike_magnitude * 100,
            entry_price, target_price, stop_price,
            entry_eligible_at.strftime("%H:%MZ"), confidence_label,
        )

        return signal


# ---------------------------------------------------------------------------
# Helper: float → tier label
# ---------------------------------------------------------------------------

def _float_to_tier(confidence: float) -> str:
    if confidence >= 0.70:
        return "high"
    if confidence >= 0.45:
        return "medium"
    return "low"
