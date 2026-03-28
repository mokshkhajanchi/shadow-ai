"""
Session management for Claude Code SDK clients.

Manages in-memory active sessions (ClaudeSDKClient instances),
with eviction, cleanup, force-kill, signal handling, and orphan reaping.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

logger = logging.getLogger("slack-claude-code")

# ─── Module-level state ──────────────────────────────────────────────────────

# Callbacks and config set via init_sessions() — avoids circular import and db_path coupling
_db_stop_thread_fn = None
_remove_thread_lock_fn = None
_max_active_sessions = 3


def init_sessions(db_stop_thread_fn=None, remove_thread_lock_fn=None, max_active_sessions=3):
    """Initialize the sessions module with required callbacks and config."""
    global _db_stop_thread_fn, _remove_thread_lock_fn, _max_active_sessions
    _db_stop_thread_fn = db_stop_thread_fn
    _remove_thread_lock_fn = remove_thread_lock_fn
    _max_active_sessions = max_active_sessions

active_sessions: dict[str, dict] = {}
session_lock = threading.Lock()

_shutting_down = False


# ─── Session CRUD ─────────────────────────────────────────────────────────────

def get_session(thread_ts: str) -> dict | None:
    with session_lock:
        return active_sessions.get(thread_ts)


def store_session(thread_ts: str, sdk_client, loop, cli_pid: int = None):
    """Store a session. Evicts the oldest if at capacity."""
    if get_active_session_count() >= _max_active_sessions:
        _evict_oldest_session()

    with session_lock:
        active_sessions[thread_ts] = {
            "client": sdk_client,
            "loop": loop,
            "cli_pid": cli_pid,
            "created_at": datetime.now().isoformat(),
            "last_activity": datetime.now().isoformat(),
        }
    logger.info(f"[SESSION] Stored session for thread {thread_ts} (PID: {cli_pid}, total: {len(active_sessions)})")


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
        cli_pid = session.get("cli_pid")
        logger.info(f"[SESSION] Removed session for thread {thread_ts} (PID: {cli_pid}, remaining: {len(active_sessions)})")
        disconnected = False
        try:
            loop = session.get("loop")
            sdk_client = session.get("client")
            if loop and sdk_client and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(sdk_client.disconnect(), loop)
                future.result(timeout=5)  # Wait up to 5 seconds
                disconnected = True
        except Exception as e:
            logger.warning(f"Error disconnecting {thread_ts}: {type(e).__name__}: {e}")

        # Force-kill if graceful disconnect failed or timed out
        if not disconnected and cli_pid:
            logger.warning(f"[SESSION] Force-killing session {thread_ts} (PID: {cli_pid})")
            _force_kill_process(cli_pid)


def get_active_session_count() -> int:
    with session_lock:
        return len(active_sessions)


def kill_all_sessions(remove_thread_lock_fn=None) -> int:
    """Kill all active Claude Code sessions. Returns the number of sessions killed."""
    # Use module-level callback as fallback
    _rtl_fn = remove_thread_lock_fn or _remove_thread_lock_fn
    with session_lock:
        sessions_to_kill = dict(active_sessions)
        active_sessions.clear()

    count = len(sessions_to_kill)
    logger.info(f"[KILL ALL] Killing {count} active sessions")

    for thread_ts, session in sessions_to_kill.items():
        cli_pid = session.get("cli_pid")
        disconnected = False
        try:
            loop = session.get("loop")
            sdk_client = session.get("client")
            if loop and sdk_client and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(sdk_client.disconnect(), loop)
                future.result(timeout=5)
                disconnected = True
        except Exception as e:
            logger.warning(f"[KILL ALL] Error disconnecting {thread_ts}: {type(e).__name__}: {e}")

        if not disconnected and cli_pid:
            logger.warning(f"[KILL ALL] Force-killing {thread_ts} (PID: {cli_pid})")
            _force_kill_process(cli_pid)

        if _db_stop_thread_fn:
            _db_stop_thread_fn(thread_ts)
        if _rtl_fn:
            _rtl_fn(thread_ts)

    logger.info(f"[KILL ALL] Done. Killed {count} sessions.")
    return count


# ─── Eviction ─────────────────────────────────────────────────────────────────

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
    if _db_stop_thread_fn:
        _db_stop_thread_fn(oldest_ts)


# ─── Process management ──────────────────────────────────────────────────────

def _get_cli_pid(sdk_client) -> int | None:
    """Safely extract the CLI subprocess PID from an SDK client."""
    try:
        return sdk_client._transport._process.pid
    except Exception:
        return None


def _force_kill_process(pid: int):
    """Force-kill a process by PID. SIGTERM -> wait -> SIGKILL. Avoids killpg to prevent killing the bot's own process group."""
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        # Brief wait for graceful shutdown
        time.sleep(1)
        # Check if still alive, escalate to SIGKILL
        try:
            os.kill(pid, 0)  # Signal 0 = check if process exists
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Already dead
    except ProcessLookupError:
        pass  # Process already gone
    except PermissionError:
        logger.warning(f"[FORCE KILL] Permission denied for PID {pid}")
    except Exception as e:
        logger.warning(f"[FORCE KILL] Error killing PID {pid}: {e}")


# ─── Shutdown / signal handling ───────────────────────────────────────────────

def _force_kill_all_sessions():
    """Synchronous force-kill of all tracked sessions. Safe for signal handlers and atexit."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True

    try:
        with session_lock:
            sessions_snapshot = dict(active_sessions)
            active_sessions.clear()
    except Exception:
        return

    for thread_ts, session in sessions_snapshot.items():
        cli_pid = session.get("cli_pid")
        if cli_pid:
            _force_kill_process(cli_pid)

    # Also try graceful disconnect where possible
    for thread_ts, session in sessions_snapshot.items():
        try:
            loop = session.get("loop")
            sdk_client = session.get("client")
            if loop and sdk_client and loop.is_running():
                asyncio.run_coroutine_threadsafe(sdk_client.disconnect(), loop)
        except Exception:
            pass


def _shutdown_handler(signum, frame):
    """Signal handler for graceful shutdown."""
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    logger.info(f"[SHUTDOWN] Received {sig_name}, cleaning up sessions...")
    _force_kill_all_sessions()
    sys.exit(0)


# ─── Orphan reaping ──────────────────────────────────────────────────────────

def _reap_orphan_processes():
    """No-op. Reaper was removed — it killed active sessions due to unreliable PID tracking."""
    pass


# ─── Background cleanup ──────────────────────────────────────────────────────

def _cleanup_idle_resources(
    session_idle_timeout: int = 0,
    details_ttl: int = 86400,
    get_details_store=None,
    get_details_lock=None,
    db_is_active_thread_fn=None,
    remove_thread_lock_fn=None,
    get_thread_locks_keys_fn=None,
):
    """Background cleanup: idle sessions, stale thread locks, expired details.

    Args:
        session_idle_timeout: Seconds of idle time before a session is killed (0 = disabled).
        details_ttl: TTL in seconds for detail store entries.
        get_details_store: Callable returning the _details_store dict.
        get_details_lock: Callable returning the _details_lock threading.Lock.
        db_is_active_thread_fn: Callable(thread_ts) -> bool.
        remove_thread_lock_fn: Callable(thread_ts) to remove a thread lock.
        get_thread_locks_keys_fn: Callable() -> list[str] returning current thread lock keys.
    """
    while True:
        time.sleep(300)  # Run every 5 minutes
        now = datetime.now()
        try:
            # 1. Kill idle sessions (skip if timeout is 0/disabled, or if session is mid-request)
            if session_idle_timeout > 0:
                with session_lock:
                    idle_threads = []
                    for ts, session in active_sessions.items():
                        if session.get("processing"):
                            continue  # Don't kill sessions mid-request
                        created = session.get("last_activity") or session.get("created_at", "")
                        try:
                            session_time = datetime.fromisoformat(created)
                            if (now - session_time).total_seconds() > session_idle_timeout:
                                idle_threads.append(ts)
                        except (ValueError, TypeError):
                            pass

                for ts in idle_threads:
                    logger.info(f"[CLEANUP] Killing idle session: {ts}")
                    remove_session(ts)
                    db_stop_thread(ts)
                    if remove_thread_lock_fn:
                        remove_thread_lock_fn(ts)

                if idle_threads:
                    logger.info(f"[CLEANUP] Killed {len(idle_threads)} idle sessions")

            # 2. Cleanup orphaned thread locks (no active session and no DB activity)
            if get_thread_locks_keys_fn and db_is_active_thread_fn and remove_thread_lock_fn:
                lock_keys = get_thread_locks_keys_fn()
                stale = 0
                for ts in lock_keys:
                    if not get_session(ts) and not db_is_active_thread_fn(ts):
                        remove_thread_lock_fn(ts)
                        stale += 1
                if stale:
                    logger.info(f"[CLEANUP] Removed {stale} stale thread locks")

            # 3. Cleanup expired details store entries
            if get_details_store and get_details_lock:
                details_store = get_details_store()
                details_lock = get_details_lock()
                cutoff = time.time() - details_ttl
                with details_lock:
                    expired = [k for k in details_store if k.rsplit("_", 1)[-1].isdigit() and int(k.rsplit("_", 1)[-1]) < cutoff]
                    for k in expired:
                        del details_store[k]
                if expired:
                    logger.info(f"[CLEANUP] Evicted {len(expired)} expired detail entries")

        except Exception as e:
            logger.warning(f"[CLEANUP] Error: {type(e).__name__}: {e}")
