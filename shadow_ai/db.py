"""
SQLite database layer — schema, CRUD, and query helpers.
Extracted from bot.py lines 382-708.

Every public function accepts ``db_path`` as its first parameter so the
module carries no global state beyond the serialisation lock.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("slack-claude-code")

_db_lock = threading.Lock()


# ─── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def _db_conn(db_path: str):
    """Context manager that yields a WAL-mode SQLite connection and closes it on exit."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ─── Schema / init ────────────────────────────────────────────────────────────

def init_db(db_path: str):
    """Create all tables and indexes if they don't exist. Safe to call repeatedly."""
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    thread_ts     TEXT PRIMARY KEY,
                    channel       TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'active',
                    last_slack_ts TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_ts   TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    user_id     TEXT,
                    content     TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (thread_ts) REFERENCES threads(thread_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_thread
                    ON messages(thread_ts, id);

                CREATE TABLE IF NOT EXISTS usage (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_ts       TEXT NOT NULL,
                    session_id      TEXT,
                    cost_usd        REAL,
                    duration_ms     INTEGER,
                    duration_api_ms INTEGER,
                    num_turns       INTEGER,
                    is_error        INTEGER DEFAULT 0,
                    stop_reason     TEXT,
                    usage_json      TEXT,
                    created_at      TEXT NOT NULL,
                    FOREIGN KEY (thread_ts) REFERENCES threads(thread_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_usage_created
                    ON usage(created_at);

                CREATE TABLE IF NOT EXISTS monitored_channels (
                    channel_id  TEXT PRIMARY KEY,
                    added_by    TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );

            """)
            # Migrate: add last_slack_ts if missing (existing DBs)
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN last_slack_ts TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()
    logger.info(f"Database initialized at {db_path}")


# ─── Thread CRUD ──────────────────────────────────────────────────────────────

def db_create_thread(db_path: str, thread_ts: str, channel: str):
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                """INSERT INTO threads (thread_ts, channel, status, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?)
                   ON CONFLICT(thread_ts) DO UPDATE SET
                       status = 'active', updated_at = ?""",
                (thread_ts, channel, now, now, now),
            )
            conn.commit()


def db_is_active_thread(db_path: str, thread_ts: str) -> bool:
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
    return row is not None and row["status"] == "active"


def db_stop_thread(db_path: str, thread_ts: str):
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "UPDATE threads SET status = 'stopped', updated_at = ? WHERE thread_ts = ?",
                (now, thread_ts),
            )
            conn.commit()


def db_get_active_thread_count(db_path: str) -> int:
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM threads WHERE status = 'active'"
            ).fetchone()
    return row["cnt"] if row else 0


def db_get_thread_channel(db_path: str, thread_ts: str) -> str | None:
    """Get the channel for a thread."""
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT channel FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
    return row["channel"] if row else None


def db_get_last_slack_ts(db_path: str, thread_ts: str) -> str | None:
    """Get the last Slack message timestamp we've already sent as context."""
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT last_slack_ts FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
    return row["last_slack_ts"] if row and row["last_slack_ts"] else None


def db_set_last_slack_ts(db_path: str, thread_ts: str, slack_ts: str):
    """Update the last Slack message timestamp we've processed."""
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "UPDATE threads SET last_slack_ts = ?, updated_at = ? WHERE thread_ts = ?",
                (slack_ts, now, thread_ts),
            )
            conn.commit()


# ─── Messages ─────────────────────────────────────────────────────────────────

def db_save_message(db_path: str, thread_ts: str, role: str, content: str, user_id: str = None):
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO messages (thread_ts, role, user_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (thread_ts, role, user_id, content, now),
            )
            conn.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_ts = ?", (now, thread_ts)
            )
            conn.commit()


def db_get_thread_messages(db_path: str, thread_ts: str, limit: int = 0) -> list[dict]:
    with _db_lock:
        with _db_conn(db_path) as conn:
            if limit > 0:
                rows = conn.execute(
                    "SELECT role, user_id, content, created_at FROM messages WHERE thread_ts = ? ORDER BY id ASC LIMIT ?",
                    (thread_ts, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT role, user_id, content, created_at FROM messages WHERE thread_ts = ? ORDER BY id ASC",
                    (thread_ts,),
                ).fetchall()
    return [dict(r) for r in rows]


# ─── Usage / cost tracking ────────────────────────────────────────────────────

def db_save_usage(db_path: str, thread_ts: str, cost_info: dict):
    """Save usage/cost data from a Claude Code response."""
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                """INSERT INTO usage (thread_ts, session_id, cost_usd, duration_ms,
                   duration_api_ms, num_turns, is_error, stop_reason, usage_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    thread_ts,
                    cost_info.get("session_id"),
                    cost_info.get("cost_usd"),
                    cost_info.get("duration_ms"),
                    cost_info.get("duration_api_ms"),
                    cost_info.get("num_turns"),
                    1 if cost_info.get("is_error") else 0,
                    cost_info.get("stop_reason"),
                    json.dumps(cost_info.get("usage"), default=str) if cost_info.get("usage") else None,
                    now,
                ),
            )
            conn.commit()


def db_get_daily_cost(db_path: str) -> float:
    """Get total cost for today (local timezone)."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage WHERE created_at >= ?",
                (today,),
            ).fetchone()
    return row["total"] if row else 0.0


def db_get_total_cost(db_path: str) -> float:
    """Get all-time total cost."""
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage"
            ).fetchone()
    return row["total"] if row else 0.0


# ─── Reporting ────────────────────────────────────────────────────────────────

def db_get_recent_threads(db_path: str, limit: int = 10) -> list[dict]:
    """Get recent threads with aggregated cost data."""
    with _db_lock:
        with _db_conn(db_path) as conn:
            rows = conn.execute("""
                SELECT t.thread_ts, t.channel, t.status, t.created_at, t.updated_at,
                       COALESCE(SUM(u.cost_usd), 0) as total_cost,
                       COUNT(u.id) as query_count
                FROM threads t
                LEFT JOIN usage u ON t.thread_ts = u.thread_ts
                GROUP BY t.thread_ts
                ORDER BY t.updated_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ─── Monitored Channels ──────────────────────────────────────────────────────

def db_add_monitored_channel(db_path: str, channel_id: str, user_id: str):
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO monitored_channels (channel_id, added_by, created_at) VALUES (?, ?, ?)",
                (channel_id, user_id, now),
            )
            conn.commit()


def db_remove_monitored_channel(db_path: str, channel_id: str):
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute("DELETE FROM monitored_channels WHERE channel_id = ?", (channel_id,))
            conn.commit()


def db_get_monitored_channels(db_path: str) -> list[str]:
    with _db_lock:
        with _db_conn(db_path) as conn:
            rows = conn.execute("SELECT channel_id FROM monitored_channels").fetchall()
    return [r["channel_id"] for r in rows]


def db_is_monitored_channel(db_path: str, channel_id: str) -> bool:
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM monitored_channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
    return row is not None
