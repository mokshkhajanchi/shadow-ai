# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Slack bot that runs Claude Code locally on macOS. Users @mention the bot in Slack threads, and it invokes Claude Code via `claude-agent-sdk` as a subprocess with full local filesystem access. Uses Slack Socket Mode (no public URL needed). This is a personal project — no Jira tickets needed for commits.

## Commands

```bash
# Setup
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run
shadow-ai              # Start the bot
shadow-ai init         # Setup wizard (creates .env)
shadow-ai doctor       # Check prerequisites

# Test
pytest tests/ -v                          # All tests (138)
pytest tests/test_note_taking.py -v       # Single file
pytest tests/test_note_taking.py::TestIsLearnIntent -v  # Single class
pytest tests/ -v --tb=short               # Compact output

# Lint
ruff check shadow_ai/
ruff format shadow_ai/

# Evals (see evals/README.md for details)
pytest evals/ -v                                                    # Recorded evals (fast, free)
python -m evals.live --channel C0AQ61HQ550 --record --concurrency 1 # Live evals (sends real Slack messages)
python -m evals.live --channel C0AQ61HQ550 --dry-run                # Preview scenarios
```

## Architecture

### Request Flow
```
Slack @mention/DM → events.py → handle_user_message() → _process_message() → invoke_claude_code()
                                                                                    ↓
                                                              Continue (existing session)
                                                              Restore (from DB history)
                                                              New (fresh session)
```

### Module Responsibilities

- **`app.py`** — Entry point. Creates Slack Bolt app, initializes DB, discovers MCP servers, installs skills, starts Socket Mode listener.
- **`events.py`** — Slack event handlers (`app_mention`, `message`, `app_home_opened`, actions). Handles channel monitoring routing and noise filtering. No slash commands — everything via @mention.
- **`handlers.py`** — Core message processing. Bot commands (status, kill all, monitor, learn, summarize, review, test) are parsed before Claude invocation. Contains `_is_learn_intent()` for fuzzy note-taking detection.
- **`claude_runner.py`** — Claude Code SDK lifecycle. Three paths: `_continue_query()`, `_restore_and_query()`, `_new_session_and_query()`. Each runs in a dedicated thread with its own asyncio event loop.
- **`claude_options.py`** — Builds `ClaudeAgentOptions` with system prompt (base + custom + skills + notes), tool restrictions (read-only for monitored channels), model selection, thinking mode. Loads agents and skills here.
- **`sessions.py`** — In-memory session store. LRU eviction when `max_active_sessions` (5) is reached. Per-thread locks, SIGTERM cleanup.
- **`db.py`** — SQLite with WAL mode. Tables: `threads`, `messages`, `usage`, `monitored_channels`. All access serialized via `_db_lock`.
- **`knowledge.py`** — Knowledge indexing (`_build_knowledge_index`), note saving (`save_learned_knowledge`, `save_conversation`), codebase signature extraction.
- **`agent_loader.py`** — Parses `.md` files with YAML frontmatter into `AgentDefinition` objects for the SDK.
- **`skill_loader.py`** — Loads skills, builds skills prompt section, symlinks to `~/.claude/skills/`.
- **`config.py`** — `BotConfig` dataclass, `from_env()` class method. All config via `.env` file.

### Project Structure (config vs data)

```
agents/              # Agent definitions (.md) — committed
skills/              # Skill definitions (*/SKILL.md) — committed
workflows/           # Workflow templates (.md) — committed
channels/            # Per-channel rules (.md) — committed
knowledge/
├── notes/           # Saved notes — gitignored, injected into system prompt
├── conversations/   # Auto-saved conversations — gitignored, NOT indexed
└── system_prompt.example.md
```

- `knowledge/notes/` full content injected into every session's system prompt
- `knowledge/conversations/` saved but NOT indexed (archive only)
- `agents/`, `skills/`, `workflows/`, `channels/` are committed to git

### Channel Monitoring

- `@bot monitor #channel` starts monitoring → bot joins channel, saves to `monitored_channels` DB table
- New messages in monitored channels pass noise filter → auto-reply in thread with **read-only tools** (Read, Glob, Grep only) and haiku model
- Thread follow-ups require `ALLOWED_USER_IDS` authorization (full tool access)
- Sensitive data filter in system prompt prevents leaking secrets

### System Prompt Assembly (claude_options.py)

```
[Claude Code preset]
+ RESPONSE GUIDELINES (Slack formatting, MCP priority, agents/skills mention)
+ AVAILABLE SKILLS (full content of all loaded skills)
+ CUSTOM INSTRUCTIONS (from SYSTEM_PROMPT_FILE)
+ NOTES FROM PREVIOUS SESSIONS (summaries from knowledge/notes/)
```

## SQLite Schema

Four tables: `threads` (thread_ts PK, channel, status), `messages` (thread_ts FK, role, content), `usage` (cost_usd, duration_ms, num_turns), `monitored_channels` (channel_id PK, added_by).

## Commit Rules

**Run tests before every commit. No exceptions.**

```bash
pytest tests/ -v
```

All 138+ tests must pass before committing. If any test fails, fix the code or update the test before committing.

## Configuration

All via `.env` (see `.env.example`). Key vars: `BOT_USERNAME`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ALLOWED_USER_IDS`, `CLAUDE_WORK_DIR` (default ~/Projects), `CLAUDE_MAX_TURNS` (50), `DAILY_BUDGET_USD` (500), `SYSTEM_PROMPT_FILE`.
