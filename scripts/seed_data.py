"""
scripts/seed_data.py — Seed the markets table with 200+ settled Kalshi markets.

Fetches settled markets from the Kalshi public REST API (no auth required),
enriches each with a brief price-history snapshot, and upserts all records
into the local SQLite database.

What it stores (per market)
---------------------------
  ticker, title, series, event_id, category (PolyEdge canonical), settlement_date,
  status='settled', yes_price (final settlement price), no_price,
  volume_7d, last_updated, open_price (earliest known price — used as backtest entry),
  result ('yes'|'no'), price_history (JSON array of {ts, yes_price} snapshots)

The three extra columns (open_price, result, price_history) are added via
ALTER TABLE if they do not yet exist in the schema.

Events-first strategy
----------------------
  The /markets?status=settled endpoint no longer populates the ``category``
  or ``series_ticker`` fields, making category-based filtering impossible.
  Instead we use /events?status=settled&with_nested_markets=true, which:
    • Returns the event-level ``category`` field (Politics, Economics, etc.)
    • Includes child markets and their outcomes inline — no extra API calls
  We map Kalshi event categories to PolyEdge canonical categories and skip
  events in unsupported categories (sports, entertainment, etc.) outright.

Usage
-----
    python scripts/seed_data.py [--target N] [--max-pages M] [--no-history]

Options
    --target N      Stop once N supported-category markets are seeded (default 200)
    --max-pages M   Hard page limit for the /events endpoint (default 15)
    --no-history    Skip per-market /history fetches (faster; no open_price data)
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Path setup — allow running from the repo root or the scripts/ subdirectory
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from data.market_scanner import CATEGORY_MAP, SUPPORTED_CATEGORIES
from persistence.database import get_connection, init_db

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed_data")

# ---------------------------------------------------------------------------
# Kalshi public API (no auth)
# ---------------------------------------------------------------------------
_BASE_URL      = "https://api.elections.kalshi.com/trade-api/v2"
_PAGE_LIMIT    = 200      # max items per page
_HISTORY_LIMIT = 60       # price-history points per market
_REQ_TIMEOUT   = 15       # seconds per HTTP request
_SLEEP_BETWEEN = 0.08     # seconds between history fetches (~12/sec, well within 20/sec)

# Default cutoff: only fetch markets that closed before 2025-01-01 UTC.
# The Kalshi settled feed is reverse-chronological; the most recent pages are
# dominated by sports parlays (MVE* series).  This date lands in the US-election
# and economic-data settlement window where supported-category markets are dense.
# Maps Kalshi event-level category strings (returned by /events) to PolyEdge
# canonical categories.  This is the primary category source — it supersedes
# the ticker-prefix inference used as a fallback.
_EVENT_CATEGORY_MAP: dict[str, str] = {
    "politics":              "politics",
    "elections":             "politics",
    "economics":             "economics",
    "financials":            "economics",
    "macro":                 "macro",
    "crypto":                "tech",
    "science and technology":"tech",
    "technology":            "tech",
    "regulatory":            "regulatory",
    # Explicitly unsupported — will be skipped
    "sports":                "sports",
    "entertainment":         "entertainment",
    "health":                "health",
    "social":                "social",
    "world":                 "world",
    "companies":             "companies",
}


def _get(path: str, params: dict | None = None) -> dict:
    """GET a public Kalshi endpoint; raise on non-2xx."""
    url = _BASE_URL + path
    resp = requests.get(url, params=params, timeout=_REQ_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fetch_settled_events_page(
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one page of settled events with nested markets.

    Uses ``with_nested_markets=true`` so each event dict already contains its
    child market outcomes — avoiding a per-market fetch and giving us the
    event-level ``category`` field that the /markets endpoint no longer populates.

    Returns (events_list, next_cursor).  next_cursor is None when exhausted.
    """
    params: dict = {
        "status":               "settled",
        "limit":                _PAGE_LIMIT,
        "with_nested_markets":  "true",
    }
    if cursor:
        params["cursor"] = cursor
    data   = _get("/events", params=params)
    events = data.get("events", [])
    nxt    = data.get("cursor") or None
    return events, nxt


def _fetch_price_history(ticker: str) -> list[dict]:
    """Fetch up to _HISTORY_LIMIT price-history points for a market.

    Handles both API formats:
      - New (dollars): yes_price_dollars / yes_bid_dollars as string floats
      - Old (cents):   yes_price / yes_bid as integers

    Returns a list of {"ts": int, "yes_price": float} dicts sorted oldest → newest.
    """
    try:
        data = _get(
            f"/markets/{ticker}/history",
            params={"limit": _HISTORY_LIMIT},
        )
        raw_points = data.get("history", [])
        points = []
        for p in raw_points:
            ts = p.get("ts") or p.get("timestamp") or 0
            if not ts:
                continue
            # Try dollar-format fields first (new API), then cent-format (old API)
            yp = None
            for field in ("yes_price_dollars", "yes_bid_dollars", "yes_ask_dollars"):
                v = p.get(field)
                if v is not None:
                    try:
                        yp = float(v)
                    except (TypeError, ValueError):
                        pass
                    break
            if yp is None:
                for field in ("yes_price", "yes_bid"):
                    v = p.get(field)
                    if v is not None:
                        try:
                            yp = float(v) / 100.0
                        except (TypeError, ValueError):
                            pass
                        break
            if yp is not None:
                points.append({"ts": int(ts), "yes_price": yp})
        return sorted(points, key=lambda x: x["ts"])
    except Exception as exc:
        logger.debug("history_fetch_failed  ticker=%s  error=%s", ticker, exc)
        return []


# ---------------------------------------------------------------------------
# Price / volume parsing helpers (dual API format support)
# ---------------------------------------------------------------------------

def _parse_yes_price(raw: dict) -> float:
    """Return yes settlement price as a dollar float (0.0–1.0).

    New API: yes_ask_dollars / settlement_value_dollars (string, e.g. "1.0000")
    Old API: yes_ask (integer cents, e.g. 100)
    """
    # settlement_value_dollars is the most reliable for finalized markets
    for field in ("settlement_value_dollars",):
        v = raw.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    # yes_ask_dollars / no_ask_dollars
    for field in ("yes_ask_dollars", "last_price_dollars"):
        v = raw.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    # Cent fallback
    for field in ("yes_ask", "last_price"):
        v = raw.get(field)
        if v is not None:
            try:
                return float(v) / 100.0
            except (TypeError, ValueError):
                pass
    return 0.0


def _parse_volume(raw: dict) -> float:
    """Return trade volume as a float (contract count)."""
    for field in ("volume_fp", "volume", "volume_24h_fp", "volume_24h"):
        v = raw.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


# ---------------------------------------------------------------------------
# Category normalisation — API field + ticker-prefix fallback
# ---------------------------------------------------------------------------

# Maps the series prefix extracted from event_ticker / ticker to a PolyEdge
# canonical category.  Keys are uppercase series prefixes.
_SERIES_PREFIX_CATEGORY: dict[str, str] = {
    # Economics / financial
    "KXWTI":    "macro",       # WTI crude oil
    "KXBRENT":  "macro",       # Brent crude
    "KXGAS":    "macro",       # natural gas
    "KXGOLD":   "macro",       # gold
    "KXSILVER": "macro",       # silver
    "KXDXY":    "macro",       # US dollar index
    "KXINX":    "economics",   # S&P 500 (INX)
    "KXNDAQ":   "economics",   # NASDAQ
    "KXDJI":    "economics",   # Dow Jones
    "KXFED":    "economics",   # Fed funds rate
    "KXCPI":    "economics",   # CPI / inflation
    "KXPCE":    "economics",   # PCE inflation
    "KXGDP":    "economics",   # GDP
    "KXUNEM":   "economics",   # unemployment
    "KXPAYROLL":"economics",   # nonfarm payrolls
    "KXJOBS":   "economics",   # jobs report
    "KXHOUSING":"economics",   # housing data
    "KXRETAIL": "economics",   # retail sales
    "INX":      "economics",   # S&P 500 direct
    "NASDAQ":   "economics",   # NASDAQ direct
    "INXD":     "economics",   # S&P 500 daily
    # Tech / crypto
    "KXBTC":    "tech",        # Bitcoin
    "KXETH":    "tech",        # Ethereum
    "KXSOL":    "tech",        # Solana
    "KXXRP":    "tech",        # XRP
    "KXDOGE":   "tech",        # Dogecoin
    "KXBNB":    "tech",        # BNB
    "BTCUSD":   "tech",
    "ETHUSD":   "tech",
    # Politics
    "PRES":     "politics",    # presidential
    "KXTRUMP":  "politics",
    "KXBIDEN":  "politics",
    "KXHARRIS": "politics",
    "KXELECT":  "politics",
    "KXGOV":    "politics",
    "KXPOL":    "politics",
    "CONGRESS": "politics",
    "SENATE":   "politics",
    "HOUSE":    "politics",
    "KXSEN":    "politics",    # senate seat
    "KXREP":    "politics",    # house seat
    "KXPRIMARY":"politics",
    "KXAPPROVAL":"politics",
    # Regulatory
    "KXCFTC":   "regulatory",
    "KXSEC":    "regulatory",
    "KXREG":    "regulatory",
    "KXFDA":    "regulatory",
    "KXDOJ":    "regulatory",
}


def _category_from_ticker(ticker: str, event_ticker: str) -> str:
    """Infer PolyEdge category from ticker / event_ticker when category is null.

    Extracts the series prefix (everything before the first '-') from
    event_ticker (preferred) or ticker, then looks it up in _SERIES_PREFIX_CATEGORY.
    Falls back to keyword scanning of the full ticker string.
    """
    for candidate in (event_ticker or "", ticker or ""):
        prefix = candidate.split("-")[0].upper().strip()
        if prefix in _SERIES_PREFIX_CATEGORY:
            return _SERIES_PREFIX_CATEGORY[prefix]

    # Keyword scan of full strings (handles e.g. "PRESWIN2024-TRUMP")
    combined = f"{ticker} {event_ticker}".upper()
    if any(k in combined for k in ("TRUMP", "BIDEN", "HARRIS", "ELECTION", "SENATE",
                                    "HOUSE", "CONGRESS", "PRESIDENT", "GOVERNOR",
                                    "PRES", "KXGOV", "KXSEN")):
        return "politics"
    if any(k in combined for k in ("BTC", "ETH", "CRYPTO", "SOL", "DOGE", "XRP")):
        return "tech"
    if any(k in combined for k in ("FED", "CPI", "GDP", "PAYROLL", "JOBS", "UNEM",
                                    "RATE", "INFLATION", "INX", "S&P", "NASDAQ",
                                    "DOW", "WTI", "OIL", "GOLD", "SILVER")):
        return "economics"
    if any(k in combined for k in ("CFTC", "SEC", "FDA", "DOJ", "REGULATION", "RULE")):
        return "regulatory"
    return "unknown"


def _normalise_category(raw_category: str, ticker: str = "", event_ticker: str = "") -> str:
    """Map a Kalshi raw category to a PolyEdge canonical category.

    Priority order:
      1. _EVENT_CATEGORY_MAP  — direct match on the event category string
         (returned by /events endpoint; e.g. "Politics", "Economics")
      2. CATEGORY_MAP         — market_scanner legacy map (e.g. "financials")
      3. _category_from_ticker — ticker-prefix inference as final fallback
    """
    key = (raw_category or "").lower().strip()
    if key:
        if key in _EVENT_CATEGORY_MAP:
            return _EVENT_CATEGORY_MAP[key]
        if key in CATEGORY_MAP:
            return CATEGORY_MAP[key]
        for map_key, canonical in CATEGORY_MAP.items():
            if map_key in key or (key and key in map_key):
                return canonical

    # API category field was empty — infer from ticker prefix
    return _category_from_ticker(ticker, event_ticker)


# ---------------------------------------------------------------------------
# Settlement date extraction
# ---------------------------------------------------------------------------

def _settlement_date(raw: dict) -> str:
    """Return the best settlement date string in YYYY-MM-DD format."""
    for field in ("settle_time", "close_time", "expiration_time", "expected_expiration_time"):
        val = raw.get(field)
        if val:
            try:
                clean = str(val)[:19].replace("T", " ")
                dt    = datetime.fromisoformat(clean)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------

def _extract_result(raw: dict) -> str | None:
    """Return 'yes' | 'no' | None from a settled/finalized market dict.

    Handles both API formats:
      New API: result field is "yes"/"no"; settlement_value_dollars is "1.0000"/"0.0000"
      Old API: result field is "yes"/"no"; yes_ask is integer cents (100 = yes, 0 = no)
    """
    result = raw.get("result")
    if isinstance(result, str):
        r = result.lower().strip()
        if r in ("yes", "no"):
            return r

    # New API: settlement_value_dollars — "1.0000" = YES won, "0.0000" = NO won
    svd = raw.get("settlement_value_dollars")
    if svd is not None:
        try:
            v = float(svd)
            if v >= 0.99:
                return "yes"
            if v <= 0.01:
                return "no"
        except (TypeError, ValueError):
            pass

    # Old API: yes_ask integer cents — 100 = YES won, 0 = NO won
    yes_ask = raw.get("yes_ask")
    if yes_ask is not None:
        try:
            ya = int(yes_ask)
            if ya >= 99:
                return "yes"
            if ya <= 1:
                return "no"
        except (TypeError, ValueError):
            pass

    # New API: yes_ask_dollars — "1.0000" = YES won, "0.0200" = NO won (residual ask)
    for field in ("yes_ask_dollars", "last_price_dollars"):
        v = raw.get(field)
        if v is not None:
            try:
                fv = float(v)
                if fv >= 0.99:
                    return "yes"
                if fv <= 0.01:
                    return "no"
            except (TypeError, ValueError):
                pass

    return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _ensure_extra_columns(db) -> None:
    """Add open_price, result, price_history columns if they don't exist yet."""
    for col, defn in [
        ("open_price",    "REAL"),
        ("result",        "TEXT"),
        ("price_history", "TEXT"),
    ]:
        try:
            await db.execute(f"ALTER TABLE markets ADD COLUMN {col} {defn}")
            await db.commit()
            logger.info("Added column markets.%s", col)
        except Exception:
            pass  # column already exists — that's fine


async def _upsert_market(db, row: dict) -> None:
    await db.execute(
        """
        INSERT INTO markets
            (ticker, title, series, event_id, category, settlement_date, status,
             yes_price, no_price, volume_7d, last_updated,
             open_price, result, price_history)
        VALUES
            (:ticker, :title, :series, :event_id, :category, :settlement_date, :status,
             :yes_price, :no_price, :volume_7d, :last_updated,
             :open_price, :result, :price_history)
        ON CONFLICT(ticker) DO UPDATE SET
            title           = excluded.title,
            series          = excluded.series,
            event_id        = excluded.event_id,
            category        = excluded.category,
            settlement_date = excluded.settlement_date,
            status          = excluded.status,
            yes_price       = excluded.yes_price,
            no_price        = excluded.no_price,
            volume_7d       = excluded.volume_7d,
            last_updated    = excluded.last_updated,
            open_price      = excluded.open_price,
            result          = excluded.result,
            price_history   = excluded.price_history
        """,
        row,
    )


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

async def seed(
    target: int = 200,
    max_pages: int = 15,
    fetch_history: bool = True,
) -> int:
    """Fetch settled events+markets from Kalshi and populate the DB.

    Uses /events?status=settled&with_nested_markets=true so we get:
      • event-level category (Politics, Economics, etc.)
      • child market outcomes inline — no extra per-market API calls

    Returns the number of supported-category markets seeded.
    """
    await init_db()

    async with get_connection() as db:
        await _ensure_extra_columns(db)

    now_str         = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor          = None
    total_events    = 0
    total_markets   = 0
    total_supported = 0
    total_skipped   = 0

    logger.info("Starting seed — target=%d  max_pages=%d", target, max_pages)

    for page_num in range(1, max_pages + 1):
        logger.info("Fetching events page %d (supported markets so far: %d/%d) …",
                    page_num, total_supported, target)

        try:
            events, cursor = _fetch_settled_events_page(cursor)
        except requests.HTTPError as exc:
            logger.error("API error on page %d: %s", page_num, exc)
            break

        if not events:
            logger.info("No more events returned — stopping pagination.")
            break

        total_events += len(events)

        async with get_connection() as db:
            batch_written = 0

            for event in events:
                event_ticker    = event.get("event_ticker", "") or ""
                event_category  = event.get("category", "") or ""
                event_title     = event.get("title", "") or ""

                # Map event category to PolyEdge canonical
                category = _normalise_category(
                    event_category,
                    ticker=event_ticker,
                    event_ticker=event_ticker,
                )
                if category not in SUPPORTED_CATEGORIES:
                    continue   # silently skip unsupported categories

                nested = event.get("markets") or []
                if not nested:
                    continue   # no child markets inline — skip

                for raw in nested:
                    total_markets += 1
                    ticker = raw.get("ticker", "") or ""
                    if not ticker:
                        continue

                    result = _extract_result(raw)
                    if result is None:
                        total_skipped += 1
                        continue

                    yes_price = _parse_yes_price(raw)
                    no_price  = round(1.0 - yes_price, 4)
                    volume_7d = round(_parse_volume(raw) * 0.50, 2)
                    series    = event_ticker.split("-")[0]

                    history_json = None
                    open_price   = None

                    if fetch_history:
                        time.sleep(_SLEEP_BETWEEN)
                        history = _fetch_price_history(ticker)
                        if history:
                            for pt in history:
                                yp = pt["yes_price"]
                                if 0.02 <= yp <= 0.98:
                                    open_price = round(yp, 4)
                                    break
                            history_json = json.dumps(history)

                    row = {
                        "ticker":          ticker,
                        "title":           raw.get("title", "") or event_title,
                        "series":          series,
                        "event_id":        event_ticker,
                        "category":        category,
                        "settlement_date": _settlement_date(raw),
                        "status":          "settled",
                        "yes_price":       yes_price,
                        "no_price":        no_price,
                        "volume_7d":       volume_7d,
                        "last_updated":    now_str,
                        "open_price":      open_price,
                        "result":          result,
                        "price_history":   history_json,
                    }

                    await _upsert_market(db, row)
                    batch_written += 1
                    total_supported += 1

            await db.commit()

        logger.info(
            "Page %d done — events=%d  batch=%d  supported_total=%d",
            page_num, len(events), batch_written, total_supported,
        )

        if total_supported >= target:
            logger.info("Reached target of %d supported markets — stopping.", target)
            break

        if cursor is None:
            logger.info("No more pages available.")
            break

    logger.info(
        "\nSeeding complete\n"
        "  Events fetched            : %d\n"
        "  Child markets seen        : %d\n"
        "  Supported & seeded        : %d\n"
        "  Skipped (no outcome)      : %d",
        total_events, total_markets, total_supported, total_skipped,
    )

    return total_supported


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed the markets table with settled Kalshi markets."
    )
    p.add_argument(
        "--target", type=int, default=200,
        help="Stop after seeding this many supported-category markets (default 200).",
    )
    p.add_argument(
        "--max-pages", type=int, default=15,
        help="Hard ceiling on paginated /events API calls (default 15).",
    )
    p.add_argument(
        "--no-history", action="store_true",
        help="Skip per-market price-history fetches (faster but no open_price).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args   = _parse_args()
    seeded = asyncio.run(
        seed(
            target=args.target,
            max_pages=args.max_pages,
            fetch_history=not args.no_history,
        )
    )
    if seeded < args.target:
        logger.warning(
            "Only %d supported markets seeded (target was %d). "
            "Try increasing --max-pages.",
            seeded, args.target,
        )
        sys.exit(0)
    sys.exit(0)
