"""
Tests for rss_aggregator.py and regulatory_feeds.py.
No live network — all HTTP responses are mocked.
"""
import asyncio
import time
import types
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# ── project root must be on path ─────────────────────────────────────────────
sys.path.insert(0, ".")

from data.rss_aggregator import (
    RssAggregator,
    Headline,
    _normalise_title,
    _jaccard,
    _is_duplicate,
    _parse_feed,
    DEDUP_THRESHOLD,
    NEWS_FEEDS,
)
from data.regulatory_feeds import (
    RegulatoryPoller,
    RegulatoryAlert,
    _classify,
    _parse_feed as reg_parse_feed,
    classify_text,
    CRITICAL_KEYWORDS,
    WARNING_KEYWORDS,
)

# ---------------------------------------------------------------------------
# Minimal RSS XML fixtures
# ---------------------------------------------------------------------------

_RSS_REUTERS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Reuters Top News</title>
    <item>
      <title>Federal Reserve raises interest rates 25 basis points inflation</title>
      <link>https://reuters.com/article/1</link>
      <pubDate>Fri, 04 Apr 2026 10:00:00 +0000</pubDate>
      <description>The Fed raised its benchmark rate for the second time this year.</description>
    </item>
    <item>
      <title>S&amp;P 500 closes above 5300 for first time</title>
      <link>https://reuters.com/article/2</link>
      <pubDate>Fri, 04 Apr 2026 11:00:00 +0000</pubDate>
      <description>Equities rallied after positive jobs data.</description>
    </item>
  </channel>
</rss>"""

_RSS_AP = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>AP Top News</title>
    <item>
      <title>Federal Reserve raises interest rates 25 basis points again</title>
      <link>https://apnews.com/article/1</link>
      <pubDate>Fri, 04 Apr 2026 10:05:00 +0000</pubDate>
      <description>Federal Reserve hikes rates again.</description>
    </item>
    <item>
      <title>Tech stocks surge on earnings optimism</title>
      <link>https://apnews.com/article/2</link>
      <pubDate>Fri, 04 Apr 2026 12:00:00 +0000</pubDate>
      <description>Nasdaq rose 1.8% in Friday trading.</description>
    </item>
  </channel>
</rss>"""

_RSS_EMPTY = """<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>"""

_RSS_REGULATORY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Federal Register CFTC</title>
    <item>
      <title>CFTC Proposes Rule Change for Prediction Market Event Contracts</title>
      <link>https://federalregister.gov/d/1</link>
      <pubDate>Fri, 04 Apr 2026 09:00:00 +0000</pubDate>
      <description>The CFTC is considering new rules that would affect prediction markets and event contracts including binary options traded on Kalshi.</description>
    </item>
    <item>
      <title>CFTC Annual Report on Derivatives Markets</title>
      <link>https://federalregister.gov/d/2</link>
      <pubDate>Fri, 04 Apr 2026 08:00:00 +0000</pubDate>
      <description>Overview of derivatives market activity and swap dealer compliance.</description>
    </item>
    <item>
      <title>Unrelated Federal Procurement Notice</title>
      <link>https://federalregister.gov/d/3</link>
      <pubDate>Fri, 04 Apr 2026 07:00:00 +0000</pubDate>
      <description>GSA seeks vendors for office supplies contract renewal.</description>
    </item>
  </channel>
</rss>"""


# ===========================================================================
# RSS Aggregator tests
# ===========================================================================

def test_normalise_title():
    tokens = _normalise_title("The Federal Reserve Raises Rates!")
    # stop-words 'the' removed; punctuation stripped; lowercase
    assert "federal" in tokens
    assert "the" not in tokens
    assert "raises" in tokens
    print("  _normalise_title  OK")


def test_jaccard():
    a = {"federal", "reserve", "rates"}
    b = {"federal", "reserve", "rates"}
    assert _jaccard(a, b) == 1.0

    c = {"federal", "reserve"}
    assert _jaccard(a, c) == pytest_approx(2 / 3, rel=0.01)   # |a&c|=2, |a|c|=3

    assert _jaccard(set(), set()) == 1.0
    assert _jaccard(a, set()) == 0.0
    print("  _jaccard  OK")


def pytest_approx(value, rel=0.01):
    """Minimal approx helper since we're not using pytest here."""
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) / max(abs(value), 1e-10) <= rel
        def __repr__(self):
            return f"~{value}"
    return _Approx()


def test_dedup_similar_titles():
    now = time.time()
    def make(title, source="Reuters", url="http://x"):
        return Headline(title=title, source=source, url=url,
                        summary="", published_time="", fetched_at=now,
                        uid=f"{source}:{url}")

    # These titles share most tokens once stop-words are stripped → Jaccard > 0.55
    h1 = make("Federal Reserve raises interest rates 25 basis points inflation", url="http://a")
    h2 = make("Federal Reserve raises interest rates 25 basis points again", url="http://b")
    h3 = make("Tech stocks surge on earnings optimism", url="http://c")

    assert _is_duplicate(h2, [h1]), "Fed rate story should be deduped"
    assert not _is_duplicate(h3, [h1, h2]), "Tech story should pass through"
    print("  dedup: similar Fed headlines collapse  OK")
    print("  dedup: unrelated tech story passes through  OK")


def test_parse_feed_reuters():
    headlines = _parse_feed("Reuters", _RSS_REUTERS)
    assert len(headlines) == 2
    assert headlines[0].title == "Federal Reserve raises interest rates 25 basis points inflation"
    assert headlines[0].source == "Reuters"
    assert headlines[0].url == "https://reuters.com/article/1"
    assert "Fed raised" in headlines[0].summary
    assert headlines[0].uid  # non-empty
    print(f"  _parse_feed Reuters: {len(headlines)} items  OK")


def test_parse_feed_empty():
    headlines = _parse_feed("Reuters", _RSS_EMPTY)
    assert headlines == []
    print("  _parse_feed empty feed  OK")


def test_fetch_all_deduplication():
    """fetch_all merges feeds and removes near-duplicate Fed rate stories."""

    async def run():
        agg = RssAggregator(
            feeds=[("Reuters", "http://r"), ("AP", "http://ap")],
            poll_interval=600,
            cache_max_age=3600,
        )

        responses = {
            "http://r":  _RSS_REUTERS,
            "http://ap": _RSS_AP,
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status = 200
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            resp.text = AsyncMock(return_value=responses[url])
            return resp

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session_inst = MagicMock()
            session_inst.__aenter__ = AsyncMock(return_value=session_inst)
            session_inst.__aexit__ = AsyncMock(return_value=False)
            session_inst.get = mock_get
            mock_session_cls.return_value = session_inst

            headlines = await agg.fetch_all()

        titles = [h.title for h in headlines]
        # Reuters + AP both have a Fed rate story; should collapse to 1
        fed_stories = [t for t in titles if "rate" in t.lower() or "rates" in t.lower()]
        assert len(fed_stories) == 1, f"Expected 1 Fed story after dedup, got: {fed_stories}"

        # Tech story and S&P story should both survive
        assert any("S&P" in t or "500" in t for t in titles), "S&P story missing"
        assert any("Tech" in t or "surge" in t.lower() for t in titles), "Tech story missing"

        # Cache should be populated
        assert agg.is_cache_fresh("Reuters")
        assert agg.is_cache_fresh("AP")

        print(f"  fetch_all: {len(headlines)} headlines after dedup  OK")
        print(f"    titles: {titles}")

    asyncio.run(run())


def test_cache_fallback_on_error():
    """On feed failure, return cached data if not stale."""

    async def run():
        agg = RssAggregator(
            feeds=[("Reuters", "http://r")],
            poll_interval=600,
            cache_max_age=3600,
        )
        # Pre-populate cache
        from data.rss_aggregator import _CacheEntry
        fake_headline = Headline(
            title="Cached story", source="Reuters", url="http://cached",
            summary="", published_time="", fetched_at=time.time(), uid="abc123"
        )
        agg._cache["Reuters"] = _CacheEntry(
            headlines=[fake_headline], fetched_at=time.time()
        )

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status = 503
            resp.request_info = MagicMock()
            resp.history = []
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            resp.text = AsyncMock(return_value="")
            return resp

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session_inst = MagicMock()
            session_inst.__aenter__ = AsyncMock(return_value=session_inst)
            session_inst.__aexit__ = AsyncMock(return_value=False)
            session_inst.get = mock_get
            mock_session_cls.return_value = session_inst

            headlines = await agg.fetch_all()

        assert len(headlines) == 1
        assert headlines[0].title == "Cached story"
        print("  cache fallback on 503  OK")

    asyncio.run(run())


def test_stale_cache_discarded():
    """Cache older than cache_max_age must not be returned on failure."""

    async def run():
        agg = RssAggregator(
            feeds=[("Reuters", "http://r")],
            poll_interval=600,
            cache_max_age=3600,
        )
        from data.rss_aggregator import _CacheEntry
        fake_headline = Headline(
            title="Old story", source="Reuters", url="http://old",
            summary="", published_time="", fetched_at=time.time() - 7200,  # 2h ago
            uid="xyz"
        )
        agg._cache["Reuters"] = _CacheEntry(
            headlines=[fake_headline], fetched_at=time.time() - 7200
        )

        def mock_get(url, **kwargs):
            raise aiohttp.ClientConnectorError(MagicMock(), OSError("timeout"))

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session_inst = MagicMock()
            session_inst.__aenter__ = AsyncMock(return_value=session_inst)
            session_inst.__aexit__ = AsyncMock(return_value=False)
            session_inst.get = mock_get
            mock_session_cls.return_value = session_inst

            headlines = await agg.fetch_all()

        assert headlines == [], f"Stale cache should return [], got {headlines}"
        print("  stale cache (2h) discarded on error  OK")

    asyncio.run(run())


# ===========================================================================
# Regulatory feeds tests
# ===========================================================================

def test_classify_critical():
    # Direct Kalshi mention
    result = _classify("CFTC Proposes Rules for Kalshi Event Contracts", "")
    assert result is not None
    level, keywords = result
    assert level == "critical"
    assert any("kalshi" in kw or "event contract" in kw for kw in keywords)
    print(f"  _classify critical: level={level} keywords={keywords}  OK")


def test_classify_warning():
    result = _classify("CFTC Annual Derivatives Market Report", "Swap dealer oversight.")
    assert result is not None
    level, keywords = result
    assert level == "warning"
    assert any("cftc" in kw or "derivatives" in kw for kw in keywords)
    print(f"  _classify warning: level={level} keywords={keywords}  OK")


def test_classify_none():
    result = _classify("GSA Procurement Notice for Office Supplies", "Vendor registration.")
    assert result is None
    print("  _classify irrelevant: None  OK")


def test_classify_critical_beats_warning():
    """An item matching both tiers should be classified critical."""
    result = _classify(
        "CFTC Prediction Market Investigation", "derivatives and kalshi binary options"
    )
    assert result is not None
    level, _ = result
    assert level == "critical"
    print("  _classify: critical beats warning  OK")


def test_reg_parse_feed():
    alerts = reg_parse_feed("FederalRegister_CFTC", _RSS_REGULATORY)
    # Item 1: critical (kalshi, prediction market, event contracts)
    # Item 2: warning (cftc, derivatives)
    # Item 3: no match → discarded
    assert len(alerts) == 2, f"Expected 2 flagged alerts, got {len(alerts)}: {[a.title for a in alerts]}"

    critical_items = [a for a in alerts if a.alert_level == "critical"]
    warning_items  = [a for a in alerts if a.alert_level == "warning"]
    assert len(critical_items) == 1
    assert len(warning_items) == 1

    critical = critical_items[0]
    assert "Kalshi" in critical.title or "kalshi" in " ".join(critical.matched_keywords)
    assert critical.uid  # stable hash populated

    print(f"  reg_parse_feed: {len(alerts)} alerts ({len(critical_items)} critical, {len(warning_items)} warning)  OK")


def test_reg_fetch_all():
    """fetch_all deduplicates by uid and sorts critical-first."""

    async def run():
        poller = RegulatoryPoller(
            feeds=[("Feed1", "http://f1"), ("Feed2", "http://f2")],
            poll_interval=1800,
            cache_max_age=3600,
        )

        # Both feeds return the same regulatory doc → only one alert after dedup
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status = 200
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            resp.text = AsyncMock(return_value=_RSS_REGULATORY)
            return resp

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session_inst = MagicMock()
            session_inst.__aenter__ = AsyncMock(return_value=session_inst)
            session_inst.__aexit__ = AsyncMock(return_value=False)
            session_inst.get = mock_get
            mock_session_cls.return_value = session_inst

            alerts = await poller.fetch_all()

        # 2 unique items per feed; same UIDs across both feeds → 2 unique after dedup
        uids = [a.uid for a in alerts]
        assert len(uids) == len(set(uids)), "Duplicate UIDs found after dedup"
        assert alerts[0].alert_level == "critical", "Critical items should sort first"
        print(f"  reg_fetch_all: {len(alerts)} unique alerts, sorted critical-first  OK")

    asyncio.run(run())


def test_reg_cache_fallback():
    """Falls back to cache on network error."""

    async def run():
        poller = RegulatoryPoller(
            feeds=[("Feed1", "http://f1")],
            poll_interval=1800,
            cache_max_age=3600,
        )
        from data.regulatory_feeds import _CacheEntry
        fake = RegulatoryAlert(
            title="Cached alert", source="Feed1", url="http://c",
            summary="", published_time="", fetched_at=time.time(),
            alert_level="warning", matched_keywords=["cftc"], uid="cached1"
        )
        poller._cache["Feed1"] = _CacheEntry(alerts=[fake], fetched_at=time.time())

        def mock_get(url, **kwargs):
            raise aiohttp.ClientConnectorError(MagicMock(), OSError("down"))

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session_inst = MagicMock()
            session_inst.__aenter__ = AsyncMock(return_value=session_inst)
            session_inst.__aexit__ = AsyncMock(return_value=False)
            session_inst.get = mock_get
            mock_session_cls.return_value = session_inst

            alerts = await poller.fetch_all()

        assert len(alerts) == 1 and alerts[0].uid == "cached1"
        print("  reg cache fallback on network error  OK")

    asyncio.run(run())


def test_classify_text_public():
    result = classify_text("Kalshi ordered to cease event contract trading")
    assert result is not None and result[0] == "critical"
    result2 = classify_text("Weather in Georgia today")
    assert result2 is None
    print("  classify_text public API  OK")


# ===========================================================================
# Runner
# ===========================================================================

if __name__ == "__main__":
    print("=== RSS aggregator — unit tests ===")
    test_normalise_title()
    test_jaccard()
    test_dedup_similar_titles()
    test_parse_feed_reuters()
    test_parse_feed_empty()
    test_fetch_all_deduplication()
    test_cache_fallback_on_error()
    test_stale_cache_discarded()

    print()
    print("=== Regulatory feeds — unit tests ===")
    test_classify_critical()
    test_classify_warning()
    test_classify_none()
    test_classify_critical_beats_warning()
    test_reg_parse_feed()
    test_reg_fetch_all()
    test_reg_cache_fallback()
    test_classify_text_public()

    print()
    print("All tests passed.")
