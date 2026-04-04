"""
Async SQLite connection management for PolyEdge v5.

Usage
-----
# One-off query:
    async with get_connection() as db:
        row = await db.execute_fetchone("SELECT * FROM trades WHERE id = ?", (1,))

# Explicit transaction (writes):
    async with get_connection() as db:
        async with db.execute("INSERT INTO alerts ...") as cur:
            await db.commit()

# Schema initialisation (run once at startup):
    await init_db()

CLI:
    python -m persistence.database --init
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from persistence.models import ALL_INDEXES, ALL_TABLES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# data_store/ lives at the project root, two levels above this file.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH: Path = _PROJECT_ROOT / "data_store" / "polyedge.db"


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------
@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager that yields a configured aiosqlite connection.

    Each call opens a fresh connection and closes it on exit.  WAL mode and
    foreign key enforcement are applied once per connection.

    Example::

        async with get_connection() as db:
            await db.execute("INSERT INTO alerts ...")
            await db.commit()
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # WAL mode: readers never block writers; writers never block readers.
        await db.execute("PRAGMA journal_mode = WAL;")
        # Enforce FK constraints — SQLite disables them by default.
        await db.execute("PRAGMA foreign_keys = ON;")
        # Reasonable cache size (4 MB) to reduce disk I/O.
        await db.execute("PRAGMA cache_size = -4096;")

        try:
            yield db
        except Exception:
            await db.rollback()
            raise


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create all tables and indexes, then verify every table exists.

    Safe to call on an already-initialised database — all statements use
    IF NOT EXISTS.  Raises RuntimeError if any table is missing after creation.
    """
    logger.info("Initialising database at %s", DB_PATH)

    async with get_connection() as db:
        # Create tables inside a single transaction.
        async with db.execute("BEGIN"):
            pass  # aiosqlite auto-begins; explicit BEGIN not needed here.

        for table_name, ddl in ALL_TABLES:
            await db.execute(ddl)
            logger.debug("Ensured table: %s", table_name)

        for idx_ddl in ALL_INDEXES:
            await db.execute(idx_ddl)

        await db.commit()
        logger.info("Schema applied — %d tables, %d indexes", len(ALL_TABLES), len(ALL_INDEXES))

        # ----------------------------------------------------------------
        # Verification — confirm every expected table is present.
        # ----------------------------------------------------------------
        missing = await _verify_tables(db)
        if missing:
            raise RuntimeError(
                f"Database initialisation failed. Missing tables: {missing}"
            )

    logger.info("Database ready.")


async def _verify_tables(db: aiosqlite.Connection) -> list[str]:
    """Return names of expected tables that are absent from sqlite_master."""
    expected = {name for name, _ in ALL_TABLES}

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
    )
    rows = await cursor.fetchall()
    present = {row["name"] for row in rows}

    return sorted(expected - present)


# ---------------------------------------------------------------------------
# Retention / cleanup helpers
# ---------------------------------------------------------------------------
async def prune_old_rows() -> dict[str, int]:
    """Delete rows that exceed the retention window for time-limited tables.

    Retention:
        edge_metrics — 180 days
        api_costs    — 90 days
        alerts       — 90 days

    Returns a dict mapping table name → rows deleted.
    """
    deleted: dict[str, int] = {}

    async with get_connection() as db:
        queries = [
            (
                "edge_metrics",
                "DELETE FROM edge_metrics WHERE snapshot_date < date('now', '-180 days');",
            ),
            (
                "api_costs",
                "DELETE FROM api_costs WHERE called_at < datetime('now', '-90 days');",
            ),
            (
                "alerts",
                "DELETE FROM alerts WHERE created_at < datetime('now', '-90 days');",
            ),
        ]

        for table, sql in queries:
            cursor = await db.execute(sql)
            deleted[table] = cursor.rowcount
            if cursor.rowcount:
                logger.info("Pruned %d rows from %s", cursor.rowcount, table)

        await db.commit()

    return deleted


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------
async def table_row_counts() -> dict[str, int]:
    """Return a {table_name: row_count} dict for all six tables."""
    counts: dict[str, int] = {}
    async with get_connection() as db:
        for table_name, _ in ALL_TABLES:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name};")  # noqa: S608
            row = await cursor.fetchone()
            counts[table_name] = row[0] if row else 0
    return counts


# ---------------------------------------------------------------------------
# CLI entrypoint:  python -m persistence.database --init
# ---------------------------------------------------------------------------
async def _cli() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    if "--init" in sys.argv:
        await init_db()
        counts = await table_row_counts()
        print("\nTable row counts after initialisation:")
        for table, count in counts.items():
            print(f"  {table:<16} {count}")
    else:
        print("Usage: python -m persistence.database --init")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_cli())
