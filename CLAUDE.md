# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Slack bot that runs Claude Code locally on macOS. Users @mention the bot in Slack threads, and it invokes Claude Code via the `claude-agent-sdk` as a subprocess with full local filesystem access. Uses Slack Socket Mode (no public URL needed).

## Running the Bot

```bash
# Activate venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run (env vars loaded from .env)
env $(grep -v '^#' .env | xargs) python bot.py
```

Required env vars: `SLACK_BOT_TOKEN` (xoxb-...), `SLACK_APP_TOKEN` (xapp-...), `ALLOWED_USER_IDS` (comma-separated Slack user IDs).

## Architecture

Single-file app (`bot.py`, ~850 lines) with these layers:

- **Slack layer**: `slack-bolt` Socket Mode. Two event handlers: `app_mention` (first contact) and `message` (follow-ups in tracked threads, DMs). Both route to `handle_user_message()` → thread pool.
- **Concurrency**: `ThreadPoolExecutor` (default 5 workers). Per-thread locks prevent duplicate processing of the same Slack thread. Each Claude session gets its own `asyncio` event loop on a dedicated thread that stays alive for follow-up queries.
- **Session management**: Two tiers — in-memory `active_sessions` dict holds live `ClaudeSDKClient` instances; SQLite DB (`slack_claude_bot.db`) persists all messages. On restart, sessions are restored from DB by replaying conversation history into a new SDK client.
- **Claude Code integration**: Uses `claude-agent-sdk` (`ClaudeSDKClient` + `ClaudeAgentOptions`). Auto-discovers MCP servers from `~/.claude/settings.json` and project `.mcp.json`, adding wildcard tool approvals. Optional `knowledge.md` file is injected as `append_system_prompt`.
- **Response handling**: `_collect_response()` streams all message types from the SDK, logs tool usage, and assembles text. Long responses are chunked at ~3900 chars. Every reply includes a "Stop Session" button.

### Key flow: `handle_user_message()` → `_process_message()` → `invoke_claude_code()`
`invoke_claude_code()` routes to one of three paths:
1. **Continue**: existing in-memory session → `_continue_query()`
2. **Restore**: no session but DB history exists → `_restore_and_query()` (replays history)
3. **New**: fresh thread → `_new_session_and_query()`

## SQLite Schema

Two tables: `threads` (thread_ts PK, channel, status, timestamps) and `messages` (auto-increment id, thread_ts FK, role, user_id, content, timestamp). All DB access is serialized via `_db_lock`.

## Configuration

All config is via env vars (see `bot.py` lines 33-53). Key ones beyond the required three: `CLAUDE_WORK_DIR` (default `~/Projects`), `CLAUDE_MAX_TURNS` (30), `CLAUDE_PERMISSION_MODE` (`acceptEdits`), `REQUEST_TIMEOUT` (600s), `MAX_CONCURRENT` (5), `KNOWLEDGE_PATHS` (comma-separated folder/file paths for knowledge base), `DAILY_BUDGET_USD` (0 = unlimited).

## MANDATORY Commit Rules (NON-NEGOTIABLE)

**EVERY commit MUST include a Jira ticket ID. NO EXCEPTIONS.**

Format:
```
ID: <Jira key>; DONE: <percentage>%; HOURS: <hours>; <description>
```

Example:
```
ID: FPP-33282; DONE: 100%; HOURS: 1; Fix ValueError on non-integer bag_id
```

- If no Jira ticket ID is available, **ASK the user** before committing. NEVER commit without one.
- If DONE% or HOURS are unknown, ask or default to `DONE: 100%; HOURS: 1`.
- This applies to ALL commits — feature, fix, test, empty, everything.
