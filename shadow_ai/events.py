"""
Slack event handlers and action handlers.

Call ``register_events(app, ...)`` once at startup to wire everything up.
"""

import logging
import re
import time

from shadow_ai.config import BotConfig
from shadow_ai.channel_rules import (
    extract_invoke_rules,
    read_channel_rules,
)
from shadow_ai.db import (
    db_get_channel_name,
    db_get_daily_cost,
    db_get_recent_threads,
    db_get_total_cost,
    db_is_monitored_channel,
)
from shadow_ai.sessions import (
    get_active_session_count,
    kill_all_sessions,
)
from shadow_ai.slack_helpers import (
    chunk_message,
    markdown_to_slack,
    pop_detail,
)
from shadow_ai.handlers import handle_user_message

logger = logging.getLogger("slack-claude-code")


# ─── App Home dashboard ─────────────────────────────────────────────────────

def _render_app_home(user_id: str, slack_client, config: BotConfig):
    """Render the App Home tab with dashboard data."""
    db_path = config.db_path
    active_count = get_active_session_count()
    daily_cost = db_get_daily_cost(db_path)
    total_cost = db_get_total_cost(db_path)
    recent_threads = db_get_recent_threads(db_path, limit=10)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Claude Code Bot Dashboard"}},
    ]

    # Status section
    budget_text = ""
    if config.daily_budget_usd > 0:
        remaining = max(0, config.daily_budget_usd - daily_cost)
        budget_text = f"\n:moneybag: *Budget remaining:* ${remaining:.4f} / ${config.daily_budget_usd:.2f}"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f":robot_face: *Active Sessions:* {active_count}\n"
            f":chart_with_upwards_trend: *Today's Cost:* ${daily_cost:.4f}"
            f"{budget_text}\n"
            f":bank: *All-Time Cost:* ${total_cost:.4f}"
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


# ─── Registration ────────────────────────────────────────────────────────────

def register_events(
    app,
    config: BotConfig,
    slack_client,
    executor,
    bot_user_id: str,
    *,
    get_thread_lock_fn,
    remove_thread_lock_fn,
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    knowledge_dirs: list[str] = None,
    repo_paths: dict[str, str] = None,
    repo_test_config: dict[str, dict] = None,
    create_options_fn=None,
):
    """
    Register all Slack event, action, and command handlers on the given Bolt ``app``.

    Args:
        app: ``slack_bolt.App`` instance.
        config: Bot configuration dataclass.
        slack_client: ``slack_sdk.WebClient`` instance.
        executor: ``ThreadPoolExecutor`` for dispatching work.
        bot_user_id: The bot's own Slack user ID.
        get_thread_lock_fn: Callable(thread_ts) -> threading.Lock.
        remove_thread_lock_fn: Callable(thread_ts) to clean up a lock.
        mcp_server_names: List of discovered MCP server names.
        mcp_tool_catalog: Pre-built MCP tool catalog string.
        knowledge_index_file: Path to the on-disk knowledge index file.
        knowledge_dirs: Directories to add via SDK add_dirs.
        repo_paths: Mapping of repo names to local filesystem paths.
        repo_test_config: Mapping of repo names to test config dicts.
        create_options_fn: Function to create ClaudeAgentOptions.
    """
    db_path = config.db_path

    # Shared kwargs for every handle_user_message call
    _hum_kwargs = dict(
        config=config,
        slack_client=slack_client,
        executor=executor,
        get_thread_lock_fn=get_thread_lock_fn,
        bot_user_id=bot_user_id,
        mcp_server_names=mcp_server_names or [],
        mcp_tool_catalog=mcp_tool_catalog,
        knowledge_index_file=knowledge_index_file,
        knowledge_dirs=knowledge_dirs or [],
        repo_paths=repo_paths or {},
        repo_test_config=repo_test_config or {},
        create_options_fn=create_options_fn,
    )

    # ── app_mention: first contact ────────────────────────────────────────

    @app.event("app_mention")
    def _handle_mention(event, say):
        handle_user_message(
            user_id=event.get("user"),
            channel=event.get("channel"),
            thread_ts=event.get("thread_ts") or event.get("ts"),
            message_ts=event.get("ts"),
            text=event.get("text", ""),
            files=event.get("files"),
            **_hum_kwargs,
        )

    # ── Noise filter for monitored channels ────────────────────────────

    _NOISE_WORDS = {
        "ok", "okay", "thanks", "thank you", "thx", "ty", "got it", "cool",
        "nice", "great", "sure", "yes", "no", "yep", "nope", "done", "np",
        "ack", "acknowledged", "noted", "lgtm", "wfm", "sg", "roger",
    }

    def _is_noise(text: str) -> bool:
        """Return True if the message is noise (not worth replying to)."""
        t = text.strip().lower()
        if len(t) < 5:
            return True
        # Emoji-only messages
        if re.match(r'^[\s:+\-_a-z0-9]*$', t) and ':' in t:
            return True
        # Common acknowledgments
        if t in _NOISE_WORDS:
            return True
        # Just a URL with no surrounding text
        if re.match(r'^<https?://[^>]+>$', t):
            return True
        return False

    # ── message: follow-ups in tracked threads + DMs + monitored channels ──

    @app.event("message")
    def _handle_message(event, say):
        # Ignore messages from the bot itself
        if event.get("user") == bot_user_id:
            return
        # Ignore automated messages from other apps/bots. Slack tags any
        # message posted via an OAuth token with `bot_id` + `app_id`, even
        # when a real human posted it via their own user token (e.g. the
        # eval harness). Distinguish the two: real-bot messages have
        # `bot_id` but no `user` field; user-token messages have BOTH, and
        # the `user` is the real human. Only drop the former.
        if event.get("bot_id") and not event.get("user"):
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
        is_bot_mentioned = bot_user_id and f"<@{bot_user_id}>" in text

        # If bot is explicitly mentioned in a channel, the app_mention handler
        # already covers it — skip here to avoid double-processing.
        if is_bot_mentioned and not is_dm:
            return

        effective_thread_ts = thread_ts or message_ts

        # Check if this is a monitored channel
        is_monitored = db_is_monitored_channel(db_path, channel)

        # Monitored channel: top-level message or reply triggers a bot response.
        if is_monitored and not is_dm:
            # Skip noise messages (cheap Python filter, no LLM cost)
            if _is_noise(text):
                return

            # Rules are MANDATORY for monitored channels — check at the
            # routing layer on TOP-LEVEL messages. If the channel rules
            # file is missing the `## When to invoke` section, stay silent
            # entirely. The actual decision of "does this message match
            # the invocation rules" happens inside Claude via the
            # monitored-prefix prompt (handlers.py), which emits
            # NO_RESPONSE and gets suppressed end-to-end.
            is_top_level = not event.get("thread_ts")
            if is_top_level:
                channel_name = db_get_channel_name(db_path, channel)
                rules_text = read_channel_rules(channel_name, config.claude_work_dir)
                invoke_rules = extract_invoke_rules(rules_text)
                if not invoke_rules:
                    logger.warning(
                        f"[RULES] Monitored channel={channel} name={channel_name!r} "
                        f"is missing the '## When to invoke' section — staying silent. "
                        f"Add channels/{channel_name or '<name>'}.md with that section to activate."
                    )
                    return

            handle_user_message(
                user_id, channel, effective_thread_ts, message_ts, text,
                files=event.get("files"),
                monitored=True,
                **_hum_kwargs,
            )
            return

        # Normal channels: the bot ONLY replies when explicitly @-mentioned
        # (handled by app_mention). Any untagged message — even in a thread
        # the bot touched earlier — is someone else's conversation. Staying
        # silent is the right default; no "mid-session" continuation, no
        # "this one's not for me" replies. DMs are the only exception.
        if not is_dm:
            return

        handle_user_message(
            user_id, channel, effective_thread_ts, message_ts, text,
            files=event.get("files"),
            monitored=False,
            **_hum_kwargs,
        )

    # ── app_home_opened: render dashboard ────────────────────────────────

    @app.event("app_home_opened")
    def _handle_app_home_opened(event, logger):
        _render_app_home(event.get("user"), slack_client, config)

    # ── show_details action (button) ─────────────────────────────────────

    @app.action("show_details")
    def _handle_show_details(ack, body):
        ack()

        actions = body.get("actions", [])
        detail_id = actions[0].get("value") if actions else None
        channel = body.get("channel", {}).get("id")
        message = body.get("message", {})
        thread_ts = message.get("thread_ts") or message.get("ts")

        if not detail_id or not channel:
            return

        details = pop_detail(detail_id)
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
        for chunk_text in chunks:
            slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk_text)
            time.sleep(0.3)

    # ── App Home actions ─────────────────────────────────────────────────

    @app.action("home_kill_all")
    def _handle_home_kill_all(ack, body):
        ack()
        kill_all_sessions(remove_thread_lock_fn=remove_thread_lock_fn)
        _render_app_home(body.get("user", {}).get("id"), slack_client, config)

    @app.action("home_refresh")
    def _handle_home_refresh(ack, body):
        ack()
        _render_app_home(body.get("user", {}).get("id"), slack_client, config)

    logger.info("[EVENTS] All Slack event/action/command handlers registered")
