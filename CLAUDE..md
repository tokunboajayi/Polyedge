# CLAUDE.md — PolyEdge v5 (Kalshi-Native)

## Project Overview

PolyEdge is an autonomous Kalshi trading bot that exploits speed defects and analytical depth advantages in CFTC-regulated event contract markets. Reactive traders overweight headlines and underweight base rates. PolyEdge synthesizes multiple signals through Claude to identify mispricings and trades the correction.

**Platform:** Kalshi — CFTC Designated Contract Market, US-legal, USD-settled.
**Capital:** $500 start. Kill switch at $300. Target: $680-850 in 6 months.

## Tech Stack

- **Language:** Python 3.11+
- **Database:** SQLite (daily Backblaze B2 backup)
- **AI:** Anthropic API — Haiku for scans, Sonnet for trade decisions
- **Market API:** Kalshi REST API v2 + WebSocket
- **News Data:** RSS aggregation (Reuters, AP, BBC, NPR)
- **Regulatory Feeds:** Federal Register RSS, SEC EDGAR RSS
- **Monitoring:** Slack webhooks (free tier)
- **Backup:** Backblaze B2 (10GB free)

## Kalshi API Details

- **REST base:** https://api.kalshi.com/trade-api/v2
- **WebSocket:** wss://api.kalshi.com/trade-api/ws/v2
- **Demo base:** https://demo-api.kalshi.co/trade-api/v2
- **Auth:** RSA-PSS key signing (API key ID + private key file)
- **Rate limits (Basic):** 20 reads/sec, 10 writes/sec
- **Public endpoints (no auth):** /markets, /events, /series, /orderbook
- **Market data is free** — no auth needed for reads
- **Fee formula:** Taker = 0.07 × P × (1-P), Maker = 0.0175 × P × (1-P)
- **Contract structure:** Binary, pays $1 YES or $0 NO, priced 1¢-99¢
- **Hierarchy:** Series → Events → Markets

## Directory Structure

```
polyedge/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── .env
├── .gitignore
├── config/
│   ├── settings.py          # Environment variables + config
│   └── constants.py         # Kelly fractions, thresholds, fees
├── core/
│   ├── __init__.py
│   ├── engine.py            # Main trading loop
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── probability_arbitrage.py   # Strategy A
│   │   └── mean_reversion.py          # Strategy B
│   ├── signals/
│   │   ├── __init__.py
│   │   ├── news_catalyst.py           # Support Layer 1
│   │   └── orderbook_confirm.py       # Support Layer 2
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── position_sizer.py          # Fractional Kelly + fee adjustment
│   │   ├── circuit_breakers.py        # Drawdown controls
│   │   └── correlation_manager.py     # Exposure limits
│   └── calibration/
│       ├── __init__.py
│       ├── brier_scorer.py
│       └── calibration_loop.py
├── data/
│   ├── __init__.py
│   ├── kalshi_client.py              # Kalshi REST API wrapper
│   ├── kalshi_websocket.py           # WebSocket streaming
│   ├── rss_aggregator.py             # News feed polling
│   ├── regulatory_feeds.py           # Federal Register + SEC
│   └── market_scanner.py             # Market selection/filtering
├── analysis/
│   ├── __init__.py
│   ├── claude_analyzer.py            # Haiku scan + Sonnet decision
│   └── probability_model.py          # Base-rate models per category
├── persistence/
│   ├── __init__.py
│   ├── database.py                   # SQLite connection + schema
│   ├── models.py                     # Table definitions
│   └── backup.py                     # Backblaze B2 sync
├── monitoring/
│   ├── __init__.py
│   ├── slack_alerts.py               # Webhook integration
│   ├── edge_erosion.py               # 5-metric tracker
│   └── daily_report.py               # P&L summaries
├── scripts/
│   ├── backtest.py                   # Historical backtest runner
│   ├── demo_trade.py                 # Demo environment trading
│   └── seed_data.py                  # Seed historical base rates
├── tests/
│   ├── test_strategies.py
│   ├── test_risk.py
│   ├── test_kalshi_client.py
│   └── test_calibration.py
└── data_store/
    ├── polyedge.db                   # SQLite (gitignored)
    └── backups/
```

## Database Schema

Six tables in SQLite:

- **trades** — entry, exit, P&L, strategy, fees_paid, order_type, ticker (forever)
- **calibration** — predicted_prob, actual_outcome, brier_contribution (forever)
- **markets** — ticker, title, series, category, settlement_date, status (forever)
- **edge_metrics** — daily snapshots: win_rate, sharpe, frequency, avg_edge, brier (180 days)
- **api_costs** — model, tokens, cost_usd per call (90 days)
- **alerts** — circuit breaker triggers, warnings, errors (90 days)

## Key Parameters

```python
# Kelly fractions
KELLY_CALIBRATION = 0.25
KELLY_FULL = 0.35
MAX_POSITION_PCT = 0.05          # 5% of bankroll
MIN_TRADE_SIZE = 5               # $5 minimum (5 contracts)
MAX_OPEN_POSITIONS = 5
MAX_BANKROLL_IN_POSITIONS = 0.50
MAX_CORRELATED_EXPOSURE = 0.30
KILL_SWITCH = 300

# Circuit breakers
DAILY_LOSS_LIMIT = 0.05
WEEKLY_LOSS_LIMIT = 0.10
MONTHLY_LOSS_LIMIT = 0.15

# Strategy A thresholds
DIVERGENCE_THRESHOLD = 10        # Minimum model-market gap (points)
CONVERGENCE_EXIT = 0.03          # Exit within 3% of model

# Strategy B thresholds
SPIKE_THRESHOLD = 0.15           # 15% move in <1 hour
REVERSION_TAKE_PROFIT = 0.50     # 50% reversion
REVERSION_STOP_LOSS = 0.25       # 25% further adverse
SPIKE_WAIT_MINUTES = 30

# Market selection
MIN_MARKET_VOLUME_7D = 5000
MAX_SPREAD = 0.08                # 8¢
MIN_SETTLEMENT_DAYS = 7
MAX_SETTLEMENT_DAYS = 90
MAX_EXIT_SLIPPAGE = 0.03

# Kalshi fees
TAKER_FEE_COEFFICIENT = 0.07
MAKER_FEE_COEFFICIENT = 0.0175

# Tax & costs
TAX_RESERVE_RATE = 0.32
BRIER_THRESHOLD = 0.30
CALIBRATION_FACTOR_RANGE = (0.7, 1.3)

# Polling intervals
RSS_SCAN_INTERVAL = 600          # 10 minutes
KALSHI_POLL_INTERVAL = 30        # 30 seconds
REGULATORY_SCAN_INTERVAL = 1800  # 30 minutes
```

## Kalshi Fee Calculation

```python
def kalshi_fee(price: float, num_contracts: int, is_maker: bool = True) -> float:
    """Calculate Kalshi fee for an order.
    price: contract price in dollars (0.01 to 0.99)
    num_contracts: number of contracts
    is_maker: True for limit orders (1.75%), False for market orders (7%)
    """
    coeff = MAKER_FEE_COEFFICIENT if is_maker else TAKER_FEE_COEFFICIENT
    fee_per_contract = coeff * price * (1 - price)
    total_fee = fee_per_contract * num_contracts
    return math.ceil(total_fee * 100) / 100  # Round up to nearest cent
```

## Strategies

**Strategy A — Probability Model Arbitrage (PRIMARY):**
Base-rate models per category. Trade when model-market divergence >10 points AND divergence exceeds round-trip maker fee drag. Use LIMIT orders (maker). Exit at convergence or hold to settlement.

**Strategy B — Post-Spike Mean Reversion (PRIMARY):**
Fade unjustified >15% spikes. Wait 30-60 min. Take profit at 50% reversion, stop at 25% adverse. LIMIT preferred, MARKET only if reversion window closing. NEVER fade official government/court actions.

**Support Layer 1 — News Catalyst Detection:** RSS scan + Claude classify. Adjusts confidence.
**Support Layer 2 — Orderbook Confirmation:** Check depth, detect spoofing, estimate slippage.

## Three-Phase Ramp

1. **Demo Trading** (30 days) — Kalshi demo environment, zero capital, 50+ predictions
2. **Micro-Live** (until 30 settled) — Production, 0.15x Kelly, $10 max/trade
3. **Calibrated** (ongoing) — 0.35x Kelly, 5% bankroll max

## API Usage Rules

- **Haiku:** RSS scanning, market screening, routine checks
- **Sonnet:** Trade decisions, probability model updates, synthesis
- Log every call to api_costs table
- Stay under 20 reads/sec, 10 writes/sec (Basic tier)

## Coding Conventions

- Python 3.11+, type hints everywhere
- `asyncio` + `aiohttp` for concurrent operations
- `anthropic` SDK for Claude calls
- `requests` for Kalshi REST API (sync is fine at Basic tier rates)
- `websockets` for Kalshi WebSocket streaming
- Config via `python-dotenv` from `.env`
- Logging: `logging` module, structured JSON format
- Money operations: explicit error handling + fallback
- Database: all writes in transactions
- Tests: `pytest`, coverage on risk + calibration modules
- Kalshi API auth: RSA-PSS signing per request

## Commands

```bash
pip install -r requirements.txt
python -m persistence.database --init
python scripts/seed_data.py
python scripts/backtest.py
python scripts/demo_trade.py        # Uses Kalshi demo API
python -m core.engine               # Live trading
pytest tests/ -v
```

## Environment Variables (.env)

```
ANTHROPIC_API_KEY=sk-ant-xxxxx
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_EMAIL=your@email.com
KALSHI_ENV=demo                     # 'demo' or 'production'
SLACK_WEBHOOK_TRADES=https://hooks.slack.com/services/xxx
SLACK_WEBHOOK_ALERTS=https://hooks.slack.com/services/xxx
SLACK_WEBHOOK_DAILY=https://hooks.slack.com/services/xxx
SLACK_WEBHOOK_WEEKLY=https://hooks.slack.com/services/xxx
BACKBLAZE_KEY_ID=xxx
BACKBLAZE_APPLICATION_KEY=xxx
BACKBLAZE_BUCKET_NAME=polyedge-backups
STARTING_BANKROLL=500
```

## Safety Rules

- Never hold >50% of bankroll in open positions
- Never trade markets settling in <48 hours
- Never fade official government/court/regulatory actions
- 2+ dependency failures → Safe Mode
- 7 days no operator interaction → auto-pause, close positions
- Bankroll < $300 → kill switch
- All limit orders preferred (maker fee 75% cheaper than taker)

## Build Order

1. persistence/database.py + models.py — SQLite schema
2. config/settings.py + constants.py — parameters from .env
3. data/kalshi_client.py — REST API wrapper (auth, markets, orders, orderbook)
4. data/kalshi_websocket.py — WebSocket streaming
5. data/rss_aggregator.py — async RSS polling
6. data/regulatory_feeds.py — Federal Register + SEC
7. data/market_scanner.py — entry/exclusion criteria filtering
8. monitoring/slack_alerts.py — webhooks
9. analysis/claude_analyzer.py — Haiku + Sonnet wrapper
10. analysis/probability_model.py — base-rate models
11. core/strategies/probability_arbitrage.py — Strategy A
12. core/strategies/mean_reversion.py — Strategy B
13. core/signals/news_catalyst.py — Support Layer 1
14. core/signals/orderbook_confirm.py — Support Layer 2
15. core/risk/position_sizer.py — Kelly + fee adjustment
16. core/risk/circuit_breakers.py — drawdown controls
17. core/risk/correlation_manager.py — exposure limits
18. core/calibration/brier_scorer.py + calibration_loop.py
19. monitoring/edge_erosion.py — 5-metric tracker
20. monitoring/daily_report.py — P&L summaries
21. persistence/backup.py — Backblaze B2 sync
22. scripts/backtest.py — historical backtest (KILL GATE)
23. scripts/demo_trade.py — Kalshi demo environment
24. core/engine.py — main orchestrator
