# PolyEdge v5 — Executive Summary

## What It Is

PolyEdge v5 is an autonomous trading bot for **Kalshi**, the only CFTC-regulated prediction market exchange legal for US residents. It uses Claude AI (Haiku for scanning, Sonnet for decisions) to identify mispricings in binary event contracts and trade the corrections.

## Core Thesis

Kalshi's markets are young, thin, and retail-dominated. Headline-reactive traders overweight news sentiment and underweight base-rate probabilities. PolyEdge exploits this by combining statistical models with Claude-powered analysis to find contracts where the market price diverges from fair value by 10+ percentage points — then trading the convergence.

## Two Primary Strategies

**Strategy A — Probability Model Arbitrage:** Build base-rate probability models per category. When the model says 68% but the contract trades at 52¢, buy YES via limit order. Exit on convergence or hold to settlement.

**Strategy B — Post-Spike Mean Reversion:** When contracts spike >15% in under 1 hour on news, analyze whether the move is justified. Fade unjustified moves. Never fade official government/regulatory actions.

## Financial Parameters

| Parameter | Value |
|---|---|
| Starting Capital | $500 |
| Kill Switch | $300 (full stop) |
| Monthly Costs | $10–23 (Claude API + trading fees) |
| 6-Month Target | $680–850 (conservative) |
| Position Sizing | Fractional Kelly (0.25x → 0.35x after calibration) |
| Max Per Trade | 5% of bankroll |
| Max Open Positions | 5 simultaneous |
| Tax Reserve | 32% of gross profits |

## Key Advantages Over v4 (Polymarket)

Zero crypto infrastructure — no Polygon, MetaMask, MATIC, or gas fees. USD settlement via ACH. CFTC-regulated (funds protected). Maker orders cut fees by 75% (1.75% vs 7% coefficient). Monthly cost reduced from $40+ to $10–23.

## Risk Management

Four-tier circuit breaker system: daily loss >5% (24hr pause), weekly >10% (72hr pause), monthly >15% (manual review), bankroll <$300 (kill switch). Maximum 30% correlated exposure. Edge erosion detector tracks win rate, Sharpe ratio, Brier score, trade frequency, and average edge on rolling 30-day windows — auto-pauses on critical degradation.

## Six-Phase Rollout

1. **Setup** (Week 1): API keys, SQLite schema, demo environment
2. **Data Pipeline** (Weeks 2–3): Kalshi client, RSS feeds, market scanner
3. **Analysis Engine** (Weeks 3–5): Claude integration, probability models
4. **Backtest** (Weeks 5–7): 200+ settled markets — **kill gate if Sharpe <0.5 or win rate <50%**
5. **Demo Trading** (Weeks 7–11): 30-day paper trading on Kalshi demo API
6. **Micro-Live → Full Operation** (Week 11+): Real capital, graduating Kelly fraction

## Edge Lifespan

Estimated 12–24 months before AI competition narrows mispricings. Plan to scale or pivot before Month 18.

## Current Status

System is operational in **demo mode**. Database contains 77 trades across 14,251 tracked markets. Current bankroll: **$455.51** (from $500 start, reflecting 5 open positions). The bot has completed 6 scan cycles, processing 10,000 markets per cycle with 98 passing tradability filters. 559 Claude API calls made at $0.26 total cost. Zero errors logged — only 2 minor warnings (404 on a delisted market).

---

*Generated from PolyEdge_v5_Kalshi_Architecture.md + live system data — April 6, 2026*
