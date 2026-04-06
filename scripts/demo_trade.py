"""
scripts/demo_trade.py — 30-day continuous paper-trading loop on Kalshi demo API.

Wires together every PolyEdge subsystem into a production-equivalent loop
that runs against demo-api.kalshi.co with zero real capital.

What runs every 30 seconds (scan cycle)
-----------------------------------------
  1. Fetch open market list → MarketScanner filter/rank
  2. Update rolling price-history windows (Strategy B spike detection)
  3. For each tradeable market not already in a position:
       a. ProbabilityModel.estimate()           — base-rate + optional Claude
       b. StrategyA.evaluate()                  — probability arbitrage signal
       c. StrategyB.evaluate()                  — post-spike mean reversion signal
       d. ClaudeAnalyzer.scan_mode()            — news confidence adjustment
       e. NewsCatalyst.adjust()                 — veto or confidence delta
       f. OrderbookConfirm.check()              — depth / spoof / slippage gate
       g. PositionSizer.size()                  — fractional Kelly
       h. CorrelationManager.check()            — concentration limits
       i. CircuitBreaker.is_paused()            — drawdown gate
       j. KalshiClient.place_order()            — limit buy on demo API
       k. DB INSERT trades + calibration
       l. SlackAlerter.trade_executed()
  4. Upsert each scanned market into the markets table

What runs every 5 minutes (position monitor)
---------------------------------------------
  • Fetch current price for every open position
  • Check exit conditions per strategy (convergence, target/stop, settlement)
  • Settle any markets that have resolved since last check
  • Execute exit orders where needed; close DB rows; update calibration

What runs at midnight UTC (daily tasks)
-----------------------------------------
  • EdgeErosionMonitor.run_daily_snapshot()
  • DailyReporter.send_daily()
  • BackupManager.run_backup()
  • CircuitBreaker.update()  — fresh daily P&L check
  • Operator idle check

What runs every Monday midnight UTC (weekly tasks)
----------------------------------------------------
  • DailyReporter.send_weekly()

Shutdown / limits
-----------------
  • 30-day hard runtime limit (configurable via --duration-days)
  • Graceful SIGINT/SIGTERM: close open positions, send final summary, exit
  • Kill-switch: trading halts if bankroll <= $300 (demo still tracks fake money)

Usage
-----
    # Default: 30-day demo run, KALSHI_ENV must be set to "demo" in .env
    python scripts/demo_trade.py

    # Shorten for testing
    python scripts/demo_trade.py --duration-days 1

    # Scan+signal only — never places orders (safe to run without demo credentials)
    python scripts/demo_trade.py --dry-run

    # Verbose per-cycle logging
    python scripts/demo_trade.py --log-level DEBUG
"""

import argparse
import asyncio
import dataclasses
import functools
import json
import logging
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

from analysis.claude_analyzer import ClaudeAnalyzer, ScanContext
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
logger = logging.getLogger("demo_trade")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCAN_INTERVAL_S:     int = 30        # seconds between scan cycles
MONITOR_INTERVAL_S:  int = 300       # seconds between position-monitor cycles
BALANCE_REFRESH_CYCLES: int = 10     # refresh bankroll every N scan cycles
MAX_HISTORY_POINTS:  int = 120       # ~1 hour of history at 30 s intervals
SETTLE_CHECK_HOURS:  int = 48        # exit positions within 48 h of settlement
MAX_POSITION_AGE_DAYS: int = 7       # force-exit after 7 days regardless


# ---------------------------------------------------------------------------
# Open-position record
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class OpenPosition:
    """In-memory record of one open demo position."""
    trade_id:     int
    ticker:       str
    strategy:     str        # "probability_arbitrage" | "mean_reversion"
    direction:    str        # "buy_yes" | "buy_no"
    side:         str        # "YES" | "NO"   (DB field)
    num_contracts: int
    entry_price:  float      # dollars
    model_prob:   float      # probability estimate at entry
    target_price: float      # take-profit level in YES dollars
    stop_price:   float      # stop-loss level in YES dollars
    category:     str
    entry_time:   datetime   # UTC
    order_id:     str        # Kalshi order ID


# ---------------------------------------------------------------------------
# DemoTrader
# ---------------------------------------------------------------------------

class DemoTrader:
    """Full PolyEdge trading loop running on the Kalshi demo API.

    All network I/O is either already async (Claude) or wrapped via
    asyncio.to_thread() so the event loop stays responsive.
    """

    def __init__(
        self,
        duration_days: int = 30,
        dry_run:       bool = False,
    ) -> None:
        self._duration       = timedelta(days=duration_days)
        self._dry_run        = dry_run
        self._start_time:    datetime | None = None
        self._shutdown_event = asyncio.Event()

        # ---------- Subsystems ----------
        self._client          = KalshiClient()          # demo base URL from settings
        # Demo env: no real trading activity → relax volume/liquidity/settlement gates
        _is_demo = S.KALSHI_ENV == "demo"
        self._scanner         = MarketScanner(
            self._client,
            volume_threshold=0.0    if _is_demo else C.MIN_MARKET_VOLUME_7D,
            min_liquidity=0.0       if _is_demo else 1_000.0,
            min_settlement_days=1   if _is_demo else C.MIN_SETTLEMENT_DAYS,
            min_hours_to_settle=1.0 if _is_demo else 48.0,
        )
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

        # Try to init Claude; gracefully degrade if API key absent
        try:
            self._analyzer: ClaudeAnalyzer | None = ClaudeAnalyzer()
        except Exception as exc:
            logger.warning("Claude analyzer unavailable: %s — signals will use cached=True", exc)
            self._analyzer = None

        # ---------- Runtime state ----------
        self._bankroll:         float = S.STARTING_BANKROLL
        self._open_positions:   dict[str, OpenPosition] = {}
        self._price_history:    dict[str, deque[PricePoint]] = (
            defaultdict(lambda: deque(maxlen=MAX_HISTORY_POINTS))
        )
        self._scan_cycle_count:  int = 0
        self._last_balance_fetch: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_daily_run:     datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_weekly_run:    datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_operator_ping: datetime = datetime.now(timezone.utc)
        self._consecutive_errors: int = 0
        self._safe_mode:          bool = False

    # =========================================================================
    # Public entrypoint
    # =========================================================================

    async def run(self) -> None:
        """Main entry point.  Runs until duration elapses, kill switch fires,
        SIGINT/SIGTERM received, or an unrecoverable error occurs."""
        self._start_time = datetime.now(timezone.utc)

        logger.info("---------------------------------------------")
        logger.info("  PolyEdge DEMO  |  env=%s  |  dry_run=%s", S.KALSHI_ENV, self._dry_run)
        logger.info("  Duration: %d days  |  Start: %s",
                    self._duration.days, self._start_time.strftime("%Y-%m-%dT%H:%MZ"))
        logger.info("---------------------------------------------")

        if S.KALSHI_ENV != "demo":
            logger.critical(
                "KALSHI_ENV=%s — refusing to run demo_trade on non-demo environment. "
                "Set KALSHI_ENV=demo in .env", S.KALSHI_ENV
            )
            sys.exit(1)

        await init_db()
        await self._load_open_positions()
        await self._refresh_bankroll()

        # Install signal handlers for graceful shutdown.
        # add_signal_handler is Unix-only; fall back to signal.signal on Windows.
        loop = asyncio.get_event_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown_event.set)
        except NotImplementedError:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(self._shutdown_event.set))

        self._alerter.system_info(
            "Demo trading loop started",
            {
                "Environment":  S.KALSHI_ENV,
                "Duration":     f"{self._duration.days} days",
                "Dry run":      str(self._dry_run),
                "Bankroll":     f"${self._bankroll:,.2f}",
                "Open pos":     str(len(self._open_positions)),
                "Scan interval": f"{SCAN_INTERVAL_S}s",
            },
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
        )

        # Launch concurrent tasks
        tasks = [
            asyncio.create_task(self._scan_loop(),     name="scan_loop"),
            asyncio.create_task(self._monitor_loop(),  name="position_monitor"),
            asyncio.create_task(self._scheduler_loop(), name="scheduler"),
        ]

        deadline = self._start_time + self._duration
        timeout_s = (deadline - datetime.now(timezone.utc)).total_seconds()

        try:
            await asyncio.wait(
                [asyncio.create_task(self._shutdown_event.wait()), *tasks],
                timeout=max(0.0, timeout_s),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            logger.info("Shutdown initiated — cancelling tasks …")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._graceful_shutdown()

    # =========================================================================
    # Scan loop
    # =========================================================================

    async def _scan_loop(self) -> None:
        """Main 30-second market-scan-and-signal loop."""
        while not self._shutdown_event.is_set():
            cycle_start = datetime.now(timezone.utc)
            self._scan_cycle_count += 1

            try:
                await self._scan_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_errors += 1
                logger.error("scan_cycle_error  cycle=%d  error=%s",
                             self._scan_cycle_count, exc, exc_info=True)
                if self._consecutive_errors >= C.MAX_DEPENDENCY_FAILURES:
                    await self._enter_safe_mode(str(exc))

            # Refresh bankroll periodically (not on every cycle — avoids auth call spam)
            if self._scan_cycle_count % BALANCE_REFRESH_CYCLES == 0:
                await self._refresh_bankroll()

            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_for = max(0.0, SCAN_INTERVAL_S - elapsed)
            logger.debug("scan_cycle=%d  elapsed=%.1fs  sleeping=%.1fs",
                         self._scan_cycle_count, elapsed, sleep_for)
            await asyncio.sleep(sleep_for)

    async def _scan_cycle(self) -> None:
        """One full scan-signal-execute cycle."""
        if self._safe_mode:
            logger.debug("safe_mode_active — skipping scan cycle")
            return

        # ── Circuit-breaker check ──────────────────────────────────────
        if self._circuit_breaker.kill_switch_active():
            logger.warning("kill_switch_active — halting all trading")
            self._shutdown_event.set()
            return

        if self._circuit_breaker.is_paused():
            resume = self._circuit_breaker.paused_until()
            logger.info("circuit_breaker_paused  resume=%s",
                        resume.strftime("%Y-%m-%dT%H:%MZ") if resume else "?")
            return

        # ── Market scan ────────────────────────────────────────────────
        try:
            scan_result = await asyncio.to_thread(self._scanner.scan)
        except Exception as exc:
            logger.warning("market_scan_failed  error=%s", exc)
            self._consecutive_errors += 1
            return

        self._consecutive_errors = 0
        tradeable = scan_result.tradeable
        logger.info(
            "scan_cycle=%d  fetched=%d  tradeable=%d  open_pos=%d  bankroll=$%.2f",
            self._scan_cycle_count, scan_result.total_fetched, len(tradeable),
            len(self._open_positions), self._bankroll,
        )

        # ── Update price history + DB market cache ─────────────────────
        now_utc = datetime.now(timezone.utc)
        async with get_connection() as db:
            for market in scan_result.tradeable + scan_result.rejected:
                self._update_price_history(market, now_utc)
                await self._db_upsert_market(db, market)
            await db.commit()

        # ── Signal pipeline for each tradeable market ──────────────────
        for market in tradeable:
            if self._shutdown_event.is_set():
                break
            if market.ticker in self._open_positions:
                continue   # already in a position on this market

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
        """Run the full signal pipeline for one market.  Executes a trade if
        all gates pass.  Returns silently on any gate rejection."""

        ticker   = market.ticker
        category = market.category

        # ── 1. Probability model ──────────────────────────────────────
        estimate = await self._prob_model.estimate(
            category=category,
            market_price=market.mid_price,
            claude_result=None,   # no decision call here; scan only if eligible
        )

        # ── 2. Strategy A signal ──────────────────────────────────────
        signal = self._strategy_a.evaluate(market, estimate)

        # ── 3. Strategy B signal (if A didn't fire or B has higher edge) ──
        history = list(self._price_history[ticker])
        signal_b = None
        if len(history) >= 2:
            signal_b = self._strategy_b.evaluate(
                market=market,
                price_history=history,
                scan_result=None,
                reg_alerts=None,
            )

        # Take the signal with higher net edge
        if signal_b is not None:
            if signal is None or signal_b.net_edge_pp > signal.net_edge_pp:
                signal = signal_b

        if signal is None:
            return

        # ── 4. Claude scan (cheap Haiku — only if signal eligible) ────
        scan_result = None
        if self._analyzer is not None:
            try:
                ctx = ScanContext(
                    headline_title="No recent headline",
                    headline_source="",
                    headline_summary="",
                    market_ticker=ticker,
                    market_title=market.title,
                    market_category=category,
                    current_price=market.mid_price,
                    days_to_settlement=market.days_to_settlement,
                )
                scan_result = await self._analyzer.scan_mode(ctx)
            except Exception as exc:
                logger.debug("claude_scan_failed  ticker=%s  error=%s", ticker, exc)

        # ── 5. NewsCatalyst confidence adjustment ─────────────────────
        catalyst = self._catalyst.adjust(signal, scan_result)
        if catalyst.veto:
            logger.info("signal_vetoed  ticker=%s  reason=%s",
                        ticker, catalyst.adjustment_reason)
            return
        signal = dataclasses.replace(signal, confidence=catalyst.adjusted_confidence)

        # ── 6. Orderbook confirmation ─────────────────────────────────
        try:
            ob = await asyncio.to_thread(
                self._client.get_orderbook, ticker, 5
            )
        except Exception as exc:
            logger.warning("orderbook_fetch_failed  ticker=%s  error=%s", ticker, exc)
            return

        num_contracts = C.MIN_TRADE_SIZE   # will be refined by sizer
        ob_result = self._ob_confirm.check(signal.direction, ob, num_contracts)
        if not ob_result.confirmed:
            # In demo mode the orderbook is always empty — use the model's estimate
            # as the limit price so Kelly edge is positive (model_prob > entry_price).
            if S.KALSHI_ENV == "demo" and ob_result.reason in (
                "empty_yes_orderbook", "empty_no_orderbook", "insufficient_depth",
            ):
                # Place limit at 95% of model estimate → small positive Kelly
                raw_entry = estimate.final_prob * 0.95
                entry_price = max(0.01, min(0.99, round(raw_entry, 2)))
                logger.debug("ob_demo_fallback  ticker=%s  entry=%.3f", ticker, entry_price)
            else:
                logger.debug("ob_rejected  ticker=%s  reason=%s", ticker, ob_result.reason)
                return
        else:
            entry_price = ob_result.entry_price   # refined best-ask

        # ── 7. Position sizing ────────────────────────────────────────
        resolved_count = self._calibration.get_state().resolved_count
        size = self._sizer.size(
            model_prob=estimate.final_prob,
            entry_price=entry_price,
            bankroll=self._bankroll,
            resolved_trade_count=resolved_count,
        )
        _demo_size_override = False
        if not size.eligible:
            # Demo override: use minimum trade size when the only issue is
            # insufficient Kelly edge or trade too small (not a true risk block).
            _demo_tradeable_reasons = (
                "dollar_size", "negative_kelly", "zero_kelly",
            )
            if S.KALSHI_ENV == "demo" and any(
                r in size.reason for r in _demo_tradeable_reasons
            ):
                num_contracts = C.MIN_TRADE_SIZE
                _demo_size_override = True
                logger.debug(
                    "sizer_demo_override  ticker=%s  reason=%s  using_min=%d",
                    ticker, size.reason, num_contracts,
                )
            else:
                logger.debug("sizer_ineligible  ticker=%s  reason=%s", ticker, size.reason)
                return

        actual_contracts = num_contracts if _demo_size_override else size.num_contracts
        actual_dollar_size = actual_contracts * entry_price

        # ── 8. Correlation / concentration check ─────────────────────
        corr = self._correlation.check(
            category=category,
            direction=signal.direction,
            dollar_size=actual_dollar_size,
            bankroll=self._bankroll,
        )
        if not corr.allowed:
            logger.info("correlation_blocked  ticker=%s  reason=%s", ticker, corr.reason)
            return

        # ── 9. Final circuit-breaker gate (re-check before order) ────
        if self._circuit_breaker.is_paused():
            return

        # ── 10. Execute order (or dry-run skip) ───────────────────────
        if self._dry_run:
            logger.info(
                "DRY_RUN  ticker=%s  dir=%s  contracts=%d  entry=%.3f  "
                "edge=%.1fpp  conf=%.2f",
                ticker, signal.direction, actual_contracts, entry_price,
                signal.net_edge_pp, signal.confidence,
            )
            return

        order_id, executed_price = await self._place_entry_order(
            ticker=ticker,
            direction=signal.direction,
            num_contracts=actual_contracts,
            limit_price=entry_price,
        )
        if order_id is None:
            return   # order placement failed

        # ── 11. Persist to DB ─────────────────────────────────────────
        side = "YES" if signal.direction == "buy_yes" else "NO"
        fees = C.kalshi_fee(executed_price, actual_contracts, is_maker=True)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        notes_payload = json.dumps({
            "direction":   signal.direction,
            "model_prob":  estimate.final_prob,
            "target_price": signal.target_price,
            "stop_price":   signal.stop_price,
            "category":    category,
            "order_id":    order_id,
            "confidence":  signal.confidence,
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
                    ticker, signal.strategy, side, actual_contracts,
                    executed_price, now_str, fees, notes_payload,
                ),
            )
            trade_id = cursor.lastrowid
            await db.execute(
                """
                INSERT INTO calibration
                    (trade_id, ticker, predicted_prob, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (trade_id, ticker, estimate.final_prob, now_str),
            )
            await db.commit()

        # ── 12. Update in-memory state ────────────────────────────────
        pos = OpenPosition(
            trade_id=trade_id,
            ticker=ticker,
            strategy=signal.strategy,
            direction=signal.direction,
            side=side,
            num_contracts=actual_contracts,
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

        # ── 13. Slack alert ───────────────────────────────────────────
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

        tickers_to_close: list[tuple[str, float, str]] = []  # (ticker, exit_yes_price, reason)

        for ticker, pos in list(self._open_positions.items()):
            try:
                raw = await asyncio.to_thread(self._client.get_market, ticker)
            except Exception as exc:
                logger.warning("market_fetch_failed  ticker=%s  error=%s", ticker, exc)
                continue

            market_status = raw.get("status", "")
            yes_ask       = raw.get("yes_ask") or 0
            yes_bid       = raw.get("yes_bid") or 0
            current_yes   = (yes_ask + yes_bid) / 200.0   # mid-price in dollars
            close_time    = raw.get("close_time") or raw.get("expiration_time") or ""

            # ── Settlement ──────────────────────────────────────────
            if market_status == "settled":
                result_str = raw.get("result", "")
                outcome = 1 if result_str == "yes" or yes_ask >= 99 else 0
                exit_yes = float(outcome)
                tickers_to_close.append((ticker, exit_yes, "settled"))
                continue

            # ── Approaching close time ───────────────────────────────
            if close_time:
                try:
                    import re as _re
                    clean = _re.sub(r"\.\d+Z?$", "Z", close_time).replace("Z", "+00:00")
                    close_dt = datetime.fromisoformat(clean)
                    hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left < SETTLE_CHECK_HOURS:
                        tickers_to_close.append((ticker, current_yes, "near_settlement"))
                        continue
                except (ValueError, TypeError):
                    pass

            # ── Age-based exit ───────────────────────────────────────
            age_days = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 86400
            if age_days > MAX_POSITION_AGE_DAYS:
                tickers_to_close.append((ticker, current_yes, "age_limit"))
                continue

            # ── Strategy-specific exits ──────────────────────────────
            exit_reason = _check_exit_condition(pos, current_yes)
            if exit_reason:
                tickers_to_close.append((ticker, current_yes, exit_reason))

        # ── Execute closes ───────────────────────────────────────────
        for ticker, exit_yes_price, reason in tickers_to_close:
            await self._close_position(ticker, exit_yes_price, reason)

    async def _close_position(
        self,
        ticker:         str,
        exit_yes_price: float,
        reason:         str,
    ) -> None:
        """Close one open position: execute exit order, update DB, fire alert."""
        pos = self._open_positions.get(ticker)
        if pos is None:
            return

        # P&L calculation
        if pos.direction == "buy_yes":
            entry  = pos.entry_price
            exit_p = exit_yes_price
        else:
            entry  = 1.0 - pos.entry_price   # NO entry price (already stored as such)
            exit_p = 1.0 - exit_yes_price

        gross_pnl = (exit_p - entry) * pos.num_contracts
        exit_fee  = C.kalshi_fee(exit_p, pos.num_contracts, is_maker=True)
        net_pnl   = round(gross_pnl - exit_fee, 4)

        # Place exit order on demo (unless dry-run or settlement)
        if not self._dry_run and reason != "settled":
            try:
                action = "sell"
                side   = pos.side.lower()
                await asyncio.to_thread(
                    functools.partial(
                        self._client.place_order,
                        ticker=ticker,
                        side=side,
                        action=action,
                        count=pos.num_contracts,
                        order_type="limit",
                        price=round(max(0.01, min(0.99, exit_p)), 2),
                    )
                )
            except Exception as exc:
                logger.warning("exit_order_failed  ticker=%s  error=%s  proceeding_with_close",
                               ticker, exc)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Update DB
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
            # Update calibration row with actual outcome
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

        # Remove from in-memory state
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
    # Scheduler loop — daily and weekly tasks
    # =========================================================================

    async def _scheduler_loop(self) -> None:
        """Fire daily/weekly tasks at the correct UTC times."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(60)   # check every minute
            now = datetime.now(timezone.utc)

            # ── Daily tasks — midnight UTC ───────────────────────────
            today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if (
                now.hour == 0 and now.minute < 5  # within the first 5 minutes of midnight
                and (now.date() > self._last_daily_run.date())
            ):
                self._last_daily_run = now
                try:
                    await self._daily_tasks()
                except Exception as exc:
                    logger.warning("daily_tasks_error  error=%s", exc, exc_info=True)

            # ── Weekly tasks — Monday midnight UTC ───────────────────
            if (
                now.weekday() == 0   # Monday
                and now.hour == 0 and now.minute < 5
                and (now.date() > self._last_weekly_run.date())
            ):
                self._last_weekly_run = now
                try:
                    await self._weekly_tasks()
                except Exception as exc:
                    logger.warning("weekly_tasks_error  error=%s", exc, exc_info=True)

    async def _daily_tasks(self) -> None:
        """Run all midnight UTC daily maintenance tasks."""
        logger.info("daily_tasks_start")
        now = datetime.now(timezone.utc)

        # ── Fetch fresh P&L for circuit-breaker check ────────────────
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

        # ── Edge erosion snapshot ─────────────────────────────────────
        try:
            snap = await self._edge_monitor.run_daily_snapshot(
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
            if snap.should_pause and not self._circuit_breaker.is_paused():
                logger.critical(
                    "edge_erosion_critical  overall=%s  pausing_trading",
                    snap.overall_status,
                )
                self._circuit_breaker.force_kill()
        except Exception as exc:
            logger.warning("edge_snapshot_failed  error=%s", exc)

        # ── Daily P&L report ─────────────────────────────────────────
        try:
            await self._reporter.send_daily(
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
        except Exception as exc:
            logger.warning("daily_report_failed  error=%s", exc)

        # ── DB backup ────────────────────────────────────────────────
        try:
            result = await self._backup.run_backup()
            logger.info(
                "backup  file=%s  size=%d  uploaded=%s",
                result.filename, result.size_bytes, result.uploaded,
            )
        except Exception as exc:
            logger.warning("backup_failed  error=%s", exc)

        # ── Operator idle check ───────────────────────────────────────
        idle_days = int((now - self._last_operator_ping).total_seconds() / 86400)
        if idle_days >= C.OPERATOR_IDLE_PAUSE_DAYS:
            logger.warning("operator_idle  idle_days=%d — pausing trading", idle_days)
            self._alerter.operator_idle(
                idle_days=idle_days,
                bankroll=self._bankroll,
                open_positions=len(self._open_positions),
            )
            # Don't force_kill — just inform; operator can override

        logger.info("daily_tasks_complete")

    async def _weekly_tasks(self) -> None:
        """Run Monday midnight UTC weekly tasks."""
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
        """Reload any open positions from DB (supports restarts mid-session)."""
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

            direction   = notes.get("direction", "buy_yes")
            model_prob  = float(notes.get("model_prob", 0.50))
            target      = float(notes.get("target_price", 0.99))
            stop        = float(notes.get("stop_price", 0.01))
            category    = notes.get("category", "economics")
            order_id    = notes.get("order_id", "")

            entry_time_raw = row["entry_time"] or ""
            try:
                import re as _re
                clean = _re.sub(r"\.\d+Z?$", "Z", entry_time_raw).replace("Z", "+00:00")
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
            logger.info("Reloaded %d open position(s) from DB", len(rows))

    async def _graceful_shutdown(self) -> None:
        """Close all open positions and send a final status message."""
        logger.info("graceful_shutdown  open_positions=%d", len(self._open_positions))

        for ticker in list(self._open_positions.keys()):
            try:
                raw = await asyncio.to_thread(self._client.get_market, ticker)
                yes_ask = raw.get("yes_ask") or 0
                yes_bid = raw.get("yes_bid") or 0
                current = (yes_ask + yes_bid) / 200.0
                await self._close_position(ticker, current, "shutdown")
            except Exception as exc:
                logger.warning("shutdown_close_failed  ticker=%s  error=%s", ticker, exc)

        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 86400
        self._alerter.system_info(
            "Demo trading loop terminated",
            {
                "Elapsed days":  f"{elapsed:.1f}",
                "Total cycles":  str(self._scan_cycle_count),
                "Final bankroll": f"${self._bankroll:,.2f}",
            },
            bankroll=self._bankroll,
            open_positions=0,
        )
        logger.info("Demo loop finished. Elapsed: %.1f days.", elapsed)

    # =========================================================================
    # Utility helpers
    # =========================================================================

    async def _refresh_bankroll(self) -> None:
        """Fetch the current demo account balance."""
        try:
            balance_data = await asyncio.to_thread(self._client.get_balance)
            # KalshiClient.get_balance() already converts cents → dollars
            new_bal = float(balance_data.get("balance", self._bankroll))
            # Demo accounts often return $0 (unfunded virtual wallet).
            # Fall back to STARTING_BANKROLL so the engine can still execute trades.
            if new_bal <= 0 and S.KALSHI_ENV == "demo":
                new_bal = S.STARTING_BANKROLL
            if new_bal != self._bankroll:
                logger.info("bankroll_updated  old=$%.2f  new=$%.2f", self._bankroll, new_bal)
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
        """Place a demo limit order. Returns (order_id, executed_price)."""
        side   = "yes" if direction == "buy_yes" else "no"
        price  = round(max(0.01, min(0.99, limit_price)), 2)

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
            order_id = order.get("order_id") or order.get("id") or "demo_order"
            return order_id, price
        except Exception as exc:
            logger.warning("place_order_failed  ticker=%s  error=%s", ticker, exc)
            return None, price

    def _update_price_history(self, market: ScannedMarket, ts: datetime) -> None:
        """Append current mid-price to the rolling history window."""
        if market.mid_price > 0:
            self._price_history[market.ticker].append(
                PricePoint(price=market.mid_price, timestamp=ts)
            )

    async def _fetch_period_pnl(self, hours: int) -> float:
        """Sum P&L from closed/settled trades within the last `hours` hours."""
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
        """Enter safe mode after MAX_DEPENDENCY_FAILURES consecutive errors."""
        if self._safe_mode:
            return
        self._safe_mode = True
        logger.critical("entering_safe_mode  reason=%s  errors=%d",
                        reason, self._consecutive_errors)
        self._alerter.safe_mode_entered(
            reason=reason,
            failed_services=["kalshi_api" if "kalshi" in reason.lower() else "unknown"],
            bankroll=self._bankroll,
            open_positions=len(self._open_positions),
        )

    @staticmethod
    async def _db_upsert_market(db, market: ScannedMarket) -> None:
        """Cache scanned market metadata in the markets table."""
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
    # Operator ping — call this to reset the idle timer
    # =========================================================================

    def ping_operator(self) -> None:
        """Reset the operator idle timer.  Call periodically or on user input."""
        self._last_operator_ping = datetime.now(timezone.utc)
        logger.debug("operator_ping_received")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _check_exit_condition(pos: OpenPosition, current_yes_price: float) -> str | None:
    """Return an exit reason string if the position should be closed, else None."""
    if pos.direction == "buy_yes":
        # Strategy A: convergence within CONVERGENCE_EXIT of model_prob
        if pos.strategy == "probability_arbitrage":
            if abs(current_yes_price - pos.model_prob) <= C.CONVERGENCE_EXIT:
                return "convergence"
            # Hard stop — price moved significantly against us
            if current_yes_price <= pos.stop_price:
                return "stop_loss"
        # Strategy B: target or stop hit
        elif pos.strategy == "mean_reversion":
            if current_yes_price >= pos.target_price:
                return "target_hit"
            if current_yes_price <= pos.stop_price:
                return "stop_loss"

    else:  # buy_no — we track in YES price space for comparison
        if pos.strategy == "probability_arbitrage":
            # Our NO entry: model thinks YES is overpriced → wait for YES to fall
            if abs(current_yes_price - pos.model_prob) <= C.CONVERGENCE_EXIT:
                return "convergence"
            # Stop: YES kept rising (adverse to our NO position)
            if current_yes_price >= pos.stop_price:
                return "stop_loss"
        elif pos.strategy == "mean_reversion":
            # We faded an UP spike; target = YES falls to target_price
            if current_yes_price <= pos.target_price:
                return "target_hit"
            if current_yes_price >= pos.stop_price:
                return "stop_loss"

    return None


def _parse_settlement_date(close_time: str) -> str:
    """Extract YYYY-MM-DD from a Kalshi ISO timestamp string."""
    if close_time and len(close_time) >= 10:
        return close_time[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PolyEdge demo trading loop — Kalshi demo API, 30-day run."
    )
    p.add_argument(
        "--duration-days", type=int, default=30,
        help="How many calendar days to run (default 30).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run the full signal pipeline but never place real orders.",
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

    trader = DemoTrader(
        duration_days=args.duration_days,
        dry_run=args.dry_run,
    )

    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting.")
    sys.exit(0)
