"""Prerequisite checker for shadow.ai — validates setup before running."""

import json
import os
import shutil
import sys
from pathlib import Path


def _check(label: str, ok: bool, detail: str, fix: str = "") -> bool:
    mark = "\u2713" if ok else "\u2717"
    status = f"  {mark}  {label:<22s}{detail}"
    print(status)
    if not ok and fix:
        print(f"     \u2192 {fix}")
    return ok


def run_doctor():
    """Check all prerequisites and configuration."""
    from shadow_ai import __version__

    print()
    print("  shadow.ai doctor")
    print("  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    print()

    all_ok = True

    # 1. Python version
    v = sys.version_info
    ok = v >= (3, 11)
    all_ok &= _check("Python 3.11+", ok, f"{v.major}.{v.minor}.{v.micro}",
                      "Install Python 3.11+ via pyenv, brew, or deadsnakes")

    # 2. Claude Code CLI
    claude_path = shutil.which("claude")
    ok = claude_path is not None
    all_ok &= _check("Claude Code CLI", ok,
                      f"found at {claude_path}" if ok else "not found",
                      "npm install -g @anthropic-ai/claude-code")

    # 3. Package installed
    all_ok &= _check("shadow-ai package", True, f"v{__version__}")

    # 4. .env file
    env_exists = Path(".env").exists()
    all_ok &= _check(".env configuration", env_exists,
                      "found" if env_exists else "not found",
                      "Run `shadow-ai init` to create it")

    # 5. Required env vars (only check if .env exists)
    if env_exists:
        from dotenv import load_dotenv
        load_dotenv()
        required = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"]
        missing = [v for v in required if not os.environ.get(v)]
        ok = len(missing) == 0
        detail = f"{len(required)} required vars set" if ok else f"missing: {', '.join(missing)}"
        all_ok &= _check("  Required vars", ok, detail,
                         "Edit .env or re-run `shadow-ai init`")
    else:
        all_ok &= _check("  Required vars", False, "skipped (.env missing)",
                         "Run `shadow-ai init` first")

    # 6. knowledge/ directory
    knowledge_dir = Path("knowledge/learned")
    ok = knowledge_dir.exists()
    all_ok &= _check("knowledge/", ok,
                      "directory exists" if ok else "not found",
                      "Run `shadow-ai init` or `mkdir -p knowledge/learned`")

    # 7. MCP servers
    mcp_count = 0
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
            mcp_count = len(data.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError):
            pass
    detail = f"{mcp_count} server{'s' if mcp_count != 1 else ''} configured" if mcp_count > 0 else "none (optional)"
    _check("MCP servers", True, detail)  # always passes — MCP is optional

    print()
    if all_ok:
        print("  All checks passed. Run `shadow-ai` to start the bot.")
    else:
        print("  Some checks failed. Fix the issues above and re-run `shadow-ai doctor`.")
    print()
