"""
Claude Code SDK session lifecycle and response streaming.

Handles creating new sessions, restoring from DB history, continuing
existing sessions, and collecting streamed responses from the SDK.
"""

import asyncio
import json
import logging
import re
import threading
import time
import traceback

logger = logging.getLogger("slack-claude-code")


def _get_cli_pid(sdk_client) -> int | None:
    """Safely extract the CLI subprocess PID from an SDK client."""
    try:
        return sdk_client._transport._process.pid
    except Exception:
        return None


async def _collect_response(
    sdk_client,
    thread_ts: str = None,
    channel: str = None,
    progress_ts: str = None,
    slack_client=None,
    markdown_to_slack=None,
    verbose_progress: bool = False,
) -> tuple[str, dict | None]:
    """
    Collect response from SDK client.
    Logs ALL message types from Claude Code subprocess to bot.log.
    Returns (response_text, cost_info_dict_or_None).
    """
    from claude_agent_sdk import (
        AssistantMessage, ResultMessage, SystemMessage, UserMessage,
        TextBlock, ToolUseBlock, ToolResultBlock,
    )

    response_parts = []
    tool_log = []
    result_text = None  # Will hold ResultMessage.result if available
    cost_info = None
    start_time = time.time()
    last_update_time = start_time
    progress_interval = 2.5  # Adaptive: backs off on rate limit errors
    _spinner_frames = [":hourglass_flowing_sand:", ":hourglass:", ":gear:", ":zap:", ":rocket:", ":brain:"]
    _spinner_idx = 0
    _toolsearch_counts: dict[str, int] = {}
    MAX_TOOLSEARCH_REPEATS = 3

    try:
        async for message in sdk_client.receive_response():
            msg_type = type(message).__name__

            if isinstance(message, SystemMessage):
                logger.info(f"[CC:{thread_ts}] SystemMessage subtype={message.subtype} data={json.dumps(message.data, default=str)[:300]}")

            elif isinstance(message, UserMessage):
                content_preview = str(message.content)[:200] if message.content else ""
                logger.info(f"[CC:{thread_ts}] UserMessage: {content_preview}")

            elif isinstance(message, AssistantMessage):
                if message.error:
                    logger.error(f"[CC:{thread_ts}] AssistantMessage ERROR: {message.error}")

                # Collect text from this AssistantMessage
                # Only replace previous parts if this message has substantive text
                current_parts = []
                for block in message.content:
                    if isinstance(block, TextBlock):
                        current_parts.append(block.text)
                        logger.info(f"[CC:{thread_ts}] Text: {block.text[:150]}{'...' if len(block.text) > 150 else ''}")

                    elif isinstance(block, ToolUseBlock):
                        tool_info = f"\U0001f527 {block.name}"
                        if block.name == "Bash":
                            cmd = block.input.get("command", "")
                            # Redact secrets from displayed command
                            cmd = re.sub(r"(Bearer|Token|Authorization:?|api[_-]?key|password|secret|token)\s+\S+", r"\1 [REDACTED]", cmd, flags=re.IGNORECASE)
                            cmd = re.sub(r"(xoxb-|xapp-|sk-|ghp_|gho_|ATATT)\S+", "[REDACTED]", cmd)
                            tool_info += f": `{cmd[:120]}`"
                        elif block.name in ("Read", "Write", "Edit"):
                            fpath = block.input.get("file_path", "")
                            # Flag sensitive file reads
                            if any(s in fpath.lower() for s in [".env", "credentials", "secret", "token", "password"]):
                                tool_info += f": {fpath} *(sensitive file)*"
                            else:
                                tool_info += f": {fpath}"
                        elif block.name == "Glob":
                            tool_info += f": {block.input.get('pattern', '')}"
                        elif block.name == "Grep":
                            tool_info += f": `{block.input.get('pattern', '')}` in {block.input.get('path', '.')}"
                        elif block.name == "Agent":
                            desc = block.input.get("description", block.input.get("prompt", ""))
                            tool_info += f": {desc[:80]}"
                        elif block.name.startswith("mcp__"):
                            # MCP tool — show a clean name
                            short_name = block.name.split("__", 2)[-1] if "__" in block.name else block.name
                            tool_info = f"\U0001f527 {short_name}"
                        else:
                            tool_info += f": {json.dumps(block.input, default=str)[:100]}"

                        logger.info(f"[CC:{thread_ts}] {tool_info}")

                        # Loop detection: break if ToolSearch called repeatedly for same tool
                        if block.name == "ToolSearch":
                            search_key = json.dumps(block.input, sort_keys=True)
                            _toolsearch_counts[search_key] = _toolsearch_counts.get(search_key, 0) + 1
                            if _toolsearch_counts[search_key] >= MAX_TOOLSEARCH_REPEATS:
                                logger.warning(f"[CC:{thread_ts}] LOOP DETECTED: ToolSearch called {MAX_TOOLSEARCH_REPEATS}+ times for {search_key}. Breaking.")
                                try:
                                    await sdk_client.disconnect()
                                except Exception:
                                    pass
                                break  # Exit the content block loop

                        # Skip internal/meta tools from user-facing progress
                        if block.name not in ("ToolSearch", "Task", "TaskOutput", "ExitPlanMode", "NotebookEdit"):
                            tool_log.append(tool_info)
                            if len(tool_log) > 50:
                                tool_log = tool_log[-50:]

                    elif isinstance(block, ToolResultBlock):
                        is_err = block.is_error if block.is_error else False
                        content_preview = ""
                        if block.content:
                            if isinstance(block.content, str):
                                content_preview = block.content[:300]
                            elif isinstance(block.content, list):
                                content_preview = str(block.content)[:300]
                        status = "\u274c ERROR" if is_err else "\u2705 OK"
                        logger.info(f"[CC:{thread_ts}] ToolResult {status}: {content_preview}")

                    else:
                        logger.info(f"[CC:{thread_ts}] ContentBlock({type(block).__name__}): {str(block)[:200]}")

                # Only replace response_parts if this message has substantive text
                # (prevents empty final AssistantMessage from wiping useful earlier text)
                substantive = any(p.strip() for p in current_parts)
                if substantive:
                    response_parts = current_parts

                # Break outer loop if ToolSearch loop was detected
                if any(c >= MAX_TOOLSEARCH_REPEATS for c in _toolsearch_counts.values()):
                    break

            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "N/A"
                duration = f"{message.duration_ms / 1000:.1f}s" if message.duration_ms else "N/A"
                logger.info(
                    f"[CC:{thread_ts}] RESULT status={message.subtype} "
                    f"turns={message.num_turns} cost={cost} duration={duration} "
                    f"error={message.is_error} session={getattr(message, 'session_id', 'N/A')}"
                )
                if message.result:
                    result_text = message.result
                    logger.info(f"[CC:{thread_ts}] Result text: {message.result[:300]}")
                cost_info = {
                    "cost_usd": message.total_cost_usd,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "num_turns": message.num_turns,
                    "session_id": message.session_id,
                    "is_error": message.is_error,
                    "stop_reason": message.stop_reason,
                    "usage": message.usage,
                }

            else:
                # Catch-all for any other message types (StreamEvent, etc.)
                logger.info(f"[CC:{thread_ts}] {msg_type}: {str(message)[:300]}")

            # ── Streaming: update progress message with adaptive interval ──
            if progress_ts and channel and slack_client and time.time() - last_update_time >= progress_interval:
                last_update_time = time.time()
                elapsed = time.time() - start_time
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{int(elapsed)}s"

                # Rotating spinner emoji
                spinner = _spinner_frames[_spinner_idx % len(_spinner_frames)]
                _spinner_idx += 1

                if verbose_progress:
                    # Verbose: show tool details and text preview
                    progress_lines = [f"{spinner} *Working...* ({elapsed_str}, {len(tool_log)} tool calls)"]
                    for t in tool_log[-5:]:
                        progress_lines.append(f"  {t}")
                    if response_parts:
                        preview = response_parts[-1][:100]
                        if len(response_parts[-1]) > 100:
                            preview += "..."
                        progress_lines.append(f"  _Writing: {preview}_")
                    progress_text = "\n".join(progress_lines)
                else:
                    # Concise: spinner + elapsed time
                    progress_text = f"{spinner} Working... ({elapsed_str})"

                try:
                    slack_client.chat_update(
                        channel=channel, ts=progress_ts,
                        text=progress_text,
                    )
                    progress_interval = 2.5  # Reset on success
                except Exception as e:
                    logger.warning(f"[STREAM] Failed to update progress: {e}")
                    progress_interval = min(progress_interval * 2, 30)  # Back off on failure

    except Exception as e:
        error_str = str(e)
        logger.error(f"[CC:{thread_ts}] _collect_response error: {type(e).__name__}: {error_str}\n{traceback.format_exc()}")

        # User-friendly error messages for known API errors
        if "overloaded" in error_str.lower() or "529" in error_str:
            return (":warning: Claude API is currently overloaded. Please try again in a minute.", None)
        elif "rate_limit" in error_str.lower() or "429" in error_str:
            return (":warning: Rate limit hit. Please wait a moment and try again.", None)
        elif "authentication" in error_str.lower() or "401" in error_str:
            return (":warning: Authentication error. API key may be invalid or expired.", None)
        else:
            return (f"Claude Code encountered an error:\n```\n{type(e).__name__}: {error_str}\n```", None)

    # Delete progress message now that we have the final response
    if progress_ts and channel and slack_client:
        try:
            slack_client.chat_delete(channel=channel, ts=progress_ts)
        except Exception as e:
            logger.warning(f"[STREAM] Failed to delete progress message: {e}")

    if tool_log:
        logger.info(f"[CC:{thread_ts}] SUMMARY — {len(tool_log)} tool calls")

    # If a ToolSearch loop was detected, provide a friendly fallback
    loop_detected = any(c >= MAX_TOOLSEARCH_REPEATS for c in _toolsearch_counts.values())
    if loop_detected and not response_parts:
        response_parts = ["_A required tool could not be loaded after multiple attempts (likely a deferred MCP tool). The tool may be temporarily unavailable. Try again or rephrase your request._"]

    # Prefer collected AssistantMessage text; only fall back to ResultMessage.result
    # if no text was collected (result_text can be a meta-message about agent tasks)
    if response_parts:
        full_response = "\n".join(response_parts)
    elif result_text and result_text.strip():
        full_response = result_text
    else:
        full_response = ""
    if not full_response.strip():
        full_response = "_Claude Code completed the task but produced no text output._"
    return (full_response, cost_info)


async def _new_session_and_query(
    prompt: str,
    thread_ts: str,
    progress_ts: str = None,
    file_blocks: list = None,
    model: str = None,
    thinking_override: str = None,
    *,
    config,
    slack_client=None,
    store_session_fn=None,
    mark_session_processing_fn=None,
    db_get_thread_channel_fn=None,
    create_options_fn=None,
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    gitnexus_available: bool = False,
    knowledge_dirs: list[str] = None,
) -> tuple[str, dict | None]:
    """Create a new SDK session and query it."""
    from claude_agent_sdk import ClaudeSDKClient

    options = create_options_fn(
        config,
        model=model,
        thinking_override=thinking_override,
        mcp_server_names=mcp_server_names,
        mcp_tool_catalog=mcp_tool_catalog,
        knowledge_index_file=knowledge_index_file,
        gitnexus_available=gitnexus_available,
        knowledge_dirs=knowledge_dirs,
    )
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.connect()
    cli_pid = _get_cli_pid(sdk_client)

    loop = asyncio.get_event_loop()
    store_session_fn(thread_ts, sdk_client, loop, cli_pid=cli_pid)
    if mark_session_processing_fn:
        mark_session_processing_fn(thread_ts, True)

    try:
        channel = db_get_thread_channel_fn(thread_ts)

        if file_blocks:
            content = [{"type": "text", "text": prompt}] + file_blocks

            async def _message_stream():
                yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

            await sdk_client.query(_message_stream())
        else:
            await sdk_client.query(prompt)
        return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts, slack_client=slack_client, verbose_progress=getattr(config, 'verbose_progress', False))
    finally:
        if mark_session_processing_fn:
            mark_session_processing_fn(thread_ts, False)


async def _restore_and_query(
    history: list[dict],
    new_prompt: str,
    thread_ts: str,
    progress_ts: str = None,
    file_blocks: list = None,
    model: str = None,
    thinking_override: str = None,
    *,
    config,
    slack_client=None,
    store_session_fn=None,
    mark_session_processing_fn=None,
    db_get_thread_channel_fn=None,
    create_options_fn=None,
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    gitnexus_available: bool = False,
    knowledge_dirs: list[str] = None,
) -> tuple[str, dict | None]:
    """Restore a session from DB history and query with a new prompt."""
    from claude_agent_sdk import ClaudeSDKClient

    options = create_options_fn(
        config,
        model=model,
        thinking_override=thinking_override,
        mcp_server_names=mcp_server_names,
        mcp_tool_catalog=mcp_tool_catalog,
        knowledge_index_file=knowledge_index_file,
        gitnexus_available=gitnexus_available,
        knowledge_dirs=knowledge_dirs,
    )
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.connect()
    cli_pid = _get_cli_pid(sdk_client)
    logger.info(f"[RESTORE] Replaying {len(history)} messages for thread {thread_ts}")

    loop = asyncio.get_event_loop()
    store_session_fn(thread_ts, sdk_client, loop, cli_pid=cli_pid)
    if mark_session_processing_fn:
        mark_session_processing_fn(thread_ts, True)

    try:
        # Limit history to last 20 messages to avoid context overload
        recent_history = history[-20:] if len(history) > 20 else history
        history_parts = []
        for msg in recent_history:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            history_parts.append(f"{prefix}: {msg['content']}")

        context_prompt = (
            "Here is the conversation history from a previous session. "
            "Review it for context, then respond to the latest message.\n\n"
            "--- CONVERSATION HISTORY ---\n"
            + "\n\n".join(history_parts)
            + "\n--- END HISTORY ---\n\n"
            f"Now, the user says: {new_prompt}"
        )

        if file_blocks:
            content = [{"type": "text", "text": context_prompt}] + file_blocks

            async def _message_stream():
                yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

            await sdk_client.query(_message_stream())
        else:
            await sdk_client.query(context_prompt)
        channel = db_get_thread_channel_fn(thread_ts)
        return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts, slack_client=slack_client, verbose_progress=getattr(config, 'verbose_progress', False))
    finally:
        if mark_session_processing_fn:
            mark_session_processing_fn(thread_ts, False)


async def _continue_query(
    sdk_client,
    prompt: str,
    thread_ts: str = None,
    progress_ts: str = None,
    file_blocks: list = None,
    *,
    slack_client=None,
    db_get_thread_channel_fn=None,
    verbose_progress: bool = False,
) -> tuple[str, dict | None]:
    """Continue an existing session with a new prompt."""
    if file_blocks:
        content = [{"type": "text", "text": prompt}] + file_blocks

        async def _message_stream():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

        await sdk_client.query(_message_stream())
    else:
        await sdk_client.query(prompt)
    channel = db_get_thread_channel_fn(thread_ts) if thread_ts else None
    return await _collect_response(sdk_client, thread_ts=thread_ts, channel=channel, progress_ts=progress_ts, slack_client=slack_client, verbose_progress=verbose_progress)


def _run_in_new_loop(coro_factory, thread_ts: str, request_timeout: int, *args, **kwargs):
    """
    Run an async coroutine in a NEW event loop on a NEW dedicated thread.
    The loop stays alive after the first response so the session persists.

    Args:
        coro_factory: Async function to call.
        thread_ts: Slack thread timestamp for logging.
        request_timeout: Timeout in seconds.
        *args, **kwargs: Passed to coro_factory.
    """
    result_container = {"response": None, "error": None, "traceback": None}
    done_event = threading.Event()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_container["response"] = loop.run_until_complete(coro_factory(*args, **kwargs))
            done_event.set()
            # Keep loop alive for follow-up queries on this session
            loop.run_forever()
        except Exception as e:
            result_container["error"] = e
            result_container["traceback"] = traceback.format_exc()
            logger.error(f"[LOOP ERROR] thread={thread_ts}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            done_event.set()
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name=f"session-{thread_ts[:8]}")
    t.start()

    # Block until done or timeout — zero CPU usage while waiting
    done_event.wait(timeout=request_timeout)

    if result_container["error"]:
        tb = result_container.get("traceback", "")
        logger.error(f"[INVOKE ERROR] thread={thread_ts}: {type(result_container['error']).__name__}: {result_container['error']}\n{tb}")
        raise result_container["error"]
    if result_container["response"] is None:
        return f"Claude Code timed out ({request_timeout}s)."
    return result_container["response"]


def invoke_claude_code(
    prompt: str,
    thread_ts: str,
    progress_ts: str = None,
    file_blocks: list = None,
    model: str = None,
    thinking_override: str = None,
    *,
    config,
    slack_client=None,
    get_session_fn=None,
    remove_session_fn=None,
    touch_session_fn=None,
    mark_session_processing_fn=None,
    store_session_fn=None,
    db_get_thread_messages_fn=None,
    db_get_thread_channel_fn=None,
    create_options_fn=None,
    mcp_server_names: list[str] = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    gitnexus_available: bool = False,
    knowledge_dirs: list[str] = None,
) -> tuple[str, dict | None]:
    """
    Synchronous entry point. Called from the thread pool.
    Routes to: continue existing | restore from DB | new session.

    All dependencies are passed explicitly instead of using globals.
    """
    request_timeout = getattr(config, "request_timeout", 600)

    session = get_session_fn(thread_ts)

    # Thinking override requires a fresh session (can't change mid-session)
    if session and thinking_override:
        logger.info(f"[THINKING] Override requested, killing existing session for thread={thread_ts}")
        remove_session_fn(thread_ts)
        session = None

    # Common kwargs for _new_session_and_query and _restore_and_query
    session_kwargs = dict(
        config=config,
        slack_client=slack_client,
        store_session_fn=store_session_fn,
        mark_session_processing_fn=mark_session_processing_fn,
        db_get_thread_channel_fn=db_get_thread_channel_fn,
        create_options_fn=create_options_fn,
        mcp_server_names=mcp_server_names,
        mcp_tool_catalog=mcp_tool_catalog,
        knowledge_index_file=knowledge_index_file,
        gitnexus_available=gitnexus_available,
        knowledge_dirs=knowledge_dirs,
    )

    if session:
        logger.info(f"[CONTINUE] thread={thread_ts}")
        touch_session_fn(thread_ts)
        mark_session_processing_fn(thread_ts, True)
        loop = session["loop"]
        sdk_client = session["client"]
        try:
            future = asyncio.run_coroutine_threadsafe(
                _continue_query(
                    sdk_client, prompt, thread_ts, progress_ts=progress_ts, file_blocks=file_blocks,
                    slack_client=slack_client,
                    db_get_thread_channel_fn=db_get_thread_channel_fn,
                    verbose_progress=getattr(config, 'verbose_progress', False),
                ),
                loop,
            )
            response, cost_info = future.result(timeout=request_timeout)

            # Auto-compact: if Claude returned empty, the session is likely context-saturated.
            if not response.strip() or response == "_Claude Code completed the task but produced no text output._":
                logger.warning(f"[AUTO-COMPACT] Empty response on continue, restarting session for thread={thread_ts}")
                remove_session_fn(thread_ts)
                history = db_get_thread_messages_fn(thread_ts)
                if history:
                    return _run_in_new_loop(
                        _restore_and_query, thread_ts, request_timeout,
                        history, prompt, thread_ts, progress_ts, file_blocks,
                        model=model, thinking_override=thinking_override,
                        **session_kwargs,
                    )

            return (response, cost_info)
        except Exception as e:
            logger.error(f"[CONTINUE ERROR] thread={thread_ts}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            remove_session_fn(thread_ts)
            raise
        finally:
            mark_session_processing_fn(thread_ts, False)

    history = db_get_thread_messages_fn(thread_ts)

    if history:
        logger.info(f"[RESTORE] thread={thread_ts}, {len(history)} messages in DB")
        return _run_in_new_loop(
            _restore_and_query, thread_ts, request_timeout,
            history, prompt, thread_ts, progress_ts, file_blocks,
            model=model, thinking_override=thinking_override,
            **session_kwargs,
        )
    else:
        logger.info(f"[NEW] thread={thread_ts}")
        return _run_in_new_loop(
            _new_session_and_query, thread_ts, request_timeout,
            prompt, thread_ts, progress_ts, file_blocks,
            model=model, thinking_override=thinking_override,
            **session_kwargs,
        )
