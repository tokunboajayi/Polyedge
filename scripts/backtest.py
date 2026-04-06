"""
scripts/backtest.py — Replay settled Kalshi markets through Strategy A & B.

For each settled market in the database this script:
  1. Constructs the conditions under which PolyEdge would have considered trading.
  2. Determines whether Strategy A (probability arbitrage) would have signalled.
  3. Determines whether Strategy B (post-spike mean reversion) would have signalled.
  4. Simulates entry / exit / P&L including maker round-trip fees.
  5. Computes aggregate metrics and prints a clear PASS / FAIL verdict.

Verdict rules
-------------
  PASS  — Sharpe ratio > 0.5  AND  win rate > 50%  (for any strategy with ≥10 trades)
  FAIL  — Either condition unmet (script exits with code 1)

Strategy A — Probability Arbitrage
-----------------------------------
Model probability = seeded category prior (from probability_model._SEEDED_PRIORS).
Entry price       = open_price stored by seed_data.py.
                    If missing: prior ± small noise (simulated).
Signal gate       = |model_prob − entry_price| > DIVERGENCE_THRESHOLD (10 pp)
                    AND net edge survives round-trip maker fee drag.
Exit              = settlement (YES payout = 1.0, NO payout = 0.0).

Strategy B — Post-Spike Mean Reversion
----------------------------------------
Uses the price_history JSON stored by seed_data.py.
Spike detection   = detect_spike() from core.strategies.mean_reversion.
Entry             = price at first point after the 30-min cooling-off window.
Exit              = first subsequent price that hits:
                      (a) target  — 50% reversion of spike move, or
                      (b) stop    — 25% further adverse move,
                    whichever occurs first.  If neither fires before the end of
                    history, the settlement outcome resolves the trade.

Metrics calculated (per strategy, then combined)
-------------------------------------------------
  • Total P&L ($)
  • Win rate (%)
  • Annualised Sharpe ratio
  • Average edge (pnl / entry_price, mean across trades)
  • Mean Brier score  (predicted_prob vs actual_outcome)^2

Usage
-----
    python scripts/backtest.py [--min-trades N] [--verbose]
"""

import argparse
import asyncio
import json
import logging
import math
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from analysis.probability_model import _SEEDED_PRIORS
from config import constants as C
from core.strategies.mean_reversion import PricePoint, detect_spike
from persistence.database import get_connection

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NUM_CONTRACTS   = 5        # minimum Kalshi trade size ($5 notional)
RANDOM_SEED     = 42       # reproducible simulated prices
MIN_OPEN_PRICE  = 0.10     # reject implausible open prices
MAX_OPEN_PRICE  = 0.90

random.seed(RANDOM_SEED)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class SimTrade:
    ticker:         str
    strategy:       str          # "strategy_a" | "strategy_b"
    direction:      str          # "buy_yes" | "buy_no"
    entry_price:    float        # dollars
    exit_price:     float        # dollars (payout at exit or settlement)
    actual_outcome: int          # 1 = YES, 0 = NO
    predicted_prob: float        # model's YES probability at entry
    num_contracts:  int
    pnl:            float        # net of fees
    won:            bool
    settlement_date: str


# ---------------------------------------------------------------------------
# Fee helper (re-exports C.kalshi_fee but named clearly)
# ---------------------------------------------------------------------------

def _rt_fee(price: float, n: int) -> float:
    """Round-trip maker fee for n contracts at price (0.01–0.99)."""
    return C.round_trip_fee(price, n, is_maker=True)


# ---------------------------------------------------------------------------
# P&L calculation
# ---------------------------------------------------------------------------

def _pnl_buy_yes(entry: float, actual_outcome: int, n: int) -> float:
    """P&L for a buy-YES position settled at actual_outcome (1 or 0)."""
    gross = n * (float(actual_outcome) - entry)
    fee   = _rt_fee(entry, n)
    return round(gross - fee, 4)


def _pnl_buy_no(entry_no: float, actual_outcome: int, n: int) -> float:
    """P&L for a buy-NO position (entry_no is the NO price = 1 − YES price)."""
    gross = n * (float(1 - actual_outcome) - entry_no)
    fee   = _rt_fee(entry_no, n)
    return round(gross - fee, 4)


# ---------------------------------------------------------------------------
# Strategy A simulation
# ---------------------------------------------------------------------------

def _simulate_strategy_a(market: dict) -> SimTrade | None:
    """Return a SimTrade if Strategy A would have traded this market, else None."""
    category = market["category"]
    result   = market["result"]           # "yes" or "no"
    ticker   = market["ticker"]
    sd       = market["settlement_date"]

    actual_outcome = 1 if result == "yes" else 0

    # Model probability = seeded category prior
    model_prob = _SEEDED_PRIORS.get(category, 0.50)

    # Entry price — from DB (open_price) or imputed
    open_price = market.get("open_price")
    if open_price is None or not (MIN_OPEN_PRICE <= open_price <= MAX_OPEN_PRICE):
        # Impute: draw from Normal(model_prob ± 0.12) clamped to [0.10, 0.90]
        # This simulates a market that was pricing near but not at fair value.
        noise      = random.gauss(0.0, 0.12)
        open_price = max(MIN_OPEN_PRICE, min(MAX_OPEN_PRICE, model_prob + noise))

    # Divergence check — Strategy A gate
    divergence_pp = (model_prob - open_price) * 100.0
    fee_drag_pp   = _rt_fee(open_price, NUM_CONTRACTS) * 100.0 / NUM_CONTRACTS
    abs_div       = abs(divergence_pp)

    if abs_div <= C.DIVERGENCE_THRESHOLD or abs_div <= fee_drag_pp:
        return None   # not eligible — no trade

    # Direction
    if divergence_pp > 0:
        # Model says YES is underpriced → buy YES
        direction      = "buy_yes"
        entry_price    = open_price
        pnl            = _pnl_buy_yes(entry_price, actual_outcome, NUM_CONTRACTS)
        predicted_prob = model_prob
    else:
        # Model says YES is overpriced → buy NO
        direction      = "buy_no"
        entry_price    = 1.0 - open_price   # NO price
        pnl            = _pnl_buy_no(entry_price, actual_outcome, NUM_CONTRACTS)
        predicted_prob = model_prob          # still our YES probability estimate

    won = pnl > 0

    logger.debug(
        "A  %-30s  dir=%-8s  entry=%.2f  model=%.2f  div=%+.1fpp  pnl=%+.4f  %s",
        ticker, direction, open_price, model_prob, divergence_pp,
        pnl, "WIN" if won else "LOSS",
    )

    return SimTrade(
        ticker=ticker,
        strategy="strategy_a",
        direction=direction,
        entry_price=open_price,
        exit_price=float(actual_outcome),
        actual_outcome=actual_outcome,
        predicted_prob=predicted_prob,
        num_contracts=NUM_CONTRACTS,
        pnl=pnl,
        won=won,
        settlement_date=sd,
    )


# ---------------------------------------------------------------------------
# Strategy B simulation
# ---------------------------------------------------------------------------

def _build_price_points(history: list[dict]) -> list[PricePoint]:
    """Convert raw history dicts to PricePoint objects with UTC datetimes."""
    pts = []
    for h in history:
        ts  = h.get("ts", 0)
        yp  = h.get("yes_price")
        if ts and yp is not None:
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                pts.append(PricePoint(price=float(yp), timestamp=dt))
            except (OSError, ValueError, OverflowError):
                continue
    return sorted(pts, key=lambda p: p.timestamp)


def _find_exit_b(
    pts:          list[PricePoint],
    spike_idx:    int,
    direction:    str,
    target_yes:   float,
    stop_yes:     float,
    actual_outcome: int,
) -> tuple[float, bool]:
    """Scan price points after spike_idx for target/stop.

    Returns (exit_yes_price, did_hit_target).
    Falls through to settlement if neither level is hit.
    """
    entry_actual_idx = spike_idx + 1  # enter at the point after spike

    for i in range(entry_actual_idx, len(pts)):
        p = pts[i].price

        if direction == "buy_no":   # fading an UP spike
            if p <= target_yes:
                return target_yes, True    # target hit — YES fell to target
            if p >= stop_yes:
                return stop_yes, False     # stop hit — YES kept rising
        else:                        # fading a DOWN spike (buy_yes)
            if p >= target_yes:
                return target_yes, True    # target hit — YES recovered
            if p <= stop_yes:
                return stop_yes, False     # stop hit — YES fell further

    # No level hit before end of history → settle at outcome
    exit_yes = float(actual_outcome)
    won      = (
        (direction == "buy_no"  and actual_outcome == 0) or
        (direction == "buy_yes" and actual_outcome == 1)
    )
    return exit_yes, won


def _simulate_strategy_b(market: dict) -> SimTrade | None:
    """Return a SimTrade if Strategy B would have traded this market, else None."""
    ticker   = market["ticker"]
    result   = market["result"]
    sd       = market["settlement_date"]
    ph_json  = market.get("price_history")

    if not ph_json:
        return None

    try:
        raw_history = json.loads(ph_json)
    except (json.JSONDecodeError, TypeError):
        return None

    pts = _build_price_points(raw_history)
    if len(pts) < 3:
        return None

    actual_outcome = 1 if result == "yes" else 0

    # Scan for the first spike in any rolling window of the price history.
    # We check every prefix of the price series to find the first spike event.
    spike_found_at = None
    spike          = None

    for i in range(2, len(pts)):
        window = pts[:i + 1]
        sp     = detect_spike(window)
        if sp is not None:
            spike_found_at = i
            spike          = sp
            break

    if spike is None or spike_found_at is None:
        return None

    # Respect the 30-minute cooling-off window — find the entry point
    spike_time        = pts[spike_found_at].timestamp
    entry_eligible_at = spike_time + timedelta(minutes=C.SPIKE_WAIT_MINUTES)

    # Find the first price point after the cooling-off
    entry_idx = None
    for i in range(spike_found_at + 1, len(pts)):
        if pts[i].timestamp >= entry_eligible_at:
            entry_idx = i
            break

    if entry_idx is None:
        # Market settled before 30-min wait expired — skip
        return None

    current_price = spike.current_price
    baseline      = spike.baseline_price
    spike_move    = current_price - baseline

    if spike.direction == "up":
        direction     = "buy_no"
        entry_yes     = pts[entry_idx].price   # YES price at entry after wait
        entry_price   = round(1.0 - entry_yes, 4)  # NO price

        # Target: 50% reversion of spike
        target_yes  = round(current_price - 0.50 * abs(spike_move), 4)
        target_yes  = max(0.01, min(0.99, target_yes))
        # Stop:   25% further adverse move
        stop_yes    = round(current_price + 0.25 * abs(spike_move), 4)
        stop_yes    = max(0.01, min(0.99, stop_yes))

        exit_yes, hit_target = _find_exit_b(
            pts, entry_idx, direction, target_yes, stop_yes, actual_outcome
        )
        exit_no  = round(1.0 - exit_yes, 4)
        pnl      = _pnl_buy_no(entry_price, actual_outcome, NUM_CONTRACTS)
        # Refine pnl if target/stop hit before settlement
        if hit_target:
            pnl = _pnl_buy_no(entry_price, 0, NUM_CONTRACTS)   # YES fell = NO wins
            pnl = round(NUM_CONTRACTS * (exit_no - entry_price) - _rt_fee(entry_price, NUM_CONTRACTS), 4)

        predicted_prob = round(current_price - 0.50 * abs(spike_move), 4)  # expected reversion

    else:  # spike down → buy YES
        direction     = "buy_yes"
        entry_yes     = pts[entry_idx].price
        entry_price   = round(entry_yes, 4)

        target_yes  = round(current_price + 0.50 * abs(spike_move), 4)
        target_yes  = max(0.01, min(0.99, target_yes))
        stop_yes    = round(current_price - 0.25 * abs(spike_move), 4)
        stop_yes    = max(0.01, min(0.99, stop_yes))

        exit_yes, hit_target = _find_exit_b(
            pts, entry_idx, direction, target_yes, stop_yes, actual_outcome
        )
        if hit_target:
            pnl = round(NUM_CONTRACTS * (exit_yes - entry_price) - _rt_fee(entry_price, NUM_CONTRACTS), 4)
        else:
            pnl = _pnl_buy_yes(entry_price, actual_outcome, NUM_CONTRACTS)

        predicted_prob = target_yes

    won = pnl > 0

    logger.debug(
        "B  %-30s  dir=%-8s  spike=%.1f%%  entry=%.2f  pnl=%+.4f  %s",
        ticker, direction, spike.spike_magnitude * 100, entry_price,
        pnl, "WIN" if won else "LOSS",
    )

    return SimTrade(
        ticker=ticker,
        strategy="strategy_b",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_yes if direction == "buy_yes" else (1.0 - exit_yes),
        actual_outcome=actual_outcome,
        predicted_prob=predicted_prob,
        num_contracts=NUM_CONTRACTS,
        pnl=pnl,
        won=won,
        settlement_date=sd,
    )


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

@dataclass
class StrategyMetrics:
    name:        str
    trades:      list[SimTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def total_pnl(self) -> float:
        return round(sum(t.pnl for t in self.trades), 4)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.won) / self.n

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe from daily P&L buckets."""
        if self.n < 2:
            return 0.0
        daily: dict[str, float] = {}
        for t in self.trades:
            d = t.settlement_date
            daily[d] = daily.get(d, 0.0) + t.pnl
        if len(daily) < 2:
            return 0.0
        vals = list(daily.values())
        mu   = statistics.mean(vals)
        sd   = statistics.stdev(vals)
        if sd == 0:
            return 0.0
        return round((mu / sd) * math.sqrt(252), 4)

    @property
    def avg_edge(self) -> float:
        edges = []
        for t in self.trades:
            ep = t.entry_price
            if ep > 0:
                edges.append(t.pnl / (ep * t.num_contracts))
        return round(statistics.mean(edges), 6) if edges else 0.0

    @property
    def brier(self) -> float:
        if not self.trades:
            return 0.0
        return round(
            sum((t.predicted_prob - t.actual_outcome) ** 2 for t in self.trades)
            / self.n,
            6,
        )

    def passes(self) -> bool:
        return self.sharpe > 0.5 and self.win_rate > 0.50


# ---------------------------------------------------------------------------
# Combined reporting
# ---------------------------------------------------------------------------

_GREEN = "\033[92m"
_RED   = "\033[91m"
_BOLD  = "\033[1m"
_RST   = "\033[0m"


def _fmt(val: float, fmt: str = ".4f") -> str:
    return f"{val:{fmt}}"


def _print_metrics(sm: StrategyMetrics, min_trades: int) -> bool:
    """Print metrics table for one strategy and return True if it passes."""
    verdict_eligible = sm.n >= min_trades

    print(f"\n{'-'*60}")
    print(f"  {_BOLD}{sm.name.upper()}{_RST}")
    print(f"{'-'*60}")
    print(f"  Trades simulated  : {sm.n}")
    print(f"  Total P&L         : ${_fmt(sm.total_pnl, '+.2f')}")
    print(f"  Win rate          : {sm.win_rate:.1%}  "
          f"{'[PASS] >50%' if sm.win_rate > 0.50 else '[FAIL] <=50%'}")
    print(f"  Sharpe (annualised): {_fmt(sm.sharpe, '.4f')}  "
          f"{'[PASS] >0.5' if sm.sharpe > 0.5 else '[FAIL] <=0.5'}")
    print(f"  Avg edge          : {sm.avg_edge:.2%}")
    print(f"  Brier score       : {_fmt(sm.brier, '.4f')}")

    if not verdict_eligible:
        print(f"  Verdict           : {_BOLD}INSUFFICIENT DATA{_RST} "
              f"(need {min_trades} trades, have {sm.n})")
        return True   # don't fail due to thin data

    if sm.passes():
        print(f"  Verdict           : {_GREEN}{_BOLD}PASS{_RST}")
        return True
    else:
        reasons = []
        if sm.sharpe <= 0.5:
            reasons.append(f"Sharpe {sm.sharpe:.4f} <= 0.5")
        if sm.win_rate <= 0.50:
            reasons.append(f"win rate {sm.win_rate:.1%} <= 50%")
        print(f"  Verdict           : {_RED}{_BOLD}FAIL{_RST}  ({' / '.join(reasons)})")
        return False


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

async def run_backtest(min_trades: int = 10, verbose: bool = False) -> bool:
    """Load seeded markets, simulate both strategies, print report.

    Returns True if both strategies PASS (or have insufficient data),
    False if either strategy FAILS.
    """
    if verbose:
        logging.getLogger("backtest").setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # Load all settled markets from DB
    # ------------------------------------------------------------------
    async with get_connection() as db:
        # Check whether the extra columns exist; if not, there's no seeded data
        cursor = await db.execute(
            "SELECT name FROM pragma_table_info('markets') WHERE name IN "
            "('open_price','result','price_history')"
        )
        cols = {row[0] for row in await cursor.fetchall()}
        if "result" not in cols:
            print(
                f"{_RED}No seeded data found.{_RST}  "
                "Run  python scripts/seed_data.py  first."
            )
            return False

        cursor = await db.execute(
            """
            SELECT ticker, category, settlement_date, result,
                   open_price, price_history
            FROM   markets
            WHERE  status = 'settled'
            AND    result IS NOT NULL
            ORDER  BY settlement_date
            """
        )
        rows = await cursor.fetchall()

    markets = [dict(r) for r in rows]
    total   = len(markets)
    logger.info("Loaded %d settled markets from DB", total)

    if total == 0:
        print(
            f"{_RED}No settled markets in database.{_RST}  "
            "Run  python scripts/seed_data.py  first."
        )
        return False

    # ------------------------------------------------------------------
    # Run both strategies
    # ------------------------------------------------------------------
    sm_a = StrategyMetrics(name="Strategy A — Probability Arbitrage")
    sm_b = StrategyMetrics(name="Strategy B — Post-Spike Mean Reversion")

    for mkt in markets:
        trade_a = _simulate_strategy_a(mkt)
        if trade_a:
            sm_a.trades.append(trade_a)

        trade_b = _simulate_strategy_b(mkt)
        if trade_b:
            sm_b.trades.append(trade_b)

    # ------------------------------------------------------------------
    # Combined metrics (all trades together)
    # ------------------------------------------------------------------
    all_trades = sm_a.trades + sm_b.trades
    sm_all     = StrategyMetrics(name="Combined (A + B)")
    sm_all.trades = all_trades

    # ------------------------------------------------------------------
    # Print report
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  {_BOLD}POLYEDGE BACKTEST REPORT{_RST}")
    print(f"  Markets loaded    : {total}")
    print(f"  Strategy A trades : {sm_a.n}")
    print(f"  Strategy B trades : {sm_b.n}")
    print(f"  Combined trades   : {sm_all.n}")
    print(f"{'='*60}")

    pass_a   = _print_metrics(sm_a,   min_trades)
    pass_b   = _print_metrics(sm_b,   min_trades)
    pass_all = _print_metrics(sm_all, min_trades)

    # ------------------------------------------------------------------
    # Final verdict
    # ------------------------------------------------------------------
    overall = pass_a and pass_b and pass_all

    print(f"\n{'='*60}")
    if overall:
        print(f"  {_GREEN}{_BOLD}OVERALL VERDICT: PASS{_RST}")
        print(f"  Both strategies meet Sharpe >0.5 AND win rate >50%.")
        print(f"  PolyEdge is viable on this dataset.")
    else:
        print(f"  {_RED}{_BOLD}OVERALL VERDICT: FAIL — STOP{_RST}")
        print(f"  One or more strategies did not meet performance thresholds.")
        print(f"  Do NOT deploy live capital until strategies are recalibrated.")
    print(f"{'='*60}\n")

    return overall


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest PolyEdge Strategy A & B on seeded settled markets."
    )
    p.add_argument(
        "--min-trades", type=int, default=10,
        help="Minimum trades required for a verdict (default 10). "
             "Strategies with fewer trades are marked INSUFFICIENT DATA.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print individual trade debug lines.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args   = _parse_args()
    passed = asyncio.run(run_backtest(min_trades=args.min_trades, verbose=args.verbose))
    sys.exit(0 if passed else 1)
