"""
CREATE TABLE statements for all six PolyEdge SQLite tables.

Retention policy (enforced by cleanup jobs in database.py):
  trades        — forever
  calibration   — forever
  markets       — forever
  edge_metrics  — rolling 180 days
  api_costs     — rolling 90 days
  alerts        — rolling 90 days

All timestamps are stored as ISO-8601 TEXT in UTC: "2026-04-03T14:22:00Z"
All monetary values are stored in USD as REAL.
"""

# ---------------------------------------------------------------------------
# trades
# Permanent record of every entry and exit.
# ---------------------------------------------------------------------------
CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,   -- 'probability_arbitrage' | 'mean_reversion'
    side            TEXT    NOT NULL,   -- 'YES' | 'NO'
    order_type      TEXT    NOT NULL,   -- 'LIMIT' | 'MARKET'
    num_contracts   INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,   -- dollars (0.01 – 0.99)
    exit_price      REAL,               -- NULL until position is closed
    entry_time      TEXT    NOT NULL,   -- UTC ISO-8601
    exit_time       TEXT,               -- NULL until closed
    pnl             REAL,               -- NULL until closed; net of fees
    fees_paid       REAL    NOT NULL DEFAULT 0.0,
    status          TEXT    NOT NULL DEFAULT 'open',  -- 'open' | 'closed' | 'settled'
    notes           TEXT
);
"""

# ---------------------------------------------------------------------------
# calibration
# One row per prediction; brier_contribution and actual_outcome filled on settlement.
# ---------------------------------------------------------------------------
CREATE_CALIBRATION = """
CREATE TABLE IF NOT EXISTS calibration (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            INTEGER NOT NULL REFERENCES trades(id),
    ticker              TEXT    NOT NULL,
    predicted_prob      REAL    NOT NULL,   -- model probability at time of trade
    actual_outcome      INTEGER,            -- 1 = YES resolved, 0 = NO resolved; NULL until settled
    brier_contribution  REAL,               -- (predicted_prob - actual_outcome)^2; NULL until settled
    recorded_at         TEXT    NOT NULL,   -- UTC ISO-8601 — time prediction was made
    settled_at          TEXT                -- UTC ISO-8601 — time market settled
);
"""

# ---------------------------------------------------------------------------
# markets
# Catalogue of every Kalshi market seen, active or settled.
# ---------------------------------------------------------------------------
CREATE_MARKETS = """
CREATE TABLE IF NOT EXISTS markets (
    ticker          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    series          TEXT NOT NULL,
    event_id        TEXT,
    category        TEXT NOT NULL,   -- 'economics' | 'politics' | 'regulatory' | 'macro' | 'tech'
    settlement_date TEXT NOT NULL,   -- UTC ISO-8601 date
    status          TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'settled' | 'voided'
    yes_price       REAL,            -- latest YES ask price
    no_price        REAL,            -- latest NO ask price
    volume_7d       REAL,            -- total volume last 7 days in USD
    last_updated    TEXT NOT NULL    -- UTC ISO-8601 — last time row was refreshed
);
"""

# ---------------------------------------------------------------------------
# edge_metrics
# One row per calendar day; the 5-metric edge erosion snapshot.
# Rows older than 180 days are pruned by the cleanup job.
# ---------------------------------------------------------------------------
CREATE_EDGE_METRICS = """
CREATE TABLE IF NOT EXISTS edge_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date       TEXT    NOT NULL UNIQUE,  -- UTC date "2026-04-03"
    win_rate            REAL    NOT NULL,
    sharpe_ratio        REAL    NOT NULL,
    trade_frequency     REAL    NOT NULL,   -- average trades per day over window
    avg_edge            REAL    NOT NULL,   -- average model-to-market divergence
    brier_score         REAL    NOT NULL,
    trades_in_window    INTEGER NOT NULL,   -- number of settled trades in rolling window
    status              TEXT    NOT NULL,   -- 'healthy' | 'warning' | 'critical'
    created_at          TEXT    NOT NULL    -- UTC ISO-8601
);
"""

# ---------------------------------------------------------------------------
# api_costs
# One row per Claude API call.
# Rows older than 90 days are pruned by the cleanup job.
# ---------------------------------------------------------------------------
CREATE_API_COSTS = """
CREATE TABLE IF NOT EXISTS api_costs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    model             TEXT    NOT NULL,    -- e.g. 'claude-haiku-4-5-20251001'
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens      INTEGER NOT NULL,
    cost_usd          REAL    NOT NULL,
    purpose           TEXT    NOT NULL,    -- 'scan' | 'decision' | 'calibration' | 'report'
    ticker            TEXT,               -- associated market, if applicable
    called_at         TEXT    NOT NULL    -- UTC ISO-8601
);
"""

# ---------------------------------------------------------------------------
# alerts
# Circuit breaker triggers, edge warnings, API errors, system events.
# Rows older than 90 days are pruned by the cleanup job.
# ---------------------------------------------------------------------------
CREATE_ALERTS = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT    NOT NULL,   -- 'info' | 'warning' | 'critical' | 'error'
    category    TEXT    NOT NULL,   -- 'circuit_breaker' | 'edge_erosion' | 'api_error' | 'system'
    message     TEXT    NOT NULL,
    ticker      TEXT,               -- associated market, if applicable
    resolved    INTEGER NOT NULL DEFAULT 0,  -- 0 = open, 1 = resolved
    created_at  TEXT    NOT NULL,   -- UTC ISO-8601
    resolved_at TEXT                -- UTC ISO-8601; NULL until resolved
);
"""

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

# trades — hot query paths: open positions, by ticker, by strategy
CREATE_IDX_TRADES_TICKER = (
    "CREATE INDEX IF NOT EXISTS idx_trades_ticker   ON trades (ticker);"
)
CREATE_IDX_TRADES_STRATEGY = (
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy);"
)
CREATE_IDX_TRADES_ENTRY_TIME = (
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades (entry_time);"
)
CREATE_IDX_TRADES_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);"
)

# calibration — joins to trades and lookups by ticker
CREATE_IDX_CALIBRATION_TRADE = (
    "CREATE INDEX IF NOT EXISTS idx_calibration_trade_id ON calibration (trade_id);"
)
CREATE_IDX_CALIBRATION_TICKER = (
    "CREATE INDEX IF NOT EXISTS idx_calibration_ticker ON calibration (ticker);"
)

# markets — scanned by category and status every 30 s
CREATE_IDX_MARKETS_CATEGORY = (
    "CREATE INDEX IF NOT EXISTS idx_markets_category ON markets (category);"
)
CREATE_IDX_MARKETS_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_markets_status ON markets (status);"
)

# edge_metrics — queried by date for rolling window
CREATE_IDX_EDGE_METRICS_DATE = (
    "CREATE INDEX IF NOT EXISTS idx_edge_metrics_date ON edge_metrics (snapshot_date);"
)

# api_costs — queried by date for cost reporting and pruning
CREATE_IDX_API_COSTS_CALLED_AT = (
    "CREATE INDEX IF NOT EXISTS idx_api_costs_called_at ON api_costs (called_at);"
)
CREATE_IDX_API_COSTS_MODEL = (
    "CREATE INDEX IF NOT EXISTS idx_api_costs_model ON api_costs (model);"
)

# alerts — queried by level and unresolved status
CREATE_IDX_ALERTS_CREATED_AT = (
    "CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts (created_at);"
)
CREATE_IDX_ALERTS_LEVEL = (
    "CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts (level);"
)
CREATE_IDX_ALERTS_RESOLVED = (
    "CREATE INDEX IF NOT EXISTS idx_alerts_resolved ON alerts (resolved);"
)

# ---------------------------------------------------------------------------
# Ordered list consumed by database.init_db()
# ---------------------------------------------------------------------------
ALL_TABLES: list[tuple[str, str]] = [
    ("trades",       CREATE_TRADES),
    ("calibration",  CREATE_CALIBRATION),
    ("markets",      CREATE_MARKETS),
    ("edge_metrics", CREATE_EDGE_METRICS),
    ("api_costs",    CREATE_API_COSTS),
    ("alerts",       CREATE_ALERTS),
]

ALL_INDEXES: list[str] = [
    CREATE_IDX_TRADES_TICKER,
    CREATE_IDX_TRADES_STRATEGY,
    CREATE_IDX_TRADES_ENTRY_TIME,
    CREATE_IDX_TRADES_STATUS,
    CREATE_IDX_CALIBRATION_TRADE,
    CREATE_IDX_CALIBRATION_TICKER,
    CREATE_IDX_MARKETS_CATEGORY,
    CREATE_IDX_MARKETS_STATUS,
    CREATE_IDX_EDGE_METRICS_DATE,
    CREATE_IDX_API_COSTS_CALLED_AT,
    CREATE_IDX_API_COSTS_MODEL,
    CREATE_IDX_ALERTS_CREATED_AT,
    CREATE_IDX_ALERTS_LEVEL,
    CREATE_IDX_ALERTS_RESOLVED,
]
