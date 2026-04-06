"""
News Catalyst Signal Adjuster for PolyEdge v5.

Role
----
Acts as a confidence modifier layer between the raw strategy signal (A or B)
and the final execution decision.  It reads a Claude ScanResult and adjusts
the signal's confidence score by –20% to +20% depending on the strength and
direction of the news.

Adjustment table
----------------
  Condition                                    Adjustment
  -----------------------------------------------  ----------
  urgency=immediate AND news_impact=positive       +20 pp  (strong tailwind)
  urgency=immediate AND news_impact=negative       -20 pp  (strong headwind)
  urgency=monitor   AND news_impact=positive       +10 pp  (mild tailwind)
  urgency=monitor   AND news_impact=negative       -10 pp  (mild headwind)
  urgency=ignore    (any impact)                    -15 pp  (news irrelevant)
  news_impact=neutral (any urgency)                  0 pp  (no change)
  scan_result=None OR cached                        -10 pp  (no fresh data)
  relevance_score < 3                               -15 pp  (irrelevant headline)
  relevance_score >= 8 AND impact != neutral        ±20 pp  (overrides urgency)

The adjustment is always applied additively then clamped to [0.05, 0.95] so
no signal reaches full certainty or near-zero confidence from this layer alone.

The direction of adjustment depends on signal.direction vs news impact:
  buy_yes + positive news → confidence UP
  buy_yes + negative news → confidence DOWN
  buy_no  + positive news → confidence DOWN  (news supports YES, hurts NO fade)
  buy_no  + negative news → confidence UP    (news supports NO)

Strategy B (mean_reversion) override
--------------------------------------
If the signal strategy is "mean_reversion" AND the scan shows urgency=immediate
with relevance_score >= 8 for positive news aligned with the spike direction,
the catalyst downgrades the signal because the spike appears JUSTIFIED —
the catalyst returns `veto=True` in the CatalystResult so the caller can
discard the signal entirely.

Output: CatalystResult
----------------------
  adjusted_confidence  float   clamped to [0.05, 0.95]
  adjustment_pp        float   how many percentage points were added/removed
  adjustment_reason    str     one-line explanation
  veto                 bool    True → discard signal (justified spike or
                               strong contradicting news drops conf below 0.15)
"""

import dataclasses
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_MIN_CONFIDENCE: float = 0.05
_MAX_CONFIDENCE: float = 0.95
_VETO_THRESHOLD: float = 0.15   # if adjusted conf drops below this → veto


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class CatalystResult:
    """Output of NewsCatalyst.adjust()."""
    original_confidence:  float
    adjusted_confidence:  float   # clamped to [0.05, 0.95]
    adjustment_pp:        float   # signed adjustment applied
    adjustment_reason:    str
    veto:                 bool    # True → engine should discard the signal


# ---------------------------------------------------------------------------
# NewsCatalyst
# ---------------------------------------------------------------------------

class NewsCatalyst:
    """Adjusts signal confidence based on a Claude ScanResult.

    Usage::

        catalyst = NewsCatalyst()
        result   = catalyst.adjust(signal, scan_result)
        if result.veto:
            return   # discard signal
        signal = dataclasses.replace(signal, confidence=result.adjusted_confidence)
    """

    def adjust(self, signal, scan_result) -> CatalystResult:
        """Apply news-based confidence adjustment to a signal.

        Args:
            signal:      Signal from StrategyA or StrategyB.
            scan_result: ScanResult from ClaudeAnalyzer.scan_mode(), or None.

        Returns:
            CatalystResult with adjusted confidence and veto flag.
        """
        original = signal.confidence
        direction = signal.direction   # "buy_yes" | "buy_no"
        strategy  = signal.strategy

        # --- Unavailable / cached scan ----------------------------------
        if scan_result is None or getattr(scan_result, "cached", True):
            adj, reason = -0.10, "no_fresh_scan_data"
            return self._build(original, adj, reason, signal)

        urgency    = getattr(scan_result, "urgency", "monitor")
        impact     = getattr(scan_result, "news_impact", "neutral")
        score      = float(getattr(scan_result, "relevance_score", 5.0))

        # --- Irrelevant headline ----------------------------------------
        if score < 3.0:
            adj, reason = -0.15, f"low_relevance_score={score:.1f}"
            return self._build(original, adj, reason, signal)

        # --- Neutral impact — no change ---------------------------------
        if impact == "neutral":
            adj, reason = 0.0, "neutral_impact"
            return self._build(original, adj, reason, signal)

        # --- Strong signal (score >= 8) overrides urgency bucket --------
        if score >= 8.0:
            raw_adj = 0.20
        elif urgency == "immediate":
            raw_adj = 0.20
        elif urgency == "monitor":
            raw_adj = 0.10
        else:  # ignore
            adj, reason = -0.15, "urgency=ignore"
            return self._build(original, adj, reason, signal)

        # --- Direction alignment check ----------------------------------
        # Positive news → helps buy_yes, hurts buy_no (and mean-rev fades)
        # Negative news → hurts buy_yes, helps buy_no
        aligned = _is_aligned(direction, impact)
        signed_adj = raw_adj if aligned else -raw_adj

        reason = (
            f"urgency={urgency}  impact={impact}  score={score:.1f}  "
            f"aligned={aligned}  adj={signed_adj:+.0%}"
        )

        # --- Strategy B veto: justified spike check ---------------------
        if (
            strategy == "mean_reversion"
            and urgency == "immediate"
            and score >= 8.0
            and not aligned   # news aligned with the spike direction hurts our fade
        ):
            # Spike appears justified — veto the fade signal
            result = self._build(original, signed_adj, reason, signal)
            logger.info(
                "catalyst_veto  ticker=%s  strategy=%s  score=%.1f  impact=%s",
                signal.ticker, strategy, score, impact,
            )
            return dataclasses.replace(result, veto=True)

        return self._build(original, signed_adj, reason, signal)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build(
        original:  float,
        adj_pp:    float,
        reason:    str,
        signal,
    ) -> CatalystResult:
        adjusted = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, original + adj_pp))
        veto     = adjusted < _VETO_THRESHOLD

        logger.debug(
            "catalyst_adjust  ticker=%s  orig=%.2f  adj=%+.2f  final=%.2f  "
            "veto=%s  reason=%s",
            signal.ticker, original, adj_pp, adjusted, veto, reason,
        )

        return CatalystResult(
            original_confidence=original,
            adjusted_confidence=round(adjusted, 4),
            adjustment_pp=round(adj_pp, 4),
            adjustment_reason=reason,
            veto=veto,
        )


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def _is_aligned(direction: str, news_impact: str) -> bool:
    """Return True if the news impact reinforces the trade direction.

      buy_yes + positive  → True   (news supports YES outcome)
      buy_yes + negative  → False  (news hurts YES outcome)
      buy_no  + negative  → True   (news supports NO outcome)
      buy_no  + positive  → False  (news hurts NO outcome)
    """
    if direction == "buy_yes":
        return news_impact == "positive"
    if direction == "buy_no":
        return news_impact == "negative"
    return False
