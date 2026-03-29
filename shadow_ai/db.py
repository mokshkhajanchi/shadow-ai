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

                CREATE TABLE IF NOT EXISTS feedback (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_ts  TEXT NOT NULL,
                    thread_ts   TEXT,
                    channel     TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    reaction    TEXT NOT NULL,
                    score       INTEGER NOT NULL,
                    created_at  TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_unique
                    ON feedback(message_ts, user_id, reaction);
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


# ─── Feedback ─────────────────────────────────────────────────────────────────

def db_save_feedback(
    db_path: str,
    message_ts: str,
    thread_ts: str | None,
    channel: str,
    user_id: str,
    reaction: str,
    score: int,
):
    now = datetime.now().isoformat()
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO feedback (message_ts, thread_ts, channel, user_id, reaction, score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_ts, thread_ts, channel, user_id, reaction, score, now),
            )
            conn.commit()


def db_remove_feedback(db_path: str, message_ts: str, user_id: str, reaction: str):
    with _db_lock:
        with _db_conn(db_path) as conn:
            conn.execute(
                "DELETE FROM feedback WHERE message_ts = ? AND user_id = ? AND reaction = ?",
                (message_ts, user_id, reaction),
            )
            conn.commit()


def db_get_feedback_stats(db_path: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        with _db_conn(db_path) as conn:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END), 0) as total_pos,
                    COALESCE(SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END), 0) as total_neg,
                    COALESCE(SUM(CASE WHEN score > 0 AND created_at >= ? THEN 1 ELSE 0 END), 0) as today_pos,
                    COALESCE(SUM(CASE WHEN score < 0 AND created_at >= ? THEN 1 ELSE 0 END), 0) as today_neg
                FROM feedback
            """, (today, today)).fetchone()
    total = row["total_pos"] + row["total_neg"]
    return {
        "total_positive": row["total_pos"],
        "total_negative": row["total_neg"],
        "today_positive": row["today_pos"],
        "today_negative": row["today_neg"],
        "satisfaction_pct": (row["total_pos"] / total * 100) if total > 0 else 0,
    }


# ─── Feedback Analysis ────────────────────────────────────────────────────────

def db_get_feedback_messages(db_path: str, score_filter: int = -1, limit: int = 20) -> list[dict]:
    """Get messages that received specific feedback (default: negative) with thread context.

    Returns list of dicts with: thread_ts, reaction, score, user_question, bot_response
    """
    with _db_lock:
        with _db_conn(db_path) as conn:
            # Get feedback entries matching score filter
            op = "<" if score_filter < 0 else ">"
            rows = conn.execute(f"""
                SELECT f.thread_ts, f.message_ts, f.reaction, f.score, f.created_at
                FROM feedback f
                WHERE f.score {op} 0
                ORDER BY f.created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

    results = []
    for row in rows:
        thread_ts = row["thread_ts"]
        if not thread_ts:
            continue
        # Get the conversation for this thread
        messages = db_get_thread_messages(db_path, thread_ts, limit=10)
        if not messages:
            continue
        # Find the user question and bot response pair
        user_q = ""
        bot_r = ""
        for i, msg in enumerate(messages):
            if msg["role"] == "assistant":
                bot_r = msg["content"][:500]  # Cap at 500 chars
                # Look for preceding user message
                if i > 0 and messages[i - 1]["role"] == "user":
                    user_q = messages[i - 1]["content"][:500]
                break
        results.append({
            "thread_ts": thread_ts,
            "reaction": row["reaction"],
            "score": row["score"],
            "user_question": user_q,
            "bot_response": bot_r,
            "created_at": row["created_at"],
        })
    return results


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
