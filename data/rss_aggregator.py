"""
Async RSS news feed aggregator for PolyEdge v5.

Polls Reuters, AP, BBC, and NPR RSS feeds every RSS_SCAN_INTERVAL seconds
(default 10 min).  Each successful poll result is cached per-feed; if a fetch
fails the cached version is returned provided it is younger than
RSS_CACHE_MAX_AGE_SECONDS (default 1 hour).  Results from all feeds are merged,
deduplicated by title similarity, and returned as a list of Headline dataclasses.

Deduplication
-------------
Two headlines are considered duplicates when their normalised titles share
Jaccard similarity >= DEDUP_THRESHOLD (default 0.55).  Normalisation strips
punctuation, lowercases, and removes common stop-words so
"Fed Raises Rates 0.25%" and "Federal Reserve raises rates by 25bps"
collapse to the same story.

Usage
-----
    agg = RssAggregator()

    # One-shot fetch (used in tests / seed scripts):
    headlines = await agg.fetch_all()

    # Long-running polling loop (used by engine):
    async for batch in agg.poll():
        process(batch)          # called every RSS_SCAN_INTERVAL seconds
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

#: List of (source_label, feed_url) pairs.
NEWS_FEEDS: list[tuple[str, str]] = [
    ("Reuters",  "https://feeds.reuters.com/reuters/topNews"),
    ("AP",       "https://feeds.apnews.com/rss/apf-topnews"),
    ("BBC",      "https://feeds.bbci.co.uk/news/rss.xml"),
    ("NPR",      "https://feeds.npr.org/1001/rss.xml"),
]

# ---------------------------------------------------------------------------
# Deduplication constants
# ---------------------------------------------------------------------------
DEDUP_THRESHOLD: float = 0.55   # Jaccard similarity above which two titles are dupes
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "as", "its", "it", "this", "that", "will", "says",
    "say", "said", "report", "reports", "new", "after", "up", "over", "out",
})

# ---------------------------------------------------------------------------
# Structured output type
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Headline:
    """A single news item returned by the aggregator."""
    title:          str
    source:         str           # "Reuters" | "AP" | "BBC" | "NPR"
    url:            str
    summary:        str           # may be empty string
    published_time: str           # ISO-8601 string or empty
    fetched_at:     float         # time.time() when fetched
    uid:            str           # stable hash of (source, url)


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class _CacheEntry:
    headlines:  list[Headline]
    fetched_at: float   # time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_title(title: str) -> set[str]:
    """Lowercase, strip punctuation, remove stop-words → set of tokens."""
    tokens = re.sub(r"[^a-z0-9\s]", "", title.lower()).split()
    return {t for t in tokens if t not in _STOP_WORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_duplicate(candidate: Headline, accepted: list[Headline]) -> bool:
    """Return True if candidate is similar to any already-accepted headline."""
    c_tokens = _normalise_title(candidate.title)
    for existing in accepted:
        e_tokens = _normalise_title(existing.title)
        if _jaccard(c_tokens, e_tokens) >= DEDUP_THRESHOLD:
            return True
    return False


def _uid(source: str, url: str) -> str:
    return hashlib.sha1(f"{source}:{url}".encode()).hexdigest()[:16]


def _parse_feed(source: str, raw_text: str) -> list[Headline]:
    """Parse a raw RSS/Atom string into Headline objects via feedparser."""
    parsed = feedparser.parse(raw_text)
    now = time.time()
    results: list[Headline] = []

    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        url = entry.get("link") or entry.get("id") or ""

        # published time — try several feedparser field names
        pub = ""
        for field in ("published", "updated", "created"):
            val = entry.get(field)
            if val:
                pub = val
                break

        summary_raw = entry.get("summary") or entry.get("description") or ""
        # Strip HTML tags from summary
        summary = re.sub(r"<[^>]+>", " ", summary_raw).strip()
        summary = re.sub(r"\s+", " ", summary)[:500]

        results.append(Headline(
            title=title,
            source=source,
            url=url,
            summary=summary,
            published_time=pub,
            fetched_at=now,
            uid=_uid(source, url),
        ))

    return results


# ---------------------------------------------------------------------------
# RssAggregator
# ---------------------------------------------------------------------------

class RssAggregator:
    """Async RSS polling aggregator with per-feed caching and deduplication.

    Args:
        feeds:              List of (source_label, url) pairs.
                            Defaults to NEWS_FEEDS.
        poll_interval:      Seconds between full poll cycles.
                            Defaults to constants.RSS_SCAN_INTERVAL (600).
        cache_max_age:      Seconds before a cached result is considered too
                            stale to use on feed failure.
                            Defaults to constants.RSS_CACHE_MAX_AGE_SECONDS (3600).
        request_timeout:    Per-feed HTTP request timeout in seconds.
        dedup_threshold:    Jaccard similarity threshold for deduplication.
    """

    def __init__(
        self,
        feeds: list[tuple[str, str]] | None = None,
        poll_interval: int = C.RSS_SCAN_INTERVAL,
        cache_max_age: int = C.RSS_CACHE_MAX_AGE_SECONDS,
        request_timeout: float = 15.0,
        dedup_threshold: float = DEDUP_THRESHOLD,
    ) -> None:
        self._feeds:          list[tuple[str, str]] = feeds or NEWS_FEEDS
        self._poll_interval:  int   = poll_interval
        self._cache_max_age:  int   = cache_max_age
        self._request_timeout: float = request_timeout
        self._dedup_threshold: float = dedup_threshold

        # Per-feed cache: source_label -> _CacheEntry
        self._cache: dict[str, _CacheEntry] = {}

        self._stopped: bool = False
        logger.info(
            "RssAggregator init  feeds=%d  interval=%ds  cache_ttl=%ds",
            len(self._feeds), self._poll_interval, self._cache_max_age,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_all(self) -> list[Headline]:
        """Fetch all feeds concurrently and return deduplicated headlines.

        Uses cached results for any feed that fails (provided cache is not stale).
        """
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._request_timeout),
            headers={"User-Agent": "PolyEdge/5.0 (RSS aggregator; +https://github.com)"},
        ) as session:
            tasks = [
                self._fetch_one(session, source, url)
                for source, url in self._feeds
            ]
            per_feed: list[list[Headline]] = await asyncio.gather(*tasks)

        all_headlines: list[Headline] = []
        for batch in per_feed:
            all_headlines.extend(batch)

        # Sort by fetched_at descending so newest items win dedup
        all_headlines.sort(key=lambda h: h.fetched_at, reverse=True)

        deduped = self._deduplicate(all_headlines)
        logger.info(
            "rss_fetch_all  raw=%d  deduped=%d",
            len(all_headlines), len(deduped),
        )
        return deduped

    async def poll(self) -> AsyncIterator[list[Headline]]:
        """Async generator that yields deduplicated headline batches.

        Fetches immediately, then waits poll_interval seconds between fetches.
        Stops when stop() is called.

        Usage::

            async for batch in aggregator.poll():
                await handle(batch)
        """
        self._stopped = False
        while not self._stopped:
            t0 = time.monotonic()
            try:
                batch = await self.fetch_all()
                yield batch
            except Exception as exc:  # noqa: BLE001
                logger.error("rss_poll_error  error=%s", exc)
                yield []

            elapsed = time.monotonic() - t0
            wait = max(0.0, self._poll_interval - elapsed)
            if not self._stopped:
                logger.debug("rss_poll_sleep  wait=%.1fs", wait)
                await asyncio.sleep(wait)

    def stop(self) -> None:
        """Signal the polling loop to exit after the current sleep."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Cache access
    # ------------------------------------------------------------------

    def cached_headlines(self, source: str | None = None) -> list[Headline]:
        """Return all cached headlines for ``source``, or all feeds if None.

        Does NOT check staleness — callers may receive old data.
        Use ``is_cache_fresh(source)`` to check age first.
        """
        if source is not None:
            entry = self._cache.get(source)
            return entry.headlines if entry else []
        results: list[Headline] = []
        for entry in self._cache.values():
            results.extend(entry.headlines)
        return results

    def is_cache_fresh(self, source: str) -> bool:
        """Return True if the cache for ``source`` is younger than cache_max_age."""
        entry = self._cache.get(source)
        if not entry:
            return False
        return (time.time() - entry.fetched_at) < self._cache_max_age

    def cache_ages(self) -> dict[str, float | None]:
        """Return {source: age_in_seconds} for all known feeds."""
        now = time.time()
        result: dict[str, float | None] = {}
        for source, _ in self._feeds:
            entry = self._cache.get(source)
            result[source] = round(now - entry.fetched_at, 1) if entry else None
        return result

    # ------------------------------------------------------------------
    # Internal — single feed fetch
    # ------------------------------------------------------------------

    async def _fetch_one(
        self, session: aiohttp.ClientSession, source: str, url: str
    ) -> list[Headline]:
        """Fetch and parse one feed URL.  Returns cached data on failure."""
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                raw_text = await resp.text(errors="replace")

            headlines = _parse_feed(source, raw_text)
            self._cache[source] = _CacheEntry(
                headlines=headlines, fetched_at=time.time()
            )
            logger.info(
                "rss_feed_ok  source=%s  items=%d", source, len(headlines)
            )
            return headlines

        except Exception as exc:  # noqa: BLE001
            logger.warning("rss_feed_error  source=%s  error=%s", source, exc)
            return self._fallback_cache(source)

    def _fallback_cache(self, source: str) -> list[Headline]:
        """Return cached headlines for a failed feed, if not stale."""
        entry = self._cache.get(source)
        if not entry:
            logger.warning("rss_no_cache  source=%s", source)
            return []

        age = time.time() - entry.fetched_at
        if age > self._cache_max_age:
            logger.warning(
                "rss_cache_stale  source=%s  age=%.0fs  max=%ds",
                source, age, self._cache_max_age,
            )
            return []

        logger.info(
            "rss_using_cache  source=%s  age=%.0fs  items=%d",
            source, age, len(entry.headlines),
        )
        return entry.headlines

    # ------------------------------------------------------------------
    # Internal — deduplication
    # ------------------------------------------------------------------

    def _deduplicate(self, headlines: list[Headline]) -> list[Headline]:
        """Remove near-duplicate headlines, keeping the first occurrence."""
        accepted: list[Headline] = []
        for h in headlines:
            if not _is_duplicate(h, accepted):
                accepted.append(h)
        return accepted
