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

Why --before-date matters
--------------------------
  Kalshi's settled-market feed is reverse-chronological.  The most recent
  thousands of markets are dominated by sports parlays (MVE* tickers) that
  belong to no supported category.  Passing --before-date skips past this
  firehose by setting a max_close_ts filter so the API only returns markets
  that closed before the given date.  The default (2025-01-01) lands squarely
  in the US-election and economic-data settlement window.

Usage
-----
    python scripts/seed_data.py [--target N] [--max-pages M] [--no-history]
                                [--before-date YYYY-MM-DD]

Options
    --target N           Stop once N supported-category markets are seeded (default 200)
    --max-pages M        Hard page limit for the /markets endpoint (default 50)
    --no-history         Skip per-market /history fetches (faster; no open_price data)
    --before-date DATE   Only fetch markets whose close_time < DATE (default 2025-01-01)
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
_DEFAULT_BEFORE_DATE = "2025-01-01"

# Series-ticker prefixes that are always sports — skip without category lookup.
# Keeps the per-page inner loop fast and the log clean.
_SPORTS_SERIES_PREFIXES: frozenset[str] = frozenset({
    "MVE",    # multi-variable events (sports parlays)
    "NBA",    # NBA game outcomes
    "NFL",    # NFL game outcomes
    "NHL",    # NHL game outcomes
    "MLB",    # MLB game outcomes
    "UFC",    # UFC fights
    "PGA",    # golf
    "WNBA",   # WNBA
    "NCAA",   # college sports
    "FIFA",   # soccer
    "EPL",    # English Premier League
    "MLS",    # Major League Soccer
    "NCAAF",  # college football
    "NCAAB",  # college basketball
})


def _get(path: str, params: dict | None = None) -> dict:
    """GET a public Kalshi endpoint; raise on non-2xx."""
    url = _BASE_URL + path
    resp = requests.get(url, params=params, timeout=_REQ_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fetch_settled_page(
    cursor: str | None = None,
    max_close_ts: int | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one page of settled markets.

    Args:
        cursor:       Pagination cursor from the previous response.
        max_close_ts: Unix timestamp (seconds).  If set, only markets whose
                      close_time < this value are returned.  Use this to skip
                      the recent sports-parlay firehose and land in an era with
                      dense economics / politics settlements.

    Returns (markets_list, next_cursor).  next_cursor is None when exhausted.
    """
    params: dict = {"status": "settled", "limit": _PAGE_LIMIT}
    if cursor:
        params["cursor"] = cursor
    if max_close_ts is not None:
        params["max_close_ts"] = max_close_ts
    data    = _get("/markets", params=params)
    markets = data.get("markets", [])
    nxt     = data.get("cursor") or None
    return markets, nxt


def _fetch_price_history(ticker: str) -> list[dict]:
    """Fetch up to _HISTORY_LIMIT price-history points for a market.

    Returns a list of {"ts": int, "yes_price": float} dicts sorted
    oldest → newest, or [] on error.
    """
    try:
        data = _get(
            f"/markets/{ticker}/history",
            params={"limit": _HISTORY_LIMIT},
        )
        raw = data.get("history", [])
        points = []
        for p in raw:
            ts = p.get("ts") or p.get("timestamp") or 0
            yp = p.get("yes_price") or p.get("yes_bid")
            if ts and yp is not None:
                # API returns integer cents; convert to dollars
                points.append({"ts": int(ts), "yes_price": float(yp) / 100.0})
        return sorted(points, key=lambda x: x["ts"])
    except Exception as exc:
        logger.debug("history_fetch_failed  ticker=%s  error=%s", ticker, exc)
        return []


# ---------------------------------------------------------------------------
# Category normalisation (reuse market_scanner logic)
# ---------------------------------------------------------------------------

def _normalise_category(raw: str) -> str:
    """Map a Kalshi raw category to a PolyEdge canonical category."""
    key = (raw or "").lower().strip()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    for map_key, canonical in CATEGORY_MAP.items():
        if map_key in key or (key and key in map_key):
            return canonical
    return "unknown"


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
    """Return 'yes' | 'no' | None from a settled market dict."""
    result = raw.get("result")
    if isinstance(result, str):
        r = result.lower().strip()
        if r in ("yes", "no"):
            return r

    # Fall back to yes_ask: 100 cents = YES won, 0 cents = NO won
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
    max_pages: int = 50,
    fetch_history: bool = True,
    before_date: str = _DEFAULT_BEFORE_DATE,
) -> int:
    """Fetch settled markets and populate the DB.

    Args:
        target:       Stop once this many supported-category markets are seeded.
        max_pages:    Hard ceiling on paginated API requests.
        fetch_history: Whether to fetch per-market price history.
        before_date:  ISO date string (YYYY-MM-DD).  Only markets whose
                      close_time < this date are fetched.  Defaults to
                      2025-01-01 to skip the recent sports-parlay firehose.

    Returns the number of supported-category markets seeded.
    """
    await init_db()

    async with get_connection() as db:
        await _ensure_extra_columns(db)

    # Convert before_date to a Unix timestamp for the API filter
    try:
        before_dt    = datetime.strptime(before_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        max_close_ts = int(before_dt.timestamp())
    except ValueError:
        logger.warning("Invalid --before-date %r — ignoring date filter", before_date)
        max_close_ts = None

    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor   = None
    total_fetched    = 0
    total_supported  = 0
    total_skipped    = 0
    total_sports     = 0

    logger.info(
        "Starting seed — target=%d  max_pages=%d  before=%s",
        target, max_pages, before_date,
    )

    for page_num in range(1, max_pages + 1):
        logger.info("Fetching page %d (supported so far: %d/%d) …",
                    page_num, total_supported, target)

        try:
            markets, cursor = _fetch_settled_page(cursor, max_close_ts=max_close_ts)
        except requests.HTTPError as exc:
            logger.error("API error on page %d: %s", page_num, exc)
            break

        if not markets:
            logger.info("No more markets returned — stopping pagination.")
            break

        total_fetched += len(markets)

        # ---- Batch DB writes ----
        async with get_connection() as db:
            batch_written = 0

            for raw in markets:
                # ── Early-exit: skip known sports series without category lookup ──
                series_ticker = raw.get("series_ticker", "") or ""
                if any(series_ticker.upper().startswith(pfx) for pfx in _SPORTS_SERIES_PREFIXES):
                    total_sports += 1
                    continue

                ticker_raw = raw.get("ticker", "") or ""
                if any(ticker_raw.upper().startswith(pfx) for pfx in _SPORTS_SERIES_PREFIXES):
                    total_sports += 1
                    continue

                category = _normalise_category(raw.get("category", ""))
                if category not in SUPPORTED_CATEGORIES:
                    total_skipped += 1
                    continue

                result = _extract_result(raw)
                if result is None:
                    # No deterministic outcome — skip (can't backtest without it)
                    total_skipped += 1
                    continue

                ticker = raw.get("ticker", "")
                if not ticker:
                    continue

                # Final settlement prices (0 or 100 cents → 0.0 or 1.0 dollars)
                yes_price = float(raw.get("yes_ask", 0) or 0) / 100.0
                no_price  = float(raw.get("no_ask",  0) or 0) / 100.0

                # Volume — Kalshi volume field is total contracts; approximate dollars
                # using the best available mid-price from before settlement.
                volume_raw = (
                    raw.get("volume")
                    or raw.get("volume_24h")
                    or 0
                )
                try:
                    volume_raw = float(volume_raw)
                except (TypeError, ValueError):
                    volume_raw = 0.0
                # Rough conversion: multiply by $0.50 midpoint assumption for
                # contracts that were open during the market's life.
                volume_7d = round(volume_raw * 0.50, 2)

                # Price history
                history_json = None
                open_price   = None

                if fetch_history and ticker:
                    time.sleep(_SLEEP_BETWEEN)
                    history = _fetch_price_history(ticker)
                    if history:
                        # open_price = earliest price that was actually mid-market
                        # (skip trivially boundary values 0.0 and 1.0)
                        for pt in history:
                            yp = pt["yes_price"]
                            if 0.02 <= yp <= 0.98:
                                open_price = round(yp, 4)
                                break
                        history_json = json.dumps(history)

                # If no valid open_price from history, estimate from category prior
                if open_price is None:
                    # Will be imputed in backtest from seeded priors ± noise
                    open_price = None

                row = {
                    "ticker":          ticker,
                    "title":           raw.get("title", ""),
                    "series":          raw.get("series_ticker", ""),
                    "event_id":        raw.get("event_ticker", ""),
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
            "Page %d done — batch=%d  supported_total=%d  fetched_total=%d",
            page_num, batch_written, total_supported, total_fetched,
        )

        if total_supported >= target:
            logger.info("Reached target of %d supported markets — stopping.", target)
            break

        if cursor is None:
            logger.info("No more pages available.")
            break

    logger.info(
        "\nSeeding complete\n"
        "  Total API markets fetched : %d\n"
        "  Supported & seeded        : %d\n"
        "  Skipped (excl. category)  : %d",
        total_fetched, total_supported, total_skipped,
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
        "--max-pages", type=int, default=25,
        help="Hard ceiling on paginated API calls (default 25).",
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
            "Try increasing --max-pages or check the API.",
            seeded, args.target,
        )
        sys.exit(0)   # not a hard failure — backtest will work with what's there
    sys.exit(0)
