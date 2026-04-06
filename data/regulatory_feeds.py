"""
Regulatory RSS feed poller for PolyEdge v5.

Polls the Federal Register and SEC EDGAR RSS feeds every
REGULATORY_SCAN_INTERVAL seconds (default 30 min) looking for items that
could materially affect Kalshi or prediction markets.

Flagging logic
--------------
Each item is scanned against two keyword tiers:

  CRITICAL keywords  — direct regulatory action on prediction markets
      e.g. "kalshi", "prediction market", "event contract", "binary option"
      → alert_level = "critical"

  WARNING keywords   — related regulatory activity that warrants monitoring
      e.g. "cftc", "derivatives", "gambling", "designated contract market"
      → alert_level = "warning"

An item can only be one level (critical takes precedence over warning).
Items with no keyword match are discarded.

Usage
-----
    poller = RegulatoryPoller()

    # One-shot:
    alerts = await poller.fetch_all()

    # Long-running loop:
    async for batch in poller.poll():
        for alert in batch:
            if alert.alert_level == "critical":
                await notify_slack(alert)
"""

import asyncio
import dataclasses
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator

import aiohttp
import feedparser

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

REGULATORY_FEEDS: list[tuple[str, str]] = [
    (
        "FederalRegister",
        "https://www.federalregister.gov/documents/search.rss"
        "?conditions%5Bagencies%5D%5B%5D=commodity-futures-trading-commission"
        "&conditions%5Btype%5D%5B%5D=RULE"
        "&conditions%5Btype%5D%5B%5D=PROPOSED_RULE"
        "&conditions%5Btype%5D%5B%5D=NOTICE",
    ),
    (
        "FederalRegisterAll",
        "https://www.federalregister.gov/documents/search.rss"
        "?conditions%5Bterm%5D=prediction+market",
    ),
    (
        "SEC_EDGAR",
        "https://efts.sec.gov/LATEST/search-index?q=%22prediction+market%22"
        "&dateRange=custom&startdt=2020-01-01&forms=33-AK,34-12B,34-15D",
    ),
    (
        "SEC_EDGAR_Rules",
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcompany&type=&dateb=&owner=include&count=40&search_text=&action=getcompany",
    ),
]

# Simpler, more reliable feed URLs for actual use
_FEEDS: list[tuple[str, str]] = [
    (
        "FederalRegister_CFTC",
        "https://www.federalregister.gov/documents/search.rss"
        "?conditions%5Bagencies%5D%5B%5D=commodity-futures-trading-commission",
    ),
    (
        "FederalRegister_PredMkt",
        "https://www.federalregister.gov/documents/search.rss"
        "?conditions%5Bterm%5D=prediction+market",
    ),
    (
        "SEC_EDGAR_News",
        "https://efts.sec.gov/LATEST/search-index?q=%22event+contract%22&forms=34-12B",
    ),
]

# ---------------------------------------------------------------------------
# Keyword tiers
# ---------------------------------------------------------------------------

#: Items matching ANY of these → alert_level = "critical"
CRITICAL_KEYWORDS: list[str] = [
    "kalshi",
    "prediction market",
    "prediction markets",
    "event contract",
    "event contracts",
    "binary option",
    "binary options",
    "event-based contract",
]

#: Items matching ANY of these (but none of the critical set) → "warning"
WARNING_KEYWORDS: list[str] = [
    "cftc",
    "commodity futures trading commission",
    "designated contract market",
    "dcm",
    "derivatives",
    "swap dealer",
    "futures commission merchant",
    "retail commodity transaction",
    "dodd-frank",
    "margin requirement",
    "speculative limit",
    "position limit",
    "market manipulation",
    "wash trading",
    "prediction",
    "gambling",
    "sports betting",
    "polymarket",
    "forecasting market",
]

# ---------------------------------------------------------------------------
# Structured output type
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class RegulatoryAlert:
    """A regulatory filing flagged as relevant to prediction markets."""
    title:          str
    source:         str           # feed label, e.g. "FederalRegister_CFTC"
    url:            str
    summary:        str           # cleaned plain text, up to 800 chars
    published_time: str           # ISO-8601 or empty
    fetched_at:     float         # time.time()
    alert_level:    str           # "critical" | "warning"
    matched_keywords: list[str]   # which keywords triggered the flag
    uid:            str           # stable hash of (source, url)


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class _CacheEntry:
    alerts:     list[RegulatoryAlert]
    fetched_at: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid(source: str, url: str) -> str:
    return hashlib.sha1(f"{source}:{url}".encode()).hexdigest()[:16]


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _classify(title: str, summary: str) -> tuple[str, list[str]] | None:
    """Return (alert_level, matched_keywords) or None if no match.

    Searches both title and summary (case-insensitive).
    """
    haystack = (title + " " + summary).lower()

    critical_hits = [kw for kw in CRITICAL_KEYWORDS if kw in haystack]
    if critical_hits:
        return ("critical", critical_hits)

    warning_hits = [kw for kw in WARNING_KEYWORDS if kw in haystack]
    if warning_hits:
        return ("warning", warning_hits)

    return None


def _parse_feed(source: str, raw_text: str) -> list[RegulatoryAlert]:
    """Parse raw feed text and return only keyword-matched alerts."""
    parsed = feedparser.parse(raw_text)
    now = time.time()
    alerts: list[RegulatoryAlert] = []

    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        url = entry.get("link") or entry.get("id") or ""

        summary_raw = (
            entry.get("summary")
            or entry.get("description")
            or entry.get("content", [{}])[0].get("value", "")
        )
        summary = _clean_html(summary_raw)[:800]

        pub = ""
        for field in ("published", "updated", "created"):
            val = entry.get(field)
            if val:
                pub = val
                break

        result = _classify(title, summary)
        if result is None:
            continue  # not relevant

        alert_level, matched = result
        alerts.append(RegulatoryAlert(
            title=title,
            source=source,
            url=url,
            summary=summary,
            published_time=pub,
            fetched_at=now,
            alert_level=alert_level,
            matched_keywords=matched,
            uid=_uid(source, url),
        ))

    return alerts


# ---------------------------------------------------------------------------
# RegulatoryPoller
# ---------------------------------------------------------------------------

class RegulatoryPoller:
    """Async poller for CFTC/SEC regulatory feeds with keyword classification.

    Args:
        feeds:           List of (label, url) pairs.  Defaults to _FEEDS.
        poll_interval:   Seconds between polls.
                         Defaults to constants.REGULATORY_SCAN_INTERVAL (1800).
        cache_max_age:   Seconds before cached results are considered stale.
                         Defaults to constants.RSS_CACHE_MAX_AGE_SECONDS (3600).
        request_timeout: Per-feed HTTP timeout in seconds.
    """

    def __init__(
        self,
        feeds: list[tuple[str, str]] | None = None,
        poll_interval: int = C.REGULATORY_SCAN_INTERVAL,
        cache_max_age: int = C.RSS_CACHE_MAX_AGE_SECONDS,
        request_timeout: float = 20.0,
    ) -> None:
        self._feeds          = feeds or _FEEDS
        self._poll_interval  = poll_interval
        self._cache_max_age  = cache_max_age
        self._request_timeout = request_timeout
        self._cache: dict[str, _CacheEntry] = {}
        self._stopped: bool = False

        logger.info(
            "RegulatoryPoller init  feeds=%d  interval=%ds",
            len(self._feeds), self._poll_interval,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_all(self) -> list[RegulatoryAlert]:
        """Fetch all regulatory feeds concurrently and return flagged alerts.

        Deduplicates by uid across feeds.  Falls back to cached data on error.
        """
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout),
            headers={"User-Agent": "PolyEdge/5.0 (regulatory monitor)"},
        ) as session:
            tasks = [
                self._fetch_one(session, source, url)
                for source, url in self._feeds
            ]
            per_feed: list[list[RegulatoryAlert]] = await asyncio.gather(*tasks)

        seen_uids: set[str] = set()
        merged: list[RegulatoryAlert] = []
        for batch in per_feed:
            for alert in batch:
                if alert.uid not in seen_uids:
                    seen_uids.add(alert.uid)
                    merged.append(alert)

        # Sort: critical first, then by fetched_at descending
        merged.sort(key=lambda a: (0 if a.alert_level == "critical" else 1, -a.fetched_at))

        critical = sum(1 for a in merged if a.alert_level == "critical")
        warning  = sum(1 for a in merged if a.alert_level == "warning")
        logger.info(
            "regulatory_fetch_all  total=%d  critical=%d  warning=%d",
            len(merged), critical, warning,
        )
        return merged

    async def poll(self) -> AsyncIterator[list[RegulatoryAlert]]:
        """Async generator that yields alert batches every poll_interval seconds.

        Usage::

            async for batch in poller.poll():
                for alert in batch:
                    if alert.alert_level == "critical":
                        await page_operator(alert)
        """
        self._stopped = False
        while not self._stopped:
            t0 = time.monotonic()
            try:
                batch = await self.fetch_all()
                yield batch
            except Exception as exc:  # noqa: BLE001
                logger.error("regulatory_poll_error  error=%s", exc)
                yield []

            elapsed = time.monotonic() - t0
            wait = max(0.0, self._poll_interval - elapsed)
            if not self._stopped:
                await asyncio.sleep(wait)

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def cached_alerts(self, source: str | None = None) -> list[RegulatoryAlert]:
        """Return cached alerts for ``source`` or all feeds if None."""
        if source is not None:
            entry = self._cache.get(source)
            return entry.alerts if entry else []
        results: list[RegulatoryAlert] = []
        for entry in self._cache.values():
            results.extend(entry.alerts)
        return results

    def cache_ages(self) -> dict[str, float | None]:
        now = time.time()
        return {
            source: round(now - self._cache[source].fetched_at, 1)
            if source in self._cache else None
            for source, _ in self._feeds
        }

    # ------------------------------------------------------------------
    # Internal — single feed fetch
    # ------------------------------------------------------------------

    async def _fetch_one(
        self, session: aiohttp.ClientSession, source: str, url: str
    ) -> list[RegulatoryAlert]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                raw_text = await resp.text(errors="replace")

            alerts = _parse_feed(source, raw_text)
            self._cache[source] = _CacheEntry(alerts=alerts, fetched_at=time.time())
            logger.info(
                "regulatory_feed_ok  source=%s  flagged=%d", source, len(alerts)
            )
            return alerts

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "regulatory_feed_error  source=%s  error=%s", source, exc
            )
            return self._fallback_cache(source)

    def _fallback_cache(self, source: str) -> list[RegulatoryAlert]:
        entry = self._cache.get(source)
        if not entry:
            return []
        age = time.time() - entry.fetched_at
        if age > self._cache_max_age:
            logger.warning(
                "regulatory_cache_stale  source=%s  age=%.0fs", source, age
            )
            return []
        logger.info(
            "regulatory_using_cache  source=%s  age=%.0fs  items=%d",
            source, age, len(entry.alerts),
        )
        return entry.alerts


# ---------------------------------------------------------------------------
# Convenience: classify arbitrary text
# ---------------------------------------------------------------------------

def classify_text(title: str, body: str = "") -> tuple[str, list[str]] | None:
    """Public wrapper around _classify for use by other modules.

    Returns (alert_level, matched_keywords) or None if not relevant.
    Useful for classifying news headlines against the regulatory keyword set.
    """
    return _classify(title, body)
