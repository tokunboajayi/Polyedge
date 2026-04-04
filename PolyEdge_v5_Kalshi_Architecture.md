# POLYEDGE v5 — Kalshi-Native Architecture

## Autonomous Prediction Market Trading System

**Verdict: BUILD | Tier 3 | $500 Start | 6-Month Horizon | US-Legal | CFTC-Regulated**

This is a ground-up rebuild of PolyEdge targeting Kalshi — the only CFTC-regulated prediction market exchange legal for US residents. Every component from v4 has been re-evaluated for Kalshi's market structure, fee model, API design, and regulatory framework. This is not a port — it is a new system.

---

## 1. Executive Summary

PolyEdge v5 is an autonomous Kalshi trading bot that profits from speed defects created by reactive traders and analytical depth advantages in event contract markets. The system uses Claude-powered deep analysis to identify mispricings, fade overreactions to news, and exploit base-rate probability divergences.

**Platform:** Kalshi (kalshi.com) — CFTC Designated Contract Market, US-legal, USD-settled.

**Core thesis:** Event contracts on Kalshi are priced by a mix of retail traders and early-stage bots. Reactive traders overweight headlines and underweight base rates. PolyEdge synthesizes multiple signals through Claude to identify when market prices diverge from statistical fair value, and trades the correction.

**Starting capital:** $500. Kill switch at $300. Realistic 6-month target: $680-$850 (conservative due to Kalshi fees). Break-even: 8% gross monthly.

**Why Kalshi over Polymarket:**
- US-legal (Polymarket blocks US users from trading API)
- USD settlement via ACH (no crypto, no gas fees, no wallet complexity)
- CFTC-regulated (funds protected, no regulatory shutdown risk)
- Built-in demo environment for paper trading
- Simpler API (unified REST + WebSocket, JWT auth)
- Lower operational complexity = fewer failure modes

**What's different from v4:**
- Zero crypto infrastructure (no Polygon, no MetaMask, no MATIC, no gas)
- Fee model integrated into position sizing (Kalshi's parabolic fee structure)
- Maker-order priority strategy (1.75% maker fee vs 7% taker fee = 4x cost advantage)
- Demo environment replaces custom paper-trading system
- RSA-PSS authentication replaces wallet signing
- Market structure adapted: Series → Events → Markets hierarchy
- Monthly cost reduced to $8-15 (Claude API only — no gas, no RPC)

---

## 2. Cost Model

| Cost Item | Monthly Cost | Notes |
|---|---|---|
| Claude API (Haiku + Sonnet) | $8-15 | Haiku for scans, Sonnet for decisions |
| Kalshi trading fees | $2-8 | ~1-2¢ per contract, maker orders preferred |
| Kalshi platform | $0 | Free API, free account, free data |
| Server | $0 | Local machine or Railway free tier |
| News data (RSS) | $0 | Reuters, AP, BBC, NPR feeds |
| Monitoring (Slack) | $0 | Webhook free tier |
| Backblaze B2 backup | $0 | First 10GB free |
| **TOTAL** | **$10-23** | Break-even: ~$40-50/month gross profit |

**DESIGN DECISION — Maker vs Taker fees:**
Kalshi's fee formula: Taker = 0.07 × P × (1-P), Maker = 0.0175 × P × (1-P), where P = contract price in dollars. At 50¢ (maximum fee point), taker pays 1.75¢/contract while maker pays 0.44¢/contract. PolyEdge uses limit orders (maker) by default, reducing fees by 75%. Only use market orders (taker) for time-critical mean reversion entries.

---

## 3. Kalshi Platform Specifics

### 3.1 Market Structure
Kalshi organizes contracts in a three-level hierarchy:
- **Series:** Recurring category (e.g., "S&P 500 closing price", "Fed rate decision")
- **Events:** Specific instance within a series (e.g., "Will the Fed cut rates in June 2026?")
- **Markets:** Tradeable binary contracts within an event (YES/NO, pays $1 or $0)

Contract prices range from 1¢ to 99¢ and reflect the market's perceived probability. A contract priced at 65¢ implies a 65% probability the event occurs.

### 3.2 API Architecture
- **REST API:** https://api.kalshi.com/trade-api/v2
- **WebSocket:** wss://api.kalshi.com/trade-api/ws/v2
- **Demo API:** https://demo-api.kalshi.co/trade-api/v2
- **Authentication:** RSA-PSS key signing (API key + private key from dashboard)
- **Rate limits (Basic tier):** 20 reads/sec, 10 writes/sec
- **Market data:** Free, no authentication required for public endpoints

### 3.3 Regulatory & Legal Status
Kalshi is a CFTC Designated Contract Market (DCM). This means:
- Funds held in regulated accounts with segregation requirements
- No regulatory shutdown risk (unlike Polymarket's CFTC settlement history)
- Bot trading explicitly supported and encouraged via API
- KYC required (US ID + SSN, 1-2 business day verification)
- All profits are taxable as short-term capital gains
- Georgia state + federal tax applies (same 32% reserve as v4)

### 3.4 Fee Structure Deep Dive
Fee per contract = coefficient × P × (1-P), rounded up to nearest cent on total order.

| Contract Price | Taker Fee (7%) | Maker Fee (1.75%) | Fee as % of Price |
|---|---|---|---|
| 10¢ | 0.63¢ | 0.16¢ | 6.3% / 1.6% |
| 25¢ | 1.31¢ | 0.33¢ | 5.3% / 1.3% |
| 50¢ | 1.75¢ | 0.44¢ | 3.5% / 0.9% |
| 75¢ | 1.31¢ | 0.33¢ | 1.7% / 0.4% |
| 90¢ | 0.63¢ | 0.16¢ | 0.7% / 0.2% |

**DESIGN DECISION:** Fees are highest at 50¢ and lowest at extremes. Strategy A (Probability Arbitrage) naturally targets contracts where model divergence exists — often at 30-70¢ range where fees are higher. This is factored into the edge threshold: minimum 10-point divergence must exceed round-trip fee drag (~2-4% for maker orders). Strategy B (Mean Reversion) targets post-spike contracts that have moved toward extremes, where fees are lower.

---

## 4. The Speed Defect Thesis (Kalshi-Adapted)

The thesis adapts to Kalshi's specific market dynamics:

**Kalshi is newer and less efficient than traditional markets.** Launched in 2021, Kalshi's order books are thinner and pricing is driven more by retail sentiment than institutional analysis. This creates larger and more persistent mispricings than you'd find in equities.

**News-reactive traders dominate.** When a headline drops, retail traders and simple bots push prices in seconds. But Kalshi's event contracts are binary (resolve YES/NO), and headline sentiment often doesn't correctly predict binary resolution probability.

**Category expertise creates edge.** Kalshi covers economics, weather, politics, tech, and culture. Most traders don't specialize — they react to headlines across all categories. A bot with deep base-rate models in 3-5 categories has an analytical monopoly.

**API latency enables deliberate trading.** Kalshi's REST API has 50-200ms latency. Combined with Basic tier rate limits (20 reads/sec), HFT is impractical. The market structurally rewards analysis over speed — exactly PolyEdge's thesis.

**Edge lifespan: 12-24 months.** As more AI-powered bots enter Kalshi, mispricings will narrow. Plan to scale or pivot before Month 18.

---

## 5. Strategy Engine

### 5.1 Strategy A (PRIMARY): Probability Model Arbitrage
Build base-rate probability models per event category. Compare model output to Kalshi market price. Trade when divergence exceeds threshold.

- Inputs: Historical base rates, Claude-analyzed conditions, event context
- Signal: Model says 68% probability, Kalshi contract priced at 52¢ = BUY YES at 52¢
- Threshold: Only trade when model-market divergence >10 percentage points AND divergence exceeds round-trip fee drag (maker)
- Exit: Sell when price converges to within 3% of model, or hold to settlement
- Order type: LIMIT (maker fee, 1.75% coefficient)
- Edge: Statistical rigor vs. sentiment-driven pricing

### 5.2 Strategy B (PRIMARY): Post-Spike Mean Reversion
When contracts spike >15% in under 1 hour on news, analyze whether the spike is justified. Fade unjustified moves.

- Inputs: Price velocity, volume spike, news sentiment analysis via Claude
- Signal: Contract moves from 50¢ to 35¢ in 30 min on a tweet with no policy substance = FADE (BUY YES)
- Filter: NEVER fade moves from official government, court, or regulatory actions
- Exit: Take profit at 50% reversion or stop-loss at 25% further adverse move
- Timing: Wait 30-60 minutes after spike before entry (let volatility settle)
- Order type: LIMIT preferred, MARKET (taker) only if reversion window is closing

### 5.3 Support Layer 1: News Catalyst Detection
Claude scans RSS feeds, classifies impact on active Kalshi events, adjusts confidence for Strategies A and B. Does not generate standalone trades.

### 5.4 Support Layer 2: Orderbook Confirmation
Confirms Strategy A/B entries via Kalshi orderbook depth. Only enter if orderbook supports direction. Monitors bid-ask spread and liquidity depth. Rejects entry if spread >8¢.

---

## 6. Data Sources

| Source | Method | Cost | Cadence |
|---|---|---|---|
| Kalshi market data | REST API (public, no auth) | $0 | Every 30 sec on active |
| Kalshi orderbook | REST API (public) | $0 | Every 30 sec on active |
| Kalshi real-time | WebSocket (authenticated) | $0 | Streaming on active |
| News headlines | RSS (Reuters, AP, BBC, NPR) | $0 | Every 10 min |
| Regulatory feeds | Federal Register + SEC EDGAR RSS | $0 | Every 30 min |
| Historical base rates | Local SQLite, seeded + auto-updated | $0 | On settlement |
| Claude analysis | Anthropic API (Haiku/Sonnet) | $8-15/mo | Per signal |

---

## 7. Market Selection Engine

### 7.1 Entry Criteria (ALL must be true)
- Volume: >$5,000 total volume in last 7 days
- Spread: Bid-ask spread <8¢
- Time to settlement: 7-90 days
- Category: Economics, politics, regulatory, macro, tech (NOT sports initially)
- Model confidence: Base-rate model covers category with >50 historical precedents
- Liquidity: Can exit full position with <3% slippage based on orderbook depth
- Fee check: Expected edge > round-trip fee drag (maker)

### 7.2 Exclusion Criteria (ANY triggers skip)
- Presidential/major election marquee markets (too efficient, heavily traded)
- Markets with <$1,000 liquidity
- Ambiguous settlement criteria
- Markets settling in <48 hours
- Sports markets (initially excluded — add at $2K graduation)
- Weather markets (too random without specialized models)
- Markets where single entity controls outcome and has signaled intent

### 7.3 Kalshi-Specific: Fee-Adjusted Edge Threshold
Before entering any position, compute: expected_edge - round_trip_fees > minimum_profit_threshold. For maker orders at 50¢: round-trip fee ≈ 0.88¢ (0.44¢ × 2). For a $25 position (50 contracts), round-trip fee = $0.44. Edge must exceed this plus a margin.

---

## 8. Risk Management Framework

### 8.1 Position Sizing
Fractional Kelly Criterion with fee adjustment:
- 0.25x Kelly during calibration phase (first 30 resolved trades)
- 0.35x Kelly after calibration
- Maximum 5% of bankroll per position ($25 at $500)
- Minimum $5 per trade (5 contracts minimum)
- Kelly input adjusted: subtract expected fee drag from win probability

### 8.2 Drawdown Circuit Breakers

| Trigger | Action | Resume Condition |
|---|---|---|
| Daily loss > 5% | Pause all trading for 24 hours | Automatic after 24 hours |
| Weekly loss > 10% | Pause all trading for 72 hours | Automatic after 72 hours |
| Monthly loss > 15% | Enter review mode, no trading | Manual override after analysis |
| Bankroll < $300 | Kill switch: close all, withdraw | Full system rebuild required |

### 8.3 Correlation Management
- Maximum 30% of bankroll in correlated positions (same category/direction)
- Maximum 5 simultaneous open positions
- Maximum 50% of bankroll in all open positions combined

### 8.4 Tax Reserve
Georgia state: 5.49%. Federal short-term capital gains: up to 24%. Reserve: 32% of gross profits. Held in separate Kalshi account balance or bank account.

---

## 9. Calibration System

### 9.1 Three-Phase Ramp

| Phase | Duration | Kelly | Max/Trade | Environment |
|---|---|---|---|---|
| Demo Trading | 30 days | N/A | $0 | Kalshi demo API (demo-api.kalshi.co) |
| Micro-Live | Until 30 settled | 0.15x | $10 max | Kalshi production |
| Calibrated | Ongoing | 0.35x | 5% bankroll | Kalshi production |

**Demo trading advantage:** Kalshi provides an official demo environment with paper money. No need to build custom paper-trading infrastructure. Full API parity with production.

Go/no-go gates:
- Historical backtest (200+ settled markets): Sharpe >0.5, win rate >50%
- Demo trading (30 days): Brier <0.30, Sharpe >0.5, win rate >52%

---

## 10. Edge Erosion Detector

Five metrics tracked on rolling 30-day window:

| Metric | Healthy | Warning | Critical (Auto-Pause) |
|---|---|---|---|
| Win Rate | >55% | 50-55% | <50% |
| Sharpe Ratio | >1.0 | 0.5-1.0 | <0.5 |
| Trade Frequency | 3-8/day | 1-3 or 8-15/day | <1 or >15/day |
| Average Edge | >5% | 3-5% | <3% |
| Brier Score | <0.20 | 0.20-0.25 | >0.25 |

Any single Critical = auto-pause + Slack alert. Monthly edge report generated.

---

## 11. Data Persistence

SQLite database with daily Backblaze B2 backup:

| Table | Contents | Retention |
|---|---|---|
| trades | Entry, exit, P&L, strategy, fees paid, order type | Forever |
| calibration | Predicted prob, actual outcome, Brier contribution | Forever |
| markets | Active + settled markets, metadata, category, ticker | Forever |
| edge_metrics | Daily snapshots of 5 erosion metrics | Rolling 180 days |
| api_costs | Every Claude API call: model, tokens, cost | Rolling 90 days |
| alerts | Circuit breaker triggers, warnings, errors | Rolling 90 days |

Backup: SQLite synced to Backblaze B2 daily at midnight UTC. Max data loss: 24 hours.

---

## 12. Monitoring & Alerting

Slack webhooks:

| Event | Level | Channel |
|---|---|---|
| Trade executed | Info | #polyedge-trades |
| Circuit breaker triggered | Critical | #polyedge-alerts |
| Edge metric Warning | Warning | #polyedge-alerts |
| Edge metric Critical | Critical | #polyedge-alerts + DM |
| API error (3+ consecutive) | Critical | #polyedge-alerts |
| Daily P&L summary | Info | #polyedge-daily |
| Weekly performance report | Info | #polyedge-weekly |

---

## 13. Graceful Degradation

| Dependency | Failure Mode | Fallback | Impact |
|---|---|---|---|
| Claude API | Timeout / error | Retry 2x, use cached analysis | No new trades |
| Kalshi API | API down | Pause all trading | Full stop |
| RSS feeds | Feed timeout | Cached headlines (max 1hr stale) | Reduced coverage |
| SQLite DB | Corruption | Restore from backup | Max 24hr loss |
| Slack webhooks | Delivery failure | Log locally, retry hourly | Delayed alerts |

**Safe Mode:** 2+ dependencies fail = no new trades, hold positions, escalate alert.

---

## 14. Graduation Thresholds

| Bankroll | Phase | Kelly | Max Position | Changes |
|---|---|---|---|---|
| $500-$2K | Startup | 0.15-0.35x | 5% ($25-100) | Free tier, local machine |
| $2K-$10K | Growth | 0.35x | 5% ($100-500) | Add sports markets, VPS |
| $10K-$50K | Scale | 0.30x | 3% ($300-1,500) | Apply for Advanced API tier, multi-strategy |
| $50K+ | Mature | 0.25x | 2% ($1,000+) | Premier API tier, entity formation, tax advisor |

---

## 15. Backtest & Demo Trading Protocol

### 15.1 Historical Backtest
- Collect 200+ settled Kalshi markets via public API historical data
- For each: record price history, spikes, settlement outcome, category
- Run Claude analysis on news context at time of each spike
- Score: would PolyEdge have traded? Simulated entry/exit/P&L (including fees)
- If Sharpe <0.5 or win rate <50%: STOP. Revise or kill.

### 15.2 Demo Trading (30 Days)
- Full system on Kalshi demo environment (demo-api.kalshi.co)
- Zero real capital. Demo uses paper money.
- Log everything to production database
- After 30 days: Brier <0.30, Sharpe >0.5, win rate >52% to proceed

---

## 16. Implementation Roadmap

| Phase | Duration | Deliverables | Go/No-Go |
|---|---|---|---|
| Phase 0: Setup | Week 1 | API keys, SQLite schema, Slack, demo env | All APIs responding |
| Phase 1: Data Pipeline | Weeks 2-3 | Kalshi client, RSS, market scanner | Pull live data for 20+ markets |
| Phase 2: Analysis Engine | Weeks 3-5 | Claude integration, probability model, signals | Generate estimates for test markets |
| Phase 3: Backtest | Weeks 5-7 | 200+ market backtest | Sharpe >0.5, win rate >50% |
| Phase 4: Demo Trading | Weeks 7-11 | Full system on demo env, 30 days | Brier <0.30, Sharpe >0.5, win rate >52% |
| Phase 5: Micro-Live | Weeks 11-20 | Live, 0.15x Kelly, $10 max/trade | 30 settled trades, calibration computed |
| Phase 6: Full Operation | Week 20+ | 0.35x Kelly, 5% max | Monthly reviews, erosion tracking |

**Critical path:** Phase 3 is the kill gate. Cost to reach it: ~$0.

---

## 17. Financial Projections

| Scenario | Gross Monthly | Costs | Tax Reserve (32%) | Net Monthly | 6-Month Balance |
|---|---|---|---|---|---|
| Conservative | 10% | $15 | 3.2% | ~5.5% | $680 |
| Realistic | 15% | $18 | 4.8% | ~9% | $850 |
| Optimistic | 25% | $22 | 8% | ~15% | $1,200 |
| Break-even | 8% | $15 | 2.6% | ~$20/mo | $600 |
| Failure | N/A | N/A | N/A | N/A | $300 (stop) |

**Note:** Projections are more conservative than v4 because Kalshi's fee structure (even as maker) creates ~1-2% drag per trade that Polymarket didn't have. This is offset by zero gas fees and zero crypto infrastructure costs.

---

## 18. Risk Register

| Risk | Likelihood | Impact | Mitigation | Contingency |
|---|---|---|---|---|
| Claude quality degrades | Medium | High | Brier tracking, auto-pause | Rewrite prompts, test alternatives |
| Edge erodes fast | Medium | High | Monthly erosion tracking | Rotate categories, pause if persistent |
| API cost exceeds budget | Low | Medium | Haiku for scans, Sonnet for decisions | Reduce scan frequency |
| Kalshi API changes | Low | Medium | Monitor changelog, version lock | Adapt client code |
| KYC/account issues | Low | Medium | Keep account in good standing | Contact Kalshi support |
| Database corruption | Low | Medium | Daily B2 backup | Restore, max 24hr loss |
| Operator unavailable 7+ days | Medium | Medium | Auto circuit breakers, safe mode | Bot self-pauses |
| Fee structure changes | Low | Medium | Monitor fee schedule page | Adjust sizing/thresholds |
| State-level restriction (GA) | Very Low | High | Monitor state legislation | Pause, assess compliance |

---

## 19. Contingency Playbook

**IF backtest fails:** Do NOT proceed. Try different categories. If both strategies fail across 3+ categories, KILL.

**IF demo passes but micro-live fails:** Audit execution (fee impact, fill rates, slippage). Run another 30-day demo. If demo succeeds again, issue is execution.

**IF edge erodes <3 months:** Rotate categories. If persistent across 3+, the LLM approach is being competed away. PAUSE.

**IF Kalshi state restriction in Georgia:** Assess whether trading can continue. If not, evaluate migration to another state's legal framework or pivot to Alpaca equities trading.

**IF operator loses interest:** Safe mode after 7 days no interaction. Positions closed. Funds in Kalshi account.

---

## 20. Next 5 Actions

| # | Action | Deadline | Blocked By |
|---|---|---|---|
| 1 | Create Kalshi account, complete KYC, fund with $500 via ACH | Day 1-3 | Nothing |
| 2 | Generate API keys, test demo environment connection | Day 3-5 | KYC approval |
| 3 | Build SQLite schema + RSS aggregator + Slack webhooks | Day 5-10 | Action 2 |
| 4 | Collect 200+ settled markets dataset, run historical backtest | Day 10-21 | Action 3 |
| 5 | If backtest passes: deploy demo trading for 30-day validation | Day 21-51 | Action 4 passes |

**Action 4 is the kill gate.** If backtest fails: 3 weeks invested, $0 lost.

---

## 21. Accounts & Links Required

| Account | URL | Cost | Purpose |
|---|---|---|---|
| Kalshi | https://kalshi.com | Free (KYC required) | Trading platform |
| Anthropic Console | https://console.anthropic.com | ~$10 initial credits | Claude API |
| Slack | https://slack.com/get-started#/createnew | Free | Alerts & monitoring |
| Backblaze B2 | https://www.backblaze.com/sign-up/cloud-storage | Free (10GB) | Database backups |
| Python 3.11+ | https://www.python.org/downloads | Free | Runtime |
| Git | https://git-scm.com/download/win | Free | Version control |
