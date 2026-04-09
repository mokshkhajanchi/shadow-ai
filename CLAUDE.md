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
pytest tests/ -v                          # All tests (196)
pytest tests/test_system_prompt.py -v     # Single file
pytest tests/ -v --tb=short               # Compact output

# Lint
ruff check shadow_ai/
ruff format shadow_ai/

# Evals (see evals/README.md for details)
pytest evals/ -v                                                    # Recorded evals (fast, free)
python -m evals.live --channel C0AQ61HQ550 --record --concurrency 1 # Live evals (38 scenarios, ~60 min)
python -m evals.live --channel C0AQ61HQ550 --dry-run                # Preview scenarios
python -m evals.live --channel C0AQ61HQ550 --category pr_review     # Run single category
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

- **`app.py`** — Entry point. Creates Slack Bolt app, initializes DB, discovers MCP servers, installs skills, runs migrations, starts Socket Mode listener.
- **`events.py`** — Slack event handlers (`app_mention`, `message`, `app_home_opened`, actions). Handles channel monitoring routing and noise filtering. No slash commands — everything via @mention.
- **`handlers.py`** — Core message processing. Bot commands (status, kill all, monitor, run workflow, summarize, review, test) parsed before Claude invocation. Monitored channel prefix with security guardrails and anti-hallucination rules injected here.
- **`claude_runner.py`** — Claude Code SDK lifecycle. Three paths: `_continue_query()`, `_restore_and_query()`, `_new_session_and_query()`. Each runs in a dedicated thread with its own asyncio event loop.
- **`claude_options.py`** — Builds `ClaudeAgentOptions` with system prompt (identity + response style + anti-hallucination + skills + notes), tool restrictions, model selection (default: opus), thinking mode. Loads agents and skills.
- **`guardrails.py`** — Code-level security for monitored channels. `can_use_tool` callback blocks destructive commands, secret reads, browser tools before execution.
- **`workflow_loader.py`** — Loads workflow templates from `workflows/`, parses parameters, builds prompts.
- **`agent_loader.py`** — Parses agent `.md` files into `AgentDefinition` objects for the SDK.
- **`skill_loader.py`** — Loads skills, builds skills prompt section, symlinks to `~/.claude/skills/`.
- **`sessions.py`** — In-memory session store. LRU eviction when `max_active_sessions` (5) is reached.
- **`db.py`** — SQLite with WAL mode. Tables: `threads`, `messages`, `usage`, `monitored_channels`.
- **`knowledge.py`** — Knowledge indexing, note saving (`save_learned_knowledge`, `save_conversation`), codebase signature extraction.
- **`config.py`** — `BotConfig` dataclass, `from_env()` class method. All config via `.env` file.

### Project Structure

```
agents/              # Agent definitions (.md) — committed
skills/              # Skill definitions (*/SKILL.md) — committed
workflows/           # Workflow templates (.md) — committed
channels/            # Per-channel rules (.md) — committed
knowledge/
├── notes/           # Saved notes — gitignored, full content injected into system prompt
├── conversations/   # Auto-saved conversations — gitignored, NOT indexed
└── system_prompt.example.md
evals/
├── scenarios/       # 9 YAML files, 38 scenarios across 9 categories
├── graders/         # Grading functions (contains, safety, tool usage, golden, quality/LLM-judge, side effects)
├── live.py          # Live eval runner (sends real Slack messages, per-scenario timeouts)
└── README.md        # Eval documentation
```

### System Prompt Assembly (claude_options.py)

Total budget: keep under 20KB for best results.

```
[Claude Code preset]
+ YOUR IDENTITY (shadow.ai bot identity)
+ RESPONSE STYLE (concise, Slack formatting, verify-first, anti-hallucination, search-before-asking, actions-first)
+ CODEBASE REFERENCE (knowledge index path — read on demand, NOT inlined)
+ AVAILABLE SKILLS (names only — full content read from skills/ dir on demand)
+ CUSTOM INSTRUCTIONS (from SYSTEM_PROMPT_FILE)
+ SAVED NOTES (full content from knowledge/notes/ — 100KB budget, authoritative)
```

Key design decisions:
- Default model: **Opus** for all queries (normal + monitored). Override with `haiku:` or `sonnet:` prefix.
- MCP tool catalog NOT in prompt (causes hallucination — Claude has tools via SDK)
- Knowledge index NOT inlined (1.4MB — read on demand via Read tool)
- Notes injected as full content (not summaries — summaries cause hallucination)
- Skills listed as names only (not full 4KB content — reduces prompt bloat)
- No NOTE-TAKING instruction (primes Claude to over-save instead of doing tasks)
- ACTIONS FIRST rule: execute tasks, never save to memory instead of acting
- SEARCH BEFORE ASKING: use tools to search before requesting clarification
- Anti-hallucination: VERIFY FIRST, NEVER fabricate, no "updated from X to Y" without reading existing note

### Channel Monitoring

- `@bot monitor #channel` starts monitoring → bot joins channel, saves to `monitored_channels` DB table
- Channels with rules file (`channels/<name>.md`) get full tools + guardrails
- Channels without rules get read-only tools (Read, Glob, Grep)
- Code-level guardrails: `can_use_tool` callback blocks rm -rf, force push, secret reads, browser tools
- Monitored prefix includes: security guardrails, anti-hallucination rules, NO_RESPONSE guidance
- NO_RESPONSE suppression: if response starts with "NO_RESPONSE", suppress posting (questions always get a response)
- Thread follow-ups require `ALLOWED_USER_IDS` authorization

### Note-Taking

Claude handles note-saving naturally via the Write tool — no keyword detection or special handler. Notes saved to `knowledge/notes/` are injected into every future session's system prompt as full content (100KB budget).

### Workflows

`@bot run <workflow> key=value` loads a workflow template from `workflows/`, substitutes parameters, and injects as a prompt. Claude executes step-by-step.

### Azure DevOps MCP

- Configured in `~/.claude/settings.json` under `mcpServers.azure-devops`
- Requires `az login --allow-no-subscriptions` before starting the bot
- Token expires periodically — if MCP returns "fetch failed", re-run `az login`
- Bot auto-detects auth failures and triggers `/azure-login` skill (requires Chrome extension)

## SQLite Schema

Four tables: `threads` (thread_ts PK, channel, status), `messages` (thread_ts FK, role, content), `usage` (cost_usd, duration_ms, num_turns), `monitored_channels` (channel_id PK, channel_name, added_by).

## Commit Rules

**Run tests before every commit. No exceptions.**

```bash
pytest tests/ -v
```

All 196 tests must pass before committing.

## Configuration

All via `.env` (see `.env.example`). Key vars: `BOT_USERNAME`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ALLOWED_USER_IDS`, `CLAUDE_WORK_DIR` (default ~/Projects), `CLAUDE_MAX_TURNS` (100), `DAILY_BUDGET_USD` (500), `SYSTEM_PROMPT_FILE`.
