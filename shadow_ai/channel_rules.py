"""Channel rules: file discovery and `## When to invoke` extraction.

Monitored channels MUST have a `channels/<name>.md` file containing a
`## When to invoke` section. This section is extracted and injected into
the monitored-channel system prompt as the AUTHORITATIVE rule for whether
the bot engages with a given message. When Claude reads the rules and
decides the message doesn't match, it emits `NO_RESPONSE`, which the
handler suppresses fully — no Slack post, no reactions.

There is no separate classifier call. Rules gating happens inside the
normal Claude invocation via the prompt, leveraging the existing
NO_RESPONSE suppression path.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger("slack-claude-code")

# Match "## When to invoke" (any case/spacing) and capture everything up to
# the next same-or-higher heading or EOF.
_INVOKE_HEADING_RE = re.compile(
    r"^##\s+When\s+to\s+invoke\s*$(.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def find_channel_rules_file(channel_name: str, claude_work_dir: str) -> Path | None:
    """Locate a channels/<channel_name>.md file in the standard candidate
    locations. Returns None if nothing matches."""
    if not channel_name:
        return None
    repo_root = Path(__file__).parent.parent
    candidates = [
        Path(claude_work_dir).expanduser() / "channels" / f"{channel_name}.md",
        Path("channels") / f"{channel_name}.md",
        repo_root / "channels" / f"{channel_name}.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def read_channel_rules(channel_name: str, claude_work_dir: str) -> str:
    """Read the raw markdown rules for a channel; '' if no file or unreadable."""
    rules_file = find_channel_rules_file(channel_name, claude_work_dir)
    if not rules_file:
        return ""
    try:
        return rules_file.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"[RULES] Failed to read {rules_file}: {e}")
        return ""


def extract_invoke_rules(rules_text: str) -> str | None:
    """Pull the `## When to invoke` section body from a channel rules file.

    Returns the section's prose stripped of whitespace, or None if the
    section is missing or empty.
    """
    if not rules_text:
        return None
    match = _INVOKE_HEADING_RE.search(rules_text)
    if not match:
        return None
    body = match.group(1).strip()
    return body or None
