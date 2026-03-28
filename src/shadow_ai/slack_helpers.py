"""
Slack message formatting, chunking, and helper utilities.
"""

import logging
import re
import threading
import time

logger = logging.getLogger("slack-claude-code")

# ─── Detail Storage (for Show Details button) ────────────────────────────────

_details_store: dict[str, str] = {}
_details_lock = threading.Lock()


def store_detail(content: str, thread_ts: str = "") -> str:
    """Store detail content and return a unique detail_id."""
    detail_id = f"detail_{thread_ts}_{int(time.time())}"
    with _details_lock:
        _details_store[detail_id] = content
    return detail_id


def pop_detail(detail_id: str) -> str | None:
    """Pop and return detail content by ID, or None if not found."""
    with _details_lock:
        return _details_store.pop(detail_id, None)


def get_details_store() -> dict[str, str]:
    """Return the raw details store dict (for cleanup use)."""
    return _details_store


def get_details_lock() -> threading.Lock:
    """Return the details lock (for cleanup use)."""
    return _details_lock


# ─── Markdown / Slack formatting ─────────────────────────────────────────────

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

    # 3. Headers -> bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 4. Bold: **text** or __text__ -> *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # 5. Strikethrough: ~~text~~ -> ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 6. Links: [text](url) -> <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # 7. Horizontal rules: --- or *** or ___ -> blank line (Slack doesn't render them)
    text = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", text, flags=re.MULTILINE)

    # 8. Collapse multiple consecutive blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 9. Restore code blocks and inline code
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINECODE{i}\x00", code)

    return text


# ─── Message cleaning / parsing ──────────────────────────────────────────────

def clean_message_text(text: str, bot_user_id: str) -> str:
    """Remove bot mentions and clean whitespace."""
    return re.sub(rf"<@{bot_user_id}>", "", text).strip()


def parse_model_prefix(text: str, model_aliases: dict | None = None) -> tuple[str | None, str]:
    """Parse optional model prefix: 'opus: review this' -> ('claude-opus-4-6', 'review this').

    Args:
        text: The raw message text.
        model_aliases: Mapping of prefix names to model IDs.
            Defaults to {"opus": "claude-opus-4-6", "haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-4-6"}.

    Returns:
        (model_id_or_None, remaining_text)
    """
    if model_aliases is None:
        model_aliases = {
            "opus": "claude-opus-4-6",
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
        }
    match = re.match(r'^(opus|haiku|sonnet):\s*', text, re.IGNORECASE)
    if match:
        return model_aliases[match.group(1).lower()], text[match.end():]
    return None, text


# ─── Chunking ────────────────────────────────────────────────────────────────

def chunk_message(text: str, max_length: int = 3900) -> list[str]:
    """Split text into chunks respecting paragraph/line boundaries."""
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


# ─── Slack message sending ───────────────────────────────────────────────────

def send_response_with_stop_button(slack_client, channel: str, thread_ts: str, text: str, bot_user_id: str = None):
    """Send a response message with optional Show Details and Stop Session buttons.

    Args:
        slack_client: Slack WebClient instance.
        channel: Slack channel ID.
        thread_ts: Thread timestamp.
        text: Response text (may contain ---DETAILS--- separator).
        bot_user_id: Bot user ID (unused currently, reserved for future use).
    """
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
        # Store details for retrieval on button click
        detail_id = store_detail(details, thread_ts)
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


def fetch_thread_messages(slack_client, channel: str, thread_ts: str, bot_user_id: str = None, since_ts: str = None) -> tuple[str, str | None]:
    """
    Fetch messages in a Slack thread, optionally only those after since_ts.
    Returns (formatted_context, latest_message_ts).

    Args:
        slack_client: Slack WebClient instance.
        channel: Slack channel ID.
        thread_ts: Thread timestamp.
        bot_user_id: Bot user ID to filter out bot messages.
        since_ts: Only return messages after this timestamp.
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
            msg_text = msg.get("text", "")
            if not msg_text.strip():
                continue
            # Skip bot messages
            if msg.get("bot_id") or (bot_user_id and user == bot_user_id):
                continue
            # Skip messages we've already sent as context
            if since_ts and msg_ts <= since_ts:
                continue
            formatted.append(f"<@{user}>: {msg_text}")
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


def send_session_ended(slack_client, channel: str, thread_ts: str):
    """Send the 'session ended' message."""
    slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":white_check_mark: *Session ended.* Mention me again to start a new one.",
    )
