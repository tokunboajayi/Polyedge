"""
Tests for monitoring/slack_alerts.py.
No live Slack calls — all HTTP is mocked.
"""
import sys
import json
import time
import types
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

# Stub settings so the module loads without a .env
_fake_settings = types.ModuleType("config.settings")
_fake_settings.SLACK_WEBHOOK_TRADES = "https://hooks.slack.com/trades"
_fake_settings.SLACK_WEBHOOK_ALERTS = "https://hooks.slack.com/alerts"
_fake_settings.SLACK_WEBHOOK_DAILY  = "https://hooks.slack.com/daily"
_fake_settings.SLACK_WEBHOOK_WEEKLY = "https://hooks.slack.com/weekly"
_fake_settings.KALSHI_ENV           = "demo"
sys.modules["config.settings"] = _fake_settings

from monitoring.slack_alerts import (
    SlackAlerter,
    _TokenBucket,
    build_payload,
    _send_http,
    _format_metric,
    _circuit_threshold,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alerter() -> SlackAlerter:
    return SlackAlerter(
        webhook_trades="https://hooks.slack.com/trades",
        webhook_alerts="https://hooks.slack.com/alerts",
        webhook_daily ="https://hooks.slack.com/daily",
        webhook_weekly="https://hooks.slack.com/weekly",
        env="demo",
    )


def _mock_post(status=200):
    """Return a requests.post mock that always gives ``status``."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok"
    resp.headers = {}
    mock = MagicMock(return_value=resp)
    return mock


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------

def test_token_bucket_allows_burst():
    b = _TokenBucket(rate=1.0, burst=3)
    results = [b.consume() for _ in range(3)]
    assert all(results), "First 3 tokens should be allowed (burst)"
    assert b.consume() is False, "4th token should be denied"
    print("  token_bucket burst: 3 allowed, 4th denied  OK")


def test_token_bucket_refills():
    b = _TokenBucket(rate=10.0, burst=1)
    assert b.consume() is True
    assert b.consume() is False
    time.sleep(0.15)          # ~1.5 tokens refilled at 10/s
    assert b.consume() is True
    print("  token_bucket refills after sleep  OK")


def test_token_bucket_thread_safety():
    b = _TokenBucket(rate=5.0, burst=5)
    allowed = []
    def worker():
        allowed.append(b.consume())
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sum(allowed) == 5, f"Expected exactly 5 allowed, got {sum(allowed)}"
    print(f"  token_bucket thread_safety: {sum(allowed)}/10 allowed  OK")


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def test_build_payload_structure():
    p = build_payload(
        level="critical", title="Test alert",
        fields={"Key": "Value", "Num": "42"},
        action="Do something",
        bankroll=487.50, open_positions=2, env="demo",
    )
    assert "blocks" in p
    assert "text" in p
    assert "[CRITICAL]" in p["text"]
    assert "Test alert" in p["text"]
    assert "$487.50" in p["text"]

    # Header block
    header = p["blocks"][0]
    assert header["type"] == "header"
    assert "CRITICAL" in header["text"]["text"]
    assert ":red_circle:" in header["text"]["text"]

    # Context block has bankroll, open positions, env
    ctx = p["blocks"][1]
    assert ctx["type"] == "context"
    ctx_text = json.dumps(ctx)
    assert "487.50" in ctx_text
    assert "2" in ctx_text
    assert "demo" in ctx_text

    # Fields block
    fields_block = p["blocks"][3]
    assert fields_block["type"] == "section"
    fields_text = json.dumps(fields_block)
    assert "Key" in fields_text
    assert "Value" in fields_text

    # Action footer
    action_block = p["blocks"][5]
    assert action_block["type"] == "context"
    assert "Do something" in json.dumps(action_block)
    print("  build_payload structure  OK")


def test_build_payload_level_emojis():
    for level, emoji in [("info", ":large_blue_circle:"),
                          ("warning", ":large_yellow_circle:"),
                          ("critical", ":red_circle:")]:
        p = build_payload(level=level, title="T", fields={}, action="A",
                          bankroll=500, open_positions=0, env="demo")
        assert emoji in p["blocks"][0]["text"]["text"], f"Missing {emoji} for {level}"
    print("  build_payload level emojis  OK")


# ---------------------------------------------------------------------------
# HTTP sender
# ---------------------------------------------------------------------------

def test_send_http_success():
    with patch("requests.post", _mock_post(200)):
        ok = _send_http("https://hooks.slack.com/x", {"text": "hi"})
    assert ok is True
    print("  _send_http 200  OK")


def test_send_http_retries_429():
    call_count = [0]
    def fake_post(url, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        resp.status_code = 429 if call_count[0] < 3 else 200
        resp.text = "ok"
        resp.headers = {"Retry-After": "0"}
        return resp

    with patch("requests.post", fake_post):
        with patch("time.sleep"):
            ok = _send_http("https://hooks.slack.com/x", {"text": "hi"})
    assert ok is True
    assert call_count[0] == 3
    print(f"  _send_http retries 429 -> succeeded on attempt 3  OK")


def test_send_http_gives_up_after_retries():
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "error"
    resp.headers = {}
    with patch("requests.post", MagicMock(return_value=resp)):
        with patch("time.sleep"):
            ok = _send_http("https://hooks.slack.com/x", {"text": "hi"})
    assert ok is False
    print("  _send_http exhausts retries on 500  OK")


def test_send_http_bad_url_returns_false():
    """Network exception -> returns False, does not raise."""
    import requests as req_module
    with patch("requests.post", side_effect=req_module.exceptions.ConnectionError("down")):
        with patch("time.sleep"):
            ok = _send_http("https://invalid/x", {"text": "hi"})
    assert ok is False
    print("  _send_http network error -> False, no raise  OK")


# ---------------------------------------------------------------------------
# SlackAlerter — channel routing and payload content
# ---------------------------------------------------------------------------

def test_trade_executed_routes_to_trades():
    sent = []
    def fake_post(url, **kw):
        sent.append((url, json.loads(kw["data"])))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        ok = alerter.trade_executed(
            ticker="TICK-001", side="yes", action="buy",
            contracts=10, price=0.52, fees=0.06,
            strategy="probability_arbitrage",
            bankroll=487.50, open_positions=2, order_id="ORD-123",
        )
    assert ok
    assert sent[0][0] == "https://hooks.slack.com/trades"
    payload = sent[0][1]
    text = json.dumps(payload)
    assert "TICK-001" in text
    assert "0.52" in text
    assert "ORD-123" in text
    assert "INFO" in text
    print("  trade_executed -> trades channel, correct fields  OK")


def test_circuit_breaker_routes_to_alerts():
    sent = []
    def fake_post(url, **kw):
        sent.append((url, json.loads(kw["data"])))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        ok = alerter.circuit_breaker(
            trigger="daily_loss_limit",
            loss_pct=0.062,
            bankroll=469.00, open_positions=3,
        )
    assert ok
    assert sent[0][0] == "https://hooks.slack.com/alerts"
    text = json.dumps(sent[0][1])
    assert "CRITICAL" in text
    assert "6.20%" in text
    assert "5%" in text   # threshold
    print("  circuit_breaker -> alerts channel, CRITICAL, correct pct  OK")


def test_kill_switch():
    sent = []
    def fake_post(url, **kw):
        sent.append(json.loads(kw["data"]))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        ok = alerter.kill_switch(bankroll=295.00, open_positions=1)
    assert ok
    text = json.dumps(sent[0])
    assert "CRITICAL" in text
    assert "KILL SWITCH" in text.upper()
    assert "295.00" in text
    print("  kill_switch -> CRITICAL, bankroll shown  OK")


def test_api_error_warning_below_threshold():
    sent = []
    def fake_post(url, **kw):
        sent.append(json.loads(kw["data"]))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.api_error("KalshiClient", "Connection refused", consecutive=2,
                          bankroll=500.0, open_positions=0)
    text = json.dumps(sent[0])
    assert "WARNING" in text
    print("  api_error(consecutive=2) -> WARNING  OK")


def test_api_error_critical_at_threshold():
    sent = []
    def fake_post(url, **kw):
        sent.append(json.loads(kw["data"]))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.api_error("KalshiClient", "Timeout", consecutive=3,
                          bankroll=500.0, open_positions=0)
    text = json.dumps(sent[0])
    assert "CRITICAL" in text
    print("  api_error(consecutive=3) -> CRITICAL  OK")


def test_regulatory_alert_critical():
    sent = []
    def fake_post(url, **kw):
        sent.append(json.loads(kw["data"]))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.regulatory_alert(
            title="CFTC Proposes New Rules for Kalshi Event Contracts",
            source="FederalRegister_CFTC",
            url="https://federalregister.gov/d/1",
            matched=["kalshi", "event contract"],
            alert_level="critical",
            bankroll=500.0, open_positions=2,
            summary="CFTC proposes rulemaking that may affect prediction markets.",
        )
    assert sent[0]["blocks"][0]["text"]["text"].startswith(":red_circle:")
    text = json.dumps(sent[0])
    assert "kalshi" in text.lower()
    print("  regulatory_alert critical -> red_circle header  OK")


def test_daily_summary_routes_to_daily():
    sent = []
    def fake_post(url, **kw):
        sent.append((url, json.loads(kw["data"])))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.daily_summary(
            date="2026-04-04", trades_today=4, pnl_today=3.42, pnl_mtd=12.10,
            win_rate_30d=0.61, brier_30d=0.215, api_cost_today=0.0032,
            bankroll=503.42, open_positions=1,
        )
    assert sent[0][0] == "https://hooks.slack.com/daily"
    text = json.dumps(sent[0][1])
    assert "2026-04-04" in text
    assert "3.42" in text
    print("  daily_summary -> daily channel  OK")


def test_weekly_summary_routes_to_weekly():
    sent = []
    def fake_post(url, **kw):
        sent.append((url, json.loads(kw["data"])))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.weekly_summary(
            week_label="2026-W14", trades_week=18, pnl_week=14.20, pnl_ytd=24.10,
            win_rate_week=0.61, sharpe_30d=1.12, avg_edge_30d=0.072,
            api_cost_week=0.021, bankroll=514.20, open_positions=2,
        )
    assert sent[0][0] == "https://hooks.slack.com/weekly"
    text = json.dumps(sent[0][1])
    assert "2026-W14" in text
    print("  weekly_summary -> weekly channel  OK")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_drops_excess():
    """Non-critical messages beyond burst (3) are dropped."""
    sent = []
    def fake_post(url, **kw):
        sent.append(1)
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        for i in range(6):
            alerter.system_info(
                f"msg {i}", {"i": str(i)},
                bankroll=500.0, open_positions=0, channel="alerts",
            )
    # Bucket burst=3, so at most 3 actually sent
    assert len(sent) <= 3, f"Expected ≤3 sent, got {len(sent)}"
    print(f"  rate_limit: {len(sent)}/6 sent (burst=3)  OK")


def test_critical_bypasses_rate_limit():
    """Critical messages must never be silently dropped."""
    sent = []
    def fake_post(url, **kw):
        sent.append(1)
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    # Drain the bucket completely
    for _ in range(10):
        alerter._buckets["alerts"].consume()

    with patch("requests.post", fake_post):
        with patch("time.sleep"):   # don't actually wait 1s
            alerter.kill_switch(bankroll=295.0, open_positions=0)

    assert len(sent) == 1, f"Critical must be sent even with empty bucket, got {len(sent)}"
    print("  critical bypasses empty rate-limit bucket  OK")


# ---------------------------------------------------------------------------
# Miscellaneous helpers
# ---------------------------------------------------------------------------

def test_format_metric():
    assert "61.0%" == _format_metric("win_rate", 0.61)
    assert "0.215" == _format_metric("brier_score", 0.215)
    assert "1.12" == _format_metric("sharpe_ratio", 1.12)
    assert "4.5/day" == _format_metric("trade_frequency", 4.5)
    assert "7.2%" == _format_metric("avg_edge", 0.072)
    print("  _format_metric  OK")


def test_circuit_threshold():
    assert _circuit_threshold("daily_loss_limit")   == "5%"
    assert _circuit_threshold("weekly_loss_limit")  == "10%"
    assert _circuit_threshold("monthly_loss_limit") == "15%"
    assert _circuit_threshold("unknown_trigger")    == "—"
    print("  _circuit_threshold  OK")


def test_no_url_skips_silently():
    """Channels without a URL must be skipped without raising."""
    alerter = SlackAlerter(
        webhook_trades="", webhook_alerts="",
        webhook_daily="",  webhook_weekly="", env="demo",
    )
    # Should not raise and should return False
    ok = alerter.trade_executed(
        ticker="X", side="yes", action="buy",
        contracts=5, price=0.50, fees=0.01,
        strategy="test", bankroll=500.0, open_positions=0,
    )
    assert ok is False
    print("  no_url skips silently -> False  OK")


def test_configured_channels():
    alerter = SlackAlerter(
        webhook_trades="https://x", webhook_alerts="",
        webhook_daily="https://y",  webhook_weekly="", env="demo",
    )
    channels = alerter.configured_channels()
    assert "trades" in channels
    assert "daily"  in channels
    assert "alerts" not in channels
    assert "weekly" not in channels
    print(f"  configured_channels={channels}  OK")


def test_position_closed_pnl_sign():
    sent = []
    def fake_post(url, **kw):
        sent.append(json.loads(kw["data"]))
        r = MagicMock(); r.status_code = 200; r.headers = {}; return r

    alerter = _alerter()
    with patch("requests.post", fake_post):
        alerter.position_closed(
            ticker="T", side="yes", contracts=10,
            entry_price=0.52, exit_price=0.65, pnl=1.25, fees_total=0.12,
            strategy="probability_arbitrage", bankroll=501.25, open_positions=1,
        )
    text = json.dumps(sent[0])
    assert "+$1.25" in text
    print("  position_closed positive P&L shows +sign  OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Token bucket ===")
    test_token_bucket_allows_burst()
    test_token_bucket_refills()
    test_token_bucket_thread_safety()

    print()
    print("=== Payload builder ===")
    test_build_payload_structure()
    test_build_payload_level_emojis()

    print()
    print("=== HTTP sender ===")
    test_send_http_success()
    test_send_http_retries_429()
    test_send_http_gives_up_after_retries()
    test_send_http_bad_url_returns_false()

    print()
    print("=== Channel routing and content ===")
    test_trade_executed_routes_to_trades()
    test_circuit_breaker_routes_to_alerts()
    test_kill_switch()
    test_api_error_warning_below_threshold()
    test_api_error_critical_at_threshold()
    test_regulatory_alert_critical()
    test_daily_summary_routes_to_daily()
    test_weekly_summary_routes_to_weekly()

    print()
    print("=== Rate limiting ===")
    test_rate_limit_drops_excess()
    test_critical_bypasses_rate_limit()

    print()
    print("=== Helpers and edge cases ===")
    test_format_metric()
    test_circuit_threshold()
    test_no_url_skips_silently()
    test_configured_channels()
    test_position_closed_pnl_sign()

    print()
    print("All tests passed.")
