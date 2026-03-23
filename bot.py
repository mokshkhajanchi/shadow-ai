"""
Slack Bot → Claude Code (local Mac)

Features:
- ClaudeSDKClient for persistent multi-turn sessions per Slack thread
- SQLite stores every message (user + assistant) — survives restarts
- On restart, conversation history is replayed to restore context
- Stop Session button on every reply
- CONCURRENT: multiple threads handled in parallel via thread pool
- Per-thread locking prevents duplicate processing of the same thread
- 10-minute timeout per request-response cycle
"""

import os
import re
import json
import asyncio
import base64
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import threading
import traceback
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

from dotenv import load_dotenv
load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

# ─── Configuration ───────────────────────────────────────────────────────────

ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "").split(",")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
CLAUDE_WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", os.path.expanduser("~/Projects"))
MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "30"))
ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_ALLOWED_TOOLS",
    "Read,Write,Edit,Bash,Glob,Grep,Agent"
).split(",")
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
DB_PATH = os.environ.get("DB_PATH", "./slack_claude_bot.db")
LOG_FILE = os.environ.get("LOG_FILE", "./bot.log")

# Timeout for a single request → Claude Code → response cycle
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "600"))  # 10 minutes

# Max concurrent Claude Code sessions
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))

# Knowledge base — comma-separated paths to folders/files
KNOWLEDGE_PATHS = [p.strip() for p in os.environ.get("KNOWLEDGE_PATHS", "").split(",") if p.strip()]
KNOWLEDGE_INLINE_THRESHOLD = 10_000   # 10KB per file — load inline
KNOWLEDGE_TOTAL_INLINE_LIMIT = 20_000  # 20KB total inline budget
KNOWLEDGE_INDEX_MAX_ENTRIES = 100  # Max files in the index table

# Daily budget limit (0 = unlimited)
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "0"))

# Model selection
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", None)
MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# Thinking mode
CLAUDE_THINKING = os.environ.get("CLAUDE_THINKING", "off")  # off | adaptive | enabled
CLAUDE_THINKING_BUDGET = int(os.environ.get("CLAUDE_THINKING_BUDGET", "10000"))

# File upload limits
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# Reaction feedback
FEEDBACK_REACTIONS = {"+1": +1, "-1": -1, "tada": +1, "confused": -1}

# Codebase indexing (runs on KNOWLEDGE_PATHS)
CODEBASE_INDEX_MAX_SIZE = int(os.environ.get("CODEBASE_INDEX_MAX_SIZE", "50000"))
CODEBASE_INDEX_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx"}

_PY_PATTERNS = [
    re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\((.*)$"),
    re.compile(r"^\s*class\s+(\w+)(?:\s*\(.*\))?\s*:"),
]
_JS_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\("),
]
CODEBASE_PATTERNS = {".py": _PY_PATTERNS, ".js": _JS_PATTERNS, ".ts": _JS_PATTERNS, ".jsx": _JS_PATTERNS, ".tsx": _JS_PATTERNS}

INDEXABLE_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts"}
SKIP_DIRS = {"backup", ".git", "__pycache__", "node_modules", ".venv", "docker", "dist", "build", ".next", "coverage", ".tox"}


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _get_file_description(filepath: Path) -> str:
    """Extract first heading or first non-empty line as description."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    line = line.lstrip("#").strip()
                return line[:120] + ("..." if len(line) > 120 else "")
        return "(empty file)"
    except Exception:
        return "(unreadable)"


def _build_knowledge_index(paths: list[str]) -> tuple[str, str, list[str]]:
    """
    Scan knowledge paths and build:
    - index_text: compact file index for the system prompt
    - inline_text: content of small files loaded inline
    - dirs: list of directory paths to add to add_dirs
    """
    all_files = []  # (absolute_path, display_path, size_bytes)
    dirs = []

    for raw_path in paths:
        p = Path(raw_path).expanduser().resolve()
        if not p.exists():
            logger.warning(f"Knowledge path not found: {raw_path}")
            continue

        if p.is_file():
            if p.suffix.lower() in INDEXABLE_EXTENSIONS:
                all_files.append((p, p.name, p.stat().st_size))
            dirs.append(str(p.parent))
        elif p.is_dir():
            dirs.append(str(p))
            for fp in sorted(p.rglob("*")):
                if not fp.is_file():
                    continue
                if fp.suffix.lower() not in INDEXABLE_EXTENSIONS:
                    continue
                if any(part in SKIP_DIRS for part in fp.parts):
                    continue
                all_files.append((fp, str(fp.relative_to(p)), fp.stat().st_size))

    if not all_files:
        return "", "", dirs

    inline_parts = []
    index_entries = []
    total_inline = 0

    for abs_path, display_path, size in all_files:
        # Only inline doc files from knowledge dirs, not entire codebases
        is_doc = abs_path.suffix.lower() in {".md", ".txt", ".rst"}
        if is_doc and size <= KNOWLEDGE_INLINE_THRESHOLD and (total_inline + size) <= KNOWLEDGE_TOTAL_INLINE_LIMIT:
            try:
                content = abs_path.read_text(encoding="utf-8", errors="ignore").strip()
                inline_parts.append(f"### {display_path}\n{content}\n")
                total_inline += size
            except Exception:
                if len(index_entries) < KNOWLEDGE_INDEX_MAX_ENTRIES:
                    desc = _get_file_description(abs_path)
                    index_entries.append((display_path, _human_size(size), desc, str(abs_path)))
        else:
            if len(index_entries) < KNOWLEDGE_INDEX_MAX_ENTRIES:
                desc = _get_file_description(abs_path)
                index_entries.append((display_path, _human_size(size), desc, str(abs_path)))

    index_text = ""
    if index_entries:
        lines = [
            "| File | Size | Description | Path |",
            "|------|------|-------------|------|",
        ]
        for display, size, desc, abspath in index_entries:
            lines.append(f"| {display} | {size} | {desc} | {abspath} |")
        index_text = "\n".join(lines)

    inline_text = "\n".join(inline_parts) if inline_parts else ""
    dirs = list(dict.fromkeys(dirs))  # deduplicate, preserve order

    return index_text, inline_text, dirs


# Populated at startup
KNOWLEDGE_INDEX = ""
KNOWLEDGE_INLINE = ""
KNOWLEDGE_DIRS: list[str] = []
CODEBASE_INDEX = ""
KNOWLEDGE_INDEX_FILE = ""  # Path to the saved index file on disk


DOC_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".rst"}
ALL_INDEX_EXTENSIONS = CODEBASE_INDEX_EXTENSIONS | DOC_EXTENSIONS


def _extract_doc_outline(filepath: Path, max_headings: int = 15) -> list[str]:
    """Extract headings and key structure from a document file."""
    headings = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip()
                if not line:
                    continue
                # Markdown headings
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                    if heading:
                        depth = len(line) - len(line.lstrip("#"))
                        indent = "  " * min(depth - 1, 3)
                        headings.append(f"{indent}{heading}")
                # YAML top-level keys
                elif filepath.suffix.lower() in (".yaml", ".yml") and re.match(r'^[a-zA-Z_]\w*:', line):
                    headings.append(f"  {line.split(':')[0]}")
                # JSON top-level structure hint (first few keys)
                elif filepath.suffix.lower() == ".json" and re.match(r'^\s{2}"[a-zA-Z_]\w*":', line) and len(headings) < 10:
                    key = line.strip().split('"')[1]
                    headings.append(f"  {key}")

                if len(headings) >= max_headings:
                    break
    except Exception:
        pass
    return headings


def _build_codebase_index(paths: list[str], max_size: int = 50000) -> str:
    """Scan directories and extract code signatures + document outlines for the system prompt."""
    if not paths:
        return ""

    file_entries = {}
    total_chars = 0
    truncated = False

    for raw_path in paths:
        root = Path(raw_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            logger.warning(f"[CODEBASE INDEX] Path not found: {raw_path}")
            continue

        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            ext = fp.suffix.lower()
            if ext not in ALL_INDEX_EXTENSIONS:
                continue
            if any(part in SKIP_DIRS for part in fp.parts):
                continue

            rel_path = str(fp.relative_to(root))
            lines_out = []

            if ext in CODEBASE_INDEX_EXTENSIONS:
                # Code file — extract signatures
                patterns = CODEBASE_PATTERNS.get(ext)
                if not patterns:
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line_stripped = line.rstrip()
                            for pattern in patterns:
                                if pattern.match(line_stripped):
                                    sig = line_stripped.strip().rstrip(":{").rstrip()
                                    if len(sig) > 120:
                                        sig = sig[:117] + "..."
                                    lines_out.append(f"  {sig}")
                                    break
                except Exception:
                    continue
            elif ext in DOC_EXTENSIONS:
                # Document file — extract outline
                lines_out = _extract_doc_outline(fp)

            if lines_out:
                entry = f"## {rel_path}\n" + "\n".join(lines_out) + "\n"
                if total_chars + len(entry) > max_size:
                    truncated = True
                    break
                file_entries[rel_path] = entry
                total_chars += len(entry)

        if truncated:
            break

    if not file_entries:
        return ""

    return (
        "--- CODEBASE & DOCS INDEX ---\n"
        "Pre-indexed view of code and documents. Use this to find relevant files, "
        "classes, functions, and topics without Glob/Grep. Use Read for full details.\n\n"
        + "\n".join(file_entries.values())
        + "\n--- END CODEBASE & DOCS INDEX ---\n"
    )

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger("slack-claude-code")

# ─── Slack App ───────────────────────────────────────────────────────────────

app = App(token=SLACK_BOT_TOKEN)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# ─── Thread Pool for Concurrent Requests ─────────────────────────────────────

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="claude")

# Per-thread locks — prevents the same Slack thread from being processed twice
# simultaneously (e.g., user sends two messages quickly)
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_lock = threading.Lock()


def get_thread_lock(thread_ts: str) -> threading.Lock:
    """Get or create a lock for a specific Slack thread."""
    with _thread_locks_lock:
        if thread_ts not in _thread_locks:
            _thread_locks[thread_ts] = threading.Lock()
        return _thread_locks[thread_ts]


def remove_thread_lock(thread_ts: str):
    """Clean up a thread lock."""
    with _thread_locks_lock:
        _thread_locks.pop(thread_ts, None)


# ─── SQLite Database ─────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_lock:
        conn = _get_db()
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
        conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


def db_create_thread(thread_ts: str, channel: str):
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                """INSERT INTO threads (thread_ts, channel, status, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?)
                   ON CONFLICT(thread_ts) DO UPDATE SET
                       status = 'active', updated_at = ?""",
                (thread_ts, channel, now, now, now),
            )
            conn.commit()
        finally:
            conn.close()


def db_save_message(thread_ts: str, role: str, content: str, user_id: str = None):
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO messages (thread_ts, role, user_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (thread_ts, role, user_id, content, now),
            )
            conn.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_ts = ?", (now, thread_ts)
            )
            conn.commit()
        finally:
            conn.close()


def db_get_thread_messages(thread_ts: str) -> list[dict]:
    with _db_lock:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT role, user_id, content, created_at FROM messages WHERE thread_ts = ? ORDER BY id ASC",
                (thread_ts,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def db_is_active_thread(thread_ts: str) -> bool:
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT status FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
        finally:
            conn.close()
    return row is not None and row["status"] == "active"


def db_stop_thread(thread_ts: str):
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE threads SET status = 'stopped', updated_at = ? WHERE thread_ts = ?",
                (now, thread_ts),
            )
            conn.commit()
        finally:
            conn.close()


def db_get_active_thread_count() -> int:
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM threads WHERE status = 'active'").fetchone()
        finally:
            conn.close()
    return row["cnt"] if row else 0


def db_get_thread_channel(thread_ts: str) -> str | None:
    """Get the channel for a thread."""
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT channel FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
        finally:
            conn.close()
    return row["channel"] if row else None


def db_get_last_slack_ts(thread_ts: str) -> str | None:
    """Get the last Slack message timestamp we've already sent as context."""
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT last_slack_ts FROM threads WHERE thread_ts = ?", (thread_ts,)
            ).fetchone()
        finally:
            conn.close()
    return row["last_slack_ts"] if row and row["last_slack_ts"] else None


def db_set_last_slack_ts(thread_ts: str, slack_ts: str):
    """Update the last Slack message timestamp we've processed."""
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                "UPDATE threads SET last_slack_ts = ?, updated_at = ? WHERE thread_ts = ?",
                (slack_ts, now, thread_ts),
            )
            conn.commit()
        finally:
            conn.close()


def db_save_usage(thread_ts: str, cost_info: dict):
    """Save usage/cost data from a Claude Code response."""
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
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
        finally:
            conn.close()


def db_get_daily_cost() -> float:
    """Get total cost for today (local timezone)."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage WHERE created_at >= ?",
                (today,),
            ).fetchone()
        finally:
            conn.close()
    return row["total"] if row else 0.0


def db_get_total_cost() -> float:
    """Get all-time total cost."""
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage").fetchone()
        finally:
            conn.close()
    return row["total"] if row else 0.0


def db_save_feedback(message_ts: str, thread_ts: str | None, channel: str, user_id: str, reaction: str, score: int):
    now = datetime.now().isoformat()
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO feedback (message_ts, thread_ts, channel, user_id, reaction, score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_ts, thread_ts, channel, user_id, reaction, score, now),
            )
            conn.commit()
        finally:
            conn.close()


def db_remove_feedback(message_ts: str, user_id: str, reaction: str):
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute(
                "DELETE FROM feedback WHERE message_ts = ? AND user_id = ? AND reaction = ?",
                (message_ts, user_id, reaction),
            )
            conn.commit()
        finally:
            conn.close()


def db_get_feedback_stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END), 0) as total_pos,
                    COALESCE(SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END), 0) as total_neg,
                    COALESCE(SUM(CASE WHEN score > 0 AND created_at >= ? THEN 1 ELSE 0 END), 0) as today_pos,
                    COALESCE(SUM(CASE WHEN score < 0 AND created_at >= ? THEN 1 ELSE 0 END), 0) as today_neg
                FROM feedback
            """, (today, today)).fetchone()
        finally:
            conn.close()
    total = row["total_pos"] + row["total_neg"]
    return {
        "total_positive": row["total_pos"],
        "total_negative": row["total_neg"],
        "today_positive": row["today_pos"],
        "today_negative": row["today_neg"],
        "satisfaction_pct": (row["total_pos"] / total * 100) if total > 0 else 0,
    }


def db_get_recent_threads(limit: int = 10) -> list[dict]:
    """Get recent threads with aggregated cost data."""
    with _db_lock:
        conn = _get_db()
        try:
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
        finally:
            conn.close()
    return [dict(r) for r in rows]


# ─── Session Manager (in-memory) ────────────────────────────────────────────

active_sessions: dict[str, dict] = {}
session_lock = threading.Lock()


def get_session(thread_ts: str) -> dict | None:
    with session_lock:
        return active_sessions.get(thread_ts)


def _evict_oldest_session():
    """Evict the least recently active session to make room for a new one."""
    with session_lock:
        if not active_sessions:
            return
        # Only evict sessions that aren't currently processing
        eligible = {ts: s for ts, s in active_sessions.items() if not s.get("processing")}
        if not eligible:
            return
        oldest_ts = min(eligible, key=lambda ts: eligible[ts].get("last_activity", ""))
    logger.info(f"[EVICT] Evicting oldest session {oldest_ts} to free memory")
    remove_session(oldest_ts)
    db_stop_thread(oldest_ts)


def store_session(thread_ts: str, sdk_client, loop):
    # Evict oldest if at capacity
    if get_active_session_count() >= MAX_ACTIVE_SESSIONS:
        _evict_oldest_session()

    with session_lock:
        active_sessions[thread_ts] = {
            "client": sdk_client,
            "loop": loop,
            "created_at": datetime.now().isoformat(),
            "last_activity": datetime.now().isoformat(),
        }
    logger.info(f"[SESSION] Stored session for thread {thread_ts} (total: {len(active_sessions)})")


def touch_session(thread_ts: str):
    """Update last_activity timestamp for a session."""
    with session_lock:
        if thread_ts in active_sessions:
            active_sessions[thread_ts]["last_activity"] = datetime.now().isoformat()


def mark_session_processing(thread_ts: str, processing: bool):
    """Mark a session as currently processing (prevents cleanup from killing it)."""
    with session_lock:
        if thread_ts in active_sessions:
            active_sessions[thread_ts]["processing"] = processing


def remove_session(thread_ts: str):
    with session_lock:
        session = active_sessions.pop(thread_ts, None)
    if session:
        logger.info(f"[SESSION] Removed session for thread {thread_ts} (remaining: {len(active_sessions)})")
        try:
            loop = session.get("loop")
            sdk_client = session.get("client")
            if loop and sdk_client and loop.is_running():
                asyncio.run_coroutine_threadsafe(sdk_client.disconnect(), loop)
        except Exception as e:
            logger.warning(f"Error disconnecting {thread_ts}: {type(e).__name__}: {e}\n{traceback.format_exc()}")


def get_active_session_count() -> int:
    with session_lock:
        return len(active_sessions)


def kill_all_sessions() -> int:
    """Kill all active Claude Code sessions. Returns the number of sessions killed."""
    with session_lock:
        sessions_to_kill = dict(active_sessions)
        active_sessions.clear()

    count = len(sessions_to_kill)
    logger.info(f"[KILL ALL] Killing {count} active sessions")

    for thread_ts, session in sessions_to_kill.items():
        try:
            loop = session.get("loop")
            sdk_client = session.get("client")
            if loop and sdk_client and loop.is_running():
                asyncio.run_coroutine_threadsafe(sdk_client.disconnect(), loop)
        except Exception as e:
            logger.warning(f"[KILL ALL] Error disconnecting {thread_ts}: {type(e).__name__}: {e}")

        db_stop_thread(thread_ts)
        remove_thread_lock(thread_ts)

    logger.info(f"[KILL ALL] Done. Killed {count} sessions.")
    return count


SESSION_IDLE_TIMEOUT = int(os.environ.get("SESSION_IDLE_TIMEOUT", "600"))  # 10 min default
MAX_ACTIVE_SESSIONS = int(os.environ.get("MAX_ACTIVE_SESSIONS", "3"))
DETAILS_TTL = 86400  # 24 hours


def _cleanup_idle_resources():
    """Background cleanup: idle sessions, stale thread locks, expired details."""
    while True:
        time.sleep(300)  # Run every 5 minutes
        now = datetime.now()
        try:
            # 1. Kill idle sessions (skip ones currently processing a request)
            with session_lock:
                idle_threads = []
                for ts, session in active_sessions.items():
                    if session.get("processing"):
                        continue  # Don't kill sessions mid-request
                    created = session.get("last_activity") or session.get("created_at", "")
                    try:
                        session_time = datetime.fromisoformat(created)
                        if (now - session_time).total_seconds() > SESSION_IDLE_TIMEOUT:
                            idle_threads.append(ts)
                    except (ValueError, TypeError):
                        pass

            for ts in idle_threads:
                logger.info(f"[CLEANUP] Killing idle session: {ts}")
                remove_session(ts)
                db_stop_thread(ts)
                remove_thread_lock(ts)

            if idle_threads:
                logger.info(f"[CLEANUP] Killed {len(idle_threads)} idle sessions")

            # 2. Cleanup orphaned thread locks (no active session and no DB activity)
            with _thread_locks_lock:
                lock_keys = list(_thread_locks.keys())
            stale = 0
            for ts in lock_keys:
                if not get_session(ts) and not db_is_active_thread(ts):
                    remove_thread_lock(ts)
                    stale += 1
            if stale:
                logger.info(f"[CLEANUP] Removed {stale} stale thread locks")

            # 3. Cleanup expired details store entries
            cutoff = time.time() - DETAILS_TTL
            with _details_lock:
                expired = [k for k in _details_store if k.rsplit("_", 1)[-1].isdigit() and int(k.rsplit("_", 1)[-1]) < cutoff]
                for k in expired:
                    del _details_store[k]
            if expired:
                logger.info(f"[CLEANUP] Evicted {len(expired)} expired detail entries")

        except Exception as e:
            logger.warning(f"[CLEANUP] Error: {type(e).__name__}: {e}")


# ─── Bot User ID ─────────────────────────────────────────────────────────────

BOT_USER_ID = None


def get_bot_user_id() -> str:
    return slack_client.auth_test()["user_id"]


# ─── Detail Storage (for Show Details button) ────────────────────────────────

_details_store: dict[str, str] = {}
_details_lock = threading.Lock()


def _store_detail(detail_id: str, content: str):
    with _details_lock:
        _details_store[detail_id] = content


def _pop_detail(detail_id: str) -> str | None:
    with _details_lock:
        return _details_store.pop(detail_id, None)


# ─── Slack Helpers ───────────────────────────────────────────────────────────

def markdown_to_slack(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format."""
    # 1. Protect code blocks from modification
    code_blocks = []

    def _save_code_block(match):
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    # Also protect inline code
    inline_codes = []

    def _save_inline_code(match):
        inline_codes.append(match.group(0))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`\n]+`", _save_inline_code, text)

    # 2. Convert Markdown tables to readable plain text
    def _convert_table(match):
        table_text = match.group(0)
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        rows = []
        for line in lines:
            # Skip separator rows (|---|---|)
            if re.match(r"^\|[\s\-:]+\|$", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
        if not rows:
            return table_text

        # Calculate column widths
        col_count = max(len(r) for r in rows)
        col_widths = [0] * col_count
        for row in rows:
            for i, cell in enumerate(row):
                if i < col_count:
                    col_widths[i] = max(col_widths[i], len(cell))

        # Format: first row as bold header, rest as data
        result_lines = []
        for idx, row in enumerate(rows):
            padded = []
            for i in range(col_count):
                cell = row[i] if i < len(row) else ""
                padded.append(cell.ljust(col_widths[i]))
            line = "  ".join(padded)
            if idx == 0:
                line = f"*{line.strip()}*"
            result_lines.append(line)

        return "\n".join(result_lines)

    text = re.sub(
        r"(?:^[ \t]*\|.+\|[ \t]*\n){2,}",
        _convert_table,
        text,
        flags=re.MULTILINE,
    )

    # 3. Headers → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 4. Bold: **text** or __text__ → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # 5. Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 6. Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # 7. Horizontal rules: --- or *** or ___ → blank line (Slack doesn't render them)
    text = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", text, flags=re.MULTILINE)

    # 8. Collapse multiple consecutive blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 9. Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{i}\x00", code)

    return text


def clean_message_text(text: str, bot_user_id: str) -> str:
    return re.sub(rf"<@{bot_user_id}>", "", text).strip()


def parse_model_prefix(text: str) -> tuple[str | None, str]:
    """Parse optional model prefix: 'opus: review this' -> ('claude-opus-4-6', 'review this')"""
    match = re.match(r'^(opus|haiku|sonnet):\s*', text, re.IGNORECASE)
    if match:
        return MODEL_ALIASES[match.group(1).lower()], text[match.end():]
    return None, text


def chunk_message(text: str, max_length: int = 3900) -> list[str]:
    if len(text) <= max_length:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = max_length
        idx = text.rfind("\n\n", 0, max_length)
        if idx > max_length // 2:
            split_at = idx + 2
        else:
            idx = text.rfind("\n", 0, max_length)
            if idx > max_length // 2:
                split_at = idx + 1
        chunks.append(text[:split_at])
        text = text[split_at:]
    return chunks


def _build_section_blocks(text: str) -> list[dict]:
    """Build Slack section blocks from text, respecting the 3000 char limit."""
    block_chunks = chunk_message(text, max_length=2900)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": bc}}
        for bc in block_chunks
    ]


def send_response_with_stop_button(channel: str, thread_ts: str, text: str):
    text = markdown_to_slack(text)

    # Split into summary + details if the separator exists
    separator = "---DETAILS---"
    summary = text
    details = None
    if separator in text:
        parts = text.split(separator, 1)
        summary = parts[0].strip()
        details = parts[1].strip()

    # Send summary with Stop Session button (and Show Details if applicable)
    blocks = _build_section_blocks(summary)

    action_elements = []
    if details:
        # Store details in DB for retrieval on button click
        detail_id = f"detail_{thread_ts}_{int(time.time())}"
        _store_detail(detail_id, details)
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": ":mag: Show Details", "emoji": True},
            "action_id": "show_details",
            "value": detail_id,
        })

    action_elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": ":octagonal_sign: Stop Session", "emoji": True},
        "style": "danger",
        "action_id": "stop_session",
        "value": thread_ts,
    })

    blocks.append({"type": "actions", "elements": action_elements})

    slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=summary,
        blocks=blocks,
    )


def fetch_thread_messages(channel: str, thread_ts: str, since_ts: str = None) -> tuple[str, str | None]:
    """
    Fetch messages in a Slack thread, optionally only those after since_ts.
    Returns (formatted_context, latest_message_ts).
    """
    try:
        result = slack_client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        messages = result.get("messages", [])
        if not messages:
            return "", None

        latest_ts = None
        formatted = []
        for msg in messages:
            msg_ts = msg.get("ts", "")
            user = msg.get("user", "bot")
            text = msg.get("text", "")
            if not text.strip():
                continue
            # Skip bot messages
            if msg.get("bot_id") or user == BOT_USER_ID:
                continue
            # Skip messages we've already sent as context
            if since_ts and msg_ts <= since_ts:
                continue
            formatted.append(f"<@{user}>: {text}")
            latest_ts = msg_ts

        if not formatted:
            return "", latest_ts

        label = "NEW SLACK THREAD MESSAGES" if since_ts else "SLACK THREAD CONTEXT"
        context = (
            f"--- {label} ---\n"
            + "\n\n".join(formatted)
            + f"\n--- END {label} ---\n\n"
        )
        return context, latest_ts
    except Exception as e:
        logger.warning(f"Failed to fetch thread messages: {type(e).__name__}: {e}")
        return "", None


def send_session_ended(channel: str, thread_ts: str):
    slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":white_check_mark: *Session ended.* Mention me again to start a new one.",
    )


# ─── MCP Server Discovery ────────────────────────────────────────────────────

def _discover_mcp_server_names() -> list[str]:
    """
    Read ~/.claude/settings.json to find configured MCP server names.
    Returns list of server names so we can auto-approve their tools.
    """
    settings_paths = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]

    server_names = []
    for path in settings_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            for name in mcp_servers:
                if name not in server_names:
                    server_names.append(name)
                    logger.info(f"[MCP] Discovered server: {name}")
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")

    # Also check project-level .mcp.json in CLAUDE_WORK_DIR
    mcp_json_path = Path(CLAUDE_WORK_DIR) / ".mcp.json"
    if mcp_json_path.exists():
        try:
            data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            for name in mcp_servers:
                if name not in server_names:
                    server_names.append(name)
                    logger.info(f"[MCP] Discovered server (project): {name}")
        except Exception as e:
            logger.warning(f"Failed to read {mcp_json_path}: {e}")

    return server_names


# Discovered at startup
MCP_SERVER_NAMES: list[str] = []
MCP_TOOL_CATALOG: str = ""  # Populated at startup by _discover_mcp_tools()


async def _discover_mcp_tools() -> str:
    """
    Connect a temporary SDK session, call get_mcp_status(),
    and build a concise tool catalog string from all connected MCP servers.
    Returns empty string if no tools found or on error.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    opts = ClaudeAgentOptions(
        permission_mode=PERMISSION_MODE,
        cwd=CLAUDE_WORK_DIR,
        setting_sources=["user", "project"],
    )
    sdk_client = ClaudeSDKClient(options=opts)
    try:
        await sdk_client.connect()
        # Brief wait for MCP servers to initialize
        await asyncio.sleep(2)
        status = await sdk_client.get_mcp_status()
        await sdk_client.disconnect()
    except Exception as e:
        logger.warning(f"[MCP] Failed to discover tools: {e}")
        return ""

    catalog_lines = []
    for server in status.get("mcpServers", []):
        if server.get("status") != "connected":
            continue
        tools = server.get("tools", [])
        if not tools:
            continue
        catalog_lines.append(f"\n*{server['name']}* ({len(tools)} tools):")
        for tool in tools:
            desc = tool.get("description", "")
            if len(desc) > 120:
                desc = desc[:117] + "..."
            catalog_lines.append(f"  - `{tool['name']}`: {desc}")

    if not catalog_lines:
        return ""

    return (
        "\n--- AVAILABLE MCP TOOLS ---\n"
        "You have the following MCP tools. ALWAYS use these when the user asks "
        "about data from external systems. Never guess or make up data — call the tool.\n"
        + "\n".join(catalog_lines)
        + "\n--- END MCP TOOLS ---\n"
    )


# ─── Claude Code SDK ────────────────────────────────────────────────────────

def _create_options(model: str | None = None, thinking_override: str | None = None):
    from claude_agent_sdk import ClaudeAgentOptions

    # Build allowed_tools: built-in tools + wildcard for every MCP server
    all_allowed = list(ALLOWED_TOOLS)
    for server_name in MCP_SERVER_NAMES:
        wildcard = f"mcp__{server_name}__*"
        if wildcard not in all_allowed:
            all_allowed.append(wildcard)

    opts = ClaudeAgentOptions(
        allowed_tools=all_allowed,
        permission_mode=PERMISSION_MODE,
        cwd=CLAUDE_WORK_DIR,
        max_turns=MAX_TURNS,
        setting_sources=["user", "project"],
    )

    # Model selection: inline override > env var > SDK default
    effective_model = model or CLAUDE_MODEL
    if effective_model:
        opts.model = effective_model

    # Thinking mode: inline override > env var
    thinking_mode = thinking_override or CLAUDE_THINKING
    if thinking_mode == "enabled":
        opts.thinking = {"type": "enabled", "budget_tokens": CLAUDE_THINKING_BUDGET}
    elif thinking_mode == "adaptive":
        opts.thinking = {"type": "adaptive"}

    # Inject system prompt: response style + knowledge base
    system_parts = [
        "\n\n--- RESPONSE GUIDELINES ---\n"
        "You are replying in a Slack thread.\n"
        "- Lead with the answer or action taken, not the process.\n"
        "- Give complete, useful responses. Include all relevant details the user needs.\n"
        "- For data lookups (tickets, PRs, etc.): show the full results with key fields — don't just summarize.\n"
        "- For code changes: state what you changed, the file(s), and the PR link.\n"
        "- IMPORTANT: After making code changes, ALWAYS run the relevant tests before reporting success. "
        f"For avis: `cd {COMMERCE_ROOT}/services/avis && ./run.test.sh test/<relevant_test_dir>/`. "
        f"For brunt: `cd {COMMERCE_ROOT}/tests/brunt && npm run test`. "
        f"For mirage: `cd {COMMERCE_ROOT}/services/mirage && ./run.local.test.sh`. "
        "If tests fail, fix the code and re-run. Only report completion after tests pass. "
        "If no tests exist for the changed code, mention this.\n"
        "- Skip preamble like \"I'd be happy to help\". Answer directly.\n"
        "- Use Slack formatting: *bold*, `code`, bullet points. No markdown headers (# ##).\n"
        "- When the user asks for data from external systems, "
        "ALWAYS use your MCP tools to fetch real data. Never guess — call the tool.\n"
        "- When using Azure DevOps MCP tools (mcp__azure-devops__*), ALWAYS include "
        "project='FyndPlatformCore'. When creating PRs, first get the repository GUID via "
        "mcp__azure-devops__repo_get_repo_by_name_or_id, then use that GUID as repositoryId.\n"
        "- IMPORTANT: After creating a PR via mcp__azure-devops__repo_create_pull_request, you MUST "
        "immediately self-review it. Fetch the PR diff using Bash (git diff), analyze the code for "
        "correctness, style, security, and test coverage, then post a review comment on the PR using "
        "mcp__azure-devops__repo_create_pull_request_thread with content starting with "
        "'## Code Review by moksh.ai' and status 'Active'. Post inline comments for specific "
        "issues (max 5) also with status 'Active'. Include a verdict: Approve / Request Changes. "
        "This is mandatory for every PR you create.\n"
        "- After reviewing the PR, also run the test suite for the changed repo and post results. "
        f"For avis: `cd {COMMERCE_ROOT}/services/avis && ./run.test.sh`. "
        f"For brunt: `cd {COMMERCE_ROOT}/tests/brunt && npm run test`. "
        f"For mirage: `cd {COMMERCE_ROOT}/services/mirage && ./run.local.test.sh`. "
        "Post test results as a PR comment using mcp__azure-devops__repo_create_pull_request_thread "
        "with content starting with '## Test Report by moksh.ai' and status 'Active'. "
        "Include: total tests run, passed, failed, skipped, and failure details if any. "
        "If tests cannot run (missing services), report that instead.\n"
        "--- END RESPONSE GUIDELINES ---\n"
    ]

    if KNOWLEDGE_INDEX_FILE:
        system_parts.append(
            "\n\n--- KNOWLEDGE & CODEBASE ---\n"
            f"A knowledge index and codebase index is saved at: {KNOWLEDGE_INDEX_FILE}\n"
            "When a question relates to domain knowledge, code architecture, or you need to find "
            "relevant files, read this index first using the Read tool. "
            "Then use Read/Grep on the actual files listed there. "
            "Do NOT guess — always check the index and read source files.\n"
            "--- END KNOWLEDGE & CODEBASE ---\n"
        )

    if MCP_TOOL_CATALOG:
        system_parts.append(MCP_TOOL_CATALOG)

    opts.system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": "".join(system_parts),
    }

    if KNOWLEDGE_DIRS:
        opts.add_dirs = list(KNOWLEDGE_DIRS)

    return opts


async def _collect_response(sdk_client, thread_ts: str = None, channel: str = None, progress_ts: str = None) -> tuple[str, dict | None]:
    """
    Collect response from SDK client.
    Logs ALL message types from Claude Code subprocess to bot.log.
    Returns (response_text, cost_info_dict_or_None).
    """
    from claude_agent_sdk import (
        AssistantMessage, ResultMessage, SystemMessage, UserMessage,
        TextBlock, ToolUseBlock, ToolResultBlock,
    )

    response_parts = []
    tool_log = []
    result_text = None  # Will hold ResultMessage.result if available
    cost_info = None
    last_update_time = time.time()
    _toolsearch_counts: dict[str, int] = {}
    MAX_TOOLSEARCH_REPEATS = 3

    try:
        async for message in sdk_client.receive_response():
            msg_type = type(message).__name__

            if isinstance(message, SystemMessage):
                logger.info(f"[CC:{thread_ts}] SystemMessage subtype={message.subtype} data={json.dumps(message.data, default=str)[:300]}")

            elif isinstance(message, UserMessage):
                content_preview = str(message.content)[:200] if message.content else ""
                logger.info(f"[CC:{thread_ts}] UserMessage: {content_preview}")

            elif isinstance(message, AssistantMessage):
                if message.error:
                    logger.error(f"[CC:{thread_ts}] AssistantMessage ERROR: {message.error}")

                # Collect text from this AssistantMessage
                # Only replace previous parts if this message has substantive text
                current_parts = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        current_parts.append(block.text)
                        logger.info(f"[CC:{thread_ts}] Text: {block.text[:150]}{'...' if len(block.text) > 150 else ''}")

                    elif isinstance(block, ToolUseBlock):
                        tool_info = f"🔧 {block.name}"
                        if block.name == "Bash":
                            cmd = block.input.get("command", "")
                            tool_info += f": `{cmd[:120]}`"
                        elif block.name in ("Read", "Write", "Edit"):
                            tool_info += f": {block.input.get('file_path', '')}"
                        elif block.name == "Glob":
                            tool_info += f": {block.input.get('pattern', '')}"
                        elif block.name == "Grep":
                            tool_info += f": `{block.input.get('pattern', '')}` in {block.input.get('path', '.')}"
                        elif block.name == "Agent":
                            desc = block.input.get("description", block.input.get("prompt", ""))
                            tool_info += f": {desc[:80]}"
                        elif block.name.startswith("mcp__"):
                            # MCP tool — show a clean name
                            short_name = block.name.split("__", 2)[-1] if "__" in block.name else block.name
                            tool_info = f"🔧 {short_name}"
                        else:
                            tool_info += f": {json.dumps(block.input, default=str)[:100]}"

                        logger.info(f"[CC:{thread_ts}] {tool_info}")

                        # Loop detection: break if ToolSearch called repeatedly for same tool
                        if block.name == "ToolSearch":
                            search_key = json.dumps(block.input, sort_keys=True)
                            _toolsearch_counts[search_key] = _toolsearch_counts.get(search_key, 0) + 1
                            if _toolsearch_counts[search_key] >= MAX_TOOLSEARCH_REPEATS:
                                logger.warning(f"[CC:{thread_ts}] LOOP DETECTED: ToolSearch called {MAX_TOOLSEARCH_REPEATS}+ times for {search_key}. Breaking.")
                                try:
                                    await sdk_client.disconnect()
                                except Exception:
                                    pass
                                break  # Exit the content block loop

                        # Skip internal/meta tools from user-facing progress
                        if block.name not in ("ToolSearch", "Task", "TaskOutput", "ExitPlanMode", "NotebookEdit"):
                            tool_log.append(tool_info)

                    elif isinstance(block, ToolResultBlock):
                        is_err = block.is_error if block.is_error else False
                        content_preview = ""
                        if block.content:
                            if isinstance(block.content, str):
                                content_preview = block.content[:300]
                            elif isinstance(block.content, list):
                                content_preview = str(block.content)[:300]
                        status = "❌ ERROR" if is_err else "✅ OK"
                        logger.info(f"[CC:{thread_ts}] ToolResult {status}: {content_preview}")

                    else:
                        logger.info(f"[CC:{thread_ts}] ContentBlock({type(block).__name__}): {str(block)[:200]}")

                # Only replace response_parts if this message has substantive text
                # (prevents empty final AssistantMessage from wiping useful earlier text)
                substantive = any(p.strip() for p in current_parts)
                if substantive:
                    response_parts = current_parts

                # Break outer loop if ToolSearch loop was detected
                if any(c >= MAX_TOOLSEARCH_REPEATS for c in _toolsearch_counts.values()):
                    break

            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
                duration = f"{message.duration_ms / 1000:.1f}s" if message.duration_ms else "N/A"
                logger.info(
                    f"[CC:{thread_ts}] RESULT status={message.subtype} "
                    f"turns={message.num_turns} cost={cost} duration={duration} "
                    f"error={message.is_error} session={getattr(message, 'session_id', 'N/A')}"
                )
                if message.result:
                    result_text = message.result
                    logger.info(f"[CC:{thread_ts}] Result text: {message.result[:300]}")
                cost_info = {
                    "cost_usd": message.total_cost_usd,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "num_turns": message.num_turns,
                    "session_id": message.session_id,
                    "is_error": message.is_error,
                    "stop_reason": message.stop_reason,
                    "usage": message.usage,
                }

            else:
                # Catch-all for any other message types (StreamEvent, etc.)
                logger.info(f"[CC:{thread_ts}] {msg_type}: {str(message)[:300]}")

            # ── Streaming: update progress message every 2.5s ──
            if progress_ts and channel and time.time() - last_update_time >= 2.5:
                last_update_time = time.time()
                progress_lines = [":robot_face: *Working...*"]
                # Show last 5 tool calls
                for t in tool_log[-5:]:
                    progress_lines.append(f"  {t}")
                # Show text preview if available
                if response_parts:
                    preview = response_parts[-1][:100]
                    if len(response_parts[-1]) > 100:
                        preview += "..."
                    progress_lines.append(f"  _Writing: {preview}_")
                try:
                    slack_client.chat_update(
                        channel=channel, ts=progress_ts,
                        text="\n".join(progress_lines),
                    )
                except Exception as e:
                    logger.warning(f"[STREAM] Failed to update progress: {e}")

    except Exception as e:
        logger.error(f"[CC:{thread_ts}] _collect_response error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return (f"Claude Code encountered an error:\n```\n{type(e).__name__}: {e}\n```", None)

    # Delete progress message now that we have the final response
    if progress_ts and channel:
        try:
            slack_client.chat_delete(channel=channel, ts=progress_ts)
        except Exception as e:
            logger.warning(f"[STREAM] Failed to delete progress message: {e}")

    if tool_log:
        logger.info(f"[CC:{thread_ts}] SUMMARY — {len(tool_log)} tool calls")

    # If a ToolSearch loop was detected, provide a friendly fallback
    loop_detected = any(c >= MAX_TOOLSEARCH_REPEATS for c in _toolsearch_counts.values())
    if loop_detected and not response_parts:
        response_parts = ["_A required tool could not be loaded after multiple attempts (likely a deferred MCP tool). The tool may be temporarily unavailable. Try again or rephrase your request._"]

    # Prefer collected AssistantMessage text; only fall back to ResultMessage.result
    # if no text was collected (result_text can be a meta-message about agent tasks)
    if response_parts:
        full_response = "\n".join(response_parts)
    elif result_text and result_text.strip():
        full_response = result_text
    else:
        full_response = ""
    if not full_response.strip():
        full_response = "_Claude Code completed the task but produced no text output._"
    return (full_response, cost_info)


async def _new_session_and_query(prompt: str, thread_ts: str, progress_ts: str = None, file_blocks: list = None, model: str = None, thinking_override: str = None) -> tuple[str, dict | None]:
    from claude_agent_sdk import ClaudeSDKClient
    options = _create_options(model=model, thinking_override=thinking_override)
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.connect()

    loop = asyncio.get_event_loop()
    store_session(thread_ts, sdk_client, loop)

    channel = db_get_thread_channel(thread_ts)

    if file_blocks:
        content = [{"type": "text", "text": prompt}] + file_blocks
        async def _message_stream():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        await sdk_client.query(_message_stream())
    else:
        await sdk_client.query(prompt)
    return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts)


async def _restore_and_query(history: list[dict], new_prompt: str, thread_ts: str, progress_ts: str = None, file_blocks: list = None, model: str = None, thinking_override: str = None) -> tuple[str, dict | None]:
    from claude_agent_sdk import ClaudeSDKClient
    options = _create_options(model=model, thinking_override=thinking_override)
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.connect()
    logger.info(f"[RESTORE] Replaying {len(history)} messages for thread {thread_ts}")

    loop = asyncio.get_event_loop()
    store_session(thread_ts, sdk_client, loop)

    # Limit history to last 20 messages to avoid context overload
    recent_history = history[-20:] if len(history) > 20 else history
    history_parts = []
    for msg in recent_history:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        history_parts.append(f"{prefix}: {msg['content']}")

    context_prompt = (
        "Here is the conversation history from a previous session. "
        "Review it for context, then respond to the latest message.\n\n"
        "--- CONVERSATION HISTORY ---\n"
        + "\n\n".join(history_parts)
        + "\n--- END HISTORY ---\n\n"
        f"Now, the user says: {new_prompt}"
    )

    if file_blocks:
        content = [{"type": "text", "text": context_prompt}] + file_blocks
        async def _message_stream():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        await sdk_client.query(_message_stream())
    else:
        await sdk_client.query(context_prompt)
    channel = db_get_thread_channel(thread_ts)
    return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts)


async def _continue_query(sdk_client, prompt: str, thread_ts: str = None, progress_ts: str = None, file_blocks: list = None) -> tuple[str, dict | None]:
    if file_blocks:
        content = [{"type": "text", "text": prompt}] + file_blocks
        async def _message_stream():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        await sdk_client.query(_message_stream())
    else:
        await sdk_client.query(prompt)
    channel = db_get_thread_channel(thread_ts) if thread_ts else None
    return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts)


# ─── Invocation (runs per-thread, non-blocking) ─────────────────────────────

def _run_in_new_loop(coro_factory, thread_ts: str, *args, **kwargs) -> str:
    """
    Run an async coroutine in a NEW event loop on a NEW dedicated thread.
    The loop stays alive after the first response so the session persists.
    """
    result_container = {"response": None, "error": None, "traceback": None}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_container["response"] = loop.run_until_complete(coro_factory(*args, **kwargs))
            # Keep loop alive for follow-up queries on this session
            loop.run_forever()
        except Exception as e:
            result_container["error"] = e
            result_container["traceback"] = traceback.format_exc()
            logger.error(f"[LOOP ERROR] thread={thread_ts}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name=f"session-{thread_ts[:8]}")
    t.start()

    deadline = time.time() + REQUEST_TIMEOUT
    while time.time() < deadline:
        if result_container["response"] is not None or result_container["error"] is not None:
            break
        time.sleep(0.1)

    if result_container["error"]:
        tb = result_container.get("traceback", "")
        logger.error(f"[INVOKE ERROR] thread={thread_ts}: {type(result_container['error']).__name__}: {result_container['error']}\n{tb}")
        raise result_container["error"]
    if result_container["response"] is None:
        return f"Claude Code timed out ({REQUEST_TIMEOUT}s)."
    return result_container["response"]


def invoke_claude_code(prompt: str, thread_ts: str, progress_ts: str = None, file_blocks: list = None, model: str = None, thinking_override: str = None) -> tuple[str, dict | None]:
    """
    Synchronous entry point. Called from the thread pool.
    Routes to: continue existing | restore from DB | new session.
    """
    session = get_session(thread_ts)

    # Thinking override requires a fresh session (can't change mid-session)
    if session and thinking_override:
        logger.info(f"[THINKING] Override requested, killing existing session for thread={thread_ts}")
        remove_session(thread_ts)
        session = None

    if session:
        logger.info(f"[CONTINUE] thread={thread_ts}")
        touch_session(thread_ts)
        mark_session_processing(thread_ts, True)
        loop = session["loop"]
        sdk_client = session["client"]
        try:
            future = asyncio.run_coroutine_threadsafe(
                _continue_query(sdk_client, prompt, thread_ts, progress_ts=progress_ts, file_blocks=file_blocks),
                loop,
            )
            response, cost_info = future.result(timeout=REQUEST_TIMEOUT)

            # Auto-compact: if Claude returned empty, the session is likely context-saturated.
            if not response.strip() or response == "_Claude Code completed the task but produced no text output._":
                logger.warning(f"[AUTO-COMPACT] Empty response on continue, restarting session for thread={thread_ts}")
                remove_session(thread_ts)
                history = db_get_thread_messages(thread_ts)
                if history:
                    return _run_in_new_loop(_restore_and_query, thread_ts, history, prompt, thread_ts, progress_ts, file_blocks, model=model, thinking_override=thinking_override)

            return (response, cost_info)
        except Exception as e:
            logger.error(f"[CONTINUE ERROR] thread={thread_ts}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            remove_session(thread_ts)
            raise
        finally:
            mark_session_processing(thread_ts, False)

    history = db_get_thread_messages(thread_ts)

    if history:
        logger.info(f"[RESTORE] thread={thread_ts}, {len(history)} messages in DB")
        return _run_in_new_loop(_restore_and_query, thread_ts, history, prompt, thread_ts, progress_ts, file_blocks, model=model, thinking_override=thinking_override)
    else:
        logger.info(f"[NEW] thread={thread_ts}")
        return _run_in_new_loop(_new_session_and_query, thread_ts, prompt, thread_ts, progress_ts, file_blocks, model=model, thinking_override=thinking_override)


# ─── Auth ────────────────────────────────────────────────────────────────────

def is_authorized(user_id: str) -> bool:
    if not ALLOWED_USER_IDS or ALLOWED_USER_IDS == [""]:
        return True
    return user_id in ALLOWED_USER_IDS


# ─── File Download ────────────────────────────────────────────────────────────

def _download_slack_files(files: list[dict]) -> list[dict]:
    """Download files from Slack and convert to Claude-compatible content blocks."""
    file_blocks = []
    for f in files:
        file_id = f.get("id")
        file_size = f.get("size", 0)
        file_name = f.get("name", "unknown")
        mimetype = f.get("mimetype", "")

        if file_size > MAX_FILE_SIZE:
            logger.warning(f"[FILES] Skipping {file_name}: {file_size} bytes exceeds {MAX_FILE_SIZE} limit")
            continue

        try:
            info = slack_client.files_info(file=file_id)
            url = info["file"].get("url_private")
            if not url:
                logger.warning(f"[FILES] No url_private for {file_name}")
                continue

            resp = httpx.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content

            if mimetype in SUPPORTED_IMAGE_TYPES:
                b64 = base64.standard_b64encode(data).decode("ascii")
                file_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mimetype, "data": b64},
                })
                logger.info(f"[FILES] Added image: {file_name} ({mimetype}, {len(data)} bytes)")
            else:
                try:
                    text_content = data.decode("utf-8")
                    file_blocks.append({
                        "type": "text",
                        "text": f"--- File: {file_name} ---\n{text_content}\n--- End: {file_name} ---",
                    })
                    logger.info(f"[FILES] Added text file: {file_name} ({len(text_content)} chars)")
                except UnicodeDecodeError:
                    logger.warning(f"[FILES] Skipping binary file: {file_name} ({mimetype})")

        except Exception as e:
            logger.error(f"[FILES] Failed to download {file_name}: {type(e).__name__}: {e}")

    return file_blocks


# ─── PR Review ────────────────────────────────────────────────────────────────

# Known local repo paths for git diff
COMMERCE_ROOT = "/Users/mokshkhajanchi/Documents/projects/Commerce/Commerce-ai"

REPO_PATHS = {
    "avis": f"{COMMERCE_ROOT}/services/avis",
    "brunt": f"{COMMERCE_ROOT}/tests/brunt",
    "api-specifications": "/Users/mokshkhajanchi/Documents/projects/api-specifications/api-specifications-ai",
    "mirage": f"{COMMERCE_ROOT}/services/mirage",
}

REPO_TEST_CONFIG = {
    "avis": {
        "path": f"{COMMERCE_ROOT}/services/avis",
        "cmd": "./run.test.sh",
        "cmd_specific": "./run.test.sh test/{module}/",
    },
    "brunt": {
        "path": f"{COMMERCE_ROOT}/tests/brunt",
        "cmd": "npm run test",
        "cmd_specific": "npm run test",
    },
    "mirage": {
        "path": f"{COMMERCE_ROOT}/services/mirage",
        "cmd": "./run.local.test.sh",
        "cmd_specific": "./run.local.test.sh",
    },
}

ADO_PR_PATTERN = re.compile(
    r"https?://dev\.azure\.com/GoFynd/FyndPlatformCore/_git/([^/]+)/pullrequest/(\d+)"
)


def _parse_ado_pr_url(text: str) -> tuple[str, int] | None:
    """Extract (repo_name, pr_id) from an Azure DevOps PR URL in the text."""
    match = ADO_PR_PATTERN.search(text)
    if match:
        return match.group(1), int(match.group(2))
    # Also handle "review PR #12345" with repo context
    match = re.match(r"^review\s+(?:pr\s+)?#?(\d+)\s+(\w+)$", text.strip(), re.IGNORECASE)
    if match:
        return match.group(2), int(match.group(1))
    return None


def _get_pr_diff(repo_name: str, source_branch: str, target_branch: str, source_commit: str) -> str:
    """Get PR diff using local git repo. Falls back to commit-level diff."""
    import subprocess

    local_path = REPO_PATHS.get(repo_name)
    if not local_path or not Path(local_path).exists():
        # Try to find it
        for candidate in Path("/Users/mokshkhajanchi/Documents/projects").iterdir():
            if candidate.name == repo_name and (candidate / ".git").exists():
                local_path = str(candidate)
                break
            sub = candidate / repo_name
            if sub.exists() and (sub / ".git").exists():
                local_path = str(sub)
                break

    if not local_path:
        return f"(Could not find local repo for '{repo_name}'. Diff unavailable.)"

    try:
        # Fetch both branches
        subprocess.run(
            ["git", "fetch", "origin", source_branch, target_branch],
            cwd=local_path, capture_output=True, timeout=30,
        )

        # Try branch diff first
        result = subprocess.run(
            ["git", "diff", f"origin/{target_branch}...origin/{source_branch}"],
            cwd=local_path, capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return result.stdout

        # Branches may be merged — fall back to commit diff
        subprocess.run(
            ["git", "fetch", "origin", source_commit],
            cwd=local_path, capture_output=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "show", source_commit, "--format="],
            cwd=local_path, capture_output=True, text=True, timeout=30,
        )
        return result.stdout if result.stdout.strip() else "(Empty diff — PR may already be merged.)"

    except Exception as e:
        return f"(Failed to get diff: {type(e).__name__}: {e})"


def _review_pr(repo_name: str, pr_id: int, channel: str, thread_ts: str):
    """
    Review an Azure DevOps PR: fetch details, get diff, analyze with Claude, post comments.
    Runs in the thread pool.
    """
    from claude_agent_sdk import ClaudeSDKClient

    try:
        # Step 1: Post progress
        progress_msg = slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":mag: Reviewing PR #{pr_id} in *{repo_name}*...",
        )
        progress_ts = progress_msg.get("ts")

        # Step 2: Fetch PR details via MCP — we need to invoke Claude for this
        # since MCP tools are only available inside a Claude session.
        # Build a review prompt and let Claude do the review with MCP tools.

        review_prompt = f"""Review Azure DevOps PR #{pr_id} in the **{repo_name}** repository (project: FyndPlatformCore).

Follow these steps:

1. **Fetch PR details**: Use `mcp__azure-devops__repo_get_pull_request_by_id` with repositoryId="{repo_name}", pullRequestId={pr_id}, project="FyndPlatformCore", includeWorkItemRefs=true.

2. **Get the diff**: The source branch and target branch are in the PR response (sourceRefName, targetRefName — strip "refs/heads/" prefix). Use the Bash tool to run:
   ```
   cd {REPO_PATHS.get(repo_name, '/Users/mokshkhajanchi/Documents/projects/' + repo_name)} && git fetch origin <source_branch> <target_branch> 2>&1 && git diff origin/<target_branch>...origin/<source_branch>
   ```
   If the diff is empty, fall back to: `git fetch origin <lastMergeSourceCommit.commitId> && git show <commitId> --format=""`

3. **Analyze the diff thoroughly**:
   - Overview: what the PR does
   - Code quality: style, patterns, DRY
   - Correctness: logic errors, edge cases
   - Performance: N+1 queries, unnecessary work
   - Security: injection risks, secrets
   - Test coverage: are changes tested?
   - Specific suggestions with file:line references

4. **Post review comments on the PR**:
   a. Post a general review comment using `mcp__azure-devops__repo_create_pull_request_thread` with:
      - repositoryId: "{repo_name}", pullRequestId: {pr_id}, project: "FyndPlatformCore"
      - content: Full review in markdown, starting with "## Code Review by moksh.ai"
      - status: "Active" (always keep review threads active for visibility)

   b. For specific issues (max 5), post inline comments using the same tool with:
      - filePath, rightFileStartLine, rightFileStartOffset=1, rightFileEndLine, rightFileEndOffset=1
      - status: "Active"

5. **Respond with**: PR title, verdict (Approve/Request Changes), number of comments posted, any critical issues.

Be thorough but concise. Reference exact file paths and line numbers."""

        # Use a temporary session for the review
        review_key = f"review_{pr_id}_{int(time.time())}"
        response, cost_info = invoke_claude_code(
            review_prompt, review_key,
            progress_ts=progress_ts,
        )

        if cost_info:
            db_save_usage(thread_ts, cost_info)

        # Clean up temporary session
        remove_session(review_key)

        # Post the review result to Slack
        send_response_with_stop_button(channel, thread_ts, response)

    except Exception as e:
        logger.error(f"[PR REVIEW] Error reviewing PR #{pr_id}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: Failed to review PR #{pr_id}:\n```\n{type(e).__name__}: {e}\n```",
        )


# ─── Core Handler (runs in thread pool) ─────────────────────────────────────

def _process_message(user_id: str, channel: str, thread_ts: str, message_ts: str, text: str, files: list = None):
    """
    The actual work — runs inside the thread pool.
    Per-thread lock ensures only one request is processed at a time per thread.
    """
    lock = get_thread_lock(thread_ts)

    if not lock.acquire(timeout=5):
        logger.warning(f"[BUSY] thread={thread_ts} is already being processed, skipping.")
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass: Still processing your previous message. Please wait...",
        )
        return

    try:
        prompt = clean_message_text(text, BOT_USER_ID)
        if not prompt.strip():
            return

        # ── Parse model and thinking prefixes ──
        model_override, prompt = parse_model_prefix(prompt)
        thinking_override = None
        if prompt.lower().startswith("think:"):
            thinking_override = "enabled"
            prompt = prompt[len("think:"):].strip()

        # ── Bot commands (before normal Claude Code flow) ──
        prompt_lower = prompt.strip().lower()
        if prompt_lower in ("kill all sessions", "kill all", "stop all sessions", "stop all"):
            count = kill_all_sessions()
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":broom: Killed *{count}* active session{'s' if count != 1 else ''}. All connections cleared.",
            )
            return

        if prompt_lower in ("status", "bot status", "cost", "usage"):
            daily = db_get_daily_cost()
            total = db_get_total_cost()
            active = get_active_session_count()
            lines = [
                f":bar_chart: *Bot Status*",
                f"• Active sessions: *{active}*",
                f"• Today's cost: *${daily:.4f}*",
            ]
            if DAILY_BUDGET_USD > 0:
                remaining = max(0, DAILY_BUDGET_USD - daily)
                lines.append(f"• Daily budget: *${DAILY_BUDGET_USD:.2f}* (${remaining:.4f} remaining)")
            lines.append(f"• All-time cost: *${total:.4f}*")
            stats = db_get_feedback_stats()
            if stats["total_positive"] + stats["total_negative"] > 0:
                lines.append(f"• Satisfaction: *{stats['satisfaction_pct']:.0f}%* ({stats['total_positive']} :+1:  {stats['total_negative']} :-1:)")
            slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text="\n".join(lines),
            )
            return

        if prompt_lower in ("summarize", "summary", "recap"):
            messages = db_get_thread_messages(thread_ts)
            if not messages:
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=":warning: No conversation history found for this thread.",
                )
                return

            convo_parts = []
            for msg in messages:
                prefix = "User" if msg["role"] == "user" else "Assistant"
                convo_parts.append(f"{prefix}: {msg['content'][:500]}")
            conversation_text = "\n\n".join(convo_parts)
            if len(conversation_text) > 30000:
                conversation_text = "...(earlier messages truncated)...\n\n" + conversation_text[-30000:]

            summary_prompt = (
                "Summarize this conversation thread. Highlight:\n"
                "1. Key decisions made\n"
                "2. Actions taken (files changed, PRs created, etc.)\n"
                "3. Outcomes and current status\n"
                "4. Any open items or follow-ups needed\n\n"
                "Keep it concise but complete.\n\n"
                f"--- CONVERSATION ---\n{conversation_text}\n--- END CONVERSATION ---"
            )

            progress_msg = slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":memo: Generating summary...",
            )
            progress_ts = progress_msg.get("ts")

            summary_key = f"summary_{thread_ts}_{int(time.time())}"
            response, cost_info = invoke_claude_code(summary_prompt, summary_key, progress_ts=progress_ts)
            if cost_info:
                db_save_usage(thread_ts, cost_info)
            remove_session(summary_key)
            send_response_with_stop_button(channel, thread_ts, response)
            return

        # ── PR Review command ──
        pr_info = _parse_ado_pr_url(prompt)
        if pr_info and (prompt_lower.startswith("review") or prompt_lower.startswith("review pr")):
            repo_name, pr_id = pr_info
            _review_pr(repo_name, pr_id, channel, thread_ts)
            return
        # Also trigger on just a bare PR URL with "review" anywhere
        if pr_info and "review" in prompt_lower:
            repo_name, pr_id = pr_info
            _review_pr(repo_name, pr_id, channel, thread_ts)
            return

        # ── Test command ──
        if prompt_lower.startswith("test ") or prompt_lower == "test":
            parts = prompt.strip().split(None, 2)  # ["test", "repo", "module"]
            repo_name = parts[1].lower() if len(parts) > 1 else None
            module = parts[2] if len(parts) > 2 else None

            if not repo_name or repo_name not in REPO_TEST_CONFIG:
                available = ", ".join(REPO_TEST_CONFIG.keys())
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":test_tube: Usage: `test <repo> [module]`\nAvailable repos: `{available}`\nExample: `test avis module_createorder`",
                )
                return

            config = REPO_TEST_CONFIG[repo_name]
            if module:
                test_cmd = config["cmd_specific"].format(module=module)
            else:
                test_cmd = config["cmd"]

            progress_msg = slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":test_tube: Running tests for *{repo_name}*{f' ({module})' if module else ''}...",
            )
            progress_ts = progress_msg.get("ts")

            test_prompt = (
                f"Run the following test command and report the results:\n\n"
                f"```\ncd {config['path']} && {test_cmd}\n```\n\n"
                f"After running:\n"
                f"1. Report the total tests run, passed, failed, skipped\n"
                f"2. If any tests failed, show the failure details with file paths and line numbers\n"
                f"3. If all passed, just say 'All tests passed' with the count\n"
                f"Keep the response concise."
            )

            test_key = f"test_{repo_name}_{int(time.time())}"
            response, cost_info = invoke_claude_code(test_prompt, test_key, progress_ts=progress_ts)
            if cost_info:
                db_save_usage(thread_ts, cost_info)
            remove_session(test_key)
            send_response_with_stop_button(channel, thread_ts, response)
            return

        # ── Daily budget check ──
        if DAILY_BUDGET_USD > 0 and db_get_daily_cost() >= DAILY_BUDGET_USD:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: Daily budget of *${DAILY_BUDGET_USD:.2f}* has been reached. Try again tomorrow or ask an admin to increase `DAILY_BUDGET_USD`.",
            )
            return

        # DB
        db_create_thread(thread_ts, channel)
        db_save_message(thread_ts, "user", prompt, user_id=user_id)

        # Fetch only new Slack thread messages (ones not already sent as context)
        since_ts = db_get_last_slack_ts(thread_ts)
        thread_context, latest_ts = fetch_thread_messages(channel, thread_ts, since_ts=since_ts)
        if thread_context:
            prompt = thread_context + "User's request: " + prompt
            logger.info(f"[THREAD CONTEXT] Prepended {len(thread_context)} chars (since={since_ts})")
        if latest_ts:
            db_set_last_slack_ts(thread_ts, latest_ts)

        # Download file attachments
        file_blocks = None
        if files:
            file_blocks = _download_slack_files(files)
            if file_blocks:
                logger.info(f"[FILES] {len(file_blocks)} file blocks ready for thread {thread_ts}")
            else:
                file_blocks = None  # No usable files

        # Working indicator (capture ts for streaming updates)
        progress_msg = slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":robot_face: Working on it... (active sessions: {get_active_session_count()})",
        )
        progress_ts = progress_msg.get("ts")

        # Invoke Claude Code
        response, cost_info = invoke_claude_code(
            prompt, thread_ts, progress_ts=progress_ts, file_blocks=file_blocks,
            model=model_override, thinking_override=thinking_override,
        )

        # Save usage data (DB only — not shown in Slack response)
        if cost_info:
            db_save_usage(thread_ts, cost_info)

        # Save response
        db_save_message(thread_ts, "assistant", response)

        # React ✅
        try:
            slack_client.reactions_add(channel=channel, name="white_check_mark", timestamp=message_ts)
        except Exception:
            pass

        # Send response + Stop button
        send_response_with_stop_button(channel, thread_ts, response)

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[HANDLER ERROR] thread={thread_ts}: {type(e).__name__}: {e}\n{tb}")
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Failed to invoke Claude Code:\n```\n{type(e).__name__}: {e}\n```",
        )
    finally:
        lock.release()


def handle_user_message(user_id: str, channel: str, thread_ts: str, message_ts: str, text: str, files: list = None):
    """
    Entry point from Slack event handlers.
    Dispatches work to the thread pool so it doesn't block other events.
    """
    if not is_authorized(user_id):
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Sorry <@{user_id}>, you're not authorized to use this bot.",
        )
        return

    # React 👀 immediately (before submitting to pool)
    try:
        slack_client.reactions_add(channel=channel, name="eyes", timestamp=message_ts)
    except Exception:
        pass

    # Submit to thread pool — returns immediately
    executor.submit(_process_message, user_id, channel, thread_ts, message_ts, text, files)


# ─── Slack Event Handlers ───────────────────────────────────────────────────

def _render_app_home(user_id: str):
    """Render the App Home tab with dashboard data."""
    active_count = get_active_session_count()
    daily_cost = db_get_daily_cost()
    total_cost = db_get_total_cost()
    recent_threads = db_get_recent_threads(limit=10)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Claude Code Bot Dashboard"}},
    ]

    # Status section
    budget_text = ""
    if DAILY_BUDGET_USD > 0:
        remaining = max(0, DAILY_BUDGET_USD - daily_cost)
        budget_text = f"\n:moneybag: *Budget remaining:* ${remaining:.4f} / ${DAILY_BUDGET_USD:.2f}"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f":robot_face: *Active Sessions:* {active_count}\n"
            f":chart_with_upwards_trend: *Today's Cost:* ${daily_cost:.4f}"
            f"{budget_text}\n"
            f":bank: *All-Time Cost:* ${total_cost:.4f}"
        )}
    })

    # Feedback stats
    fb_stats = db_get_feedback_stats()
    fb_total = fb_stats["total_positive"] + fb_stats["total_negative"]
    if fb_total > 0:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f":star: *Satisfaction:* {fb_stats['satisfaction_pct']:.0f}% ({fb_total} ratings) | "
                f"Today: {fb_stats['today_positive']} :+1:  {fb_stats['today_negative']} :-1:"
            )}
        })

    # Quick actions
    blocks.append({"type": "actions", "elements": [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": ":broom: Kill All Sessions", "emoji": True},
            "style": "danger",
            "action_id": "home_kill_all",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Refresh", "emoji": True},
            "action_id": "home_refresh",
        },
    ]})

    blocks.append({"type": "divider"})
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Recent Threads"}})

    if not recent_threads:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No threads yet._"}})
    else:
        for thread in recent_threads:
            status_emoji = ":large_green_circle:" if thread["status"] == "active" else ":white_circle:"
            cost_str = f"${thread['total_cost']:.4f}" if thread["total_cost"] else "$0"
            ts_link = thread["thread_ts"].replace(".", "")
            link = f"<https://slack.com/archives/{thread['channel']}/p{ts_link}|View>"
            updated = thread["updated_at"][:16] if thread["updated_at"] else "?"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": (
                    f"{status_emoji} *{thread['status'].title()}* | "
                    f"Cost: {cost_str} | Queries: {thread['query_count']} | "
                    f"{updated} | {link}"
                )}
            })

    try:
        slack_client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error(f"[APP HOME] Failed to render: {type(e).__name__}: {e}")


@app.event("app_home_opened")
def handle_app_home_opened(event, logger):
    _render_app_home(event.get("user"))


@app.event("reaction_added")
def handle_reaction_added(event, logger):
    reaction = event.get("reaction", "")
    if reaction not in FEEDBACK_REACTIONS:
        return
    item_user = event.get("item_user")
    if item_user != BOT_USER_ID:
        return
    item = event.get("item", {})
    if item.get("type") != "message":
        return

    message_ts = item.get("ts")
    channel = item.get("channel")
    user_id = event.get("user")

    # Look up thread_ts from the reacted message
    thread_ts = None
    try:
        result = slack_client.conversations_history(channel=channel, latest=message_ts, inclusive=True, limit=1)
        msgs = result.get("messages", [])
        if msgs:
            thread_ts = msgs[0].get("thread_ts", message_ts)
    except Exception:
        pass

    score = FEEDBACK_REACTIONS[reaction]
    db_save_feedback(message_ts, thread_ts, channel, user_id, reaction, score)
    logger.info(f"[FEEDBACK] {reaction} ({score:+d}) on {message_ts} by {user_id}")


@app.event("reaction_removed")
def handle_reaction_removed(event, logger):
    reaction = event.get("reaction", "")
    if reaction not in FEEDBACK_REACTIONS:
        return
    if event.get("item_user") != BOT_USER_ID:
        return
    item = event.get("item", {})
    if item.get("type") != "message":
        return
    db_remove_feedback(item.get("ts"), event.get("user"), reaction)
    logger.info(f"[FEEDBACK] Removed {reaction} on {item.get('ts')} by {event.get('user')}")


@app.event("app_mention")
def handle_mention(event, say):
    handle_user_message(
        user_id=event.get("user"),
        channel=event.get("channel"),
        thread_ts=event.get("thread_ts") or event.get("ts"),
        message_ts=event.get("ts"),
        text=event.get("text", ""),
        files=event.get("files"),
    )


@app.event("message")
def handle_message(event, say):
    if event.get("bot_id") or event.get("user") == BOT_USER_ID:
        return
    if event.get("subtype"):
        return

    user_id = event.get("user")
    channel = event.get("channel")
    channel_type = event.get("channel_type", "")
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")
    text = event.get("text", "")

    is_dm = channel_type == "im"
    is_bot_mentioned = BOT_USER_ID and f"<@{BOT_USER_ID}>" in text

    if is_bot_mentioned and not is_dm:
        return

    effective_thread_ts = thread_ts or message_ts
    has_session = get_session(effective_thread_ts) is not None
    has_db_thread = db_is_active_thread(effective_thread_ts)

    if not is_dm and not has_session and not has_db_thread:
        return

    handle_user_message(user_id, channel, effective_thread_ts, message_ts, text, files=event.get("files"))


# ─── Stop Button ─────────────────────────────────────────────────────────────

@app.action("stop_session")
def handle_stop_session(ack, body):
    ack()

    user_id = body.get("user", {}).get("id")
    actions = body.get("actions", [])
    thread_ts = actions[0].get("value") if actions else None
    channel = body.get("channel", {}).get("id")

    if not thread_ts:
        return

    logger.info(f"[STOP] thread={thread_ts} by user={user_id}")

    remove_session(thread_ts)
    db_stop_thread(thread_ts)
    remove_thread_lock(thread_ts)

    if channel:
        send_session_ended(channel, thread_ts)


@app.action("show_details")
def handle_show_details(ack, body):
    ack()

    actions = body.get("actions", [])
    detail_id = actions[0].get("value") if actions else None
    channel = body.get("channel", {}).get("id")
    message = body.get("message", {})
    thread_ts = message.get("thread_ts") or message.get("ts")

    if not detail_id or not channel:
        return

    details = _pop_detail(detail_id)
    if not details:
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="_Details are no longer available (bot may have restarted)._",
        )
        return

    # Send details as a follow-up message
    detail_text = markdown_to_slack(details)
    chunks = chunk_message(detail_text)
    for chunk in chunks:
        slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
        time.sleep(0.3)


# ─── App Home Actions ─────────────────────────────────────────────────────────

@app.action("home_kill_all")
def handle_home_kill_all(ack, body):
    ack()
    kill_all_sessions()
    _render_app_home(body.get("user", {}).get("id"))


@app.action("home_refresh")
def handle_home_refresh(ack, body):
    ack()
    _render_app_home(body.get("user", {}).get("id"))


# ─── Slash Commands ──────────────────────────────────────────────────────────

@app.command("/claude")
def handle_claude_command(ack, command, respond):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    text = command.get("text", "").strip()

    if not is_authorized(user_id):
        respond("Not authorized.", response_type="ephemeral")
        return
    if not text:
        respond(
            "Usage: `/claude <prompt>`\n"
            "Prefixes: `opus:`, `haiku:`, `sonnet:` (model), `think:` (reasoning mode)",
            response_type="ephemeral",
        )
        return

    # Post visible message to create a thread anchor
    result = slack_client.chat_postMessage(
        channel=channel_id,
        text=f":speech_balloon: <@{user_id}>: {text}",
    )
    thread_ts = result["ts"]

    # Route into the standard flow
    handle_user_message(user_id, channel_id, thread_ts, thread_ts, text)


@app.command("/claude-status")
def handle_claude_status_command(ack, command, respond):
    ack()
    if not is_authorized(command["user_id"]):
        respond("Not authorized.", response_type="ephemeral")
        return

    daily = db_get_daily_cost()
    total = db_get_total_cost()
    active = get_active_session_count()
    lines = [
        ":bar_chart: *Bot Status*",
        f"• Active sessions: *{active}*",
        f"• Today's cost: *${daily:.4f}*",
    ]
    if DAILY_BUDGET_USD > 0:
        remaining = max(0, DAILY_BUDGET_USD - daily)
        lines.append(f"• Daily budget: *${DAILY_BUDGET_USD:.2f}* (${remaining:.4f} remaining)")
    lines.append(f"• All-time cost: *${total:.4f}*")
    stats = db_get_feedback_stats()
    if stats["total_positive"] + stats["total_negative"] > 0:
        lines.append(f"• Satisfaction: *{stats['satisfaction_pct']:.0f}%* ({stats['total_positive']} :+1:  {stats['total_negative']} :-1:)")
    respond("\n".join(lines), response_type="ephemeral")


@app.command("/claude-cost")
def handle_claude_cost_command(ack, command, respond):
    ack()
    if not is_authorized(command["user_id"]):
        respond("Not authorized.", response_type="ephemeral")
        return

    daily = db_get_daily_cost()
    total = db_get_total_cost()
    lines = [
        ":moneybag: *Cost Summary*",
        f"• Today: *${daily:.4f}*",
        f"• All-time: *${total:.4f}*",
    ]
    if DAILY_BUDGET_USD > 0:
        remaining = max(0, DAILY_BUDGET_USD - daily)
        pct = (daily / DAILY_BUDGET_USD * 100) if DAILY_BUDGET_USD > 0 else 0
        lines.append(f"• Budget: *${DAILY_BUDGET_USD:.2f}* ({pct:.1f}% used, ${remaining:.4f} remaining)")
    respond("\n".join(lines), response_type="ephemeral")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global BOT_USER_ID, KNOWLEDGE_INDEX, KNOWLEDGE_INLINE, KNOWLEDGE_DIRS, MCP_TOOL_CATALOG, CODEBASE_INDEX, KNOWLEDGE_INDEX_FILE

    logger.info("Starting Slack Claude Code Bot...")
    logger.info(f"Max concurrent sessions: {MAX_CONCURRENT}")
    logger.info(f"Request timeout: {REQUEST_TIMEOUT}s")

    # Load knowledge base from configured paths
    if KNOWLEDGE_PATHS:
        KNOWLEDGE_INDEX, KNOWLEDGE_INLINE, KNOWLEDGE_DIRS = _build_knowledge_index(KNOWLEDGE_PATHS)
        if KNOWLEDGE_INDEX or KNOWLEDGE_INLINE:
            idx_count = KNOWLEDGE_INDEX.count("\n") - 2 if KNOWLEDGE_INDEX else 0
            logger.info(f"Knowledge loaded: {idx_count} files indexed, {len(KNOWLEDGE_INLINE)} chars inline, dirs: {KNOWLEDGE_DIRS}")
        else:
            logger.warning(f"No knowledge base loaded from: {KNOWLEDGE_PATHS}")
    else:
        logger.info("No KNOWLEDGE_PATHS configured. Set it to enable knowledge base.")

    # Build codebase & docs index (from KNOWLEDGE_PATHS or CLAUDE_WORK_DIR)
    index_paths = KNOWLEDGE_PATHS or [CLAUDE_WORK_DIR]
    CODEBASE_INDEX = _build_codebase_index(index_paths, CODEBASE_INDEX_MAX_SIZE)
    if CODEBASE_INDEX:
        sig_count = CODEBASE_INDEX.count("\n  ")
        logger.info(f"[CODEBASE INDEX] Built: ~{sig_count} signatures, {len(CODEBASE_INDEX)} chars")
    else:
        logger.info("[CODEBASE INDEX] No index built (no matching source files found)")

    # Write combined index to disk (not injected into system prompt — read on demand)
    if KNOWLEDGE_INDEX or KNOWLEDGE_INLINE or CODEBASE_INDEX:
        index_path = Path(CLAUDE_WORK_DIR).resolve() / ".knowledge-index.md"
        parts = ["# Knowledge & Codebase Index\n\nGenerated at startup. Use Read tool on the file paths listed below for details.\n"]
        if KNOWLEDGE_INLINE:
            parts.append(f"\n## Inline Knowledge\n\n{KNOWLEDGE_INLINE}\n")
        if KNOWLEDGE_INDEX:
            parts.append(f"\n## Knowledge File Index\n\n{KNOWLEDGE_INDEX}\n")
        if CODEBASE_INDEX:
            parts.append(f"\n## Codebase Signatures\n\n{CODEBASE_INDEX}\n")
        index_path.write_text("".join(parts), encoding="utf-8")
        KNOWLEDGE_INDEX_FILE = str(index_path)
        logger.info(f"[INDEX] Written to {KNOWLEDGE_INDEX_FILE} ({index_path.stat().st_size / 1024:.1f} KB)")

    # Discover MCP servers
    MCP_SERVER_NAMES.extend(_discover_mcp_server_names())
    if MCP_SERVER_NAMES:
        logger.info(f"MCP servers auto-approved: {', '.join(MCP_SERVER_NAMES)}")
    else:
        logger.info("No MCP servers found in settings.")

    # Discover MCP tool catalog (temporary session to enumerate all tools)
    if MCP_SERVER_NAMES:
        try:
            MCP_TOOL_CATALOG = asyncio.run(_discover_mcp_tools())
            if MCP_TOOL_CATALOG:
                tool_count = MCP_TOOL_CATALOG.count("  - `")
                logger.info(f"[MCP] Tool catalog cached: {tool_count} tools")
            else:
                logger.warning("[MCP] No tools discovered from MCP servers")
        except Exception as e:
            logger.warning(f"[MCP] Tool discovery failed: {e}")

    init_db()

    BOT_USER_ID = get_bot_user_id()
    logger.info(f"Bot user ID: {BOT_USER_ID}")
    logger.info(f"Allowed users: {ALLOWED_USER_IDS}")
    logger.info(f"Claude Code work dir: {CLAUDE_WORK_DIR}")
    logger.info(f"Allowed tools: {ALLOWED_TOOLS}")
    logger.info(f"Permission mode: {PERMISSION_MODE}")

    active_count = db_get_active_thread_count()
    logger.info(f"Active threads in DB: {active_count}")

    # Pre-flight: check Azure CLI auth
    import subprocess as _sp
    try:
        az_result = _sp.run(["az", "account", "show", "--query", "user.name", "-o", "tsv"], capture_output=True, text=True, timeout=10)
        if az_result.returncode == 0 and az_result.stdout.strip():
            logger.info(f"[AZURE] Authenticated as: {az_result.stdout.strip()}")
        else:
            logger.warning("[AZURE] Not authenticated! Run 'az login' before starting the bot for Azure DevOps MCP to work.")
    except Exception:
        logger.warning("[AZURE] 'az' CLI not found. Azure DevOps MCP tools may not work.")

    # Start background cleanup thread (idle sessions, stale locks, expired details)
    cleanup_thread = threading.Thread(target=_cleanup_idle_resources, daemon=True, name="resource-cleanup")
    cleanup_thread.start()
    logger.info(f"[CLEANUP] Background cleanup started (session idle timeout: {SESSION_IDLE_TIMEOUT}s)")

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Bot is running! Ctrl+C to stop.")
    handler.start()


if __name__ == "__main__":
    main()