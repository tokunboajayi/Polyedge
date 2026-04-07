"""
Claude API wrapper for PolyEdge v5.

Two operating modes
-------------------
scan_mode  — claude-haiku (cheap, fast)
    Input:  one headline + minimal market context
    Output: ScanResult with relevance_score 0-10, news_impact, urgency
    Use:    RSS feed triage, market screening, routine checks

decision_mode — claude-sonnet (expensive, thorough)
    Input:  full DecisionContext (headlines, orderbook, base rates, history)
    Output: DecisionResult with predicted_probability, confidence,
            recommended_action, edge_pp, key_factors, risk_flags, reasoning
    Use:    final go/no-go trade decisions only

Retry & fallback
----------------
Both modes retry up to 2 times (3 total attempts) on timeout or API error,
with exponential back-off (1 s, 2 s).  After all retries are exhausted the
last successful result for that ticker is returned from the in-memory cache
(flagged cached=True).  If the cache is empty a safe default is returned so
the caller never receives an exception.

Cost logging
------------
Every successful API call is written to the api_costs SQLite table via
database.get_connection().  Failures to log are caught and warned, never
raised — analysis must proceed even if the DB is unavailable.

Pricing constants (approximate, update if Anthropic changes rates)
-----------
  Haiku:   $0.80 / 1M input,   $4.00 / 1M output
  Sonnet:  $3.00 / 1M input,  $15.00 / 1M output
"""

import asyncio
import dataclasses
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing table (dollars per token)
# ---------------------------------------------------------------------------

_INPUT_COST: dict[str, float] = {
    C.CLAUDE_SCAN_MODEL:     0.80 / 1_000_000,
    C.CLAUDE_DECISION_MODEL: 3.00 / 1_000_000,
}
_OUTPUT_COST: dict[str, float] = {
    C.CLAUDE_SCAN_MODEL:      4.00 / 1_000_000,
    C.CLAUDE_DECISION_MODEL: 15.00 / 1_000_000,
}

# ---------------------------------------------------------------------------
# Request / response dataclasses
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class ScanContext:
    """Inputs to scan_mode — one headline versus one market."""
    headline_title:   str
    headline_source:  str
    headline_summary: str
    market_ticker:    str
    market_title:     str
    market_category:  str
    current_price:    float   # dollars (0.01–0.99)
    days_to_settlement: float


@dataclasses.dataclass(slots=True)
class ScanResult:
    """Output of scan_mode."""
    relevance_score:  float   # 0–10; higher = more relevant to market outcome
    news_impact:      str     # "positive" | "negative" | "neutral"
    urgency:          str     # "immediate" | "monitor" | "ignore"
    reasoning:        str     # one-sentence explanation
    cached:           bool    # True when returned from fallback cache
    model:            str
    input_tokens:     int
    output_tokens:    int
    latency_ms:       int


@dataclasses.dataclass(slots=True)
class DecisionContext:
    """Full context passed to decision_mode."""
    market_ticker:        str
    market_title:         str
    market_category:      str
    current_price:        float           # dollars
    days_to_settlement:   float
    relevant_headlines:   list[dict]      # [{title, source, summary, published_time}]
    orderbook:            dict            # {yes: [[price_cents,size],...], no: [...]}
    base_rate:            float | None    # historical YES rate for category (0–1)
    base_rate_n:          int             # number of historical precedents
    recent_prices:        list[int]       # last N yes_ask prices in cents
    volume_7d:            float           # dollars
    open_interest:        int


@dataclasses.dataclass(slots=True)
class DecisionResult:
    """Output of decision_mode."""
    predicted_probability: float   # 0.00–1.00
    confidence:            str     # "high" | "medium" | "low"
    edge_pp:               float   # model_prob - market_price, percentage points
    recommended_action:    str     # "buy_yes" | "buy_no" | "skip" | "wait"
    key_factors:           list[str]
    risk_flags:            list[str]
    reasoning:             str
    cached:                bool
    model:                 str
    input_tokens:          int
    output_tokens:         int
    latency_ms:            int


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class _CacheEntry:
    result:     ScanResult | DecisionResult
    created_at: float   # time.monotonic()


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from Claude's response text.

    Tries four strategies in order:
      1. Parse the full trimmed response directly.
      2. Extract from a ```json ... ``` code fence.
      3. Find the outermost { ... } span.
      4. Greedy last-resort search.
    Returns an empty dict if all strategies fail.
    """
    text = text.strip()

    # 1 — direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2 — code fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3 — find balanced braces
    depth = start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == -1:
                start, depth = i, 0
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    pass
                break

    # 4 — greedy
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return {}


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------

def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    ic = _INPUT_COST.get(model, 3.00 / 1_000_000)
    oc = _OUTPUT_COST.get(model, 15.00 / 1_000_000)
    return round(ic * input_tokens + oc * output_tokens, 8)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _scan_prompt(ctx: ScanContext) -> tuple[str, str]:
    """Return (system, user) strings for scan_mode."""
    system = (
        "You are a quantitative prediction market analyst. "
        "Rate how strongly a news headline affects the probability of a Kalshi "
        "event contract resolving YES. Be concise. Always respond with valid JSON only."
    )
    price_pct = round(ctx.current_price * 100, 0)
    user = f"""MARKET
Ticker:   {ctx.market_ticker}
Title:    {ctx.market_title}
Category: {ctx.market_category}
Price:    {price_pct:.0f}c implied {price_pct:.0f}% YES
Days:     {ctx.days_to_settlement:.1f}

HEADLINE
Source:  {ctx.headline_source}
Title:   {ctx.headline_title}
Summary: {ctx.headline_summary[:400]}

Rate the headline's effect on this specific market and respond with JSON only:
{{
  "relevance_score": <integer 0-10>,
  "news_impact":     "<positive|negative|neutral>",
  "urgency":         "<immediate|monitor|ignore>",
  "reasoning":       "<one sentence, max 120 chars>"
}}

Scoring guide:
  0-2  Unrelated to market outcome
  3-5  Loosely related, indirect effect
  6-7  Relevant, modestly shifts probability
  8-9  Directly relevant, meaningfully shifts probability
  10   Definitively determines outcome"""
    return system, user


def _decision_prompt(ctx: DecisionContext) -> tuple[str, str]:
    """Return (system, user) strings for decision_mode."""
    system = (
        "You are an expert prediction market analyst for Kalshi CFTC-regulated contracts. "
        "Provide a calibrated probability estimate and clear trading recommendation. "
        "Be precise, analytical, and risk-aware. Respond with valid JSON only."
    )

    # Format headlines
    if ctx.relevant_headlines:
        headlines_text = "\n".join(
            f"  [{h.get('source','?')}] {h.get('title','')}: {h.get('summary','')[:180]}"
            for h in ctx.relevant_headlines[:6]
        )
    else:
        headlines_text = "  No relevant headlines available."

    # Format orderbook top-3
    def fmt_book(levels: list) -> str:
        return "  " + "  |  ".join(
            f"{p}c x {s}" for p, s in levels[:3]
        ) if levels else "  (empty)"

    yes_levels = ctx.orderbook.get("yes", [])
    no_levels  = ctx.orderbook.get("no", [])

    # Format recent prices
    price_hist = (
        " -> ".join(f"{p}c" for p in ctx.recent_prices[-10:])
        if ctx.recent_prices else "unavailable"
    )

    base_rate_text = (
        f"{ctx.base_rate * 100:.1f}% (n={ctx.base_rate_n})"
        if ctx.base_rate is not None
        else "insufficient data"
    )

    price_pct = round(ctx.current_price * 100, 0)
    market_price_for_action = ctx.current_price

    user = f"""MARKET ANALYSIS REQUEST
Ticker:   {ctx.market_ticker}
Title:    {ctx.market_title}
Category: {ctx.market_category}
Price:    {price_pct:.0f}c ({price_pct:.0f}% YES probability implied)
Days:     {ctx.days_to_settlement:.1f} to settlement

RELEVANT HEADLINES (recent)
{headlines_text}

ORDERBOOK SNAPSHOT
YES bids/asks: {fmt_book(yes_levels)}
NO bids/asks:  {fmt_book(no_levels)}

BASE RATE
Historical YES rate for {ctx.market_category}: {base_rate_text}

RECENT PRICE HISTORY
{price_hist}

MARKET STATS
7-day volume:   ${ctx.volume_7d:,.0f}
Open interest:  {ctx.open_interest:,} contracts

Analyze this market comprehensively and respond with JSON only:
{{
  "predicted_probability": <0.00-1.00, two decimal places>,
  "confidence":            "<high|medium|low>",
  "edge_pp":               <model_prob*100 - {price_pct:.0f}, signed float>,
  "recommended_action":    "<buy_yes|buy_no|skip|wait>",
  "key_factors":           ["<factor>", "<factor>", "<factor>"],
  "risk_flags":            ["<flag>", ...],
  "reasoning":             "<2-3 sentences>"
}}

Action rules (apply strictly):
  buy_yes  predicted_probability > {market_price_for_action + 0.10:.2f} AND confidence != low
  buy_no   predicted_probability < {market_price_for_action - 0.10:.2f} AND confidence != low
  wait     spike/volatility, insufficient data, confidence = low, or <30 min post-spike
  skip     no meaningful edge, ambiguous resolution, regulatory risk, or official action already taken"""
    return system, user


# ---------------------------------------------------------------------------
# Safe defaults (returned when all retries fail and no cache exists)
# ---------------------------------------------------------------------------

_SCAN_DEFAULT = ScanResult(
    relevance_score=5.0,
    news_impact="neutral",
    urgency="monitor",
    reasoning="Analysis unavailable — API failure.",
    cached=True,
    model=C.CLAUDE_SCAN_MODEL,
    input_tokens=0, output_tokens=0, latency_ms=0,
)

_DECISION_DEFAULT = DecisionResult(
    predicted_probability=0.0,
    confidence="low",
    edge_pp=0.0,
    recommended_action="skip",
    key_factors=[],
    risk_flags=["analysis_unavailable"],
    reasoning="Analysis unavailable — API failure. Skipping trade.",
    cached=True,
    model=C.CLAUDE_DECISION_MODEL,
    input_tokens=0, output_tokens=0, latency_ms=0,
)


# ---------------------------------------------------------------------------
# ClaudeAnalyzer
# ---------------------------------------------------------------------------

class ClaudeAnalyzer:
    """Two-mode Claude API wrapper with retry, fallback cache, and cost logging.

    Usage::

        analyzer = ClaudeAnalyzer()

        # cheap scan: is this headline relevant?
        result = await analyzer.scan_mode(ctx)

        # expensive decision: should we trade?
        result = await analyzer.decision_mode(ctx)
    """

    _consecutive_api_failures: int = 0
    _api_unavailable: bool = False

    def __init__(self, api_key: str | None = None) -> None:
        from config import settings as S
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or S.ANTHROPIC_API_KEY
        )
        # Per-ticker caches: "scan:{ticker}" and "decision:{ticker}"
        self._cache: dict[str, _CacheEntry] = {}

        self._consecutive_api_failures: int = 0
        self._api_unavailable: bool = False

        logger.info(
            "ClaudeAnalyzer ready  scan_model=%s  decision_model=%s",
            C.CLAUDE_SCAN_MODEL, C.CLAUDE_DECISION_MODEL,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan_mode(self, ctx: ScanContext) -> ScanResult:
        """Rate a headline's relevance to a market using Haiku.

        Retries 2x on failure, falls back to cached result or safe default.
        """
        system, user = _scan_prompt(ctx)
        cache_key = f"scan:{ctx.market_ticker}"

        try:
            result_tuple = await self._call_claude(
                model=C.CLAUDE_SCAN_MODEL,
                system=system,
                user=user,
                purpose="scan",
                ticker=ctx.market_ticker,
                max_tokens=256,
            )
        except Exception as exc:
            logger.warning(
                "scan_mode_failed  ticker=%s  error=%s  using_cache=%s",
                ctx.market_ticker, exc, cache_key in self._cache,
            )
            return self._get_cached(cache_key, _SCAN_DEFAULT)  # type: ignore[return-value]

        if result_tuple is None:
            return self._get_cached(cache_key, _SCAN_DEFAULT)  # type: ignore[return-value]

        raw, input_tok, output_tok, latency = result_tuple

        data = _extract_json(raw)
        result = ScanResult(
            relevance_score=float(
                max(0.0, min(10.0, data.get("relevance_score", 5.0)))
            ),
            news_impact=data.get("news_impact", "neutral"),
            urgency=data.get("urgency", "monitor"),
            reasoning=str(data.get("reasoning", ""))[:200],
            cached=False,
            model=C.CLAUDE_SCAN_MODEL,
            input_tokens=input_tok,
            output_tokens=output_tok,
            latency_ms=latency,
        )
        self._cache[cache_key] = _CacheEntry(result=result, created_at=time.monotonic())
        return result

    async def decision_mode(self, ctx: DecisionContext) -> DecisionResult:
        """Produce a trade decision using Sonnet.

        Retries 2x on failure, falls back to cached result or safe default.
        """
        system, user = _decision_prompt(ctx)
        cache_key = f"decision:{ctx.market_ticker}"

        try:
            result_tuple = await self._call_claude(
                model=C.CLAUDE_DECISION_MODEL,
                system=system,
                user=user,
                purpose="decision",
                ticker=ctx.market_ticker,
                max_tokens=512,
            )
        except Exception as exc:
            logger.warning(
                "decision_mode_failed  ticker=%s  error=%s  using_cache=%s",
                ctx.market_ticker, exc, cache_key in self._cache,
            )
            return self._get_cached(cache_key, _DECISION_DEFAULT)  # type: ignore[return-value]

        if result_tuple is None:
            return self._get_cached(cache_key, _DECISION_DEFAULT)  # type: ignore[return-value]

        raw, input_tok, output_tok, latency = result_tuple

        data = _extract_json(raw)
        prob = float(max(0.0, min(1.0, data.get("predicted_probability", 0.5))))
        edge = round(prob * 100 - ctx.current_price * 100, 2)

        result = DecisionResult(
            predicted_probability=prob,
            confidence=data.get("confidence", "low"),
            edge_pp=float(data.get("edge_pp", edge)),
            recommended_action=data.get("recommended_action", "skip"),
            key_factors=list(data.get("key_factors", []))[:5],
            risk_flags=list(data.get("risk_flags", []))[:5],
            reasoning=str(data.get("reasoning", ""))[:500],
            cached=False,
            model=C.CLAUDE_DECISION_MODEL,
            input_tokens=input_tok,
            output_tokens=output_tok,
            latency_ms=latency,
        )
        self._cache[cache_key] = _CacheEntry(result=result, created_at=time.monotonic())
        return result

    # ------------------------------------------------------------------
    # Internal — core API call with retry
    # ------------------------------------------------------------------

    async def _call_claude(
        self,
        model:     str,
        system:    str,
        user:      str,
        purpose:   str,
        ticker:    str,
        max_tokens: int = 256,
    ) -> tuple[str, int, int, int] | None:
        """Call the Anthropic API with up to 2 retries.

        Returns (response_text, input_tokens, output_tokens, latency_ms) on
        success.  Returns None (never raises) if all attempts fail — sets the
        _api_unavailable flag and logs CRITICAL.
        """
        last_exc: Exception | None = None

        for attempt in range(3):   # initial + 2 retries
            wait = 2.0 ** attempt  # 1 s, 2 s (not used on attempt 0)
            try:
                t0 = time.monotonic()
                response = await asyncio.wait_for(
                    self._client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user}],
                    ),
                    timeout=30.0,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                content      = response.content[0].text
                input_tokens  = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                cost          = _compute_cost(model, input_tokens, output_tokens)

                logger.info(
                    "claude_call  model=%s  purpose=%s  ticker=%s  "
                    "input_tok=%d  output_tok=%d  cost=$%.6f  latency_ms=%d",
                    model, purpose, ticker,
                    input_tokens, output_tokens, cost, latency_ms,
                )

                await self._log_cost(
                    model, input_tokens, output_tokens, cost, purpose, ticker
                )

                self.reset_availability()
                return content, input_tokens, output_tokens, latency_ms

            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "claude_timeout  model=%s  attempt=%d/%d  ticker=%s",
                    model, attempt + 1, 3, ticker,
                )
            except anthropic.APIStatusError as exc:
                last_exc = exc
                # 529 overloaded / 5xx → retry; 4xx (except 429) → don't retry
                if exc.status_code not in (429, 500, 502, 503, 529):
                    raise
                logger.warning(
                    "claude_api_error  model=%s  status=%d  attempt=%d/%d  ticker=%s",
                    model, exc.status_code, attempt + 1, 3, ticker,
                )
            except anthropic.APIConnectionError as exc:
                last_exc = exc
                logger.warning(
                    "claude_connection_error  model=%s  attempt=%d/%d  ticker=%s",
                    model, attempt + 1, 3, ticker,
                )

            if attempt < 2:
                await asyncio.sleep(wait)

        self._consecutive_api_failures += 1
        self._api_unavailable = True
        logger.critical(
            "claude_api_unavailable  model=%s  purpose=%s  ticker=%s  "
            "consecutive_failures=%d — returning safe default, no new trades",
            model, purpose, ticker, self._consecutive_api_failures,
        )
        return None

    # ------------------------------------------------------------------
    # Internal — DB cost logging
    # ------------------------------------------------------------------

    async def _log_cost(
        self,
        model:         str,
        input_tokens:  int,
        output_tokens: int,
        cost_usd:      float,
        purpose:       str,
        ticker:        str,
    ) -> None:
        """Write one row to api_costs.  Silently ignores all errors."""
        try:
            from persistence.database import get_connection
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            async with get_connection() as db:
                await db.execute(
                    """INSERT INTO api_costs
                       (model, prompt_tokens, completion_tokens, total_tokens,
                        cost_usd, purpose, ticker, called_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        model,
                        input_tokens,
                        output_tokens,
                        input_tokens + output_tokens,
                        cost_usd,
                        purpose,
                        ticker or None,
                        ts,
                    ),
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude_cost_log_failed  error=%s", exc)

    # ------------------------------------------------------------------
    # Internal — cache helpers
    # ------------------------------------------------------------------

    def _get_cached(
        self,
        key: str,
        default: ScanResult | DecisionResult,
    ) -> ScanResult | DecisionResult:
        entry = self._cache.get(key)
        if entry is None:
            return default
        # Return a copy with cached=True
        result = entry.result
        # Use dataclasses.replace to flip the cached flag
        return dataclasses.replace(result, cached=True)  # type: ignore[type-var]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def is_unavailable(self) -> bool:
        """Return True when the Claude API has been flagged as unavailable."""
        return self._api_unavailable

    def reset_availability(self) -> None:
        """Reset failure tracking after a successful API call."""
        self._consecutive_api_failures = 0
        self._api_unavailable = False

    def cache_keys(self) -> list[str]:
        return list(self._cache.keys())

    def cache_age_seconds(self, key: str) -> float | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        return round(time.monotonic() - entry.created_at, 1)
