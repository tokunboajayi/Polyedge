"""
Environment variable loading for PolyEdge v5.
All configuration is sourced from .env via python-dotenv.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _require(key: str) -> str:
    """Return env var value or raise if missing/empty."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

# ---------------------------------------------------------------------------
# Kalshi
# ---------------------------------------------------------------------------
KALSHI_API_KEY_ID: str = _require("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH: str = _require("KALSHI_PRIVATE_KEY_PATH")
KALSHI_EMAIL: str = _require("KALSHI_EMAIL")

# 'demo' or 'production' — controls which base URL is used
KALSHI_ENV: str = _optional("KALSHI_ENV", "demo")

KALSHI_BASE_URL: str = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if KALSHI_ENV == "demo"
    else "https://api.kalshi.com/trade-api/v2"
)

KALSHI_WS_URL: str = (
    "wss://demo-api.kalshi.co/trade-api/ws/v2"
    if KALSHI_ENV == "demo"
    else "wss://api.kalshi.com/trade-api/ws/v2"
)

# ---------------------------------------------------------------------------
# Slack webhooks
# ---------------------------------------------------------------------------
SLACK_WEBHOOK_TRADES: str = _optional("SLACK_WEBHOOK_TRADES")
SLACK_WEBHOOK_ALERTS: str = _optional("SLACK_WEBHOOK_ALERTS")
SLACK_WEBHOOK_DAILY: str = _optional("SLACK_WEBHOOK_DAILY")
SLACK_WEBHOOK_WEEKLY: str = _optional("SLACK_WEBHOOK_WEEKLY")

# ---------------------------------------------------------------------------
# Backblaze B2
# ---------------------------------------------------------------------------
BACKBLAZE_KEY_ID: str = _optional("BACKBLAZE_KEY_ID")
BACKBLAZE_APPLICATION_KEY: str = _optional("BACKBLAZE_APPLICATION_KEY")
BACKBLAZE_BUCKET_NAME: str = _optional("BACKBLAZE_BUCKET_NAME", "polyedge-backups")

# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
STARTING_BANKROLL: float = float(_optional("STARTING_BANKROLL", "500"))
