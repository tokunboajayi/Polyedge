"""
Kalshi market scanner for PolyEdge v5.

Fetches all open markets from the Kalshi REST API, runs each through
the two-phase filter (exclusion → entry criteria), computes fee-adjusted
minimum required edge, and returns a ranked list of tradeable markets.

Filter logic (from CLAUDE.md)
------------------------------
Exclusion (ANY → skip, no further checks):
  ✗  Settlement < 48 hours away
  ✗  Category in EXCLUDED_CATEGORIES (sports, weather, crypto, entertainment …)
  ✗  Presidential / major election market (keyword match on title/ticker)
  ✗  Manual exclusion list (configurable at init)
  ✗  Ambiguous settlement keywords detected in title

Entry criteria (ALL → pass):
  ✓  7-day volume  ≥ MIN_MARKET_VOLUME_7D  ($5,000)
  ✓  Bid-ask spread  < MAX_SPREAD  (8¢)
  ✓  Liquidity (open_interest × mid_price)  ≥ MIN_LIQUIDITY  ($1,000)
  ✓  Days to settlement  ∈ [MIN_SETTLEMENT_DAYS=7, MAX_SETTLEMENT_DAYS=90]
  ✓  Normalised category  ∈ SUPPORTED_CATEGORIES
  ✓  Estimated exit slippage  ≤ MAX_EXIT_SLIPPAGE  (3%)

Fee-adjusted minimum edge
--------------------------
At scan time we do not yet know the model's edge, but we pre-compute the
minimum edge (as a dollar amount) that a trade at MIN_TRADE_SIZE contracts
must produce to break even after maker round-trip fees.  The analyzer uses
this value to gate trade decisions:

    min_edge_dollars = round_trip_fee(mid_price, MIN_TRADE_SIZE, is_maker=True)

Ranking
-------
Tradeable markets are sorted by a composite score:
  60% normalised 7-day volume  (higher → better)
  30% spread tightness          (lower spread → better)
  10% settlement timing         (14–30 day window is ideal)
"""

import dataclasses
import logging
import re
from datetime import datetime, timezone
from typing import Any

from config import constants as C
from config.constants import round_trip_fee

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category normalisation
# ---------------------------------------------------------------------------

#: Maps Kalshi category strings (lowercased) → PolyEdge canonical category.
CATEGORY_MAP: dict[str, str] = {
    "economics":       "economics",
    "economy":         "economics",
    "financial":       "economics",
    "financials":      "economics",
    "finance":         "economics",
    "macro":           "macro",
    "macroeconomics":  "macro",
    "federal reserve": "macro",
    "interest rates":  "macro",
    "inflation":       "macro",
    "politics":        "politics",
    "political":       "politics",
    "geopolitics":     "politics",
    "government":      "politics",
    "elections":       "politics",
    "regulatory":      "regulatory",
    "regulation":      "regulatory",
    "legal":           "regulatory",
    "tech":            "tech",
    "technology":      "tech",
    "ai":              "tech",
    "science":         "tech",
}

SUPPORTED_CATEGORIES: frozenset[str] = frozenset({
    "economics", "politics", "regulatory", "macro", "tech",
})

#: Any market whose Kalshi category normalises to one of these is auto-excluded.
EXCLUDED_CATEGORIES: frozenset[str] = frozenset({
    "sports", "sport", "weather", "crypto", "cryptocurrency",
    "entertainment", "pop culture", "celebrity", "music", "tv",
    "gaming", "esports",
})

# ---------------------------------------------------------------------------
# Exclusion keyword sets
# ---------------------------------------------------------------------------

#: Title / ticker patterns that identify presidential or major general elections.
_ELECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bpresidential\s+election\b", re.I),
    re.compile(r"\bwin\s+the\s+(2\d{3}\s+)?election\b", re.I),
    re.compile(r"\belectoral\s+college\b", re.I),
    re.compile(r"\bwho\s+wins?\s+the\s+(us|u\.s\.)\s+(presidential\s+)?election\b", re.I),
    re.compile(r"\b(trump|harris|biden)\s+win\s+", re.I),
    re.compile(r"\bpres\d{2}[A-Z]+-", re.I),   # Kalshi election ticker prefix
]

#: Title phrases that suggest settlement criteria are ambiguous.
_AMBIGUITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bapproximately\b", re.I),
    re.compile(r"\broughly\b", re.I),
    re.compile(r"\bsomething significant\b", re.I),
    re.compile(r"\bmeaningful\s+action\b", re.I),
    re.compile(r"\bsubstantial\s+progress\b", re.I),
    re.compile(r"\bat\s+some\s+point\b", re.I),
]

# ---------------------------------------------------------------------------
# Minimum liquidity threshold (separate from volume check)
# ---------------------------------------------------------------------------
MIN_LIQUIDITY_DOLLARS: float = 1_000.0   # open_interest × mid_price

# ---------------------------------------------------------------------------
# Structured output types
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class ScannedMarket:
    """A Kalshi market that has been evaluated by the scanner.

    Fields are populated regardless of pass/fail so the caller can inspect
    why a market was rejected.
    """
    # Identity
    ticker:            str
    title:             str
    event_ticker:      str
    series_ticker:     str

    # Category (Kalshi raw → PolyEdge canonical)
    raw_category:      str
    category:          str          # normalised; "unknown" if unmapped

    # Pricing (cents; None if not available)
    yes_bid:           int | None
    yes_ask:           int | None
    no_bid:            int | None
    no_ask:            int | None
    mid_price:         float        # dollars (0.01–0.99); derived from yes ask/bid

    # Liquidity
    spread_cents:      float | None  # yes_ask – yes_bid
    volume_7d:         float         # dollar volume (best available proxy)
    open_interest:     int           # contracts outstanding

    # Settlement
    close_time:        str           # ISO-8601 UTC
    days_to_settlement: float        # calendar days from now

    # Fee edge
    min_edge_dollars:  float         # round-trip maker fee at MIN_TRADE_SIZE

    # Scanner output
    scan_score:        float         # composite ranking score (0–1)
    scan_rank:         int           # 1 = best; 0 = not yet ranked
    passed:            bool
    rejection_reasons: list[str]     # empty when passed=True


@dataclasses.dataclass(slots=True)
class ScanResult:
    """Complete output of one scan cycle."""
    tradeable:         list[ScannedMarket]   # passed all checks; ranked
    rejected:          list[ScannedMarket]   # failed one or more checks
    scan_time:         str                   # ISO-8601 UTC
    total_fetched:     int
    total_tradeable:   int
    rejection_summary: dict[str, int]        # reason text → count


# ---------------------------------------------------------------------------
# MarketScanner
# ---------------------------------------------------------------------------

class MarketScanner:
    """Fetches, filters, and ranks open Kalshi markets.

    Usage::

        client  = KalshiClient()
        scanner = MarketScanner(client)
        result  = scanner.scan()

        for market in result.tradeable:
            print(market.ticker, market.scan_score)
    """

    def __init__(
        self,
        client: Any,                          # KalshiClient (avoid circular import)
        extra_exclusions: list[str] | None = None,
        volume_threshold:     float = C.MIN_MARKET_VOLUME_7D,
        spread_threshold:     float = C.MAX_SPREAD,
        min_settlement_days:  int   = C.MIN_SETTLEMENT_DAYS,
        max_settlement_days:  int   = C.MAX_SETTLEMENT_DAYS,
        slippage_threshold:   float = C.MAX_EXIT_SLIPPAGE,
        min_liquidity:        float = MIN_LIQUIDITY_DOLLARS,
    ) -> None:
        """
        Args:
            client:             Initialised KalshiClient instance.
            extra_exclusions:   Additional ticker substrings to always exclude
                                (e.g. ["KXBTC", "KXETH"]).
            volume_threshold:   Minimum 7-day dollar volume.
            spread_threshold:   Maximum bid-ask spread in dollars (0.08 = 8¢).
            min_settlement_days: Earliest acceptable settlement window.
            max_settlement_days: Latest acceptable settlement window.
            slippage_threshold: Maximum estimated exit slippage fraction.
            min_liquidity:      Minimum open-interest × mid-price.
        """
        self._client              = client
        self._extra_exclusions:   list[str] = extra_exclusions or []
        self._vol_threshold       = volume_threshold
        self._spread_threshold    = spread_threshold
        self._min_days            = min_settlement_days
        self._max_days            = max_settlement_days
        self._slippage_threshold  = slippage_threshold
        self._min_liquidity       = min_liquidity

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> ScanResult:
        """Fetch all open markets and return a ScanResult.

        Network errors from KalshiClient propagate to the caller.
        """
        now_utc = datetime.now(timezone.utc)
        logger.info("market_scan_start  time=%s", now_utc.isoformat())

        raw_markets: list[dict[str, Any]] = self._client.get_markets(
            status="open", fetch_all=True
        )
        logger.info("market_scan_fetched  count=%d", len(raw_markets))

        tradeable: list[ScannedMarket] = []
        rejected:  list[ScannedMarket] = []

        for raw in raw_markets:
            sm = self._evaluate(raw, now_utc)
            if sm.passed:
                tradeable.append(sm)
            else:
                rejected.append(sm)

        # Rank tradeable markets
        tradeable = self._rank(tradeable)

        # Rejection summary
        reason_counts: dict[str, int] = {}
        for sm in rejected:
            for r in sm.rejection_reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1

        result = ScanResult(
            tradeable=tradeable,
            rejected=rejected,
            scan_time=now_utc.isoformat(),
            total_fetched=len(raw_markets),
            total_tradeable=len(tradeable),
            rejection_summary=reason_counts,
        )

        self._log_result(result)
        return result

    # ------------------------------------------------------------------
    # Internal — per-market evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, raw: dict[str, Any], now_utc: datetime) -> ScannedMarket:
        """Parse one raw API market dict and run all checks."""
        ticker       = raw.get("ticker", "")
        title        = raw.get("title", "")
        event_ticker = raw.get("event_ticker", "")
        series_ticker = raw.get("series_ticker", "")

        raw_category = (raw.get("category") or "").strip()
        category     = self._normalise_category(raw_category)

        yes_bid = _int_or_none(raw.get("yes_bid"))
        yes_ask = _int_or_none(raw.get("yes_ask"))
        no_bid  = _int_or_none(raw.get("no_bid"))
        no_ask  = _int_or_none(raw.get("no_ask"))

        spread_cents = _compute_spread(yes_bid, yes_ask)
        mid_price    = _compute_mid(yes_bid, yes_ask)

        # Volume: try 7d field first, fall back to whatever is available;
        # if the field is in contracts, multiply by mid_price to estimate dollars.
        volume_raw  = _first_float(raw, "volume_24h", "volume_7d", "volume_7day", "volume")
        volume_7d   = volume_raw * mid_price if volume_raw > 0 and mid_price > 0 else volume_raw

        open_interest = int(raw.get("open_interest") or 0)

        close_time, days = self._parse_settlement(raw, now_utc)

        min_edge = round_trip_fee(mid_price, C.MIN_TRADE_SIZE, is_maker=True) if mid_price > 0 else 0.0

        # ---- Phase 1: exclusion (hard stops) -----
        rejections: list[str] = []
        rejections.extend(self._exclusion_checks(ticker, title, category, days))

        # ---- Phase 2: entry criteria (only if not already excluded) ----
        if not rejections:
            rejections.extend(
                self._entry_checks(
                    volume_7d, spread_cents, open_interest,
                    mid_price, days, category, raw,
                )
            )

        score = _composite_score(volume_7d, spread_cents, days) if not rejections else 0.0

        return ScannedMarket(
            ticker=ticker,
            title=title,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            raw_category=raw_category,
            category=category,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            mid_price=mid_price,
            spread_cents=spread_cents,
            volume_7d=volume_7d,
            open_interest=open_interest,
            close_time=close_time,
            days_to_settlement=days if days is not None else -1.0,
            min_edge_dollars=min_edge,
            scan_score=score,
            scan_rank=0,
            passed=len(rejections) == 0,
            rejection_reasons=rejections,
        )

    # ------------------------------------------------------------------
    # Phase 1 — exclusion checks
    # ------------------------------------------------------------------

    def _exclusion_checks(
        self,
        ticker:   str,
        title:    str,
        category: str,
        days:     float | None,
    ) -> list[str]:
        reasons: list[str] = []

        # Settlement < 48 hours
        if days is not None and days < 2.0:
            reasons.append(f"settlement_too_soon:{days:.1f}d")

        # Excluded category
        raw_lower = category.lower()
        if raw_lower in EXCLUDED_CATEGORIES or category == "excluded":
            reasons.append(f"excluded_category:{category}")

        # Presidential / major election
        combined = f"{ticker} {title}"
        for pat in _ELECTION_PATTERNS:
            if pat.search(combined):
                reasons.append("presidential_election_market")
                break

        # Ambiguous settlement criteria
        for pat in _AMBIGUITY_PATTERNS:
            if pat.search(title):
                reasons.append("ambiguous_settlement_criteria")
                break

        # Manual extra exclusions
        for excl in self._extra_exclusions:
            if excl.upper() in ticker.upper():
                reasons.append(f"manual_exclusion:{excl}")
                break

        return reasons

    # ------------------------------------------------------------------
    # Phase 2 — entry criteria
    # ------------------------------------------------------------------

    def _entry_checks(
        self,
        volume_7d:     float,
        spread_cents:  float | None,
        open_interest: int,
        mid_price:     float,
        days:          float | None,
        category:      str,
        raw:           dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []

        # Volume
        if volume_7d < self._vol_threshold:
            reasons.append(
                f"low_volume:{volume_7d:.0f}<{self._vol_threshold:.0f}"
            )

        # Spread
        if spread_cents is None:
            reasons.append("no_spread_data")
        elif spread_cents > self._spread_threshold * 100:
            reasons.append(
                f"wide_spread:{spread_cents:.1f}c>{self._spread_threshold*100:.0f}c"
            )

        # Liquidity (open_interest × mid_price)
        liquidity = open_interest * mid_price
        if liquidity < self._min_liquidity:
            reasons.append(
                f"low_liquidity:{liquidity:.0f}<{self._min_liquidity:.0f}"
            )

        # Settlement window
        if days is None:
            reasons.append("no_settlement_date")
        else:
            if days < self._min_days:
                reasons.append(f"settlement_too_close:{days:.1f}d<{self._min_days}d")
            elif days > self._max_days:
                reasons.append(f"settlement_too_far:{days:.1f}d>{self._max_days}d")

        # Supported category
        if category not in SUPPORTED_CATEGORIES:
            reasons.append(f"unsupported_category:{category}")

        # Exit slippage estimate
        slippage = _estimate_slippage(raw, C.MIN_TRADE_SIZE)
        if slippage is not None and slippage > self._slippage_threshold:
            reasons.append(
                f"high_slippage:{slippage:.3f}>{self._slippage_threshold:.3f}"
            )

        return reasons

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_category(raw: str) -> str:
        """Map a Kalshi category string to a PolyEdge canonical category.

        Returns "unknown" for unrecognised categories, "excluded" for those
        in EXCLUDED_CATEGORIES.
        """
        key = raw.lower().strip()
        if key in EXCLUDED_CATEGORIES:
            return "excluded"
        if key in CATEGORY_MAP:
            return CATEGORY_MAP[key]
        # Partial match: if any map key is a substring of the raw category
        for map_key, canonical in CATEGORY_MAP.items():
            if map_key in key:
                return canonical
        # Partial match: if the raw category is a substring of any map key
        for map_key, canonical in CATEGORY_MAP.items():
            if key and key in map_key:
                return canonical
        return "unknown"

    @staticmethod
    def _parse_settlement(
        raw: dict[str, Any], now_utc: datetime
    ) -> tuple[str, float | None]:
        """Return (close_time_str, days_to_settlement).

        Tries `close_time`, then `expiration_time`, then `expected_expiration_time`.
        Returns ("", None) if no parseable date is found.
        """
        for field in ("close_time", "expiration_time", "expected_expiration_time"):
            val = raw.get(field)
            if not val:
                continue
            try:
                # Strip sub-second precision that Python 3.10 can't parse
                clean = re.sub(r"\.\d+Z?$", "Z", str(val)).replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (dt - now_utc).total_seconds() / 86_400
                return (val, days)
            except (ValueError, TypeError):
                continue
        return ("", None)

    def _rank(self, markets: list[ScannedMarket]) -> list[ScannedMarket]:
        """Sort by composite score descending and assign scan_rank."""
        markets.sort(key=lambda m: m.scan_score, reverse=True)
        for i, m in enumerate(markets, start=1):
            object.__setattr__(m, "scan_rank", i) if hasattr(m, "__slots__") else setattr(m, "scan_rank", i)
        return markets

    @staticmethod
    def _log_result(result: ScanResult) -> None:
        logger.info(
            "market_scan_complete  fetched=%d  tradeable=%d  rejected=%d",
            result.total_fetched,
            result.total_tradeable,
            len(result.rejected),
        )
        if result.tradeable:
            top = result.tradeable[0]
            logger.info(
                "market_scan_top  ticker=%s  score=%.3f  vol7d=%.0f  spread=%.1fc  days=%.1f",
                top.ticker, top.scan_score, top.volume_7d,
                top.spread_cents or 0, top.days_to_settlement,
            )
        if result.rejection_summary:
            # Log top 5 rejection reasons
            top_reasons = sorted(
                result.rejection_summary.items(), key=lambda x: x[1], reverse=True
            )[:5]
            for reason, count in top_reasons:
                logger.info("market_scan_rejection  reason=%s  count=%d", reason, count)


# ---------------------------------------------------------------------------
# Module-level pure functions (testable without a KalshiClient)
# ---------------------------------------------------------------------------

def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _first_float(d: dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _compute_spread(yes_bid: int | None, yes_ask: int | None) -> float | None:
    """Bid-ask spread in cents, or None if either side is missing."""
    if yes_bid is None or yes_ask is None:
        return None
    return float(yes_ask - yes_bid)


def _compute_mid(yes_bid: int | None, yes_ask: int | None) -> float:
    """Mid-price in dollars.  Falls back to 0.50 if data unavailable."""
    if yes_bid is not None and yes_ask is not None:
        return (yes_bid + yes_ask) / 200.0   # avg cents → dollars
    if yes_ask is not None:
        return yes_ask / 100.0
    if yes_bid is not None:
        return yes_bid / 100.0
    return 0.50


def _estimate_slippage(raw: dict[str, Any], num_contracts: int) -> float | None:
    """Estimate exit slippage as position_size / available_liquidity.

    The bid-ask spread is already checked separately.  Here we measure whether
    the intended position is small relative to the total outstanding liquidity
    (open_interest × mid_price), which determines whether we can exit without
    moving the market.  Precise slippage from orderbook depth is computed at
    trade-decision time by the orderbook_confirm signal.

    Returns None if there is not enough data to estimate.
    """
    open_interest = int(raw.get("open_interest") or 0)
    yes_bid = _int_or_none(raw.get("yes_bid"))
    yes_ask = _int_or_none(raw.get("yes_ask"))
    if open_interest <= 0 or yes_bid is None or yes_ask is None:
        return None
    mid_cents = (yes_bid + yes_ask) / 2.0
    if mid_cents <= 0:
        return None
    mid_dollars = mid_cents / 100.0
    position_dollars  = num_contracts * mid_dollars
    liquidity_dollars = open_interest * mid_dollars
    return position_dollars / liquidity_dollars


def _composite_score(
    volume_7d: float,
    spread_cents: float | None,
    days: float | None,
) -> float:
    """Return a 0–1 composite ranking score for a passing market.

    Weights:
        60%  normalised 7-day volume   (higher is better, soft-capped at $100K)
        30%  spread tightness          (lower cents spread is better)
        10%  settlement timing         (14–30 day window is optimal)
    """
    # Volume component — log-scale so $10K vs $5K matters but $500K vs $1M doesn't
    import math
    vol_norm = math.log10(max(volume_7d, 1)) / math.log10(100_000)
    vol_score = min(1.0, max(0.0, vol_norm))

    # Spread component — 0¢ spread → 1.0, 8¢ spread → 0.0
    max_spread_c = C.MAX_SPREAD * 100   # 8¢
    if spread_cents is None:
        spread_score = 0.0
    else:
        spread_score = max(0.0, 1.0 - spread_cents / max_spread_c)

    # Timing component — peak at 14–30 days
    if days is None or days <= 0:
        time_score = 0.0
    elif days < 14:
        time_score = days / 14.0
    elif days <= 30:
        time_score = 1.0
    else:
        time_score = max(0.0, 1.0 - (days - 30.0) / 60.0)

    return 0.60 * vol_score + 0.30 * spread_score + 0.10 * time_score


def check_fee_adjusted_edge(
    model_prob: float,
    market_price_dollars: float,
    num_contracts: int,
    is_maker: bool = True,
) -> dict[str, float]:
    """Check whether a model edge survives the round-trip fee drag.

    Args:
        model_prob:           Model's probability estimate (0.0–1.0).
        market_price_dollars: Current market YES price in dollars (0.01–0.99).
        num_contracts:        Intended position size.
        is_maker:             True for limit orders (maker fee).

    Returns a dict with:
        edge_pp           — model-market divergence in percentage points
        gross_pnl         — expected gross P&L in dollars
        rt_fee            — round-trip fee in dollars
        net_pnl           — gross_pnl - rt_fee
        fee_adjusted_edge — net_pnl / (num_contracts * market_price_dollars)
        viable            — True if net_pnl > 0 AND edge_pp >= DIVERGENCE_THRESHOLD
    """
    edge_pp   = (model_prob - market_price_dollars) * 100
    # If edge_pp > 0, strategy is to BUY YES; expected gross P&L:
    # contracts × (model_prob × $1 payout - market_price)
    gross_pnl = num_contracts * (model_prob - market_price_dollars)
    rt_fee    = round_trip_fee(market_price_dollars, num_contracts, is_maker)
    net_pnl   = gross_pnl - rt_fee
    trade_cost = num_contracts * market_price_dollars
    fee_adj_edge = net_pnl / trade_cost if trade_cost > 0 else 0.0

    return {
        "edge_pp":            round(edge_pp, 2),
        "gross_pnl":          round(gross_pnl, 4),
        "rt_fee":             round(rt_fee, 4),
        "net_pnl":            round(net_pnl, 4),
        "fee_adjusted_edge":  round(fee_adj_edge, 4),
        "viable":             net_pnl > 0 and abs(edge_pp) >= C.DIVERGENCE_THRESHOLD,
    }
