"""Local Claude Code session discovery and resolution.

Claude Code stores every session as a JSONL transcript at
``~/.claude/projects/<cwd-hash>/<session_id>.jsonl``. Each transcript records
the ``cwd`` it ran in, the ``gitBranch``, timestamps, an optional ``summary``,
and the full message history.

This module scans those transcripts so shadow.ai can let a user *resume any
local session* and continue it from Slack (e.g. "fix the PR comments on the
feature I built locally"). The resume itself is handled by the existing
SDK-native ``resume=`` path in ``claude_runner`` — this module only finds the
right ``session_id`` and the ``cwd`` it must run in.

Pure stdlib, no SDK dependency, so it's cheap (no LLM cost) and easy to test.
"""

import json
import logging
import os
from dataclasses import dataclass
from glob import glob
from pathlib import Path

logger = logging.getLogger("slack-claude-code")

# Where Claude Code keeps per-project session transcripts.
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


@dataclass
class LocalSession:
    """A discovered local Claude Code session transcript."""

    session_id: str
    cwd: str | None
    git_branch: str | None
    mtime: float
    summary: str | None
    first_user_msg: str | None
    path: str

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def label(self) -> str:
        """The most useful one-line description of what this session was about."""
        return self.summary or self.first_user_msg or "(no description)"


def _extract_first_user_text(message: dict) -> str | None:
    """Pull plain text out of a transcript ``user`` message (str or block list)."""
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    return text
    return None


def _parse_transcript(path: str) -> LocalSession | None:
    """Parse one ``.jsonl`` transcript into a LocalSession.

    Reads line-by-line and stops scanning for metadata once everything useful
    has been found, so large transcripts don't have to be read in full.
    """
    session_id = Path(path).stem
    cwd = git_branch = summary = first_user = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                # Use the FIRST cwd seen — that's the directory Claude Code
                # indexes the session under (the project-dir hash). Claude may
                # `cd` elsewhere mid-session, but resuming must launch from the
                # original cwd or the SDK can't find the session id.
                if cwd is None:
                    cwd = record.get("cwd") or None
                if git_branch is None:
                    git_branch = record.get("gitBranch") or None
                if record.get("type") == "summary" and not summary:
                    summary = (record.get("summary") or "").strip() or None
                if record.get("type") == "user" and not first_user:
                    msg = record.get("message")
                    if isinstance(msg, dict):
                        first_user = _extract_first_user_text(msg)

                # Stop early once we have all the metadata we care about.
                if cwd and git_branch and summary and first_user:
                    break
    except OSError as e:
        logger.warning(f"[LOCAL-SESSIONS] Could not read {path}: {e}")
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    if first_user and len(first_user) > 200:
        first_user = first_user[:197] + "..."
    if summary and len(summary) > 200:
        summary = summary[:197] + "..."

    return LocalSession(
        session_id=session_id,
        cwd=cwd,
        git_branch=git_branch,
        mtime=mtime,
        summary=summary,
        first_user_msg=first_user,
        path=path,
    )


def scan_local_sessions(filter_text: str = "", limit: int = 0) -> list[LocalSession]:
    """Discover local Claude Code sessions, newest first.

    Args:
        filter_text: case-insensitive substring; keeps sessions whose cwd,
            branch, summary, or first message contains it. Empty = all.
        limit: cap the number returned (0 = no cap).

    Returns sessions sorted by transcript mtime, most recent first.
    """
    sessions: list[LocalSession] = []
    for path in glob(PROJECTS_GLOB):
        parsed = _parse_transcript(path)
        if parsed is not None:
            sessions.append(parsed)

    sessions.sort(key=lambda s: s.mtime, reverse=True)

    if filter_text:
        needle = filter_text.lower()
        sessions = [
            s for s in sessions
            if needle in (s.cwd or "").lower()
            or needle in (s.git_branch or "").lower()
            or needle in (s.summary or "").lower()
            or needle in (s.first_user_msg or "").lower()
        ]

    if limit > 0:
        sessions = sessions[:limit]
    return sessions


@dataclass
class ResolveResult:
    """Outcome of resolving a session reference (id or prefix)."""

    session: LocalSession | None = None          # set on a unique match
    ambiguous: list[LocalSession] | None = None   # set when a prefix matches >1
    error: str | None = None                      # set when nothing matched


def resolve_session(ref: str) -> ResolveResult:
    """Resolve a session reference (full id or unique prefix) to one session.

    - Exact ``session_id`` match always wins, even if it's also a prefix of others.
    - Otherwise, a unique prefix match resolves.
    - A prefix matching multiple sessions returns them in ``ambiguous`` so the
      caller can ask the user to disambiguate.
    - No match returns an ``error``.
    """
    # Slack often wraps a pasted id in a code span (`id`) or auto-links it;
    # strip that formatting so the raw session id survives.
    ref = (ref or "").strip().strip("`<>").strip()
    if not ref:
        return ResolveResult(error="No session id provided.")

    sessions = scan_local_sessions()

    for s in sessions:
        if s.session_id == ref:
            return ResolveResult(session=s)

    matches = [s for s in sessions if s.session_id.startswith(ref)]
    if not matches:
        return ResolveResult(error=f"No local session matches `{ref}`.")
    if len(matches) == 1:
        return ResolveResult(session=matches[0])
    return ResolveResult(ambiguous=matches)


def format_session_list(sessions: list[LocalSession]) -> str:
    """Render sessions as a Slack-friendly list (no markdown headers)."""
    if not sessions:
        return ":mag: No local Claude Code sessions found."

    from datetime import datetime

    lines = [":card_index_dividers: *Recent local Claude Code sessions:*"]
    for s in sessions:
        when = (
            datetime.fromtimestamp(s.mtime).strftime("%b %d %H:%M")
            if s.mtime else "?"
        )
        repo = Path(s.cwd).name if s.cwd else "?"
        branch = s.git_branch or "?"
        lines.append(
            f"• `{s.short_id}` · *{repo}* · `{branch}` · {when}\n    _{s.label}_"
        )
    lines.append("\nResume with: `resume <id> <your task>`")
    return "\n".join(lines)
