"""
Tests for analysis/claude_analyzer.py
"""

import asyncio
import dataclasses
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic

from analysis.claude_analyzer import (
    ClaudeAnalyzer,
    DecisionContext,
    DecisionResult,
    ScanContext,
    ScanResult,
    _compute_cost,
    _extract_json,
    _SCAN_DEFAULT,
    _DECISION_DEFAULT,
)
from config import constants as C


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scan_ctx(**kwargs) -> ScanContext:
    defaults = dict(
        headline_title="Fed raises rates by 25bps",
        headline_source="Reuters",
        headline_summary="The Federal Reserve raised interest rates.",
        market_ticker="FED-RATE-2024",
        market_title="Will the Fed raise rates in 2024?",
        market_category="finance",
        current_price=0.60,
        days_to_settlement=30.0,
    )
    defaults.update(kwargs)
    return ScanContext(**defaults)


def _make_decision_ctx(**kwargs) -> DecisionContext:
    defaults = dict(
        market_ticker="FED-RATE-2024",
        market_title="Will the Fed raise rates in 2024?",
        market_category="finance",
        current_price=0.60,
        days_to_settlement=30.0,
        relevant_headlines=[{"title": "Fed meeting", "source": "Reuters",
                              "summary": "FOMC meeting scheduled", "published_time": "2024-01-01"}],
        orderbook={"yes": [[60, 100], [59, 200]], "no": [[40, 150], [41, 100]]},
        base_rate=0.55,
        base_rate_n=20,
        recent_prices=[58, 59, 60, 61, 60],
        volume_7d=10000.0,
        open_interest=500,
    )
    defaults.update(kwargs)
    return DecisionContext(**defaults)


def _make_anthropic_response(text: str, input_tokens: int = 100, output_tokens: int = 50):
    """Build a mock Anthropic message response."""
    content_block = MagicMock()
    content_block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [content_block]
    response.usage = usage
    return response


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson(unittest.TestCase):
    def test_direct_json(self):
        result = _extract_json('{"a": 1, "b": "hello"}')
        self.assertEqual(result, {"a": 1, "b": "hello"})

    def test_code_fence_json(self):
        text = '```json\n{"score": 7, "impact": "positive"}\n```'
        result = _extract_json(text)
        self.assertEqual(result["score"], 7)
        self.assertEqual(result["impact"], "positive")

    def test_code_fence_no_lang(self):
        text = '```\n{"x": 42}\n```'
        result = _extract_json(text)
        self.assertEqual(result["x"], 42)

    def test_embedded_in_prose(self):
        text = 'Here is my analysis: {"relevance_score": 8} that was my answer.'
        result = _extract_json(text)
        self.assertEqual(result["relevance_score"], 8)

    def test_greedy_fallback(self):
        text = 'some text {"key": "value", "num": 3} more text after'
        result = _extract_json(text)
        self.assertIn("key", result)

    def test_empty_on_failure(self):
        result = _extract_json("no json here at all")
        self.assertEqual(result, {})

    def test_empty_string(self):
        result = _extract_json("")
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _compute_cost
# ---------------------------------------------------------------------------

class TestComputeCost(unittest.TestCase):
    def test_haiku_cost(self):
        cost = _compute_cost(C.CLAUDE_SCAN_MODEL, 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.80, places=4)

    def test_sonnet_cost(self):
        cost = _compute_cost(C.CLAUDE_DECISION_MODEL, 0, 1_000_000)
        self.assertAlmostEqual(cost, 15.00, places=4)

    def test_mixed_tokens(self):
        # haiku: 1000 input @ $0.80/M + 500 output @ $4.00/M
        cost = _compute_cost(C.CLAUDE_SCAN_MODEL, 1000, 500)
        expected = 0.80 / 1_000_000 * 1000 + 4.00 / 1_000_000 * 500
        self.assertAlmostEqual(cost, expected, places=8)

    def test_unknown_model_uses_sonnet_rate(self):
        cost = _compute_cost("unknown-model", 1_000_000, 0)
        self.assertAlmostEqual(cost, 3.00, places=4)


# ---------------------------------------------------------------------------
# ClaudeAnalyzer — scan_mode
# ---------------------------------------------------------------------------

class TestScanMode(unittest.IsolatedAsyncioTestCase):
    def _make_analyzer(self):
        with patch("analysis.claude_analyzer.anthropic.AsyncAnthropic"):
            analyzer = ClaudeAnalyzer(api_key="test-key")
        return analyzer

    async def test_scan_mode_success(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "relevance_score": 8,
            "news_impact": "positive",
            "urgency": "immediate",
            "reasoning": "Fed rate decision directly affects this contract.",
        })
        mock_response = _make_anthropic_response(payload, 120, 60)

        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertIsInstance(result, ScanResult)
        self.assertEqual(result.relevance_score, 8.0)
        self.assertEqual(result.news_impact, "positive")
        self.assertEqual(result.urgency, "immediate")
        self.assertFalse(result.cached)
        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.output_tokens, 60)

    async def test_scan_mode_clamps_score_high(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({"relevance_score": 99, "news_impact": "positive",
                               "urgency": "monitor", "reasoning": "x"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertEqual(result.relevance_score, 10.0)

    async def test_scan_mode_clamps_score_low(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({"relevance_score": -5, "news_impact": "negative",
                               "urgency": "ignore", "reasoning": "x"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertEqual(result.relevance_score, 0.0)

    async def test_scan_mode_missing_fields_use_defaults(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({})  # empty JSON
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertEqual(result.relevance_score, 5.0)
        self.assertEqual(result.news_impact, "neutral")
        self.assertEqual(result.urgency, "monitor")

    async def test_scan_mode_caches_result(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({"relevance_score": 7, "news_impact": "positive",
                               "urgency": "monitor", "reasoning": "relevant"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertIn("scan:FED-RATE-2024", analyzer.cache_keys())
        age = analyzer.cache_age_seconds("scan:FED-RATE-2024")
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)

    async def test_scan_mode_returns_default_on_failure(self):
        analyzer = self._make_analyzer()
        analyzer._client.messages.create = AsyncMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock())
        )

        result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertIsInstance(result, ScanResult)
        self.assertTrue(result.cached)
        self.assertEqual(result.urgency, "monitor")

    async def test_scan_mode_returns_cached_on_failure(self):
        """If cache has a prior result, return that on failure."""
        analyzer = self._make_analyzer()

        # Prime the cache
        payload = json.dumps({"relevance_score": 9, "news_impact": "positive",
                               "urgency": "immediate", "reasoning": "primed"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            await analyzer.scan_mode(_make_scan_ctx())

        # Now fail
        analyzer._client.messages.create = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        result = await analyzer.scan_mode(_make_scan_ctx())

        self.assertTrue(result.cached)
        self.assertEqual(result.relevance_score, 9.0)


# ---------------------------------------------------------------------------
# ClaudeAnalyzer — decision_mode
# ---------------------------------------------------------------------------

class TestDecisionMode(unittest.IsolatedAsyncioTestCase):
    def _make_analyzer(self):
        with patch("analysis.claude_analyzer.anthropic.AsyncAnthropic"):
            analyzer = ClaudeAnalyzer(api_key="test-key")
        return analyzer

    async def test_decision_mode_success(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "predicted_probability": 0.72,
            "confidence": "high",
            "edge_pp": 12.0,
            "recommended_action": "buy_yes",
            "key_factors": ["Fed hawkish", "Inflation high"],
            "risk_flags": [],
            "reasoning": "Strong momentum towards rate hike.",
        })
        mock_response = _make_anthropic_response(payload, 300, 100)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.decision_mode(_make_decision_ctx())

        self.assertIsInstance(result, DecisionResult)
        self.assertAlmostEqual(result.predicted_probability, 0.72)
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.recommended_action, "buy_yes")
        self.assertFalse(result.cached)
        self.assertEqual(len(result.key_factors), 2)
        self.assertEqual(result.input_tokens, 300)
        self.assertEqual(result.output_tokens, 100)

    async def test_decision_mode_clamps_probability(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "predicted_probability": 1.5,  # over max
            "confidence": "high",
            "edge_pp": 50.0,
            "recommended_action": "buy_yes",
            "key_factors": [],
            "risk_flags": [],
            "reasoning": "Extreme.",
        })
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.decision_mode(_make_decision_ctx())

        self.assertEqual(result.predicted_probability, 1.0)

    async def test_decision_mode_key_factors_truncated(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "predicted_probability": 0.55,
            "confidence": "medium",
            "edge_pp": 0.0,
            "recommended_action": "skip",
            "key_factors": ["a", "b", "c", "d", "e", "f", "g"],  # 7, should cap at 5
            "risk_flags": [],
            "reasoning": "Many factors.",
        })
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.decision_mode(_make_decision_ctx())

        self.assertEqual(len(result.key_factors), 5)

    async def test_decision_mode_edge_computed_from_model_price(self):
        """edge_pp defaults to model_prob*100 - current_price*100 if not in JSON."""
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "predicted_probability": 0.75,
            "confidence": "medium",
            "recommended_action": "buy_yes",
            "key_factors": [],
            "risk_flags": [],
            "reasoning": "test",
            # no edge_pp key
        })
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        ctx = _make_decision_ctx(current_price=0.60)  # 60 cents
        with patch.object(analyzer, "_log_cost", AsyncMock()):
            result = await analyzer.decision_mode(ctx)

        # 0.75 * 100 - 0.60 * 100 = 15
        self.assertAlmostEqual(result.edge_pp, 15.0, places=1)

    async def test_decision_mode_returns_default_on_failure(self):
        analyzer = self._make_analyzer()
        analyzer._client.messages.create = AsyncMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock())
        )

        result = await analyzer.decision_mode(_make_decision_ctx())

        self.assertIsInstance(result, DecisionResult)
        self.assertTrue(result.cached)
        self.assertEqual(result.recommended_action, "skip")
        self.assertEqual(result.confidence, "low")

    async def test_decision_mode_caches_result(self):
        analyzer = self._make_analyzer()
        payload = json.dumps({
            "predicted_probability": 0.65,
            "confidence": "medium",
            "edge_pp": 5.0,
            "recommended_action": "skip",
            "key_factors": [],
            "risk_flags": [],
            "reasoning": "small edge",
        })
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            await analyzer.decision_mode(_make_decision_ctx())

        self.assertIn("decision:FED-RATE-2024", analyzer.cache_keys())


# ---------------------------------------------------------------------------
# ClaudeAnalyzer — _call_claude retry logic
# ---------------------------------------------------------------------------

class TestCallClaudeRetry(unittest.IsolatedAsyncioTestCase):
    def _make_analyzer(self):
        with patch("analysis.claude_analyzer.anthropic.AsyncAnthropic"):
            analyzer = ClaudeAnalyzer(api_key="test-key")
        return analyzer

    async def test_retries_on_timeout_then_succeeds(self):
        analyzer = self._make_analyzer()
        good_response = _make_anthropic_response('{"x":1}')

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            return good_response

        analyzer._client.messages.create = AsyncMock(side_effect=side_effect)

        with patch("analysis.claude_analyzer.asyncio.sleep", AsyncMock()):
            with patch.object(analyzer, "_log_cost", AsyncMock()):
                content, _, _, _ = await analyzer._call_claude(
                    model=C.CLAUDE_SCAN_MODEL,
                    system="sys",
                    user="usr",
                    purpose="test",
                    ticker="TEST",
                )

        self.assertEqual(call_count, 2)
        self.assertEqual(content, '{"x":1}')

    async def test_retries_on_429_then_succeeds(self):
        analyzer = self._make_analyzer()
        good_response = _make_anthropic_response('{"y":2}')

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                exc = anthropic.APIStatusError(
                    "rate limited", response=MagicMock(), body={}
                )
                exc.status_code = 429
                raise exc
            return good_response

        analyzer._client.messages.create = AsyncMock(side_effect=side_effect)

        with patch("analysis.claude_analyzer.asyncio.sleep", AsyncMock()):
            with patch.object(analyzer, "_log_cost", AsyncMock()):
                content, _, _, _ = await analyzer._call_claude(
                    model=C.CLAUDE_SCAN_MODEL,
                    system="sys",
                    user="usr",
                    purpose="test",
                    ticker="TEST",
                )

        self.assertEqual(call_count, 3)

    async def test_raises_on_non_retriable_4xx(self):
        analyzer = self._make_analyzer()

        async def side_effect(*args, **kwargs):
            exc = anthropic.APIStatusError(
                "bad request", response=MagicMock(), body={}
            )
            exc.status_code = 400
            raise exc

        analyzer._client.messages.create = AsyncMock(side_effect=side_effect)

        with self.assertRaises(anthropic.APIStatusError):
            await analyzer._call_claude(
                model=C.CLAUDE_SCAN_MODEL,
                system="sys",
                user="usr",
                purpose="test",
                ticker="TEST",
            )

    async def test_raises_after_all_retries_exhausted(self):
        analyzer = self._make_analyzer()
        analyzer._client.messages.create = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("analysis.claude_analyzer.asyncio.sleep", AsyncMock()):
            with self.assertRaises(asyncio.TimeoutError):
                await analyzer._call_claude(
                    model=C.CLAUDE_SCAN_MODEL,
                    system="sys",
                    user="usr",
                    purpose="test",
                    ticker="TEST",
                )

        # Should have been called 3 times
        self.assertEqual(analyzer._client.messages.create.call_count, 3)


# ---------------------------------------------------------------------------
# ClaudeAnalyzer — _log_cost
# ---------------------------------------------------------------------------

class TestLogCost(unittest.IsolatedAsyncioTestCase):
    def _make_analyzer(self):
        with patch("analysis.claude_analyzer.anthropic.AsyncAnthropic"):
            analyzer = ClaudeAnalyzer(api_key="test-key")
        return analyzer

    async def test_log_cost_executes_insert(self):
        analyzer = self._make_analyzer()

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with patch("analysis.claude_analyzer.ClaudeAnalyzer._log_cost",
                   wraps=analyzer._log_cost):
            with patch("persistence.database.get_connection", return_value=mock_db):
                await analyzer._log_cost(
                    model=C.CLAUDE_SCAN_MODEL,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.0003,
                    purpose="scan",
                    ticker="TEST-TICKER",
                )

    async def test_log_cost_silences_db_error(self):
        """DB failure in _log_cost must not propagate."""
        analyzer = self._make_analyzer()

        with patch("persistence.database.get_connection",
                   side_effect=Exception("DB unavailable")):
            # Should not raise
            await analyzer._log_cost(
                model=C.CLAUDE_SCAN_MODEL,
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0001,
                purpose="scan",
                ticker="TEST",
            )


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class TestCacheHelpers(unittest.IsolatedAsyncioTestCase):
    def _make_analyzer(self):
        with patch("analysis.claude_analyzer.anthropic.AsyncAnthropic"):
            analyzer = ClaudeAnalyzer(api_key="test-key")
        return analyzer

    async def test_cache_miss_returns_default(self):
        analyzer = self._make_analyzer()
        result = analyzer._get_cached("missing:key", _SCAN_DEFAULT)
        self.assertIs(result, _SCAN_DEFAULT)

    async def test_cache_hit_returns_copy_with_cached_true(self):
        analyzer = self._make_analyzer()

        # Prime cache via scan_mode
        payload = json.dumps({"relevance_score": 6, "news_impact": "neutral",
                               "urgency": "monitor", "reasoning": "cached test"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            first = await analyzer.scan_mode(_make_scan_ctx())

        self.assertFalse(first.cached)

        # Retrieve from cache
        cached = analyzer._get_cached("scan:FED-RATE-2024", _SCAN_DEFAULT)
        self.assertTrue(cached.cached)
        self.assertEqual(cached.relevance_score, 6.0)

    def test_cache_age_returns_none_for_missing_key(self):
        analyzer = self._make_analyzer()
        self.assertIsNone(analyzer.cache_age_seconds("nonexistent"))

    async def test_cache_age_returns_float_after_entry(self):
        analyzer = self._make_analyzer()

        payload = json.dumps({"relevance_score": 7, "news_impact": "positive",
                               "urgency": "immediate", "reasoning": "age test"})
        mock_response = _make_anthropic_response(payload)
        analyzer._client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(analyzer, "_log_cost", AsyncMock()):
            await analyzer.scan_mode(_make_scan_ctx())

        age = analyzer.cache_age_seconds("scan:FED-RATE-2024")
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)


# ---------------------------------------------------------------------------
# Safe defaults
# ---------------------------------------------------------------------------

class TestSafeDefaults(unittest.TestCase):
    def test_scan_default_is_cached(self):
        self.assertTrue(_SCAN_DEFAULT.cached)
        self.assertEqual(_SCAN_DEFAULT.relevance_score, 5.0)
        self.assertEqual(_SCAN_DEFAULT.news_impact, "neutral")

    def test_decision_default_is_cached(self):
        self.assertTrue(_DECISION_DEFAULT.cached)
        self.assertEqual(_DECISION_DEFAULT.recommended_action, "skip")
        self.assertEqual(_DECISION_DEFAULT.confidence, "low")
        self.assertIn("analysis_unavailable", _DECISION_DEFAULT.risk_flags)


if __name__ == "__main__":
    unittest.main()
