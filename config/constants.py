"""
All fixed parameters for PolyEdge v5.
Edit here to tune behaviour — nothing in this file reads from the environment.
"""

import math

# ---------------------------------------------------------------------------
# Kalshi fee coefficients
# ---------------------------------------------------------------------------
TAKER_FEE_COEFFICIENT: float = 0.07     # Market orders
MAKER_FEE_COEFFICIENT: float = 0.0175   # Limit orders (preferred)


def kalshi_fee(price: float, num_contracts: int, is_maker: bool = True) -> float:
    """Calculate Kalshi fee for an order, rounded up to nearest cent.

    Args:
        price:         Contract price in dollars (0.01 – 0.99).
        num_contracts: Number of contracts in the order.
        is_maker:      True for limit orders (1.75% coeff),
                       False for market orders (7% coeff).

    Returns:
        Total fee in dollars, rounded up to the nearest cent.
    """
    coeff = MAKER_FEE_COEFFICIENT if is_maker else TAKER_FEE_COEFFICIENT
    fee_per_contract = coeff * price * (1 - price)
    total_fee = fee_per_contract * num_contracts
    return math.ceil(total_fee * 100) / 100


def round_trip_fee(price: float, num_contracts: int, is_maker: bool = True) -> float:
    """Entry fee + exit fee for a position (entry and exit at same price)."""
    return kalshi_fee(price, num_contracts, is_maker) * 2


# ---------------------------------------------------------------------------
# Kelly sizing
# ---------------------------------------------------------------------------
KELLY_CALIBRATION: float = 0.25   # Phase 1 — first 30 resolved trades
KELLY_MICRO_LIVE: float = 0.15    # Micro-live phase
KELLY_FULL: float = 0.35          # Calibrated phase

# ---------------------------------------------------------------------------
# Position limits
# ---------------------------------------------------------------------------
MAX_POSITION_PCT: float = 0.05       # 5% of bankroll per position
MIN_TRADE_SIZE: int = 5              # $5 minimum (5 contracts at $1 each)
MAX_OPEN_POSITIONS: int = 5
MAX_BANKROLL_IN_POSITIONS: float = 0.50   # 50% of bankroll max in open positions
MAX_CORRELATED_EXPOSURE: float = 0.30     # 30% in same category/direction
MAX_TRADE_SIZE_MICRO_LIVE: float = 10.00  # $10 hard cap during micro-live phase

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
KILL_SWITCH: float = 300.0   # Close all and halt when bankroll drops to $300

# ---------------------------------------------------------------------------
# Circuit breakers (loss as fraction of bankroll)
# ---------------------------------------------------------------------------
DAILY_LOSS_LIMIT: float = 0.05      # Pause 24 h when daily loss exceeds 5%
WEEKLY_LOSS_LIMIT: float = 0.10     # Pause 72 h when weekly loss exceeds 10%
MONTHLY_LOSS_LIMIT: float = 0.15    # Enter review mode when monthly loss exceeds 15%

# ---------------------------------------------------------------------------
# Strategy A — Probability Model Arbitrage
# ---------------------------------------------------------------------------
DIVERGENCE_THRESHOLD: float = 10.0   # Minimum model-to-market gap in percentage points
CONVERGENCE_EXIT: float = 0.03       # Exit when price is within 3% of model estimate

# ---------------------------------------------------------------------------
# Strategy B — Post-Spike Mean Reversion
# ---------------------------------------------------------------------------
SPIKE_THRESHOLD: float = 0.15          # Trigger when price moves ≥15% in <1 hour
REVERSION_TAKE_PROFIT: float = 0.50    # Take profit at 50% reversion
REVERSION_STOP_LOSS: float = 0.25      # Stop loss at 25% further adverse move
SPIKE_WAIT_MINUTES: int = 30           # Wait 30 min after spike before entry

# ---------------------------------------------------------------------------
# Market selection / entry criteria
# ---------------------------------------------------------------------------
MIN_MARKET_VOLUME_7D: float = 5_000.0   # $5,000 minimum volume over last 7 days
MAX_SPREAD: float = 0.08                # Maximum bid-ask spread (8¢)
MIN_SETTLEMENT_DAYS: int = 7            # Minimum days to settlement
MAX_SETTLEMENT_DAYS: int = 90           # Maximum days to settlement
MAX_EXIT_SLIPPAGE: float = 0.03         # Maximum acceptable exit slippage (3%)
MIN_HISTORICAL_PRECEDENTS: int = 50     # Base-rate model must have ≥50 data points

# ---------------------------------------------------------------------------
# Tax reserve
# ---------------------------------------------------------------------------
TAX_RESERVE_RATE: float = 0.32   # 32% of gross profits (Georgia state + federal)

# ---------------------------------------------------------------------------
# Calibration thresholds
# ---------------------------------------------------------------------------
BRIER_THRESHOLD: float = 0.30
CALIBRATION_FACTOR_RANGE: tuple[float, float] = (0.7, 1.3)

# ---------------------------------------------------------------------------
# Edge erosion — rolling 30-day thresholds
# ---------------------------------------------------------------------------
# Each metric: (healthy_min, warning_min, critical_threshold)
EDGE_WIN_RATE_HEALTHY: float = 0.55
EDGE_WIN_RATE_WARNING: float = 0.50    # Critical auto-pause below this

EDGE_SHARPE_HEALTHY: float = 1.0
EDGE_SHARPE_WARNING: float = 0.5      # Critical auto-pause below this

EDGE_FREQUENCY_MIN_HEALTHY: float = 3.0   # trades/day
EDGE_FREQUENCY_MAX_HEALTHY: float = 8.0
EDGE_FREQUENCY_MIN_WARNING: float = 1.0
EDGE_FREQUENCY_MAX_WARNING: float = 15.0

EDGE_AVG_EDGE_HEALTHY: float = 0.05    # 5%
EDGE_AVG_EDGE_WARNING: float = 0.03   # Critical auto-pause below 3%

EDGE_BRIER_HEALTHY: float = 0.20
EDGE_BRIER_WARNING: float = 0.25      # Critical auto-pause above this

# ---------------------------------------------------------------------------
# Polling intervals (seconds)
# ---------------------------------------------------------------------------
RSS_SCAN_INTERVAL: int = 600       # News feed polling — every 10 minutes
KALSHI_POLL_INTERVAL: int = 30     # Active market refresh — every 30 seconds
REGULATORY_SCAN_INTERVAL: int = 1800  # Federal Register / SEC EDGAR — every 30 min

# ---------------------------------------------------------------------------
# Kalshi API rate limits (Basic tier)
# ---------------------------------------------------------------------------
KALSHI_MAX_READS_PER_SEC: int = 20
KALSHI_MAX_WRITES_PER_SEC: int = 10

# ---------------------------------------------------------------------------
# Operator safety
# ---------------------------------------------------------------------------
OPERATOR_IDLE_PAUSE_DAYS: int = 7   # Auto-pause if no operator interaction for 7 days
MAX_DEPENDENCY_FAILURES: int = 2    # Enter safe mode after 2+ simultaneous failures
RSS_CACHE_MAX_AGE_SECONDS: int = 3600  # Stale headline limit — 1 hour

# ---------------------------------------------------------------------------
# Claude model names
# ---------------------------------------------------------------------------
CLAUDE_SCAN_MODEL: str = "claude-haiku-4-5-20251001"    # Haiku — RSS scans, screening
CLAUDE_DECISION_MODEL: str = "claude-sonnet-4-6"        # Sonnet — trade decisions
