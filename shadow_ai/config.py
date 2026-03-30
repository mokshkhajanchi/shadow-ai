"""
Bot configuration — all env var parsing, constants, and small utility functions.
Extracted from bot.py lines 14-137.
"""

import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


# ─── Pattern constants (compiled regexes, shared with knowledge module) ───────

_PY_PATTERNS = [
    re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\((.*)$"),
    re.compile(r"^\s*class\s+(\w+)(?:\s*\(.*\))?\s*:"),
]
_JS_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\("),
]

CODEBASE_PATTERNS = {
    ".py": _PY_PATTERNS,
    ".js": _JS_PATTERNS,
    ".ts": _JS_PATTERNS,
    ".jsx": _JS_PATTERNS,
    ".tsx": _JS_PATTERNS,
}

CODEBASE_INDEX_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx"}

INDEXABLE_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts",
}

SKIP_DIRS = {
    "backup", ".git", "__pycache__", "node_modules", ".venv",
    "docker", "dist", "build", ".next", "coverage", ".tox",
}

DOC_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".rst"}

ALL_INDEX_EXTENSIONS = CODEBASE_INDEX_EXTENSIONS | DOC_EXTENSIONS


# ─── Model aliases ────────────────────────────────────────────────────────────

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# ─── File upload limits ───────────────────────────────────────────────────────

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ─── Feedback reactions ───────────────────────────────────────────────────────

FEEDBACK_REACTIONS = {"+1": +1, "-1": -1, "tada": +1, "confused": -1}


# ─── Utility functions ────────────────────────────────────────────────────────

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


# ─── BotConfig dataclass ─────────────────────────────────────────────────────

@dataclass
class BotConfig:
    """All bot configuration, parsed from environment variables."""

    # Identity
    bot_username: str = ""  # e.g. "moksh" → "moksh.shadow.ai"

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)

    @property
    def bot_identity(self) -> str:
        """Full bot identity string, e.g. 'moksh.shadow.ai'."""
        return f"{self.bot_username}.shadow.ai" if self.bot_username else "shadow.ai"

    # Claude
    claude_work_dir: str = ""
    max_turns: int = 50
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent",
    ])
    permission_mode: str = "bypassPermissions"
    claude_model: str | None = None
    claude_thinking: str = "off"       # off | adaptive | enabled
    claude_thinking_budget: int = 10000

    # Paths
    db_path: str = "./shadow_ai.db"
    log_file: str = "./bot.log"

    # Timeouts / limits
    request_timeout: int = 3600   # 1 hour — supports long-running tasks
    max_concurrent: int = 5

    # Knowledge base
    knowledge_paths: list[str] = field(default_factory=list)
    knowledge_inline_threshold: int = 10_000   # 10KB per file
    knowledge_total_inline_limit: int = 20_000  # 20KB total inline budget
    knowledge_index_max_entries: int = 100

    # Budget
    daily_budget_usd: float = 500.0  # USD per day

    # GitNexus
    gitnexus_enabled: str = "auto"  # auto | on | off
    gitnexus_available: bool = False

    # Output
    verbose_progress: bool = False  # Show tool details in progress messages

    # Codebase indexing
    codebase_index_max_size: int = 50_000

    # Session management
    session_idle_timeout: int = 0          # 0 = disabled (no idle eviction)
    max_active_sessions: int = 3

    # NEW configurable fields
    system_prompt_file: str = ""           # path to custom system prompt .md file
    repo_paths: dict = field(default_factory=dict)        # JSON from REPO_PATHS
    repo_test_config: dict = field(default_factory=dict)  # JSON from REPO_TEST_CONFIG

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Build config by reading all environment variables. Calls load_dotenv()."""
        load_dotenv()

        knowledge_raw = os.environ.get("KNOWLEDGE_PATHS", "")
        knowledge_paths = [p.strip() for p in knowledge_raw.split(",") if p.strip()]

        # Auto-include knowledge/notes/ directory for curated knowledge
        notes_dir = os.path.join(os.getcwd(), "knowledge", "notes")
        if os.path.isdir(notes_dir) and notes_dir not in knowledge_paths:
            knowledge_paths.append(notes_dir)

        allowed_tools = os.environ.get(
            "CLAUDE_ALLOWED_TOOLS",
            "Read,Write,Edit,Bash,Glob,Grep,Agent",
        ).split(",")

        allowed_user_ids = os.environ.get("ALLOWED_USER_IDS", "").split(",")

        # Parse REPO_PATHS as JSON (default empty dict)
        repo_paths_raw = os.environ.get("REPO_PATHS", "{}")
        try:
            repo_paths = json.loads(repo_paths_raw)
        except (json.JSONDecodeError, TypeError):
            repo_paths = {}

        # Parse REPO_TEST_CONFIG as JSON (default empty dict)
        repo_test_config_raw = os.environ.get("REPO_TEST_CONFIG", "{}")
        try:
            repo_test_config = json.loads(repo_test_config_raw)
        except (json.JSONDecodeError, TypeError):
            repo_test_config = {}

        return cls(
            bot_username=os.environ.get("BOT_USERNAME", ""),
            slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
            slack_app_token=os.environ["SLACK_APP_TOKEN"],
            allowed_user_ids=allowed_user_ids,
            claude_work_dir=os.path.expanduser(
                os.environ.get("CLAUDE_WORK_DIR", "~/Projects"),
            ),
            max_turns=int(os.environ.get("CLAUDE_MAX_TURNS", "30")),
            allowed_tools=allowed_tools,
            permission_mode=os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits"),
            claude_model=os.environ.get("CLAUDE_MODEL", None),
            claude_thinking=os.environ.get("CLAUDE_THINKING", "off"),
            claude_thinking_budget=int(os.environ.get("CLAUDE_THINKING_BUDGET", "10000")),
            db_path=os.environ.get("DB_PATH", "./shadow_ai.db"),
            log_file=os.environ.get("LOG_FILE", "./bot.log"),
            request_timeout=int(os.environ.get("REQUEST_TIMEOUT", "600")),
            max_concurrent=int(os.environ.get("MAX_CONCURRENT", "5")),
            knowledge_paths=knowledge_paths,
            knowledge_inline_threshold=10_000,
            knowledge_total_inline_limit=20_000,
            knowledge_index_max_entries=100,
            daily_budget_usd=float(os.environ.get("DAILY_BUDGET_USD", "0")),
            gitnexus_enabled=os.environ.get("GITNEXUS_ENABLED", "auto"),
            gitnexus_available=False,
            codebase_index_max_size=int(os.environ.get("CODEBASE_INDEX_MAX_SIZE", "50000")),
            verbose_progress=os.environ.get("VERBOSE_PROGRESS", "").lower() in ("1", "true", "yes"),
            session_idle_timeout=int(os.environ.get("SESSION_IDLE_TIMEOUT", "0")),
            max_active_sessions=int(os.environ.get("MAX_ACTIVE_SESSIONS", "3")),
            system_prompt_file=os.environ.get("SYSTEM_PROMPT_FILE", ""),
            repo_paths=repo_paths,
            repo_test_config=repo_test_config,
        )
