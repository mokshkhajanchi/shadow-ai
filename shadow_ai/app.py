"""
Main entry point for the shadow.ai.

Orchestrates startup: config loading, logging, knowledge indexing,
MCP discovery, DB init, event registration, and SocketModeHandler launch.
"""

import asyncio
import atexit
import logging
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from shadow_ai.config import BotConfig
from shadow_ai.log import setup_logging
from shadow_ai.knowledge import (
    _build_knowledge_index,
    _build_codebase_index,
    _check_gitnexus_available,
)
from shadow_ai.mcp import discover_mcp_server_names, discover_mcp_tools
from shadow_ai.db import init_db, db_get_active_thread_count, db_stop_thread, db_is_active_thread
from shadow_ai.sessions import (
    _cleanup_idle_resources,
    _force_kill_all_sessions,
    _shutdown_handler,
)
from shadow_ai.slack_helpers import (
    _details_store,
    _details_lock,
)

logger = logging.getLogger("slack-claude-code")

# ─── Module-level globals (set once in main(), importable by other modules) ──

app: App | None = None
slack_client: WebClient | None = None

# Mutable startup state shared across modules
BOT_USER_ID: str | None = None
KNOWLEDGE_INDEX: str = ""
KNOWLEDGE_INLINE: str = ""
KNOWLEDGE_DIRS: list[str] = []
CODEBASE_INDEX: str = ""
KNOWLEDGE_INDEX_FILE: str = ""
MCP_SERVER_NAMES: list[str] = []
MCP_TOOL_CATALOG: str = ""
GITNEXUS_AVAILABLE: bool = False

# Thread pool and per-thread locks
executor: ThreadPoolExecutor | None = None
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


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    global app, slack_client, executor
    global BOT_USER_ID, KNOWLEDGE_INDEX, KNOWLEDGE_INLINE, KNOWLEDGE_DIRS
    global CODEBASE_INDEX, KNOWLEDGE_INDEX_FILE, MCP_TOOL_CATALOG, GITNEXUS_AVAILABLE

    # 1. Load config from environment
    config = BotConfig.from_env()

    # 2. Setup logging (rotating file + console)
    setup_logging(config.log_file)

    from shadow_ai import __version__, __author__
    print(f"\n  {config.bot_identity} v{__version__} | by {__author__}\n")
    logger.info(f"Starting shadow.ai v{__version__}...")
    logger.info(f"Max concurrent sessions: {config.max_concurrent}")
    logger.info(f"Request timeout: {config.request_timeout}s")

    # Migrate: move files from old knowledge/ paths to new root paths (v2.0 → v2.1)
    import shutil
    for dirname in ("agents", "skills", "channels", "workflows"):
        old_dir = Path("knowledge") / dirname
        new_dir = Path(dirname)
        if old_dir.is_dir() and any(old_dir.iterdir()):
            new_dir.mkdir(parents=True, exist_ok=True)
            for item in old_dir.iterdir():
                dest = new_dir / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
                    logger.info(f"[MIGRATE] Moved {old_dir / item.name} → {dest}")
            # Remove old dir if empty
            if not any(old_dir.iterdir()):
                old_dir.rmdir()
                logger.info(f"[MIGRATE] Removed empty {old_dir}")

    # Auto-create directories
    Path("knowledge/notes").mkdir(parents=True, exist_ok=True)
    Path("knowledge/conversations").mkdir(parents=True, exist_ok=True)
    Path("agents").mkdir(parents=True, exist_ok=True)
    Path("skills").mkdir(parents=True, exist_ok=True)
    Path("channels").mkdir(parents=True, exist_ok=True)
    Path("workflows").mkdir(parents=True, exist_ok=True)

    # Install skills into ~/.claude/skills/ for native Claude Code discovery
    from shadow_ai.skill_loader import install_skills_to_claude
    skills_dir = Path("skills")
    skill_count = install_skills_to_claude(skills_dir)
    logger.info(f"Skills symlinked to ~/.claude/skills/: {skill_count}")

    # 3. Create Slack Bolt App and WebClient
    app = App(token=config.slack_bot_token)
    slack_client = WebClient(token=config.slack_bot_token)

    # Thread pool for concurrent requests
    executor = ThreadPoolExecutor(
        max_workers=config.max_concurrent, thread_name_prefix="claude",
    )

    # 4. Get bot user ID first (needed for event registration)
    BOT_USER_ID = slack_client.auth_test()["user_id"]
    logger.info(f"Bot user ID: {BOT_USER_ID}")

    # 5. Register event handlers EARLY (before slow MCP discovery)
    #    so events arriving during startup are not dropped.
    #    MCP catalog/knowledge will be empty initially — they're updated in-place later.
    from shadow_ai.events import register_events
    from shadow_ai.claude_options import create_options
    register_events(
        app, config, slack_client, executor, BOT_USER_ID,
        get_thread_lock_fn=get_thread_lock,
        remove_thread_lock_fn=remove_thread_lock,
        mcp_server_names=MCP_SERVER_NAMES,
        mcp_tool_catalog=MCP_TOOL_CATALOG,
        knowledge_index_file=KNOWLEDGE_INDEX_FILE,
        knowledge_dirs=KNOWLEDGE_DIRS,
        repo_paths=config.repo_paths,
        repo_test_config=config.repo_test_config,
        create_options_fn=create_options,
    )

    # 6. Build knowledge index from configured paths
    if config.knowledge_paths:
        KNOWLEDGE_INDEX, KNOWLEDGE_INLINE, KNOWLEDGE_DIRS = _build_knowledge_index(
            config.knowledge_paths,
            config.knowledge_inline_threshold,
            config.knowledge_total_inline_limit,
            config.knowledge_index_max_entries,
        )
        if KNOWLEDGE_INDEX or KNOWLEDGE_INLINE:
            idx_count = KNOWLEDGE_INDEX.count("\n") - 2 if KNOWLEDGE_INDEX else 0
            logger.info(
                f"Knowledge loaded: {idx_count} files indexed, "
                f"{len(KNOWLEDGE_INLINE)} chars inline, dirs: {KNOWLEDGE_DIRS}"
            )
        else:
            logger.warning(f"No knowledge base loaded from: {config.knowledge_paths}")
    else:
        logger.info("No KNOWLEDGE_PATHS configured. Set it to enable knowledge base.")

    # 5. Check GitNexus availability (primary code intelligence)
    if config.gitnexus_enabled != "off":
        GITNEXUS_AVAILABLE = _check_gitnexus_available()
        if GITNEXUS_AVAILABLE:
            logger.info("[GITNEXUS] Available -- using as primary code intelligence")
        else:
            logger.info("[GITNEXUS] Not available -- using regex codebase index as fallback")

    # 6. Build codebase & docs index (skip if GitNexus provides on-demand intelligence)
    index_paths = config.knowledge_paths or [config.claude_work_dir]
    if not GITNEXUS_AVAILABLE:
        CODEBASE_INDEX = _build_codebase_index(index_paths, config.codebase_index_max_size)
        if CODEBASE_INDEX:
            sig_count = CODEBASE_INDEX.count("\n  ")
            logger.info(f"[CODEBASE INDEX] Built: ~{sig_count} signatures, {len(CODEBASE_INDEX)} chars")
        else:
            logger.info("[CODEBASE INDEX] No index built (no matching source files found)")
    else:
        logger.info("[CODEBASE INDEX] Skipped -- GitNexus provides on-demand code intelligence")

    # 7. Write combined knowledge index to disk (read on demand, not injected into system prompt)
    if KNOWLEDGE_INDEX or KNOWLEDGE_INLINE or CODEBASE_INDEX:
        index_path = Path(config.claude_work_dir).resolve() / ".knowledge-index.md"
        parts = [
            "# Knowledge & Codebase Index\n\n"
            "Generated at startup. Use Read tool on the file paths listed below for details.\n"
        ]
        if KNOWLEDGE_INLINE:
            parts.append(f"\n## Inline Knowledge\n\n{KNOWLEDGE_INLINE}\n")
        if KNOWLEDGE_INDEX:
            parts.append(f"\n## Knowledge File Index\n\n{KNOWLEDGE_INDEX}\n")
        if CODEBASE_INDEX:
            parts.append(f"\n## Codebase Signatures\n\n{CODEBASE_INDEX}\n")
        index_path.write_text("".join(parts), encoding="utf-8")
        KNOWLEDGE_INDEX_FILE = str(index_path)
        logger.info(f"[INDEX] Written to {KNOWLEDGE_INDEX_FILE} ({index_path.stat().st_size / 1024:.1f} KB)")

    # 8. Discover MCP servers and enumerate their tools
    MCP_SERVER_NAMES.extend(discover_mcp_server_names(config.claude_work_dir))
    if MCP_SERVER_NAMES:
        logger.info(f"MCP servers auto-approved: {', '.join(MCP_SERVER_NAMES)}")
    else:
        logger.info("No MCP servers found in settings.")

    if MCP_SERVER_NAMES:
        try:
            MCP_TOOL_CATALOG = asyncio.run(
                discover_mcp_tools(config.claude_work_dir, config.permission_mode)
            )
            if MCP_TOOL_CATALOG:
                tool_count = MCP_TOOL_CATALOG.count("  - `")
                logger.info(f"[MCP] Tool catalog cached: {tool_count} tools")
            else:
                logger.warning("[MCP] No tools discovered from MCP servers")
        except Exception as e:
            logger.warning(f"[MCP] Tool discovery failed: {e}")

    # 9. Initialize SQLite database
    init_db(config.db_path)

    # 10. Log config summary
    logger.info(f"Allowed users: {config.allowed_user_ids}")
    logger.info(f"Claude Code work dir: {config.claude_work_dir}")
    logger.info(f"Allowed tools: {config.allowed_tools}")
    logger.info(f"Permission mode: {config.permission_mode}")

    active_count = db_get_active_thread_count(config.db_path)
    logger.info(f"Active threads in DB: {active_count}")

    # Pre-flight: check Azure CLI auth
    try:
        az_result = subprocess.run(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
            capture_output=True, text=True, timeout=10,
        )
        if az_result.returncode == 0 and az_result.stdout.strip():
            logger.info(f"[AZURE] Authenticated as: {az_result.stdout.strip()}")
        else:
            logger.warning("[AZURE] Not authenticated! Run 'az login' before starting the bot for Azure DevOps MCP to work.")
    except Exception:
        logger.warning("[AZURE] 'az' CLI not found. Azure DevOps MCP tools may not work.")

    # 11b. Initialize sessions module with db callback
    from shadow_ai.sessions import init_sessions
    init_sessions(
        db_stop_thread_fn=lambda ts: db_stop_thread(config.db_path, ts),
        remove_thread_lock_fn=remove_thread_lock,
        max_active_sessions=config.max_active_sessions,
    )

    # 12. Start background cleanup thread (idle sessions, stale locks, expired details)
    cleanup_thread = threading.Thread(
        target=_cleanup_idle_resources,
        kwargs={
            "session_idle_timeout": config.session_idle_timeout,
            "get_details_store": lambda: _details_store,
            "get_details_lock": lambda: _details_lock,
            "remove_thread_lock_fn": remove_thread_lock,
            "get_thread_locks_keys_fn": lambda: list(_thread_locks.keys()),
            "db_is_active_thread_fn": lambda ts: db_is_active_thread(config.db_path, ts),
        },
        daemon=True,
        name="resource-cleanup",
    )
    cleanup_thread.start()
    timeout_msg = "disabled" if config.session_idle_timeout == 0 else f"{config.session_idle_timeout}s"
    logger.info(f"[CLEANUP] Background cleanup started (session idle timeout: {timeout_msg})")

    # 13. Register signal handlers (SIGTERM, SIGHUP) and atexit for clean shutdown
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGHUP, _shutdown_handler)
    atexit.register(_force_kill_all_sessions)
    logger.info("[SHUTDOWN] Signal handlers and atexit registered for clean process cleanup")

    # 14. Start Slack SocketModeHandler (blocks until Ctrl+C / signal)
    handler = SocketModeHandler(app, config.slack_app_token)
    logger.info("Bot is running! Ctrl+C to stop.")
    handler.start()
