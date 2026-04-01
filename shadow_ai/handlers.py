"""
Core message processing — auth check, command dispatch, Claude invocation.

Extracted from bot.py: handle_user_message() and _process_message().
All dependencies are injected via keyword arguments (no module-level globals).
"""

import logging
import traceback
from datetime import datetime

from shadow_ai.config import BotConfig
from shadow_ai.db import (
    db_create_thread,
    db_get_daily_cost,
    db_get_last_slack_ts,
    db_get_thread_channel,
    db_get_thread_messages,
    db_get_total_cost,
    db_save_message,
    db_save_usage,
    db_set_last_slack_ts,
)
from shadow_ai.sessions import (
    get_active_session_count,
    get_session,
    kill_all_sessions,
    mark_session_processing,
    remove_session,
    store_session,
    touch_session,
)
from shadow_ai.slack_helpers import (
    clean_message_text,
    chunk_message,
    fetch_thread_messages,
    markdown_to_slack,
    parse_model_prefix,
    send_response_with_stop_button,
    send_session_ended,
)
from shadow_ai.claude_runner import invoke_claude_code
from shadow_ai.commands import (
    parse_ado_pr_url,
    review_pr,
    run_summary_command,
    run_test_command,
)
from shadow_ai.files import download_slack_files

logger = logging.getLogger("slack-claude-code")


# ─── Auth ────────────────────────────────────────────────────────────────────

def _is_learn_intent(text: str) -> bool:
    """Detect if user wants the bot to save/learn/remember something.

    Uses keyword combination matching. Matches patterns like:
    "learn this", "save what we discussed", "please remember",
    "note this down", "take a note", etc.
    """
    t = text.strip().lower()

    # Direct single-word commands
    if t in ("learn", "remember"):
        return True

    # Special phrases
    if "take note" in t or "take a note" in t:
        return True

    # Action + context word matching
    action_words = {"learn", "remember", "save", "note", "store", "record", "memorize", "retain"}
    context_words = {"this", "that", "conversation", "discussion", "discussed", "above", "chat", "thread", "it"}
    modifier_words = {"please", "kindly", "can", "could", "you"}

    words = set(t.split())
    has_action = bool(words & action_words)
    has_context = bool(words & context_words)
    has_modifier = bool(words & modifier_words)

    if has_action and has_context:
        return True

    # "please remember" / "kindly save"
    if has_action and has_modifier and len(words) <= 4:
        return True

    # "learn from this" / "learn from conversation"
    if has_action and "from" in words:
        return True

    return False


def is_authorized(user_id: str, allowed_user_ids: list[str]) -> bool:
    """Check whether *user_id* is in the allow-list (empty list = everyone allowed)."""
    if not allowed_user_ids or allowed_user_ids == [""]:
        return True
    return user_id in allowed_user_ids


# ─── Entry point (called from event handlers) ───────────────────────────────

def handle_user_message(
    user_id: str,
    channel: str,
    thread_ts: str,
    message_ts: str,
    text: str,
    files: list = None,
    monitored: bool = False,
    *,
    config: BotConfig,
    slack_client,
    executor,
    get_thread_lock_fn,
    bot_user_id: str,
    # Runtime state passed through
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    knowledge_dirs: list[str] = None,
    repo_paths: dict[str, str] = None,
    repo_test_config: dict[str, dict] = None,
    create_options_fn=None,
):
    """
    Entry point from Slack event handlers.
    Dispatches work to the thread pool so it doesn't block other events.
    """
    # Monitored channels: anyone can use the bot. Normal: check auth.
    if not monitored and not is_authorized(user_id, config.allowed_user_ids):
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Sorry <@{user_id}>, you're not authorized to use this bot.",
        )
        return

    # React immediately (🤖 for monitored, 👀 for normal)
    try:
        reaction = "robot_face" if monitored else "eyes"
        slack_client.reactions_add(channel=channel, name=reaction, timestamp=message_ts)
    except Exception:
        pass

    # Submit to thread pool — returns immediately
    executor.submit(
        _process_message,
        user_id, channel, thread_ts, message_ts, text, files,
        monitored=monitored,
        config=config,
        slack_client=slack_client,
        bot_user_id=bot_user_id,
        get_thread_lock_fn=get_thread_lock_fn,
        mcp_server_names=mcp_server_names,
        mcp_tool_catalog=mcp_tool_catalog,
        knowledge_index_file=knowledge_index_file,
        knowledge_dirs=knowledge_dirs,
        repo_paths=repo_paths,
        repo_test_config=repo_test_config,
        create_options_fn=create_options_fn,
    )


# ─── Core processing (runs in thread pool) ──────────────────────────────────

def _process_message(
    user_id: str,
    channel: str,
    thread_ts: str,
    message_ts: str,
    text: str,
    files: list = None,
    monitored: bool = False,
    *,
    config: BotConfig,
    slack_client,
    bot_user_id: str,
    get_thread_lock_fn,
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    knowledge_dirs: list[str] = None,
    repo_paths: dict[str, str] = None,
    repo_test_config: dict[str, dict] = None,
    create_options_fn=None,
):
    """
    The actual work — runs inside the thread pool.
    Per-thread lock ensures only one request is processed at a time per thread.
    """
    db_path = config.db_path

    lock = get_thread_lock_fn(thread_ts)

    if not lock.acquire(timeout=5):
        logger.warning(f"[BUSY] thread={thread_ts} is already being processed, skipping.")
        # Show elapsed time if session is tracked
        session = get_session(thread_ts)
        if session and session.get("last_activity"):
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(session["last_activity"])).total_seconds()
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{int(elapsed)}s"
                msg = f":hourglass: Still working on your previous request ({elapsed_str} elapsed). Your message will be processed next."
            except Exception:
                msg = ":hourglass: Still processing your previous message. Please wait..."
        else:
            msg = ":hourglass: Still processing your previous message. Please wait..."
        slack_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)
        return

    try:
        prompt = clean_message_text(text, bot_user_id)
        if not prompt.strip():
            return

        # ── Parse model and thinking prefixes ──
        model_override, prompt = parse_model_prefix(prompt)
        thinking_override = None
        if prompt.lower().startswith("think:"):
            thinking_override = "enabled"
            prompt = prompt[len("think:"):].strip()

        # Monitored channel: use haiku, add context + channel rules
        if monitored and not model_override:
            model_override = "haiku"
        if monitored:
            # Load per-channel rules if they exist
            channel_rules = ""
            from shadow_ai.db import db_get_channel_name
            channel_name = db_get_channel_name(db_path, channel)
            if channel_name:
                from pathlib import Path as _Path
                rules_file = _Path(config.claude_work_dir).expanduser() / "knowledge" / "channels" / f"{channel_name}.md"
                if not rules_file.exists():
                    # Also check relative to cwd
                    rules_file = _Path("knowledge") / "channels" / f"{channel_name}.md"
                if rules_file.exists():
                    try:
                        channel_rules = rules_file.read_text(encoding="utf-8").strip()
                        logger.info(f"[MONITOR] Loaded rules for #{channel_name} ({len(channel_rules)} chars)")
                    except Exception:
                        pass

            monitor_prefix = (
                "[MONITORED CHANNEL — READ-ONLY MODE]\n"
                "You are monitoring this Slack channel and auto-replying.\n"
                "Reply helpfully and concisely. If the message doesn't need a response "
                "(it's a statement, acknowledgment, or not directed at anyone), "
                "reply with ONLY the text 'NO_RESPONSE' and nothing else.\n\n"
                "SECURITY RULES:\n"
                "- NEVER share sensitive information: API keys, tokens, passwords, .env contents, "
                "credentials, secret files, or private configuration\n"
                "- NEVER expose full file paths from the host machine\n"
                "- If asked for sensitive data, decline politely\n"
                "- You have read-only access — you cannot modify files or run commands\n\n"
            )
            if channel_rules:
                monitor_prefix += f"CHANNEL RULES:\n{channel_rules}\n\n"
            prompt = monitor_prefix + prompt

        # ── Build helpers that bind config/deps for command functions ──
        def _send_response(ch, ts, resp):
            tagline = f"\n\n_sent by {config.bot_identity}_"
            resp = resp.rstrip() + tagline
            send_response_with_stop_button(slack_client, ch, ts, resp)

        # Wrap create_options_fn with monitored flag if needed
        _effective_create_options_fn = create_options_fn
        if monitored:
            _effective_create_options_fn = lambda *args, **kwargs: create_options_fn(*args, monitored=True, **kwargs)

        def _invoke(p, t, progress_ts=None, file_blocks=None, model=None, thinking_override=None):
            return invoke_claude_code(
                p, t,
                progress_ts=progress_ts,
                file_blocks=file_blocks,
                model=model,
                thinking_override=thinking_override,
                config=config,
                slack_client=slack_client,
                get_session_fn=get_session,
                remove_session_fn=remove_session,
                touch_session_fn=touch_session,
                mark_session_processing_fn=mark_session_processing,
                store_session_fn=store_session,
                db_get_thread_messages_fn=lambda ts: db_get_thread_messages(db_path, ts),
                db_get_thread_channel_fn=lambda ts: db_get_thread_channel(db_path, ts),
                create_options_fn=_effective_create_options_fn,
                mcp_server_names=mcp_server_names or [],
                mcp_tool_catalog=mcp_tool_catalog,
                knowledge_index_file=knowledge_index_file,
                gitnexus_available=config.gitnexus_available,
                knowledge_dirs=knowledge_dirs or [],
            )

        def _db_save_usage(ts, cost):
            db_save_usage(db_path, ts, cost)

        # ── Bot commands (before normal Claude Code flow) ──
        prompt_lower = prompt.strip().lower()
        logger.info(f"[CMD] prompt_lower={prompt_lower!r}")

        if prompt_lower in ("kill all sessions", "kill all", "stop all sessions", "stop all"):
            count = kill_all_sessions()
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":broom: Killed *{count}* active session{'s' if count != 1 else ''}. All connections cleared.",
            )
            return

        # ── Monitor commands ──
        if prompt_lower in ("monitoring", "list monitors", "monitors"):
            from shadow_ai.db import db_get_monitored_channels
            channels = db_get_monitored_channels(db_path)
            if not channels:
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=":eyes: Not monitoring any channels.",
                )
            else:
                channel_list = "\n".join(f"• <#{c}>" for c in channels)
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":robot_face: *Monitored channels:*\n{channel_list}",
                )
            return

        if prompt_lower.startswith("monitor "):
            import re as _re
            from shadow_ai.db import db_add_monitored_channel
            # Extract channel ID and name from Slack's <#C123|channel-name> format
            channel_match = _re.search(r"<#([CG][A-Z0-9]+)\|?([^>]*)", prompt)
            if not channel_match:
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="Usage: `monitor #channel`",
                )
                return
            target_channel = channel_match.group(1)
            channel_name = channel_match.group(2) or ""
            # Try to join (works for public channels, fails silently for private)
            try:
                slack_client.conversations_join(channel=target_channel)
            except Exception as e:
                logger.info(f"[MONITOR] Could not auto-join {target_channel} (private channel? invite bot manually): {e}")
            db_add_monitored_channel(db_path, target_channel, user_id, channel_name=channel_name)
            slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":robot_face: Now monitoring <#{target_channel}>. I'll reply to questions in threads.",
            )
            logger.info(f"[MONITOR] Started monitoring {target_channel} by {user_id}")
            return

        if prompt_lower.startswith("stop monitoring"):
            import re as _re
            from shadow_ai.db import db_remove_monitored_channel
            channel_match = _re.search(r"<#([CG][A-Z0-9]+)", prompt)
            if not channel_match:
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="Usage: `stop monitoring #channel`",
                )
                return
            target_channel = channel_match.group(1)
            db_remove_monitored_channel(db_path, target_channel)
            slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":octagonal_sign: Stopped monitoring <#{target_channel}>.",
            )
            logger.info(f"[MONITOR] Stopped monitoring {target_channel} by {user_id}")
            return

        if prompt_lower in ("status", "bot status", "cost", "usage"):
            daily = db_get_daily_cost(db_path)
            total = db_get_total_cost(db_path)
            active = get_active_session_count()
            lines = [
                f":bar_chart: *Bot Status*",
                f"• Active sessions: *{active}*",
                f"• Today's cost: *${daily:.4f}*",
            ]
            if config.daily_budget_usd > 0:
                remaining = max(0, config.daily_budget_usd - daily)
                lines.append(f"• Daily budget: *${config.daily_budget_usd:.2f}* (${remaining:.4f} remaining)")
            lines.append(f"• All-time cost: *${total:.4f}*")
            slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text="\n".join(lines),
            )
            return

        if prompt_lower in ("summarize", "summary", "recap"):
            run_summary_command(
                channel, thread_ts,
                slack_client=slack_client,
                invoke_claude_code_fn=_invoke,
                remove_session_fn=remove_session,
                db_get_thread_messages_fn=lambda ts: db_get_thread_messages(db_path, ts),
                db_save_usage_fn=_db_save_usage,
                send_response_fn=_send_response,
            )
            return

        # ── PR Review command ──
        pr_info = parse_ado_pr_url(prompt)
        if pr_info and (prompt_lower.startswith("review") or "review" in prompt_lower):
            repo_name, pr_id = pr_info
            review_pr(
                repo_name, pr_id, channel, thread_ts,
                repo_paths=repo_paths or {},
                slack_client=slack_client,
                invoke_claude_code_fn=_invoke,
                remove_session_fn=remove_session,
                db_save_usage_fn=_db_save_usage,
                send_response_fn=_send_response,
            )
            return

        # ── Test command ──
        if prompt_lower.startswith("test ") or prompt_lower == "test":
            run_test_command(
                prompt, channel, thread_ts,
                repo_test_config=repo_test_config or config.repo_test_config or {},
                slack_client=slack_client,
                invoke_claude_code_fn=_invoke,
                remove_session_fn=remove_session,
                db_save_usage_fn=_db_save_usage,
                send_response_fn=_send_response,
            )
            return

        # ── Learn command: save knowledge from thread ──
        if _is_learn_intent(prompt_lower):
            from shadow_ai.knowledge import save_learned_knowledge
            slack_client.reactions_add(channel=channel, name="brain", timestamp=message_ts)
            messages = db_get_thread_messages(db_path, thread_ts, limit=100)

            if messages:
                # Build from DB history
                convo_parts = []
                for msg in messages:
                    role = "User" if msg["role"] == "user" else "Assistant"
                    convo_parts.append(f"**{role}**: {msg['content']}")
                convo_text = "\n\n".join(convo_parts)
            else:
                # Fallback: fetch thread messages from Slack API directly
                convo_text, _ = fetch_thread_messages(slack_client, channel, thread_ts)
                if not convo_text:
                    slack_client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=":x: No conversation history found.",
                    )
                    return

            # Topic from first line of conversation text
            topic = "conversation"
            for line in convo_text.split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    topic = line.strip("*_#`- ").strip()[:50] or topic
                    break

            filepath = save_learned_knowledge(convo_text, topic, thread_ts)

            # Rebuild knowledge index in background
            import threading
            from shadow_ai.knowledge import rebuild_knowledge_index
            threading.Thread(
                target=rebuild_knowledge_index,
                args=(config.knowledge_paths, config.claude_work_dir),
                kwargs=dict(
                    inline_threshold=config.knowledge_inline_threshold,
                    total_inline_limit=config.knowledge_total_inline_limit,
                    index_max_entries=config.knowledge_index_max_entries,
                    codebase_max_size=config.codebase_index_max_size,
                    gitnexus_available=config.gitnexus_available,
                ),
                daemon=True,
            ).start()

            slack_client.reactions_add(channel=channel, name="white_check_mark", timestamp=message_ts)
            return

        # ── Daily budget check ──
        if config.daily_budget_usd > 0 and db_get_daily_cost(db_path) >= config.daily_budget_usd:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: Daily budget of *${config.daily_budget_usd:.2f}* has been reached. Try again tomorrow or ask an admin to increase `DAILY_BUDGET_USD`.",
            )
            return

        # ── DB: create thread & save user message ──
        db_create_thread(db_path, thread_ts, channel)
        db_save_message(db_path, thread_ts, "user", prompt, user_id=user_id)

        # Fetch only new Slack thread messages (ones not already sent as context)
        since_ts = db_get_last_slack_ts(db_path, thread_ts)
        thread_context, latest_ts = fetch_thread_messages(
            slack_client, channel, thread_ts,
            bot_user_id=bot_user_id, since_ts=since_ts,
        )
        if thread_context:
            prompt = thread_context + "User's request: " + prompt
            logger.info(f"[THREAD CONTEXT] Prepended {len(thread_context)} chars (since={since_ts})")
        if latest_ts:
            db_set_last_slack_ts(db_path, thread_ts, latest_ts)

        # Download file attachments
        file_blocks = None
        if files:
            file_blocks = download_slack_files(
                files, slack_client, config.slack_bot_token,
            )
            if file_blocks:
                logger.info(f"[FILES] {len(file_blocks)} file blocks ready for thread {thread_ts}")
            else:
                file_blocks = None  # No usable files

        # Working indicator (capture ts for streaming updates)
        progress_msg = slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":robot_face: Working...",
        )
        progress_ts = progress_msg.get("ts")

        # Invoke Claude Code
        response, cost_info = _invoke(
            prompt, thread_ts,
            progress_ts=progress_ts, file_blocks=file_blocks,
            model=model_override, thinking_override=thinking_override,
        )

        # Save usage data (DB only — not shown in Slack response)
        if cost_info:
            _db_save_usage(thread_ts, cost_info)

        # Save response
        db_save_message(db_path, thread_ts, "assistant", response)

        # Monitored channel: if Claude says NO_RESPONSE, skip posting
        if monitored and "NO_RESPONSE" in response:
            logger.info(f"[MONITOR] Skipped reply (NO_RESPONSE) for thread={thread_ts}")
            try:
                slack_client.reactions_remove(channel=channel, name="robot_face", timestamp=message_ts)
            except Exception:
                pass
        else:
            # React check mark
            try:
                slack_client.reactions_add(channel=channel, name="white_check_mark", timestamp=message_ts)
            except Exception:
                pass

            # Send response + Stop button
            _send_response(channel, thread_ts, response)

        # Auto-save: save raw conversation to knowledge/conversations/ in background
        try:
            messages = db_get_thread_messages(db_path, thread_ts, limit=100)
            if len(messages) >= 2:  # At least 1 user + 1 assistant message
                import threading as _thr
                from shadow_ai.knowledge import save_conversation

                def _auto_save():
                    try:
                        convo = "\n\n".join(
                            f"**{'User' if m['role'] == 'user' else 'Assistant'}**: {m['content']}"
                            for m in messages
                        )

                        # Extract topic from the last assistant message (first line, max 50 chars)
                        topic = "conversation"
                        for m in reversed(messages):
                            if m["role"] == "assistant" and m["content"].strip():
                                first_line = m["content"].strip().split("\n")[0]
                                topic = first_line.strip("*_#`- ").strip()[:50] or topic
                                break

                        save_conversation(convo, topic, thread_ts)
                        logger.info(f"[AUTO-SAVE] Saved conversation from thread {thread_ts}: {topic}")
                    except Exception as save_err:
                        logger.warning(f"[AUTO-SAVE] Failed: {save_err}")

                _thr.Thread(target=_auto_save, daemon=True).start()
        except Exception:
            pass  # Auto-save is best-effort, never block the response

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
