# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SQLite trade log and P&L tracker for Brain Hourly BTC.

Stores every prediction, trade, and resolution in a local SQLite database.

Usage:
    uv run trade_log.py stats --db trades.db
    uv run trade_log.py history --db trades.db --limit 20
    uv run trade_log.py pending --db trades.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    btc_price REAL NOT NULL,
    brain_direction TEXT NOT NULL,
    brain_confidence REAL NOT NULL,
    brain_reasoning TEXT,
    market_slug TEXT,
    outcome_bought TEXT,
    shares REAL,
    price REAL,
    order_id TEXT,
    status TEXT DEFAULT 'pending',
    resolved_at TEXT,
    pnl REAL,
    notes TEXT,
    commit_hash TEXT
)
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent live migrations for existing databases.

    Each daemon container has a persistent SQLite at /data/state/trades.db,
    so we cannot drop/recreate. Add new columns here as the schema evolves.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    if "commit_hash" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN commit_hash TEXT")
        conn.commit()


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the database and return a connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_TABLE)
    conn.commit()
    _ensure_schema(conn)
    return conn


def record_prediction(
    conn: sqlite3.Connection,
    btc_price: float,
    direction: str,
    confidence: float,
    reasoning: str | None = None,
    market_slug: str | None = None,
    outcome_bought: str | None = None,
    shares: float | None = None,
    price: float | None = None,
    order_id: str | None = None,
    status: str = "pending",
    notes: str | None = None,
    commit_hash: str | None = None,
) -> int:
    """Record a prediction and optional trade. Returns the trade ID."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO trades
        (timestamp, btc_price, brain_direction, brain_confidence, brain_reasoning,
         market_slug, outcome_bought, shares, price, order_id, status, notes, commit_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, btc_price, direction, confidence, reasoning,
         market_slug, outcome_bought, shares, price, order_id, status, notes, commit_hash),
    )
    conn.commit()
    return cursor.lastrowid


def record_skip(
    conn: sqlite3.Connection,
    btc_price: float,
    direction: str,
    confidence: float,
    reasoning: str | None = None,
    notes: str | None = None,
) -> int:
    """Record a prediction that was skipped (no trade placed)."""
    return record_prediction(
        conn, btc_price, direction, confidence, reasoning,
        status="skipped", notes=notes,
    )


def resolve_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    won: bool,
    pnl: float,
    notes: str | None = None,
    settled: bool = False,
) -> None:
    """Resolve a trade.

    Status lifecycle: pending → settled_won/settled_lost → won/lost

    Args:
        settled: If True, mark as settled_won/settled_lost (outcome known,
                 cash not yet collected). If False, mark as won/lost (final).
    """
    now = datetime.now(timezone.utc).isoformat()
    if settled:
        status = "settled_won" if won else "settled_lost"
    else:
        status = "won" if won else "lost"
    conn.execute(
        """UPDATE trades SET status = ?, resolved_at = ?, pnl = ?, notes = ?
        WHERE id = ?""",
        (status, now, pnl, notes, trade_id),
    )
    conn.commit()


def get_pending_trades(conn: sqlite3.Connection) -> list[dict]:
    """Get all trades with status 'pending'."""
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_rolling_results(
    conn: sqlite3.Connection,
    window: int = 50,
    commit_hash: str | None = None,
) -> list[bool]:
    """Get the most recent `window` resolved results as booleans.

    Includes both final (won/lost) and settled (settled_won/settled_lost)
    statuses — the outcome is known either way.

    If `commit_hash` is provided, only trades tagged with that strategy
    commit are counted (regime-scoped rolling window).
    """
    if commit_hash:
        rows = conn.execute(
            """SELECT status FROM trades
            WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')
              AND commit_hash = ?
            ORDER BY id DESC LIMIT ?""",
            (commit_hash, window),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT status FROM trades
            WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')
            ORDER BY id DESC LIMIT ?""",
            (window,),
        ).fetchall()
    # Reverse to oldest-first order
    return [r["status"] in ("won", "settled_won") for r in reversed(rows)]


def get_stats(
    conn: sqlite3.Connection,
    window: int = 50,
    commit_hash: str | None = None,
) -> dict:
    """Get rolling statistics.

    Status lifecycle: pending → settled_won/settled_lost → won/lost
    Both settled and final statuses count toward win rate and P&L.

    If `commit_hash` is provided, the rolling window is scoped to trades
    tagged with that strategy commit. Lifetime counters (total, resolved,
    pnl, etc.) remain global so the operator can see the full picture.
    """
    total = conn.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
    resolved = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status = 'pending'"
    ).fetchone()["n"]
    settled = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status IN ('settled_won', 'settled_lost')"
    ).fetchone()["n"]
    skipped = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status = 'skipped'"
    ).fetchone()["n"]
    wins = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status IN ('won', 'settled_won')"
    ).fetchone()["n"]
    redeemed = conn.execute(
        "SELECT COUNT(*) as n FROM trades WHERE status = 'won'"
    ).fetchone()["n"]
    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE pnl IS NOT NULL"
    ).fetchone()["total"]

    # Rolling win rate (regime-scoped if commit_hash given)
    results = get_rolling_results(conn, window, commit_hash=commit_hash)
    if results:
        rolling_wins = sum(results)
        rolling_rate = rolling_wins / len(results)
    else:
        rolling_wins = 0
        rolling_rate = None

    # Current streak (regime-scoped if commit_hash given so a stale streak
    # from the previous strategy version doesn't pause a fresh regime)
    if commit_hash:
        recent = conn.execute(
            """SELECT status FROM trades
            WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')
              AND commit_hash = ?
            ORDER BY id DESC LIMIT 50""",
            (commit_hash,),
        ).fetchall()
    else:
        recent = conn.execute(
            """SELECT status FROM trades
            WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')
            ORDER BY id DESC LIMIT 50"""
        ).fetchall()
    streak = 0
    streak_type = None
    for r in recent:
        normalized = "won" if r["status"] in ("won", "settled_won") else "lost"
        if streak_type is None:
            streak_type = normalized
            streak = 1
        elif normalized == streak_type:
            streak += 1
        else:
            break

    return {
        "total_trades": total,
        "resolved": resolved,
        "pending": pending,
        "settled_awaiting_redeem": settled,
        "redeemed": redeemed,
        "skipped": skipped,
        "wins": wins,
        "losses": resolved - wins,
        "win_rate": round(wins / resolved, 4) if resolved > 0 else None,
        "rolling_win_rate": round(rolling_rate, 4) if rolling_rate is not None else None,
        "rolling_window": window,
        "rolling_sample_size": len(results),
        "rolling_commit_hash": commit_hash,
        "total_pnl": round(total_pnl, 2),
        "current_streak": streak,
        "current_streak_type": streak_type,
    }


def get_regimes(conn: sqlite3.Connection) -> list[dict]:
    """Group resolved trades by commit_hash and return per-regime stats."""
    rows = conn.execute(
        """SELECT
            COALESCE(commit_hash, '<none>') as commit_hash,
            COUNT(*) as resolved,
            SUM(CASE WHEN status IN ('won', 'settled_won') THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(pnl), 0) as pnl,
            MIN(timestamp) as first_trade,
            MAX(timestamp) as last_trade
        FROM trades
        WHERE status IN ('won', 'lost', 'settled_won', 'settled_lost')
        GROUP BY commit_hash
        ORDER BY first_trade"""
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        resolved = r["resolved"]
        wins = r["wins"]
        out.append({
            "commit_hash": r["commit_hash"],
            "resolved": resolved,
            "wins": wins,
            "losses": resolved - wins,
            "win_rate": round(wins / resolved, 4) if resolved else None,
            "pnl": round(r["pnl"], 2),
            "first_trade": r["first_trade"],
            "last_trade": r["last_trade"],
        })
    return out


def get_history(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict]:
    """Get trade history, most recent first."""
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Trade log manager")
    parser.add_argument("--db", default="trades.db", help="Path to SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stats", help="Show rolling stats")
    p.add_argument("--window", type=int, default=50, help="Rolling window size")

    p = sub.add_parser("history", help="Show trade history")
    p.add_argument("--limit", type=int, default=50, help="Number of trades to show")

    sub.add_parser("pending", help="Show pending trades")

    sub.add_parser("regimes", help="Per-commit-hash trade breakdown")

    sub.add_parser("init", help="Initialize the database")

    args = parser.parse_args()
    conn = init_db(args.db)

    if args.command == "stats":
        result = get_stats(conn, args.window)
        print(json.dumps(result, indent=2))
    elif args.command == "history":
        result = get_history(conn, args.limit)
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "pending":
        result = get_pending_trades(conn)
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "regimes":
        result = get_regimes(conn)
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "init":
        print(json.dumps({"success": True, "db": args.db}))

    conn.close()


if __name__ == "__main__":
    main()
