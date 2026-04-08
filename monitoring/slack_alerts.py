"""
Slack webhook alerting for PolyEdge v5.

Channels
--------
  TRADES   — every order fill, position open/close        (info)
  ALERTS   — circuit breakers, API errors, regulatory     (warning / critical)
  DAILY    — P&L summary, edge-metric snapshot            (info)
  WEEKLY   — weekly performance report                    (info)

Alert levels
------------
  INFO      — trades executed, daily summaries, system status
  WARNING   — edge metric degradation, dependency failures
  CRITICAL  — circuit breakers tripped, kill-switch, regulatory flag,
               consecutive API errors, safe-mode entry

Message shape
-------------
Every alert is a Slack Block Kit message with three sections:
  1. Header  — coloured emoji prefix + level badge + title
  2. Context — timestamp UTC | bankroll | open positions | env
  3. Body    — alert-specific fields rendered as a compact key-value block
  4. Action  — suggested action for the operator (footer)

Rate limiting
-------------
Slack's incoming-webhook limit is ~1 msg/sec per URL.  A per-channel
token-bucket (1 token/sec, burst of 3) drops messages that arrive faster
than the limit and logs a warning instead of raising.  Critical messages
bypass the drop (they are queued and retried once).

Retry
-----
HTTP 429 or 5xx → exponential backoff, 3 attempts maximum.
Delivery failures are logged locally; they never raise to the caller.

Usage
-----
    alerts = SlackAlerter()                        # loads URLs from settings

    alerts.trade_executed(
        ticker="INXW-26APR04-T5300",
        side="yes", action="buy",
        contracts=10, price=0.52,
        fees=0.06, strategy="probability_arbitrage",
        bankroll=487.50, open_positions=2,
    )

    alerts.circuit_breaker(
        trigger="daily_loss_limit",
        loss_pct=0.062,
        bankroll=469.00, open_positions=3,
    )
"""

import dataclasses
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLACK_TIMEOUT_S: float   = 8.0
_MAX_RETRIES:      int    = 3
_BUCKET_RATE:      float  = 1.0   # tokens / second
_BUCKET_BURST:     int    = 10    # max burst tokens per channel

# Emoji prefix per level
_LEVEL_EMOJI: dict[str, str] = {
    "info":     ":large_blue_circle:",
    "warning":  ":large_yellow_circle:",
    "critical": ":red_circle:",
}

_LEVEL_LABEL: dict[str, str] = {
    "info":     "INFO",
    "warning":  "WARNING",
    "critical": "CRITICAL",
}

# Channel logical names → settings attribute names
_CHANNEL_SETTING: dict[str, str] = {
    "trades":  "SLACK_WEBHOOK_TRADES",
    "alerts":  "SLACK_WEBHOOK_ALERTS",
    "daily":   "SLACK_WEBHOOK_DAILY",
    "weekly":  "SLACK_WEBHOOK_WEEKLY",
}


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (per-channel)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Thread-safe token bucket: 1 token/sec, configurable burst."""

    def __init__(self, rate: float = _BUCKET_RATE, burst: int = _BUCKET_BURST) -> None:
        self._rate   = rate
        self._burst  = float(burst)
        self._tokens = float(burst)
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def consume(self) -> bool:
        """Attempt to consume one token.  Returns True if allowed."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._burst,
                self._tokens + (now - self._last) * self._rate,
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def _ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _context_block(bankroll: float, open_positions: int, env: str) -> dict:
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"*{_ts_utc()}*"},
            {"type": "mrkdwn", "text": f"Bankroll: *${bankroll:,.2f}*"},
            {"type": "mrkdwn", "text": f"Open: *{open_positions}* pos"},
            {"type": "mrkdwn", "text": f"Env: *{env}*"},
        ],
    }


def _fields_block(fields: dict[str, Any]) -> dict:
    """Render a dict as two-column mrkdwn fields."""
    items = [
        {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
        for k, v in fields.items()
    ]
    return {"type": "section", "fields": items}


def _action_block(action: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f":arrow_right: *Action:* {action}"}],
    }


def _divider() -> dict:
    return {"type": "divider"}


def build_payload(
    level:          str,
    title:          str,
    fields:         dict[str, Any],
    action:         str,
    bankroll:       float,
    open_positions: int,
    env:            str,
) -> dict:
    """Assemble a complete Slack Block Kit payload dict."""
    emoji = _LEVEL_EMOJI.get(level, ":white_circle:")
    label = _LEVEL_LABEL.get(level, level.upper())

    header = {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"{emoji}  [{label}]  {title}",
            "emoji": True,
        },
    }

    blocks = [
        header,
        _context_block(bankroll, open_positions, env),
        _divider(),
        _fields_block(fields),
        _divider(),
        _action_block(action),
    ]

    return {
        "blocks": blocks,
        # Fallback text for notifications / accessibility
        "text": f"[{label}] {title} | bankroll=${bankroll:,.2f}",
    }


# ---------------------------------------------------------------------------
# HTTP sender
# ---------------------------------------------------------------------------

def _send_http(url: str, payload: dict) -> bool:
    """POST payload to a Slack webhook URL.  Returns True on success."""
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=_SLACK_TIMEOUT_S,
            )
            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning(
                    "slack_rate_limited  attempt=%d  retry_after=%.1fs", attempt + 1, retry_after
                )
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                wait = 2.0 ** attempt
                logger.warning(
                    "slack_server_error  status=%d  attempt=%d  retry_in=%.1fs",
                    resp.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
                continue

            # 4xx other than 429 — bad config, don't retry
            logger.error(
                "slack_client_error  status=%d  body=%s",
                resp.status_code, resp.text[:200],
            )
            return False

        except requests.exceptions.RequestException as exc:
            last_exc = exc
            wait = 2.0 ** attempt
            logger.warning(
                "slack_network_error  attempt=%d  error=%s  retry_in=%.1fs",
                attempt + 1, exc, wait,
            )
            time.sleep(wait)

    logger.error("slack_delivery_failed  retries=%d  last_error=%s", _MAX_RETRIES, last_exc)
    return False


# ---------------------------------------------------------------------------
# SlackAlerter
# ---------------------------------------------------------------------------

class SlackAlerter:
    """Sends structured, rate-limited Slack alerts across four channels.

    All public methods are fire-and-forget: delivery failures are logged but
    never raised.  Critical alerts bypass the rate-limit drop and are retried.

    Args:
        webhook_trades:  URL for the #polyedge-trades channel.
        webhook_alerts:  URL for the #polyedge-alerts channel.
        webhook_daily:   URL for the #polyedge-daily channel.
        webhook_weekly:  URL for the #polyedge-weekly channel.
        env:             "demo" or "production" (shown in every message).
    """

    def __init__(
        self,
        webhook_trades: str | None = None,
        webhook_alerts: str | None = None,
        webhook_daily:  str | None = None,
        webhook_weekly: str | None = None,
        env:            str | None = None,
    ) -> None:
        from config import settings as S

        # Use provided value if not None; fall back to settings only for None.
        # Empty string "" is intentional "no URL configured" and is kept as-is.
        self._urls: dict[str, str] = {
            "trades": S.SLACK_WEBHOOK_TRADES if webhook_trades is None else webhook_trades,
            "alerts": S.SLACK_WEBHOOK_ALERTS if webhook_alerts is None else webhook_alerts,
            "daily":  S.SLACK_WEBHOOK_DAILY  if webhook_daily  is None else webhook_daily,
            "weekly": S.SLACK_WEBHOOK_WEEKLY if webhook_weekly is None else webhook_weekly,
        }
        self._env = env or S.KALSHI_ENV

        # One rate-limiter bucket per channel
        self._buckets: dict[str, _TokenBucket] = {
            ch: _TokenBucket() for ch in self._urls
        }

        logger.info(
            "SlackAlerter ready  env=%s  channels=%s",
            self._env,
            [ch for ch, url in self._urls.items() if url],
        )

    # ------------------------------------------------------------------
    # Core dispatch
    # ------------------------------------------------------------------

    def _send(
        self,
        channel:        str,
        level:          str,
        title:          str,
        fields:         dict[str, Any],
        action:         str,
        bankroll:       float,
        open_positions: int,
    ) -> bool:
        """Build and deliver one alert message.

        Critical messages bypass rate-limit drops — they are sent even if
        the bucket is empty (with a 1-second forced delay instead).
        """
        url = self._urls.get(channel, "")
        if not url:
            logger.debug("slack_no_url  channel=%s  skipping", channel)
            return False

        payload = build_payload(
            level=level,
            title=title,
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
            env=self._env,
        )

        bucket = self._buckets[channel]
        allowed = bucket.consume()

        if not allowed:
            if level == "critical":
                # Critical: force a 1-second pause rather than drop
                logger.warning(
                    "slack_rate_limit_bypass  channel=%s  level=critical  sleeping_1s",
                    channel,
                )
                time.sleep(1.0)
            else:
                logger.warning(
                    "slack_rate_limited_drop  channel=%s  title=%s", channel, title
                )
                return False

        ok = _send_http(url, payload)
        if ok:
            logger.info(
                "slack_sent  channel=%s  level=%s  title=%s", channel, level, title
            )
        return ok

    # ------------------------------------------------------------------
    # Trade alerts → #trades channel
    # ------------------------------------------------------------------

    def trade_executed(
        self,
        ticker:         str,
        side:           str,
        action:         str,
        contracts:      int,
        price:          float,
        fees:           float,
        strategy:       str,
        bankroll:       float,
        open_positions: int,
        order_id:       str = "",
    ) -> bool:
        """Notify that a limit/market order was filled."""
        cost = contracts * price
        fields = {
            "Ticker":    ticker,
            "Action":    f"{action.upper()} {side.upper()}",
            "Contracts": str(contracts),
            "Price":     f"${price:.2f}",
            "Cost":      f"${cost:.2f}",
            "Fees":      f"${fees:.4f}",
            "Strategy":  strategy,
            "Order ID":  order_id or "—",
        }
        return self._send(
            channel="trades",
            level="info",
            title=f"Order filled — {ticker}",
            fields=fields,
            action="Monitor position; set exit alerts.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    def position_closed(
        self,
        ticker:         str,
        side:           str,
        contracts:      int,
        entry_price:    float,
        exit_price:     float,
        pnl:            float,
        fees_total:     float,
        strategy:       str,
        bankroll:       float,
        open_positions: int,
    ) -> bool:
        """Notify that a position was fully closed."""
        pnl_sign = "+" if pnl >= 0 else ""
        fields = {
            "Ticker":      ticker,
            "Side":        side.upper(),
            "Contracts":   str(contracts),
            "Entry":       f"${entry_price:.2f}",
            "Exit":        f"${exit_price:.2f}",
            "Net P&L":     f"{pnl_sign}${pnl:.2f}",
            "Total Fees":  f"${fees_total:.4f}",
            "Strategy":    strategy,
        }
        return self._send(
            channel="trades",
            level="info",
            title=f"Position closed — {ticker}  ({pnl_sign}${pnl:.2f})",
            fields=fields,
            action="No action required." if pnl >= 0 else "Review strategy edge.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Circuit-breaker / kill-switch alerts → #alerts channel
    # ------------------------------------------------------------------

    def circuit_breaker(
        self,
        trigger:        str,
        loss_pct:       float,
        bankroll:       float,
        open_positions: int,
        resume_in:      str = "automatic",
    ) -> bool:
        """Notify that a drawdown circuit breaker was tripped."""
        trigger_labels = {
            "daily_loss_limit":   "Daily loss limit (5%)",
            "weekly_loss_limit":  "Weekly loss limit (10%)",
            "monthly_loss_limit": "Monthly loss limit (15%)",
        }
        fields = {
            "Trigger":   trigger_labels.get(trigger, trigger),
            "Loss":      f"{loss_pct*100:.2f}%",
            "Threshold": _circuit_threshold(trigger),
            "Resume":    resume_in,
            "Status":    "TRADING PAUSED",
        }
        return self._send(
            channel="alerts",
            level="critical",
            title=f"Circuit breaker tripped — {trigger}",
            fields=fields,
            action=f"Trading paused. Resume: {resume_in}. Review recent trades.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    def kill_switch(
        self,
        bankroll:       float,
        open_positions: int,
        reason:         str = "bankroll_below_threshold",
    ) -> bool:
        """Notify that the kill switch fired and all positions are being closed."""
        fields = {
            "Bankroll":   f"${bankroll:,.2f}",
            "Threshold":  f"${C.KILL_SWITCH:,.2f}",
            "Reason":     reason,
            "Status":     "ALL POSITIONS CLOSING",
        }
        return self._send(
            channel="alerts",
            level="critical",
            title="KILL SWITCH ACTIVATED",
            fields=fields,
            action="Immediate action required: check all open positions and withdraw funds.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    def safe_mode_entered(
        self,
        reason:          str,
        failed_services: list[str],
        bankroll:        float,
        open_positions:  int,
    ) -> bool:
        """Notify that the engine entered safe mode (2+ dependency failures)."""
        fields = {
            "Reason":   reason,
            "Failed":   ", ".join(failed_services) or "unknown",
            "Max Fail": str(C.MAX_DEPENDENCY_FAILURES),
            "Status":   "SAFE MODE — no new trades",
        }
        return self._send(
            channel="alerts",
            level="critical",
            title="Safe mode entered",
            fields=fields,
            action="Check failed services and restart if resolved.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Edge-metric / warning alerts → #alerts channel
    # ------------------------------------------------------------------

    def edge_metric_warning(
        self,
        metric:         str,
        value:          float,
        threshold:      float,
        severity:       str,          # "warning" | "critical"
        bankroll:       float,
        open_positions: int,
    ) -> bool:
        """Notify that an edge erosion metric entered warning or critical range."""
        fields = {
            "Metric":    metric,
            "Value":     _format_metric(metric, value),
            "Threshold": _format_metric(metric, threshold),
            "Severity":  severity.upper(),
        }
        action = (
            "Monitor closely; reduce position sizes."
            if severity == "warning"
            else "Auto-pausing trading. Investigate edge erosion immediately."
        )
        return self._send(
            channel="alerts",
            level=severity,
            title=f"Edge metric {severity} — {metric}",
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
        )

    def api_error(
        self,
        service:         str,
        error_message:   str,
        consecutive:     int,
        bankroll:        float,
        open_positions:  int,
    ) -> bool:
        """Notify of consecutive API errors (Kalshi or Claude)."""
        level = "critical" if consecutive >= 3 else "warning"
        fields = {
            "Service":     service,
            "Error":       error_message[:120],
            "Consecutive": str(consecutive),
        }
        action = (
            "Monitor; likely transient."
            if consecutive < 3
            else "Check API status pages. Bot may enter safe mode."
        )
        return self._send(
            channel="alerts",
            level=level,
            title=f"API error — {service} ({consecutive} consecutive)",
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Credit exhaustion alert → #alerts channel
    # ------------------------------------------------------------------

    def credits_exhausted(
        self,
        bankroll:       float,
        open_positions: int,
    ) -> bool:
        """Alert that Anthropic API credits are exhausted — trading halted."""
        return self._send(
            channel="alerts",
            level="critical",
            title="ANTHROPIC CREDITS EXHAUSTED",
            fields={
                "Status":  "Trading halted — no new positions will be opened",
                "Reason":  "Anthropic API returned a credit/quota exhaustion error",
            },
            action=(
                "Top up credits at console.anthropic.com — "
                "no new trades until resolved."
            ),
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Regulatory alert → #alerts channel
    # ------------------------------------------------------------------

    def regulatory_alert(
        self,
        title:          str,
        source:         str,
        url:            str,
        matched:        list[str],
        alert_level:    str,
        bankroll:       float,
        open_positions: int,
        summary:        str = "",
    ) -> bool:
        """Notify that a regulatory filing triggered a keyword match."""
        fields = {
            "Source":   source,
            "Level":    alert_level.upper(),
            "Keywords": ", ".join(matched[:5]),
            "URL":      url[:80] or "—",
        }
        if summary:
            fields["Summary"] = summary[:200]

        action = (
            "Immediate review required. Consider pausing trading."
            if alert_level == "critical"
            else "Review filing. Monitor for escalation."
        )
        return self._send(
            channel="alerts",
            level=alert_level,
            title=f"Regulatory — {title[:60]}",
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Operator idle alert → #alerts channel
    # ------------------------------------------------------------------

    def operator_idle(
        self,
        idle_days:      int,
        bankroll:       float,
        open_positions: int,
    ) -> bool:
        """Notify that the operator has been inactive beyond the safe threshold."""
        fields = {
            "Idle":       f"{idle_days} days",
            "Threshold":  f"{C.OPERATOR_IDLE_PAUSE_DAYS} days",
            "Status":     "AUTO-PAUSE IMMINENT" if idle_days < C.OPERATOR_IDLE_PAUSE_DAYS
                          else "AUTO-PAUSED",
        }
        level = "critical" if idle_days >= C.OPERATOR_IDLE_PAUSE_DAYS else "warning"
        return self._send(
            channel="alerts",
            level=level,
            title=f"Operator idle — {idle_days}d without interaction",
            fields=fields,
            action="Log in and confirm system health to resume.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Daily / weekly summaries → #daily / #weekly channels
    # ------------------------------------------------------------------

    def daily_summary(
        self,
        date:            str,
        trades_today:    int,
        pnl_today:       float,
        pnl_mtd:         float,
        win_rate_30d:    float,
        brier_30d:       float,
        api_cost_today:  float,
        bankroll:        float,
        open_positions:  int,
    ) -> bool:
        """Send the daily P&L and performance summary."""
        pnl_sign = "+" if pnl_today >= 0 else ""
        fields = {
            "Date":            date,
            "Trades today":    str(trades_today),
            "P&L today":       f"{pnl_sign}${pnl_today:.2f}",
            "P&L MTD":         f"{'+'if pnl_mtd>=0 else ''}${pnl_mtd:.2f}",
            "Win rate (30d)":  f"{win_rate_30d*100:.1f}%",
            "Brier (30d)":     f"{brier_30d:.3f}",
            "API cost today":  f"${api_cost_today:.4f}",
        }
        action = "No action required." if pnl_today >= 0 else "Review losing trades."
        return self._send(
            channel="daily",
            level="info",
            title=f"Daily summary — {date}",
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
        )

    def weekly_summary(
        self,
        week_label:      str,
        trades_week:     int,
        pnl_week:        float,
        pnl_ytd:         float,
        win_rate_week:   float,
        sharpe_30d:      float,
        avg_edge_30d:    float,
        api_cost_week:   float,
        bankroll:        float,
        open_positions:  int,
    ) -> bool:
        """Send the weekly performance report."""
        pnl_sign = "+" if pnl_week >= 0 else ""
        fields = {
            "Week":           week_label,
            "Trades":         str(trades_week),
            "P&L week":       f"{pnl_sign}${pnl_week:.2f}",
            "P&L YTD":        f"{'+'if pnl_ytd>=0 else ''}${pnl_ytd:.2f}",
            "Win rate":       f"{win_rate_week*100:.1f}%",
            "Sharpe (30d)":   f"{sharpe_30d:.2f}",
            "Avg edge (30d)": f"{avg_edge_30d*100:.2f}%",
            "API cost week":  f"${api_cost_week:.4f}",
        }
        action = "Review edge metrics for the week."
        return self._send(
            channel="weekly",
            level="info",
            title=f"Weekly report — {week_label}",
            fields=fields,
            action=action,
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # System / generic
    # ------------------------------------------------------------------

    def system_info(
        self,
        message:        str,
        details:        dict[str, Any],
        bankroll:       float,
        open_positions: int,
        channel:        str = "alerts",
    ) -> bool:
        """Send a freeform informational system message."""
        return self._send(
            channel=channel,
            level="info",
            title=message,
            fields=details,
            action="No action required.",
            bankroll=bankroll,
            open_positions=open_positions,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def configured_channels(self) -> list[str]:
        """Return the names of channels that have a webhook URL configured."""
        return [ch for ch, url in self._urls.items() if url]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _circuit_threshold(trigger: str) -> str:
    mapping = {
        "daily_loss_limit":   f"{C.DAILY_LOSS_LIMIT*100:.0f}%",
        "weekly_loss_limit":  f"{C.WEEKLY_LOSS_LIMIT*100:.0f}%",
        "monthly_loss_limit": f"{C.MONTHLY_LOSS_LIMIT*100:.0f}%",
    }
    return mapping.get(trigger, "—")


def _format_metric(metric: str, value: float) -> str:
    """Human-friendly formatting for edge erosion metric values."""
    pct_metrics = {"win_rate", "avg_edge"}
    if metric in pct_metrics:
        return f"{value*100:.1f}%"
    if metric == "brier_score":
        return f"{value:.3f}"
    if metric == "sharpe_ratio":
        return f"{value:.2f}"
    if metric == "trade_frequency":
        return f"{value:.1f}/day"
    return str(round(value, 4))
