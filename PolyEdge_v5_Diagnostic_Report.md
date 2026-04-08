# PolyEdge v5 — Full Diagnostic Report
**Date:** 2026-04-07
**Scope:** Complete source-level audit of all key files
**Prepared for:** Master AJ

---

## Table of Contents
1. [What Is Currently Working Correctly](#1-what-is-currently-working-correctly)
2. [Bugs and Errors Found](#2-bugs-and-errors-found)
3. [Parity Gaps: demo_trade.py vs core/engine.py](#3-parity-gaps-demo_tradepy-vs-coreenginepy)
4. [Silent Failure Risks During 30-Day Demo Run](#4-silent-failure-risks-during-30-day-demo-run)
5. [Logging, DB Schema, and Slack Alerting State](#5-logging-db-schema-and-slack-alerting-state)
6. [Prioritized Fix List Before Going Live](#6-prioritized-fix-list-before-going-live)

---

## 1. What Is Currently Working Correctly

### Infrastructure & Persistence
- **`persistence/database.py`** — `init_db()` correctly creates all 6 tables with `IF NOT EXISTS`, verifies they are all present, and raises `RuntimeError` if any are missing. WAL journal mode, foreign key enforcement, and 4MB cache are set on every connection via `get_connection()`. Robust.
- **`persistence/models.py`** — Schema is clean. 6 tables (trades, calibration, markets, edge_metrics, api_costs, alerts), 14 indexes, all correctly defined. `prune_old_rows()` correctly expires edge_metrics (180d), api_costs (90d), alerts (90d).
- **`config/settings.py`** — `.env` loading via `python-dotenv` works correctly. `_require()` guard raises `EnvironmentError` immediately on startup if any critical key is missing — prevents silent misconfiguration.

### Risk Controls
- **`core/risk/circuit_breakers.py`** — Kill switch, daily/weekly/monthly loss tiers, pause durations, and DB persistence to `alerts` table are all correctly implemented in `update()`. Slack fires on every trigger. Rate-limit bypass for critical messages is implemented correctly in `monitoring/slack_alerts.py`.
- **`core/risk/position_sizer.py`** — Kelly formula is correctly implemented with fractional multipliers (0.15x / 0.25x / 0.35x by phase). Hard caps (MAX_POSITION_PCT=5% bankroll, micro_live $10 ceiling) are enforced. Phase selection by resolved-trade count is correct.
- **`config/constants.py`** — Fee math (`kalshi_fee()`, `round_trip_fee()`) is correct. `KILL_SWITCH = 300.0`, `MIN_TRADE_SIZE = 5`, `MAX_OPEN_POSITIONS = 5`, `DIVERGENCE_THRESHOLD = 10.0` all correct.

### Signal Pipeline
- **`core/strategies/probability_arbitrage.py`** — Gate 0 (yes_ask 5–95¢), Gate 1 (action_eligible), Gate 2 (net edge > 0 after fees) are logically sound. `Signal` dataclass with `__slots__` is clean.
- **`analysis/probability_model.py`** — Bayesian blend of base_rate + claude_prob is correctly weighted by confidence tier and calibration sample count (n). Seeded priors per category are reasonable starting values. `_load_base_rate()` correctly joins calibration + markets tables.
- **`monitoring/slack_alerts.py`** — Four-channel architecture (trades, alerts, daily, weekly) with per-channel token-bucket rate limiting (1/sec, burst 3) is correct. Retry with exponential backoff on 429/5xx. Critical messages bypass the bucket and retry once.

### Market Scanner (Core Logic)
- **`data/market_scanner.py`** — Category inference via `CATEGORY_MAP`, `_EVENT_CATEGORY_MAP`, and `SERIES_PREFIX_CATEGORY` is correctly implemented as a fallback chain. Exclusion and entry check separation is clean. `scan_zero_tradeable` warning fires when 0 tradeable markets are found from >1000 fetched.

---

## 2. Bugs and Errors Found

### BUG-01 — CRITICAL: Circuit Breaker Never Updated in `demo_trade.py`
**File:** `scripts/demo_trade.py`
**Method:** `_scan_cycle()`, `_close_position()`
**Severity:** Critical — kill switch and loss limits are non-functional in the demo run

`demo_trade.py` calls `self._circuit_breaker.is_paused()` to check state, but **never calls `self._circuit_breaker.update(bankroll, daily_pnl, weekly_pnl, monthly_pnl)`**.

`CircuitBreaker.update()` is the only method that evaluates bankroll vs kill switch and loss thresholds. Without it being called, `_kill_active` is always `False`, `_paused_until` is never set, and the kill switch at `$300` **cannot fire**.

Your bankroll was logged at `$287.26` — **$12.74 below the kill switch threshold** — yet the bot continued running. This is a live capital risk.

**Fix:** Call `self._circuit_breaker.update(...)` inside `_scan_cycle()` in `demo_trade.py` after computing P&L, exactly as `core/engine.py` does in its `_scan_cycle()` via `_compute_pnl_windows()`.

---

### BUG-02 — CRITICAL: `no_price_data` Guard Causes 100% Market Rejection
**File:** `data/market_scanner.py`
**Method:** `_entry_checks()`
**Approximate Line:** ~559
**Severity:** Critical — bot finds 0 tradeable markets every cycle, makes no trades

The guard:
```python
if not yes_bid or not yes_ask or yes_bid <= 0 or yes_ask <= 0:
    reasons.append("no_price_data")
    return reasons  # ← EARLY RETURN, all subsequent checks skipped
```
...is rejecting every market fetched from the Kalshi demo API. This early return means no market ever reaches the spread, volume, or edge checks. 4,527+ markets rejected per cycle.

The Kalshi demo API currently returns markets where `yes_bid` and/or `yes_ask` fields are absent or zero in the JSON response (common in demo/sandbox environments where no real order books exist). The guard is too strict for demo conditions.

**Fix (demo only):** Either relax the guard to fall back to `mid_price` if bid/ask are absent, or use the market's `last_price` / `yes_price` field as the price source when bid/ask are missing. The demo overrides already set `min_liquidity=0.0` and `volume_threshold=0.0` — this guard needs a parallel relaxation.

---

### BUG-03 — HIGH: Wrong Production Base URL in `config/settings.py`
**File:** `config/settings.py`
**Line:** ~43
**Variable:** `KALSHI_BASE_URL`
**Severity:** High — will cause all production API calls to fail on go-live

Current code:
```python
KALSHI_BASE_URL: str = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if KALSHI_ENV == "demo"
    else "https://api.elections.kalshi.com/trade-api/v2"  # ← WRONG
)
```
The production URL `api.elections.kalshi.com` is the **elections-specific domain**, not the general Kalshi trade API. The correct production URL is `https://trading-api.kalshi.com/trade-api/v2` (or `https://api.kalshi.com/trade-api/v2` depending on your account tier — verify with Kalshi docs).

**Fix:** Update the production branch URL. Verify against your Kalshi API dashboard before going live.

---

### BUG-04 — HIGH: `_compute_pnl_windows()` Missing from `demo_trade.py`
**File:** `scripts/demo_trade.py`
**Severity:** High — P&L window data never computed, circuit breaker update has no data to work with

`core/engine.py` calls `self._compute_pnl_windows()` every cycle, which aggregates daily/weekly/monthly P&L from the DB before passing values to `circuit_breaker.update()`. This method does not exist in `demo_trade.py`. Even if BUG-01 is fixed by adding the `update()` call, it will have no P&L data to pass unless this method is also ported.

**Fix:** Port `_compute_pnl_windows()` from `core/engine.py` into `DemoTrader` in `demo_trade.py`.

---

### BUG-05 — MEDIUM: Redundant Dead Code in `probability_arbitrage.py`
**File:** `core/strategies/probability_arbitrage.py`
**Method:** `StrategyA.evaluate()`
**Lines:** ~128–129
**Severity:** Medium — code smell, indicates logic error or unfinished refactor

```python
entry_check = market.mid_price if hasattr(market, "market_price") else market.mid_price
# variable `entry_check` is never used after this line
```
Both branches of the ternary are identical (`market.mid_price`). The variable is assigned and then discarded. This is almost certainly a copy-paste error from an earlier version where `market.market_price` was intended for one branch. The unused assignment also means whatever validation or guard was intended here is silently absent.

**Fix:** Determine the intended logic (likely `market.market_price` vs `market.mid_price` depending on field availability), implement it correctly, and actually use `entry_check` in the entry price assignment.

---

### BUG-06 — MEDIUM: Bankroll Reconstruction on Restart Is Fragile
**File:** `scripts/demo_trade.py`
**Method:** `__init__()` or `_load_open_positions()`
**Severity:** Medium — incorrect bankroll after restart skews all position sizing

On restart, `DemoTrader` loads `STARTING_BANKROLL` from `config/settings.py` (default `$500`) but does not reconstruct current bankroll from the DB (closed trade P&L + starting capital). If the bot restarts mid-run, it will size positions as if it has $500, even if actual equity is $287.

`core/engine.py` has the same issue — `_load_open_positions()` reloads open positions but bankroll is not recomputed from closed trade history.

**Fix:** On startup, query the trades table for the sum of `pnl` on all `status='closed'` rows, add to `STARTING_BANKROLL`, and use that as the working bankroll.

---

### BUG-07 — LOW: Slack Webhooks Are Silent on Missing Config
**File:** `config/settings.py`
**Variables:** `SLACK_WEBHOOK_TRADES`, `SLACK_WEBHOOK_ALERTS`, `SLACK_WEBHOOK_DAILY`, `SLACK_WEBHOOK_WEEKLY`
**Severity:** Low — operational blind spot

All four Slack webhooks use `_optional()` — they default to empty string with no warning if not set. If the `.env` file is missing these keys, all Slack alerts silently fail. `monitoring/slack_alerts.py` checks for empty string before posting, so no exception is raised — messages are just dropped.

**Fix:** Add a startup warning log if any Slack webhook is empty. Consider making `SLACK_WEBHOOK_ALERTS` a `_require()` since it carries circuit breaker notifications.

---

### BUG-08 — LOW: `calibration` Table Starts Empty → Probability Model Anchored to Seeded Priors
**File:** `analysis/probability_model.py`
**Method:** `_load_base_rate()`
**Severity:** Low during demo, Medium at production scale

With 0 settled trades (n=0), the probability model blends: `0.80 × seeded_prior + 0.20 × claude_prob`. The seeded priors are 0.48–0.52 across all categories. This means `final_prob` will hover near 50% until enough trades settle. With `DIVERGENCE_THRESHOLD = 10.0pp`, both StrategyA and StrategyB will rarely fire until Claude's probability estimate pushes >60% or <40%.

This is expected behavior but is worth knowing: **the first 30 resolved trades in demo are calibration data, not alpha**. Don't read the first month's edge metrics as signal quality — they are prior-building.

---

## 3. Parity Gaps: `demo_trade.py` vs `core/engine.py`

| Feature | `core/engine.py` | `scripts/demo_trade.py` | Risk |
|---|---|---|---|
| `_compute_pnl_windows()` | ✅ Called every cycle | ❌ Missing entirely | Circuit breaker has no data |
| `circuit_breaker.update()` | ✅ Called every cycle with P&L | ❌ Never called | Kill switch cannot fire |
| Decision mode (Claude Sonnet) | ✅ Step 7 in `_evaluate_and_trade()` | ❌ Absent — scan mode only | No deep analysis on any trade |
| `RssAggregator` task | ✅ `rss_loop` runs in background | ❌ Not instantiated | `_find_relevant_headline()` has no data |
| `RegulatoryPoller` task | ✅ `regulatory_loop` runs | ❌ Not instantiated | `reg_alerts` always empty in StrategyB |
| `CreditMonitor` | ✅ Checks Anthropic API credits | ❌ Not present | No warning before Claude API exhaustion |
| `_find_relevant_headline()` | ✅ Passes RSS context to Claude scan | ❌ No equivalent | Claude scan lacks news context |
| `reg_alerts` passed to StrategyB | ✅ `self._active_reg_alerts` | ❌ Always empty list or absent | StrategyB missing regulatory signal |
| Safe mode auto-clear | ✅ `_consecutive_failures = 0; _safe_mode = False` on cycle success | ❌ Check unclear | Safe mode may never clear on demo |
| `_scheduler_loop` daily/weekly reports | ✅ Present | ✅ Present | Parity OK |
| `_monitor_loop` position monitoring | ✅ Present | ✅ Present | Parity OK |
| Demo override flags | N/A (production) | ✅ `volume_threshold=0.0`, `min_liquidity=0.0`, `min_settlement_days=1` | Correct for demo |

**Summary:** `demo_trade.py` is missing 7 subsystems present in `core/engine.py`. The two most dangerous gaps are the circuit breaker update (BUG-01) and the missing P&L windows method (BUG-04).

---

## 4. Silent Failure Risks During 30-Day Demo Run

### RISK-01 — Kill Switch Cannot Trigger (Maps to BUG-01)
As documented above: bankroll at $287.26, kill switch at $300, but `circuit_breaker.update()` is never called. The bot will continue running and placing trades even after crossing the kill switch threshold. **This is the highest-severity operational risk.**

### RISK-02 — 0 Tradeable Markets Logged as Warning, Not Escalated (Maps to BUG-02)
`market_scanner.py` logs `scan_zero_tradeable` at `WARNING` level when 0 tradeable markets are found. After 180+ cycles of 0 trades, this warning has fired hundreds of times. It is written to `demo_err.log` but never triggers a Slack alert or escalation. Without someone reading logs daily, this failure is invisible.

**Fix:** In `_scan_cycle()` of `demo_trade.py`, if `len(signals) == 0` for N consecutive cycles (e.g., 10), fire a Slack `ALERTS` channel message.

### RISK-03 — Bankroll Drift After Restart (Maps to BUG-06)
If the process crashes and restarts, position sizing resets to `STARTING_BANKROLL = $500` from `.env`. Any trades placed post-restart will be oversized relative to actual equity.

### RISK-04 — Claude API Exhaustion with No Warning
No `CreditMonitor` in `demo_trade.py`. If Anthropic API credits run out, `_claude_scan()` will throw an exception. This is caught by the broad `except Exception` in `_evaluate_and_trade()`, which logs and continues — meaning Claude scan silently fails and the bot falls back to base probability only, with no notification.

**Fix:** Add credit threshold check at startup and log/alert when API spend exceeds 80% of budget.

### RISK-05 — StrategyB Missing Regulatory Context
`RegulatoryPoller` is not running in `demo_trade.py`. `StrategyB` receives `reg_alerts=[]` always. A regulatory announcement that should suppress a mean-reversion trade will not suppress it. This is a logic gap that can't be caught by monitoring — it's a silent strategy degradation.

### RISK-06 — `no_price_data` Failure Is Non-Alerting
Across 180+ cycles, the rejection reason breakdown is never surfaced to Slack. The rejection log is written per-market at DEBUG level. You have no visibility into whether the API is degraded or the scanner logic changed without reading raw logs.

**Fix:** After each `scan()` call, log a rejection reason summary at INFO level and periodically post a Slack digest of top rejection reasons.

### RISK-07 — DB Write Failures Are Non-Fatal
In `_evaluate_and_trade()`, the `DB INSERT` for a new trade is followed immediately by in-memory `self._open_positions` update. If the DB write fails (disk full, lock timeout, corruption) but does not raise, the in-memory position exists but is not persisted. On restart, `_load_open_positions()` won't find it and the position will be orphaned — the bot won't monitor or close it.

The `aiosqlite` context manager will raise on failure, but a timeout with no exception (e.g., database locked briefly) could slip through. **Fix:** Use explicit `rowcount` check after INSERT and treat 0-row-affected as a critical alert.

---

## 5. Logging, DB Schema, and Slack Alerting State

### Logging

| File | Status |
|---|---|
| `logs/demo.log` | **Empty (0 bytes).** The `FileHandler` in `demo_trade.py` writes to `logs/demo_err.log` (hardcoded at module level). The name `demo.log` is never used. |
| `logs/demo_err.log` | **Active.** 536KB+, 3,238+ lines as of last check. Contains all cycle output, market scan results, errors. Grows every 30-second cycle. |

Log format is correct (timestamp, level, message). No log rotation is configured — over 30 days at current verbosity, `demo_err.log` will likely exceed 50MB. Consider adding a `RotatingFileHandler`.

### DB Schema

The schema in `persistence/models.py` (code) and the recovered `polyedge.db` (after user's fix) now match. The previous corruption included ghost columns (`open_price`, `result`, `price_history` in the markets table) that are not in the current schema — these are confirmed absent in the recovered DB.

Current schema state: **clean and correct**. All 6 tables present, all 14 indexes present.

**Data state:** After the database recovery, all tables are empty except `markets` (10,000 rows from API sync). All trade history from the previous run was lost. The demo run effectively reset to day 0.

### Slack Alerting

| Channel | Webhook Config | Status |
|---|---|---|
| `SLACK_WEBHOOK_TRADES` | `_optional()` | Fires only if key is set in `.env` |
| `SLACK_WEBHOOK_ALERTS` | `_optional()` | Fires only if key is set in `.env` |
| `SLACK_WEBHOOK_DAILY` | `_optional()` | Fires only if key is set in `.env` |
| `SLACK_WEBHOOK_WEEKLY` | `_optional()` | Fires only if key is set in `.env` |

If any webhook URL is missing from `.env`, that channel silently drops all messages. No startup validation. Rate limiting and retry logic in `slack_alerts.py` are correctly implemented. The critical-message bypass (circuit breaker alerts) works correctly — but only if `circuit_breaker.update()` is actually called (see BUG-01).

---

## 6. Prioritized Fix List Before Going Live

Fixes are ordered by impact × urgency. Do not go live with any P0 or P1 unresolved.

---

### P0 — Must Fix Before Any Live Capital

**Fix 1: Add `circuit_breaker.update()` to `demo_trade.py`**
- **File:** `scripts/demo_trade.py`, method `_scan_cycle()`
- **Action:** Port `_compute_pnl_windows()` from `core/engine.py` and call it each cycle. Pass returned `(daily_pnl, weekly_pnl, monthly_pnl)` plus current bankroll into `self._circuit_breaker.update(bankroll, daily_pnl, weekly_pnl, monthly_pnl)`.
- **Why P0:** Without this, the kill switch is non-functional. You can lose more than $300 with no automated stop.

**Fix 2: Resolve `no_price_data` rejection — investigate and fix price field handling**
- **File:** `data/market_scanner.py`, method `_entry_checks()`, line ~559
- **Action:** Log what `yes_bid` and `yes_ask` actually contain for a sample of 10 markets. If the demo API returns `last_price` or `yes_price` (a field distinct from `yes_bid`), update `_parse_price_cents()` to fall back to that field. Add an alert if 100% of markets are rejected for this reason.
- **Why P0:** The bot has made 0 trades across 180+ cycles. It is not functioning.

**Fix 3: Correct the production base URL**
- **File:** `config/settings.py`, `KALSHI_BASE_URL`
- **Action:** Replace `"https://api.elections.kalshi.com/trade-api/v2"` with the correct general API URL. Verify with Kalshi API docs for your account tier.
- **Why P0:** Going live with a wrong base URL means every production API call returns 404 or 403 instantly.

---

### P1 — Fix Before Extended Demo or Live

**Fix 4: Bankroll reconstruction on restart**
- **File:** `scripts/demo_trade.py`, `__init__()` or a new `_rehydrate_bankroll()` method
- **Action:** On startup, query `SELECT SUM(pnl) FROM trades WHERE status='closed'` and add to `STARTING_BANKROLL` to get real current equity. Use this as `self._bankroll`.
- **Why P1:** Every restart currently re-bases position sizing to $500. After a crash this causes oversizing.

**Fix 5: Consecutive-zero-signal escalation alert**
- **File:** `scripts/demo_trade.py`, `_scan_cycle()`
- **Action:** Track `self._zero_signal_streak`. Increment if `len(signals) == 0`. Reset if signals found. If streak ≥ 10, post to `SLACK_WEBHOOK_ALERTS` with reason breakdown. This turns a silent failure into a visible one.

**Fix 6: Make `SLACK_WEBHOOK_ALERTS` required**
- **File:** `config/settings.py`
- **Action:** Change `SLACK_WEBHOOK_ALERTS` from `_optional()` to `_require()`. Circuit breaker and critical system alerts should be mandatory, not optional.

**Fix 7: Log rotation for `demo_err.log`**
- **File:** `scripts/demo_trade.py`, logger setup section
- **Action:** Replace `FileHandler` with `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)`. At current growth rate, the log file will hit 50MB+ before the 30-day demo ends.

---

### P2 — Port for Production Parity

**Fix 8: Add `RssAggregator` and `RegulatoryPoller` to `demo_trade.py`**
- **Why:** StrategyB is receiving empty `reg_alerts`, and Claude scan has no news context. The demo is testing a degraded version of the strategy.

**Fix 9: Add `CreditMonitor` to `demo_trade.py`**
- **Why:** Anthropic API exhaustion currently causes silent fallback to base-prior-only mode with no alert.

**Fix 10: Add decision_mode (Claude Sonnet) call to `demo_trade.py`**
- **Why:** The production engine uses `CLAUDE_DECISION_MODEL = "claude-sonnet-4-6"` for full analysis on high-confidence signals. Demo only uses Haiku scan mode. The demo is not actually testing decision_mode performance.

---

### P3 — Code Quality / Low Risk

**Fix 11: Remove dead code in `probability_arbitrage.py`**
- **File:** `core/strategies/probability_arbitrage.py`, lines ~128–129
- **Action:** Determine what `entry_check` was intended to do. Either implement the correct logic (checking `market.market_price` vs `market.mid_price`) or remove the dead assignment entirely.

**Fix 12: Add startup validation summary log**
- **Action:** On `DemoTrader.__init__()` completion, log a structured summary: bankroll, open positions loaded, Slack channels active/inactive, circuit breaker state, DB table counts. Makes restarts auditable in logs.

---

## Summary Scorecard

| Area | Status |
|---|---|
| Core infrastructure (DB, config, async) | ✅ Solid |
| Risk controls (circuit breaker code) | ⚠️ Correct code, never called in demo |
| Signal pipeline (StrategyA, StrategyB, prob model) | ⚠️ Logic correct, 0 trades executing |
| Market scanner | 🔴 100% rejection — bot is not trading |
| Production URL | 🔴 Wrong — will fail on go-live |
| Slack alerting | ⚠️ Works if webhooks set, silent if not |
| Logging | ⚠️ Active but no rotation, wrong filename |
| DB schema | ✅ Clean (data lost after recovery) |
| Demo ↔ Engine parity | 🔴 7 subsystems missing from demo |

**Bottom line:** PolyEdge v5 has a well-architected foundation, but the demo runner is not actually testing the full system. The circuit breaker is disabled by omission, the scanner rejects every market, and the production URL is wrong. Fix P0 items before any live capital is touched.

---

*Report generated by automated source audit. All findings reference file paths relative to the PolyEdge project root.*
