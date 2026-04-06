"""
core/engine.py — PolyEdge production trading engine.

This is the live-money orchestrator that ties every subsystem together into
a continuously running async loop against the Kalshi production API.

What runs every 30 seconds (scan cycle)
-----------------------------------------
  1. Market scan → price-history update
  2. For each tradeable market not already in a position:
       a. ProbabilityModel.estimate()           — base-rate + calibration correction
       b. StrategyA.evaluate()                  — probability arbitrage signal
       c. StrategyB.evaluate()                  — post-spike mean reversion signal
       d. ClaudeAnalyzer.scan_mode()            — cheap Haiku relevance score
       e. NewsCatalyst.adjust()                 — veto or confidence delta
       f. OrderbookConfirm.check()              — depth / spoof / slippage gate
       g. ClaudeAnalyzer.decision_mode()        — Sonnet final probability + risk flags
       h. PositionSizer.size()                  — fractional Kelly
       i. CorrelationManager.check()            — concentration limits
       j. CircuitBreaker checks                 — drawdown gate + kill switch
       k. KalshiClient.place_order()            — production limit order
       l. DB INSERT trades + calibration
       m. SlackAlerter.trade_executed()

What runs every 5 minutes (position monitor)
---------------------------------------------
  • Fetch live price for every open position
  • Check exit conditions per strategy (convergence, target/stop, settlement)
  • Execute exits where required; update DB; update calibration

What runs asynchronously (RSS/regulatory feeds)
-------------------------------------------------
  • RssAggregator.poll()      — batches of headlines every ~600s
  • RegulatoryPoller.poll()   — CFTC / SEC alerts every ~1800s
    → CRITICAL alerts trigger Slack + StrategyB official-action veto

What runs at midnight UTC (daily tasks)
-----------------------------------------
  • EdgeErosionMonitor.run_daily_snapshot()
  • DailyReporter.send_daily()
  • BackupManager.run_backup()
  • CircuitBreaker.update()
  • Operator idle check (alert at 7 days)

What runs every Monday midnight UTC (weekly tasks)
----------------------------------------------------
  • DailyReporter.send_weekly()

Safe Mode
---------
  Triggered when MAX_DEPENDENCY_FAILURES consecutive scan errors occur.
  In safe mode: no new trades are opened; existing positions are held and
  monitored normally; a critical Slack alert is sent.  Safe mode clears
  automatically once a scan cycle completes without error.

Kill Switch
-----------
  Checked every scan cycle via CircuitBreaker.kill_switch_active().
  Fires when bankroll <= KILL_SWITCH ($300).  Initiates graceful shutdown.

Operator Idle Detection
-----------------------
  If ping_operator() has not been called for OPERATOR_IDLE_PAUSE_DAYS (7) days,
  a Slack alert is sent each midnight UTC until the operator responds.

Graceful Shutdown
-----------------
  SIGINT/SIGTERM → all tasks cancelled → open positions closed at market mid
  → final Slack summary → exit.

Usage
-----
    # Production — KALSHI_ENV must be "production" in .env
    python -m core.engine

    # Dry-run (full signal pipeline, never places real orders)
    python -m core.engine --dry-run

    # Verbose logging
    python -m core.engine --log-level DEBUG
"""

import argparse
import asyncio
import dataclasses
import functools
import json
import logging
import re
import signal
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from analysis.claude_analyzer import (
    ClaudeAnalyzer,
    DecisionContext,
    ScanContext,
)
from analysis.probability_model import ProbabilityModel
from config import constants as C
from config import settings as S
from core.calibration.calibration_loop import CalibrationLoop
from core.risk.circuit_breakers import CircuitBreaker
from core.risk.correlation_manager import CorrelationManager
from core.risk.position_sizer import PositionSizer
from core.signals.news_catalyst import NewsCatalyst
from core.signals.orderbook_confirm import OrderbookConfirm
from core.strategies.mean_reversion import PricePoint, StrategyB, detect_spike
from core.strategies.probability_arbitrage import StrategyA
from data.kalshi_client import KalshiClient
from data.market_scanner import MarketScanner, ScannedMarket
from data.regulatory_feeds import RegulatoryAlert, RegulatoryPoller
from data.rss_aggregator import Headline, RssAggregator
from monitoring.daily_report import DailyReporter
from monitoring.edge_erosion import EdgeErosionMonitor
from monitoring.slack_alerts import SlackAlerter
from persistence.backup import BackupManager
from persistence.database import get_connection, init_db

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("engine")

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------
SCAN_INTERVAL_S:      int = C.KALSHI_POLL_INTERVAL   # 30 s
MONITOR_INTERVAL_S:   int = 300                       # 5 min
BALANCE_REFRESH_CYCLES: int = 10                      # every 10 scan cycles
MAX_HISTORY_POINTS:   int = 120                       # ~1 hour at 30 s intervals
SETTLE_CHECK_HOURS:   int = 48                        # exit within 48 h of settlement
MAX_POSITION_AGE_DAYS: int = 7                        # force-exit after 7 days


# ---------------------------------------------------------------------------
# Open-position record
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class OpenPosition:
    """In-memory record of one live production position."""
    trade_id:      int
    ticker:        str
    strategy:      str        # "probability_arbitrage" | "mean_reversion"
    direction:     str        # "buy_yes" | "buy_no"
    side:          str        # "YES" | "NO"
    num_contracts: int
    entry_price:   float      # dollars (0–1)
    model_prob:    float      # probability estimate at entry
    target_price:  float      # take-profit level (YES dollars)
    stop_price:    float      # stop-loss level (YES dollars)
    category:      str
    entry_time:    datetime   # UTC
    order_id:      str        # Kalshi order ID


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """PolyEdge production orchestrator.

    All blocking I/O (Kalshi REST, DB writes) runs via asyncio.to_thread()
    so the event loop stays responsive for Claude async calls and feed polling.
    """

    def __init__(self, dry_run: bool = False) -> None:
        self._dry_run         = dry_run
        self._start_time:     datetime | None = None
        self._shutdown_event  = asyncio.Event()

        # ---------- Subsystems ----------
        self._client          = KalshiClient()
        self._scanner         = MarketScanner(self._client)
        self._prob_model      = ProbabilityModel()
        self._strategy_a      = StrategyA()
        self._strategy_b      = StrategyB()
        self._catalyst        = NewsCatalyst()
        self._ob_confirm      = OrderbookConfirm()
        self._sizer           = PositionSizer()
        self._correlation     = CorrelationManager()
        self._alerter         = SlackAlerter()
        self._circuit_breaker = CircuitBreaker(slack_alerter=self._alerter)
        self._calibration     = CalibrationLoop(slack_alerter=self._alerter)
        self._edge_monitor    = EdgeErosionMonitor(slack_alerter=self._alerter)
        self._reporter        = DailyReporter(slack_alerter=self._alerter)
        self._backup          = BackupManager()
        self._rss             = RssAggregator()
        self._regulatory      = RegulatoryPoller()

        try:
            self._analyzer: ClaudeAnalyzer | None = ClaudeAnalyzer()
        except Exception as exc:
            logger.warning("claude_analyzer_unavailable  error=%s — signals degrade", exc)
            self._analyzer = None

        # ---------- Runtime state ----------
        self._bankroll:            float = S.STARTING_BANKROLL
        self._open_positions:      dict[str, OpenPosition] = {}
        self._price_history:       dict[str, deque[PricePoint]] = (
            defaultdict(lambda: deque(maxlen=MAX_HISTORY_POINTS))
        )
        self._latest_headlines:    list[Headline] = []
        self._active_reg_alerts:   list[RegulatoryAlert] = []

        self._scan_cycle_count:    int = 0
        self._consecutive_failures: int = 0
        self._safe_mode:           bool = False
        self._last_daily_run:      datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_weekly_run:     datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_operator_ping:  datetime = datetime.now(timezone.utc)

    # =========================================================================
    # Public entrypoint
    # =========================================================================

    async def run(self) -> None:
        """Start the production engine.  Runs until kill switch fires,
        SIGINT/SIGTERM received, or an unrecoverable error occurs."""
        self._start_time = datetime.now(timezone.utc)

        if S.KALSHI_ENV != "production":
            logger.critical(
                "KALSHI_ENV=%s — refusing to start production engine on non-production "
                "environment.  Set KALSHI_ENV=production in .env", S.KALSHI_ENV
            )
            sys.exit(1)

        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("  PolyEdge ENGINE  |  env=%s  |  dry_run=%s  |  start=%s",
                    S.KALSHI_ENV, self._dry_run,
                    self._start_time.strftime("%Y-%m-%dT%H:%MZ"))
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        await init_db()
        await self._load_open_positions()
        await self._refresh_bankroll()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        self._alerter.system_info(
            "Production engine started",
            {
                "Environment":   S.KALSHI_ENV,
                "Dry run":       str(self._dry_run),
                "Bankroll":      f"${self._bankroll:,.2f}",
                "Open pos":      str(len(self._open_positions)),
                "Scan interval": f"{SCAN_INTERVAL_S}s",
            },
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
        )

        tasks = [
            asyncio.create_task(self._scan_loop(),       name="scan_loop"),
            asyncio.create_task(self._monitor_loop(),    name="position_monitor"),
            asyncio.create_task(self._rss_loop(),        name="rss_loop"),
            asyncio.create_task(self._regulatory_loop(), name="regulatory_loop"),
            asyncio.create_task(self._scheduler_loop(),  name="scheduler"),
        ]

        try:
            await asyncio.wait(
                [asyncio.create_task(self._shutdown_event.wait()), *tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            logger.info("shutdown_initiated — cancelling tasks …")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._graceful_shutdown()

    # =========================================================================
    # Scan loop
    # =========================================================================

    async def _scan_loop(self) -> None:
        """30-second market-scan-and-signal loop."""
        while not self._shutdown_event.is_set():
            cycle_start = datetime.now(timezone.utc)
            self._scan_cycle_count += 1

            try:
                await self._scan_cycle()
                # Success — clear failure streak and exit safe mode if set
                if self._consecutive_failures > 0:
                    logger.info("scan_recovered  resetting_failure_count")
                self._consecutive_failures = 0
                if self._safe_mode:
                    self._safe_mode = False
                    logger.info("safe_mode_cleared — scan cycle succeeded")
                    self._alerter.system_info(
                        "Safe mode cleared",
                        {"Reason": "Scan cycle completed without error"},
                        bankroll=self._bankroll,
                        open_positions=len(self._open_positions),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_failures += 1
                logger.error("scan_cycle_error  cycle=%d  error=%s",
                             self._scan_cycle_count, exc, exc_info=True)
                if self._consecutive_failures >= C.MAX_DEPENDENCY_FAILURES:
                    await self._enter_safe_mode(str(exc))

            if self._scan_cycle_count % BALANCE_REFRESH_CYCLES == 0:
                await self._refresh_bankroll()

            elapsed   = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_for = max(0.0, SCAN_INTERVAL_S - elapsed)
            logger.debug("scan_cycle=%d  elapsed=%.1fs  sleep=%.1fs",
                         self._scan_cycle_count, elapsed, sleep_for)
            await asyncio.sleep(sleep_for)

    async def _scan_cycle(self) -> None:
        """One full scan-signal-execute cycle."""
        if self._safe_mode:
            logger.debug("safe_mode_active — skipping scan cycle")
            return

        # ── Kill switch ────────────────────────────────────────────────
        if self._circuit_breaker.kill_switch_active():
            logger.critical("kill_switch_active — initiating shutdown")
            self._shutdown_event.set()
            return

        # ── Circuit-breaker pause ──────────────────────────────────────
        if self._circuit_breaker.is_paused():
            resume = self._circuit_breaker.paused_until()
            logger.info("circuit_breaker_paused  resume=%s",
                        resume.strftime("%Y-%m-%dT%H:%MZ") if resume else "?")
            return

        # ── Market scan ────────────────────────────────────────────────
        try:
            scan_result = await asyncio.to_thread(self._scanner.scan)
        except Exception as exc:
            raise RuntimeError(f"market_scan_failed: {exc}") from exc

        tradeable = scan_result.tradeable
        logger.info(
            "scan_cycle=%d  fetched=%d  tradeable=%d  open_pos=%d  bankroll=$%.2f",
            self._scan_cycle_count, scan_result.total_fetched, len(tradeable),
            len(self._open_positions), self._bankroll,
        )

        # ── Update price history + market cache ────────────────────────
        now_utc = datetime.now(timezone.utc)
        async with get_connection() as db:
            for market in scan_result.tradeable + scan_result.rejected:
                self._update_price_history(market, now_utc)
                await self._db_upsert_market(db, market)
            await db.commit()

        # ── Signal pipeline ────────────────────────────────────────────
        for market in tradeable:
            if self._shutdown_event.is_set():
                break
            if market.ticker in self._open_positions:
                continue

            try:
                await self._evaluate_and_trade(market)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("signal_pipeline_error  ticker=%s  error=%s",
                               market.ticker, exc, exc_info=True)

    # =========================================================================
    # Signal pipeline
    # =========================================================================

    async def _evaluate_and_trade(self, market: ScannedMarket) -> None:
        """Full signal pipeline for one market.  Places a trade if all gates pass."""
        ticker   = market.ticker
        category = market.category

        # ── 1. Probability model  ─────────────────────────────────────
        estimate = await self._prob_model.estimate(
            category=category,
            market_price=market.mid_price,
            claude_result=None,
        )

        # ── 2. Strategy A  ────────────────────────────────────────────
        signal = self._strategy_a.evaluate(market, estimate)

        # ── 3. Strategy B  ────────────────────────────────────────────
        history = list(self._price_history[ticker])
        signal_b = None
        if len(history) >= 2:
            signal_b = self._strategy_b.evaluate(
                market=market,
                price_history=history,
                scan_result=None,
                reg_alerts=self._active_reg_alerts if self._active_reg_alerts else None,
            )

        if signal_b is not None:
            if signal is None or signal_b.net_edge_pp > signal.net_edge_pp:
                signal = signal_b

        if signal is None:
            return

        # ── 4. Claude scan (Haiku — cheap relevance check) ───────────
        scan_result = None
        if self._analyzer is not None:
            # Use most recent relevant headline if available
            best_headline = self._find_relevant_headline(ticker, market.title)
            try:
                ctx = ScanContext(
                    headline_title=best_headline.title if best_headline else "No recent headline",
                    headline_source=best_headline.source if best_headline else "",
                    headline_summary=best_headline.summary if best_headline else "",
                    market_ticker=ticker,
                    market_title=market.title,
                    market_category=category,
                    current_price=market.mid_price,
                    days_to_settlement=market.days_to_settlement,
                )
                scan_result = await self._analyzer.scan_mode(ctx)
            except Exception as exc:
                logger.debug("claude_scan_failed  ticker=%s  error=%s", ticker, exc)

        # ── 5. News catalyst veto / confidence delta ──────────────────
        catalyst = self._catalyst.adjust(signal, scan_result)
        if catalyst.veto:
            logger.info("signal_vetoed  ticker=%s  reason=%s",
                        ticker, catalyst.adjustment_reason)
            return
        signal = dataclasses.replace(signal, confidence=catalyst.adjusted_confidence)

        # ── 6. Orderbook confirmation ─────────────────────────────────
        try:
            ob = await asyncio.to_thread(self._client.get_orderbook, ticker, 5)
        except Exception as exc:
            logger.warning("orderbook_fetch_failed  ticker=%s  error=%s", ticker, exc)
            return

        ob_result = self._ob_confirm.check(signal.direction, ob, C.MIN_TRADE_SIZE)
        if not ob_result.confirmed:
            logger.debug("ob_rejected  ticker=%s  reason=%s", ticker, ob_result.reason)
            return

        entry_price = ob_result.entry_price

        # ── 7. Claude decision mode (Sonnet — full probability analysis) ──
        if self._analyzer is not None:
            try:
                recent_prices = [
                    int(round(pt.price * 100))
                    for pt in list(self._price_history[ticker])[-20:]
                ]
                relevant_headlines = [
                    {
                        "title":   h.title,
                        "source":  h.source,
                        "summary": h.summary,
                    }
                    for h in self._latest_headlines[:5]
                ]
                dec_ctx = DecisionContext(
                    market_ticker=ticker,
                    market_title=market.title,
                    market_category=category,
                    current_price=market.mid_price,
                    days_to_settlement=market.days_to_settlement,
                    relevant_headlines=relevant_headlines,
                    orderbook=ob,
                    base_rate=estimate.base_rate,
                    base_rate_n=estimate.n,
                    recent_prices=recent_prices,
                    volume_7d=market.volume_7d,
                    open_interest=getattr(market, "open_interest", 0),
                )
                decision = await self._analyzer.decision_mode(dec_ctx)

                # Override signal confidence with Claude's assessment
                if decision.predicted_probability is not None:
                    # Apply calibration correction factor to Claude's probability
                    correction = self._calibration.get_correction_factor()
                    corrected_prob = min(0.99, max(0.01,
                        decision.predicted_probability * correction))
                    estimate = dataclasses.replace(estimate, final_prob=corrected_prob)
                    logger.debug(
                        "decision_mode  ticker=%s  claude_prob=%.3f  "
                        "correction=%.3f  final_prob=%.3f",
                        ticker, decision.predicted_probability, correction, corrected_prob,
                    )

                # Veto if Claude flags a risk issue
                if decision.recommended_action == "pass":
                    logger.info("decision_mode_pass  ticker=%s  reason=%s",
                                ticker, decision.reasoning[:80] if decision.reasoning else "")
                    return
            except Exception as exc:
                logger.debug("claude_decision_failed  ticker=%s  error=%s", ticker, exc)
                # Proceed without Claude — degrade gracefully

        # ── 8. Position sizing ────────────────────────────────────────
        resolved_count = self._calibration.get_state().resolved_count
        size = self._sizer.size(
            model_prob=estimate.final_prob,
            entry_price=entry_price,
            bankroll=self._bankroll,
            resolved_trade_count=resolved_count,
        )
        if not size.eligible:
            logger.debug("sizer_ineligible  ticker=%s  reason=%s", ticker, size.reason)
            return

        # ── 9. Correlation / concentration check ─────────────────────
        corr = self._correlation.check(
            category=category,
            direction=signal.direction,
            dollar_size=size.dollar_size,
            bankroll=self._bankroll,
        )
        if not corr.allowed:
            logger.info("correlation_blocked  ticker=%s  reason=%s", ticker, corr.reason)
            return

        # ── 10. Final circuit-breaker re-check ────────────────────────
        if self._circuit_breaker.kill_switch_active():
            self._shutdown_event.set()
            return
        if self._circuit_breaker.is_paused():
            return

        # ── 11. Execute order ─────────────────────────────────────────
        if self._dry_run:
            logger.info(
                "DRY_RUN  ticker=%s  dir=%s  contracts=%d  entry=%.3f  "
                "edge=%.1fpp  conf=%.2f",
                ticker, signal.direction, size.num_contracts, entry_price,
                signal.net_edge_pp, signal.confidence,
            )
            return

        order_id, executed_price = await self._place_entry_order(
            ticker=ticker,
            direction=signal.direction,
            num_contracts=size.num_contracts,
            limit_price=entry_price,
        )
        if order_id is None:
            return

        # ── 12. Persist trade to DB ───────────────────────────────────
        side     = "YES" if signal.direction == "buy_yes" else "NO"
        fees     = C.kalshi_fee(executed_price, size.num_contracts, is_maker=True)
        now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        notes_payload = json.dumps({
            "direction":    signal.direction,
            "model_prob":   estimate.final_prob,
            "target_price": signal.target_price,
            "stop_price":   signal.stop_price,
            "category":     category,
            "order_id":     order_id,
            "confidence":   signal.confidence,
        })

        async with get_connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, side, order_type, num_contracts,
                     entry_price, entry_time, fees_paid, status, notes)
                VALUES (?, ?, ?, 'LIMIT', ?, ?, ?, ?, 'open', ?)
                """,
                (
                    ticker, signal.strategy, side, size.num_contracts,
                    executed_price, now_str, fees, notes_payload,
                ),
            )
            trade_id = cursor.lastrowid
            await db.execute(
                """
                INSERT INTO calibration (trade_id, ticker, predicted_prob, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (trade_id, ticker, estimate.final_prob, now_str),
            )
            await db.commit()

        # ── 13. Update in-memory state ────────────────────────────────
        pos = OpenPosition(
            trade_id=trade_id,
            ticker=ticker,
            strategy=signal.strategy,
            direction=signal.direction,
            side=side,
            num_contracts=size.num_contracts,
            entry_price=executed_price,
            model_prob=estimate.final_prob,
            target_price=signal.target_price,
            stop_price=signal.stop_price,
            category=category,
            entry_time=datetime.now(timezone.utc),
            order_id=order_id,
        )
        self._open_positions[ticker] = pos
        self._correlation.add_position(
            ticker=ticker,
            category=category,
            direction=signal.direction,
            dollar_size=size.dollar_size,
        )

        logger.info(
            "trade_opened  ticker=%s  strategy=%s  dir=%s  contracts=%d  "
            "price=%.3f  fees=$%.4f  edge=%.1fpp  trade_id=%d",
            ticker, signal.strategy, signal.direction, size.num_contracts,
            executed_price, fees, signal.net_edge_pp, trade_id,
        )

        # ── 14. Slack alert ───────────────────────────────────────────
        self._alerter.trade_executed(
            ticker=ticker,
            side=side.lower(),
            action="buy",
            contracts=size.num_contracts,
            price=executed_price,
            fees=fees,
            strategy=signal.strategy,
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
            order_id=order_id,
        )

    # =========================================================================
    # Position monitor loop
    # =========================================================================

    async def _monitor_loop(self) -> None:
        """Check open positions every 5 minutes for exits and settlements."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(MONITOR_INTERVAL_S)
            try:
                await self._monitor_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("monitor_cycle_error  error=%s", exc, exc_info=True)

    async def _monitor_cycle(self) -> None:
        """Check every open position for exit conditions."""
        if not self._open_positions:
            return

        tickers_to_close: list[tuple[str, float, str]] = []

        for ticker, pos in list(self._open_positions.items()):
            try:
                raw = await asyncio.to_thread(self._client.get_market, ticker)
            except Exception as exc:
                logger.warning("market_fetch_failed  ticker=%s  error=%s", ticker, exc)
                continue

            market_status = raw.get("status", "")
            yes_ask       = raw.get("yes_ask") or 0
            yes_bid       = raw.get("yes_bid") or 0
            current_yes   = (yes_ask + yes_bid) / 200.0
            close_time    = raw.get("close_time") or raw.get("expiration_time") or ""

            # Settlement
            if market_status == "settled":
                result_str = raw.get("result", "")
                outcome    = 1 if result_str == "yes" or yes_ask >= 99 else 0
                tickers_to_close.append((ticker, float(outcome), "settled"))
                continue

            # Approaching settlement
            if close_time:
                try:
                    clean    = re.sub(r"\.\d+Z?$", "Z", close_time).replace("Z", "+00:00")
                    close_dt = datetime.fromisoformat(clean)
                    hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left < SETTLE_CHECK_HOURS:
                        tickers_to_close.append((ticker, current_yes, "near_settlement"))
                        continue
                except (ValueError, TypeError):
                    pass

            # Age-based exit
            age_days = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 86400
            if age_days > MAX_POSITION_AGE_DAYS:
                tickers_to_close.append((ticker, current_yes, "age_limit"))
                continue

            # Strategy-specific exits
            exit_reason = _check_exit_condition(pos, current_yes)
            if exit_reason:
                tickers_to_close.append((ticker, current_yes, exit_reason))

        for ticker, exit_yes_price, reason in tickers_to_close:
            await self._close_position(ticker, exit_yes_price, reason)

    async def _close_position(
        self,
        ticker:         str,
        exit_yes_price: float,
        reason:         str,
    ) -> None:
        """Execute exit order, update DB, remove from in-memory state."""
        pos = self._open_positions.get(ticker)
        if pos is None:
            return

        if pos.direction == "buy_yes":
            entry  = pos.entry_price
            exit_p = exit_yes_price
        else:
            entry  = 1.0 - pos.entry_price
            exit_p = 1.0 - exit_yes_price

        gross_pnl = (exit_p - entry) * pos.num_contracts
        exit_fee  = C.kalshi_fee(exit_p, pos.num_contracts, is_maker=True)
        net_pnl   = round(gross_pnl - exit_fee, 4)

        if not self._dry_run and reason != "settled":
            try:
                await asyncio.to_thread(
                    functools.partial(
                        self._client.place_order,
                        ticker=ticker,
                        side=pos.side.lower(),
                        action="sell",
                        count=pos.num_contracts,
                        order_type="limit",
                        price=round(max(0.01, min(0.99, exit_p)), 2),
                    )
                )
            except Exception as exc:
                logger.warning("exit_order_failed  ticker=%s  error=%s  closing_locally",
                               ticker, exc)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        async with get_connection() as db:
            await db.execute(
                """
                UPDATE trades
                SET exit_price = ?, exit_time = ?, pnl = ?,
                    fees_paid = fees_paid + ?, status = ?
                WHERE id = ?
                """,
                (
                    exit_p, now_str, net_pnl, exit_fee,
                    "settled" if reason == "settled" else "closed",
                    pos.trade_id,
                ),
            )
            if reason == "settled":
                outcome = 1 if exit_yes_price >= 0.99 else 0
                brier   = (pos.model_prob - outcome) ** 2
                await db.execute(
                    """
                    UPDATE calibration
                    SET actual_outcome = ?, brier_contribution = ?, settled_at = ?
                    WHERE trade_id = ?
                    """,
                    (outcome, round(brier, 6), now_str, pos.trade_id),
                )
                await self._calibration.record_outcome(
                    trade_id=pos.trade_id,
                    ticker=ticker,
                    predicted_prob=pos.model_prob,
                    actual_outcome=outcome,
                    settled_at=now_str,
                )
            await db.commit()

        del self._open_positions[ticker]
        self._correlation.remove_position(ticker)

        logger.info(
            "trade_closed  ticker=%s  strategy=%s  reason=%s  pnl=%+.4f  "
            "entry=%.3f  exit=%.3f  contracts=%d",
            ticker, pos.strategy, reason, net_pnl,
            pos.entry_price, exit_p, pos.num_contracts,
        )

        total_fees = C.kalshi_fee(pos.entry_price, pos.num_contracts) + exit_fee
        self._alerter.position_closed(
            ticker=ticker,
            side=pos.side,
            contracts=pos.num_contracts,
            entry_price=pos.entry_price,
            exit_price=exit_p,
            pnl=net_pnl,
            fees_total=round(total_fees, 4),
            strategy=pos.strategy,
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
        )

    # =========================================================================
    # RSS loop
    # =========================================================================

    async def _rss_loop(self) -> None:
        """Consume RssAggregator.poll() and keep _latest_headlines up-to-date."""
        try:
            async for batch in self._rss.poll():
                if self._shutdown_event.is_set():
                    break
                if batch:
                    self._latest_headlines = batch
                    logger.debug("rss_update  headlines=%d  first=%s",
                                 len(batch), batch[0].title[:60] if batch else "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("rss_loop_error  error=%s", exc, exc_info=True)
        finally:
            self._rss.stop()

    # =========================================================================
    # Regulatory loop
    # =========================================================================

    async def _regulatory_loop(self) -> None:
        """Consume RegulatoryPoller.poll() and alert on CRITICAL items."""
        try:
            async for batch in self._regulatory.poll():
                if self._shutdown_event.is_set():
                    break
                if not batch:
                    continue

                self._active_reg_alerts = batch
                logger.info("regulatory_update  alerts=%d", len(batch))

                for alert in batch:
                    if alert.alert_level == "critical":
                        logger.critical(
                            "regulatory_critical  source=%s  keywords=%s  title=%s",
                            alert.source,
                            alert.matched_keywords,
                            alert.title[:80],
                        )
                        self._alerter.regulatory_alert(
                            title=alert.title,
                            source=alert.source,
                            url=alert.url,
                            level=alert.alert_level,
                            matched_keywords=alert.matched_keywords,
                            bankroll=self._bankroll,
                            open_positions=len(self._open_positions),
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("regulatory_loop_error  error=%s", exc, exc_info=True)
        finally:
            self._regulatory.stop()

    # =========================================================================
    # Scheduler loop
    # =========================================================================

    async def _scheduler_loop(self) -> None:
        """Fire daily/weekly tasks at the correct UTC times."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)

            # Daily — midnight UTC (within first 5 minutes)
            if (
                now.hour == 0 and now.minute < 5
                and now.date() > self._last_daily_run.date()
            ):
                self._last_daily_run = now
                try:
                    await self._daily_tasks()
                except Exception as exc:
                    logger.warning("daily_tasks_error  error=%s", exc, exc_info=True)

            # Weekly — Monday midnight UTC
            if (
                now.weekday() == 0
                and now.hour == 0 and now.minute < 5
                and now.date() > self._last_weekly_run.date()
            ):
                self._last_weekly_run = now
                try:
                    await self._weekly_tasks()
                except Exception as exc:
                    logger.warning("weekly_tasks_error  error=%s", exc, exc_info=True)

    async def _daily_tasks(self) -> None:
        """Midnight UTC daily maintenance."""
        logger.info("daily_tasks_start")
        now = datetime.now(timezone.utc)

        daily_pnl   = await self._fetch_period_pnl(hours=24)
        weekly_pnl  = await self._fetch_period_pnl(hours=24 * 7)
        monthly_pnl = await self._fetch_period_pnl(hours=24 * 30)

        await self._circuit_breaker.update(
            bankroll=self._bankroll,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            monthly_pnl=monthly_pnl,
            open_positions=len(self._open_positions),
            starting_bankroll=S.STARTING_BANKROLL,
        )

        try:
            snap = await self._edge_monitor.run_daily_snapshot(
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
            if snap.should_pause and not self._circuit_breaker.is_paused():
                logger.critical(
                    "edge_erosion_critical  overall=%s — activating kill switch",
                    snap.overall_status,
                )
                self._circuit_breaker.force_kill()
        except Exception as exc:
            logger.warning("edge_snapshot_failed  error=%s", exc)

        try:
            await self._reporter.send_daily(
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
        except Exception as exc:
            logger.warning("daily_report_failed  error=%s", exc)

        try:
            result = await self._backup.run_backup()
            logger.info("backup  file=%s  size=%d  uploaded=%s",
                        result.filename, result.size_bytes, result.uploaded)
        except Exception as exc:
            logger.warning("backup_failed  error=%s", exc)

        idle_days = int((now - self._last_operator_ping).total_seconds() / 86400)
        if idle_days >= C.OPERATOR_IDLE_PAUSE_DAYS:
            logger.warning("operator_idle  idle_days=%d", idle_days)
            self._alerter.operator_idle(
                idle_days=idle_days,
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )

        logger.info("daily_tasks_complete")

    async def _weekly_tasks(self) -> None:
        """Monday midnight UTC weekly tasks."""
        logger.info("weekly_tasks_start")
        try:
            await self._reporter.send_weekly(
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
        except Exception as exc:
            logger.warning("weekly_report_failed  error=%s", exc)
        logger.info("weekly_tasks_complete")

    # =========================================================================
    # Startup / shutdown helpers
    # =========================================================================

    async def _load_open_positions(self) -> None:
        """Reload open positions from DB (supports engine restarts)."""
        async with get_connection() as db:
            cursor = await db.execute(
                """
                SELECT id, ticker, strategy, side, num_contracts,
                       entry_price, entry_time, notes
                FROM   trades
                WHERE  status = 'open'
                """
            )
            rows = await cursor.fetchall()

        for row in rows:
            notes: dict = {}
            try:
                notes = json.loads(row["notes"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass

            direction  = notes.get("direction", "buy_yes")
            model_prob = float(notes.get("model_prob", 0.50))
            target     = float(notes.get("target_price", 0.99))
            stop       = float(notes.get("stop_price", 0.01))
            category   = notes.get("category", "economics")
            order_id   = notes.get("order_id", "")

            try:
                clean    = re.sub(r"\.\d+Z?$", "Z", row["entry_time"] or "").replace("Z", "+00:00")
                entry_dt = datetime.fromisoformat(clean)
            except (ValueError, TypeError):
                entry_dt = datetime.now(timezone.utc)

            pos = OpenPosition(
                trade_id=row["id"],
                ticker=row["ticker"],
                strategy=row["strategy"],
                direction=direction,
                side=row["side"],
                num_contracts=row["num_contracts"],
                entry_price=float(row["entry_price"]),
                model_prob=model_prob,
                target_price=target,
                stop_price=stop,
                category=category,
                entry_time=entry_dt,
                order_id=order_id,
            )
            self._open_positions[row["ticker"]] = pos
            self._correlation.add_position(
                ticker=row["ticker"],
                category=category,
                direction=direction,
                dollar_size=row["num_contracts"] * float(row["entry_price"]),
            )

        if rows:
            logger.info("loaded_open_positions  count=%d", len(rows))

    async def _graceful_shutdown(self) -> None:
        """Close all open positions and send a final status message."""
        logger.info("graceful_shutdown  open_positions=%d", len(self._open_positions))

        for ticker in list(self._open_positions.keys()):
            try:
                raw     = await asyncio.to_thread(self._client.get_market, ticker)
                yes_ask = raw.get("yes_ask") or 0
                yes_bid = raw.get("yes_bid") or 0
                current = (yes_ask + yes_bid) / 200.0
                await self._close_position(ticker, current, "shutdown")
            except Exception as exc:
                logger.warning("shutdown_close_failed  ticker=%s  error=%s", ticker, exc)

        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 86400
        self._alerter.system_info(
            "Production engine terminated",
            {
                "Elapsed days":   f"{elapsed:.2f}",
                "Total cycles":   str(self._scan_cycle_count),
                "Final bankroll": f"${self._bankroll:,.2f}",
            },
            bankroll=self._bankroll,
            open_positions=0,
        )
        logger.info("engine_stopped  elapsed_days=%.2f  cycles=%d  bankroll=$%.2f",
                    elapsed, self._scan_cycle_count, self._bankroll)

    # =========================================================================
    # Utility helpers
    # =========================================================================

    async def _refresh_bankroll(self) -> None:
        """Fetch current production account balance."""
        try:
            balance_data = await asyncio.to_thread(self._client.get_balance)
            new_bal = float(balance_data.get("balance", self._bankroll))
            if new_bal != self._bankroll:
                logger.info("bankroll_updated  old=$%.2f  new=$%.2f",
                            self._bankroll, new_bal)
            self._bankroll = new_bal
        except Exception as exc:
            logger.warning("balance_fetch_failed  error=%s  using_cached=$%.2f",
                           exc, self._bankroll)

    async def _place_entry_order(
        self,
        ticker:        str,
        direction:     str,
        num_contracts: int,
        limit_price:   float,
    ) -> tuple[str | None, float]:
        """Place a production limit order. Returns (order_id, executed_price)."""
        side  = "yes" if direction == "buy_yes" else "no"
        price = round(max(0.01, min(0.99, limit_price)), 2)

        try:
            order = await asyncio.to_thread(
                functools.partial(
                    self._client.place_order,
                    ticker=ticker,
                    side=side,
                    action="buy",
                    count=num_contracts,
                    order_type="limit",
                    price=price,
                )
            )
            order_id = order.get("order_id") or order.get("id") or ""
            return order_id, price
        except Exception as exc:
            logger.error("place_order_failed  ticker=%s  error=%s", ticker, exc)
            self._alerter.api_error(
                service="kalshi_place_order",
                error_message=str(exc),
                consecutive=self._consecutive_failures,
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
            return None, price

    def _update_price_history(self, market: ScannedMarket, ts: datetime) -> None:
        if market.mid_price > 0:
            self._price_history[market.ticker].append(
                PricePoint(price=market.mid_price, timestamp=ts)
            )

    async def _fetch_period_pnl(self, hours: int) -> float:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            async with get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(pnl), 0.0)
                    FROM   trades
                    WHERE  status IN ('closed', 'settled')
                    AND    pnl IS NOT NULL
                    AND    exit_time >= ?
                    """,
                    (since,),
                )
                row = await cursor.fetchone()
                return float(row[0]) if row else 0.0
        except Exception as exc:
            logger.warning("fetch_period_pnl_failed  hours=%d  error=%s", hours, exc)
            return 0.0

    async def _enter_safe_mode(self, reason: str) -> None:
        """Enter safe mode: no new trades, hold positions, critical Slack alert."""
        if self._safe_mode:
            return
        self._safe_mode = True
        failed_services: list[str] = []
        if "kalshi" in reason.lower():
            failed_services.append("kalshi_api")
        if "scan" in reason.lower():
            failed_services.append("market_scanner")
        if not failed_services:
            failed_services = ["unknown"]

        logger.critical("entering_safe_mode  reason=%s  failures=%d",
                        reason, self._consecutive_failures)
        self._alerter.safe_mode_entered(
            reason=reason,
            failed_services=failed_services,
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
        )

    def _find_relevant_headline(
        self,
        ticker: str,
        market_title: str,
    ) -> Headline | None:
        """Return the most recent headline whose title overlaps with the market."""
        if not self._latest_headlines:
            return None
        market_words = set(market_title.lower().split())
        best: Headline | None = None
        best_overlap = 0
        for h in self._latest_headlines:
            headline_words = set(h.title.lower().split())
            overlap = len(market_words & headline_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best = h
        return best if best_overlap >= 2 else None

    @staticmethod
    async def _db_upsert_market(db, market: ScannedMarket) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.execute(
            """
            INSERT INTO markets
                (ticker, title, series, event_id, category, settlement_date,
                 status, yes_price, no_price, volume_7d, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                title        = excluded.title,
                category     = excluded.category,
                yes_price    = excluded.yes_price,
                no_price     = excluded.no_price,
                volume_7d    = excluded.volume_7d,
                last_updated = excluded.last_updated
            """,
            (
                market.ticker, market.title, market.series_ticker,
                market.event_ticker, market.category,
                _parse_settlement_date(market.close_time),
                round(market.mid_price, 4),
                round(1.0 - market.mid_price, 4),
                round(market.volume_7d, 2),
                now_str,
            ),
        )

    # =========================================================================
    # Public API
    # =========================================================================

    def ping_operator(self) -> None:
        """Reset the operator idle timer.  Call periodically or on any user action."""
        self._last_operator_ping = datetime.now(timezone.utc)
        logger.debug("operator_ping_received")

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def open_positions(self) -> dict[str, OpenPosition]:
        return dict(self._open_positions)

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _check_exit_condition(pos: OpenPosition, current_yes_price: float) -> str | None:
    """Return an exit reason string if the position should be closed, else None."""
    if pos.direction == "buy_yes":
        if pos.strategy == "probability_arbitrage":
            if abs(current_yes_price - pos.model_prob) <= C.CONVERGENCE_EXIT:
                return "convergence"
            if current_yes_price <= pos.stop_price:
                return "stop_loss"
        elif pos.strategy == "mean_reversion":
            if current_yes_price >= pos.target_price:
                return "target_hit"
            if current_yes_price <= pos.stop_price:
                return "stop_loss"
    else:  # buy_no
        if pos.strategy == "probability_arbitrage":
            if abs(current_yes_price - pos.model_prob) <= C.CONVERGENCE_EXIT:
                return "convergence"
            if current_yes_price >= pos.stop_price:
                return "stop_loss"
        elif pos.strategy == "mean_reversion":
            if current_yes_price <= pos.target_price:
                return "target_hit"
            if current_yes_price >= pos.stop_price:
                return "stop_loss"
    return None


def _parse_settlement_date(close_time: str) -> str:
    if close_time and len(close_time) >= 10:
        return close_time[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PolyEdge production engine — live Kalshi trading."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run full signal pipeline but never place real orders.",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Root log level (default INFO).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.getLogger().setLevel(args.log_level)

    engine = Engine(dry_run=args.dry_run)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting.")
    sys.exit(0)
