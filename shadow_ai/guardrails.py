"""Code-level security guardrails for monitored channel sessions.

Provides a `can_use_tool` callback that inspects every tool call and
blocks dangerous operations before they execute.
"""

import logging
import re

logger = logging.getLogger("slack-claude-code")

# ─── Dangerous patterns ──────────────────────────────────────────────────────

# Bash commands that are always blocked
BLOCKED_BASH_PATTERNS = [
    # Destructive file operations
    r"\brm\s+(-[rRf]+\s+|--recursive|--force)",
    r"\brm\b.*\s+/",              # rm anything starting with /
    r"\brmdir\b",
    r"\bshred\b",
    r"\bmkfs\b",
    r"\bdd\b\s+.*of=/",
    # Git destructive operations
    r"\bgit\s+push\s+.*--force",
    r"\bgit\s+push\s+-f\b",
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+branch\s+-[dD]\b",
    r"\bgit\s+push\s+.*--delete",
    r"\bgit\s+rebase\s+-i\b",
    r"\bgit\s+filter-branch\b",
    # Database destructive
    r"\bDROP\s+(TABLE|DATABASE|INDEX)\b",
    r"\bDELETE\s+FROM\b",
    r"\bTRUNCATE\b",
    # System modification
    r"\bchmod\s+777\b",
    r"\bchown\b.*\s+/",
    r"\bsudo\b",
    r"\bsu\s+-\b",
    # Process killing
    r"\bkill\s+-9\b",
    r"\bkillall\b",
    r"\bpkill\b",
    # Package management (prevent installs)
    r"\bpip\s+install\b",
    r"\bnpm\s+install\b",
    r"\byarn\s+add\b",
    r"\bbrew\s+install\b",
    r"\bapt\s+install\b",
    r"\bapt-get\s+install\b",
    # Network exfiltration
    r"\bcurl\b.*\b(POST|PUT|PATCH|DELETE)\b",
    r"\bwget\b",
    r"\bnc\s+-",                   # netcat
    r"\bncat\b",
    r"\bssh\b\s+",                 # SSH connections
    r"\bscp\b\s+",
    r"\brsync\b.*:",
    # Background processes
    r"\bnohup\b",
    r"&\s*$",                      # background process
    r"\bscreen\b",
    r"\btmux\b",
]

# Files/paths that should never be read
BLOCKED_READ_PATTERNS = [
    r"\.env$",
    r"\.env\.",
    r"/\.ssh/",
    r"/\.aws/",
    r"/\.kube/",
    r"/\.config/gcloud/",
    r"/\.netrc$",
    r"/\.pgpass$",
    r"/\.docker/config\.json$",
    r"credentials\.json$",
    r"secrets\.ya?ml$",
    r"id_rsa",
    r"id_ed25519",
    r"\.pem$",
    r"\.key$",
]

# File paths that should never be written to
BLOCKED_WRITE_PATTERNS = [
    r"^/etc/",
    r"^/usr/",
    r"^/System/",
    r"/\.bashrc$",
    r"/\.zshrc$",
    r"/\.bash_profile$",
    r"/\.profile$",
    r"/\.ssh/",
    r"/\.aws/",
    r"/\.kube/",
    r"/\.env$",
    r"/\.env\.",
]

# Tools that are completely blocked
BLOCKED_TOOLS = {
    "WebFetch",
    "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__computer",
    "mcp__claude-in-chrome__javascript_tool",
    "mcp__claude-in-chrome__get_page_text",
    "mcp__claude-in-chrome__read_page",
    "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__form_input",
    "mcp__playwright__navigate",
}

_COMPILED_BASH = [re.compile(p, re.IGNORECASE) for p in BLOCKED_BASH_PATTERNS]
_COMPILED_READ = [re.compile(p) for p in BLOCKED_READ_PATTERNS]
_COMPILED_WRITE = [re.compile(p) for p in BLOCKED_WRITE_PATTERNS]


# Safe patterns that override blocks (e.g. cleanup of temp dirs)
SAFE_BASH_PATTERNS = [
    re.compile(r"^rm\s+-rf?\s+/tmp/pr-review-", re.IGNORECASE),
]


def _check_bash_command(command: str) -> str | None:
    """Check if a bash command is dangerous. Returns reason if blocked, None if ok."""
    # Check safe overrides first
    for safe in SAFE_BASH_PATTERNS:
        if safe.search(command):
            return None
    for pattern in _COMPILED_BASH:
        if pattern.search(command):
            return f"Blocked dangerous command pattern: {pattern.pattern}"
    return None


def _check_file_read(file_path: str) -> str | None:
    """Check if a file path is sensitive. Returns reason if blocked, None if ok."""
    for pattern in _COMPILED_READ:
        if re.search(pattern, file_path):
            return f"Blocked read of sensitive file: {file_path}"
    return None


def _check_file_write(file_path: str) -> str | None:
    """Check if a file path should never be written to. Returns reason if blocked, None if ok."""
    for pattern in _COMPILED_WRITE:
        if re.search(pattern, file_path):
            return f"Blocked write to protected path: {file_path}"
    return None


async def monitored_tool_guard(tool_name: str, tool_input: dict, context) -> object:
    """can_use_tool callback for monitored channel sessions.

    Inspects every tool call and blocks dangerous operations.
    Returns PermissionResultAllow or PermissionResultDeny.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    # Completely blocked tools
    if tool_name in BLOCKED_TOOLS:
        logger.warning(f"[GUARDRAIL] Blocked tool: {tool_name}")
        return PermissionResultDeny(
            behavior="deny",
            message=f"Tool '{tool_name}' is not available in monitored channels.",
            interrupt=False,
        )

    # Block any browser-related tool
    if "chrome" in tool_name.lower() or "playwright" in tool_name.lower():
        logger.warning(f"[GUARDRAIL] Blocked browser tool: {tool_name}")
        return PermissionResultDeny(
            behavior="deny",
            message="Browser tools are not available in monitored channels.",
            interrupt=False,
        )

    # Bash command inspection
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        reason = _check_bash_command(command)
        if reason:
            logger.warning(f"[GUARDRAIL] {reason} | command: {command[:200]}")
            return PermissionResultDeny(
                behavior="deny",
                message=reason,
                interrupt=False,
            )

    # File read inspection
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        reason = _check_file_read(file_path)
        if reason:
            logger.warning(f"[GUARDRAIL] {reason}")
            return PermissionResultDeny(
                behavior="deny",
                message=reason,
                interrupt=False,
            )

    # File write/edit inspection
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        reason = _check_file_write(file_path)
        if reason:
            logger.warning(f"[GUARDRAIL] {reason}")
            return PermissionResultDeny(
                behavior="deny",
                message=reason,
                interrupt=False,
            )

    # Allow everything else
    return PermissionResultAllow(behavior="allow")
