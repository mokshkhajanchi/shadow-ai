# moksh.ai — Slack Claude Code Bot

Run Claude Code on your local Mac via Slack. Tag the bot in any thread and it invokes Claude Code with full filesystem access, MCP integrations, and multi-turn conversation support.

## Architecture

```
Slack Thread → Bot (Socket Mode) → Claude Agent SDK → Claude Code CLI → Your Local Files
                                                    → MCP Servers (Jira, Azure DevOps, Slack, Filesystem)
                                                    → Knowledge Base (indexed docs + code)
```

- **No public URL needed** — uses Slack Socket Mode over WebSocket
- **Runs locally** — full access to your filesystem, git repos, dev tools
- **Persistent sessions** — multi-turn conversations survive bot restarts via SQLite
- **MCP integrations** — auto-discovers and uses Jira, Azure DevOps, Slack, and filesystem MCP servers

---

## Features at a Glance

| Feature | Description |
|---|---|
| **Multi-turn conversations** | Continue conversations in Slack threads — bot tracks context across messages |
| **File & image uploads** | Attach screenshots, code files, configs — bot passes them to Claude as context |
| **Model selection** | Prefix with `opus:`, `haiku:`, `sonnet:` to choose model per request |
| **Thinking mode** | Prefix with `think:` for extended reasoning on complex problems |
| **Streaming progress** | "Working on it..." message updates live with tool activity |
| **Cost tracking** | Per-session cost tracking, daily budgets, usage analytics in SQLite |
| **Knowledge base** | Auto-indexes docs and code from configured paths — Claude reads on demand |
| **MCP tool discovery** | Auto-discovers Jira, Azure DevOps, Slack tools at startup |
| **PR review** | `review <PR-URL>` — analyzes diff and posts review comments on Azure DevOps PRs |
| **Auto PR review** | Claude automatically reviews every PR it creates |
| **Thread summary** | `summarize` — generates a recap of decisions, actions, and outcomes |
| **App Home dashboard** | Live dashboard with sessions, cost, threads, and quick actions |
| **Slash commands** | `/claude`, `/claude-status`, `/claude-cost` for quick access |
| **Reaction feedback** | Tracks 👍/👎 on bot responses for quality metrics |
| **Auto-compact** | Detects context overload and auto-restarts sessions with clean history |
| **Session management** | Idle timeout, session cap, memory optimization |

---

## Use Cases

### 1. Code Development

Write, refactor, and debug code directly from Slack:

```
@bot create a Python script that parses CSV files and generates summary statistics
@bot refactor the order processing module to use async/await
@bot fix the TypeError in src/utils/parser.py line 42
```

Claude Code has full access to Read, Write, Edit, Bash, Glob, Grep, and Agent tools on your local machine.

### 2. Code Review

Review any Azure DevOps PR — bot fetches the diff, analyzes it, and posts review comments directly on the PR:

```
@bot review https://dev.azure.com/GoFynd/FyndPlatformCore/_git/avis/pullrequest/243753
```

The review covers:
- Code correctness and logic errors
- Style and DRY principle adherence
- Performance implications
- Security concerns
- Test coverage gaps
- Specific inline comments on problematic lines

**Auto-review**: When the bot creates a PR itself, it automatically reviews its own code and posts comments.

### 3. Jira Ticket Management

Query, create, and update Jira tickets directly from Slack:

```
@bot share my latest Jira tickets I need to work on
@bot what's the status of FPP-33050?
@bot create a Jira ticket for fixing the order timeout bug
@bot transition FPP-33282 to "In Progress"
```

### 4. Azure DevOps Operations

Manage PRs, branches, and work items:

```
@bot list my open PRs in the avis repo
@bot create a PR from branch fix/order-timeout to version/2.12.0 in avis
@bot check the build status of PR #243561
```

### 5. Knowledge Base Queries

Ask questions about your team's documentation, codebase, or domain:

```
@bot how does the delivery charge calculation work?
@bot what's the flow for RMA returns?
@bot explain the DP assignment algorithm
@bot search my notes for kafka topic configuration
```

The bot indexes your configured knowledge paths at startup:
- **Small files** (< 20KB) are loaded inline for instant answers
- **Large files** are indexed (headings/signatures) — Claude reads them on demand
- **Code files** have function/class signatures extracted for quick navigation

### 6. Debugging & Investigation

Upload screenshots, logs, or error traces for analysis:

```
@bot [attach error_screenshot.png] what's causing this UI issue?
@bot [attach stacktrace.log] debug this exception
@bot check the last 50 lines of /var/log/app.log and find the error
```

### 7. Git Operations

Manage branches, commits, and repositories:

```
@bot create a new branch fix/order-timeout from version/2.12.0
@bot commit and push the changes with message "Fix order timeout"
@bot show me the git log for the last 5 commits
@bot cherry-pick commit abc123 into version/2.11.7
```

### 8. Complex Multi-Step Tasks

Claude Code can chain multiple operations:

```
@bot find all usages of SkywarpClient in the avis codebase, replace them with
SafeSkywarpClient, update the imports, run the tests, and create a PR
```

This single message triggers Claude to:
1. Search the codebase (`Glob`, `Grep`)
2. Read and understand the code (`Read`)
3. Make the changes (`Edit`, `Write`)
4. Run tests (`Bash`)
5. Commit, push, create PR (`Bash`, MCP tools)
6. Auto-review the PR (MCP tools)

### 9. Thread Summary & Export

After a long debugging session, get a recap:

```
@bot summarize
```

Generates a structured summary covering:
- Key decisions made
- Actions taken (files changed, PRs created)
- Outcomes and current status
- Open items or follow-ups needed

### 10. Extended Reasoning

For complex architectural questions or debugging, enable thinking mode:

```
@bot think: we're seeing intermittent 500 errors on the order creation API
under high load. The error happens in the DP assignment step. What could
cause this and how should we fix it?
```

Claude uses extended reasoning tokens to think through the problem before responding.

### 11. Model Selection for Cost Optimization

Use cheaper models for simple tasks, expensive ones for complex work:

```
@bot haiku: what's the syntax for a Python list comprehension?
@bot sonnet: explain how the manifest flow works
@bot opus: review this complex database migration and suggest improvements
```

### 12. Bot Administration

Monitor and manage the bot:

```
@bot status              → active sessions, cost, budget, satisfaction rating
@bot kill all            → terminate all Claude sessions
@bot summarize           → recap the current thread
```

Or use slash commands:
```
/claude <prompt>         → start a new session (creates a thread)
/claude-status           → ephemeral status check
/claude-cost             → ephemeral cost breakdown
```

---

## Quick Start

### Prerequisites

- **macOS** with Python 3.10+
- **Node.js 18+** (for Claude Code CLI)
- **Claude Code CLI** installed and authenticated (`claude` command works in terminal)
- **Claude Max/Pro subscription** (for Claude Code usage)

### Step 1: Create the Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it (e.g., `moksh.ai`), pick your workspace

**Enable Socket Mode:**
3. **Settings → Basic Information → App-Level Tokens** → Generate with `connections:write` scope → copy `xapp-...` token

**Enable Events:**
4. **Features → Event Subscriptions** → Enable Events ON
5. Subscribe to bot events: `app_mention`, `message.channels`, `message.groups`, `message.im`, `reaction_added`, `reaction_removed`

**Set Bot Permissions:**
6. **Features → OAuth & Permissions → Bot Token Scopes**, add:
   - `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`
   - `files:read`, `groups:history`, `groups:read`, `im:history`, `im:read`
   - `reactions:read`, `reactions:write`, `commands`

**Enable Home Tab:**
7. **Features → App Home** → toggle **Home Tab** ON

**Register Slash Commands (optional):**
8. **Features → Slash Commands** → Create: `/claude`, `/claude-status`, `/claude-cost`

**Install:**
9. **Settings → Install App** → Install to Workspace → copy `xoxb-...` token
10. **Settings → Socket Mode** → toggle ON

### Step 2: Set Up Locally

```bash
git clone <repo-url> && cd slack-claude-code-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your tokens
```

### Step 3: Configure `.env`

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
ALLOWED_USER_IDS=U0361L1S11C              # Your Slack user ID
CLAUDE_WORK_DIR=.                          # Working directory for Claude Code
DB_PATH=./slack_claude_bot.db
MAX_CONCURRENT=5
CLAUDE_MAX_TURNS=200
REQUEST_TIMEOUT=12000
CLAUDE_PERMISSION_MODE=bypassPermissions
DAILY_BUDGET_USD=100
KNOWLEDGE_PATHS=/path/to/your/knowledge/folder
```

### Step 4: Run

```bash
env $(grep -v '^#' .env | xargs) python bot.py
```

Find your Slack User ID: Profile → **⋯** menu → **Copy member ID**

---

## Configuration Reference

| Env Variable | Default | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | (required) | `xoxb-...` bot OAuth token |
| `SLACK_APP_TOKEN` | (required) | `xapp-...` socket mode token |
| `ALLOWED_USER_IDS` | (required) | Comma-separated Slack user IDs |
| `CLAUDE_WORK_DIR` | `~/Projects` | Claude Code's working directory |
| `CLAUDE_MAX_TURNS` | `30` | Max agent turns per request |
| `CLAUDE_ALLOWED_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,Agent` | Permitted Claude Code tools |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | `acceptEdits` or `bypassPermissions` |
| `DAILY_BUDGET_USD` | `0` | Daily cost limit in USD (0 = unlimited) |
| `KNOWLEDGE_PATHS` | (none) | Comma-separated paths to knowledge/code folders |
| `CLAUDE_MODEL` | (SDK default) | Default model (e.g., `claude-sonnet-4-6`) |
| `CLAUDE_THINKING` | `off` | Thinking mode: `off`, `adaptive`, `enabled` |
| `CLAUDE_THINKING_BUDGET` | `10000` | Token budget when thinking is `enabled` |
| `MAX_CONCURRENT` | `5` | Thread pool workers for concurrent sessions |
| `REQUEST_TIMEOUT` | `600` | Per-request timeout in seconds |
| `SESSION_IDLE_TIMEOUT` | `600` | Kill idle sessions after N seconds (default 10 min) |
| `MAX_ACTIVE_SESSIONS` | `3` | Max concurrent in-memory sessions (LRU eviction) |
| `DB_PATH` | `./slack_claude_bot.db` | SQLite database path |
| `LOG_FILE` | `./bot.log` | Log file path (auto-rotated at 10MB) |

---

## Bot Commands Reference

### In-Thread Commands (via @mention)

| Command | Description |
|---|---|
| `status` / `cost` / `usage` | Active sessions, today's cost, budget, satisfaction rating |
| `kill all` / `stop all` | Kill all active Claude Code sessions |
| `summarize` / `summary` / `recap` | Generate thread summary with decisions and actions |
| `review <PR-URL>` | Review an Azure DevOps PR and post comments |

### Slash Commands

| Command | Description |
|---|---|
| `/claude <prompt>` | Start a new Claude session in a new thread |
| `/claude-status` | Show bot status (ephemeral — only you see it) |
| `/claude-cost` | Show cost breakdown (ephemeral) |

### Message Prefixes

| Prefix | Effect |
|---|---|
| `opus:` | Use Claude Opus model for this request |
| `sonnet:` | Use Claude Sonnet model |
| `haiku:` | Use Claude Haiku model (fastest, cheapest) |
| `think:` | Enable extended reasoning for this request |

Prefixes can be combined: `@bot opus: think: analyze this race condition`

### Interactive Elements

| Element | Where | Action |
|---|---|---|
| **Stop Session** button | Every bot response | Kills the session for that thread |
| **Show Details** button | Long responses | Expands truncated content |
| **Kill All Sessions** button | App Home tab | Kills all active sessions |
| **Refresh** button | App Home tab | Refreshes dashboard data |

### Reaction Feedback

React to any bot message with these emojis to provide feedback:

| Reaction | Score | Meaning |
|---|---|---|
| 👍 | +1 | Good response |
| 👎 | -1 | Bad response |
| 🎉 | +1 | Excellent response |
| 😕 | -1 | Confusing response |

Feedback is tracked in the database and shown in the `status` command and App Home dashboard.

---

## Knowledge Base System

Configure `KNOWLEDGE_PATHS` to point at your documentation and code folders:

```bash
KNOWLEDGE_PATHS=/path/to/notes,/path/to/codebase,/path/to/api-docs
```

At startup, the bot scans all paths and builds two indexes:

**Document Index** (`.md`, `.txt`, `.yaml`, `.json`):
- Files < 20KB → loaded **inline** into the system prompt (instant answers)
- Files > 20KB → listed as a **file index table** (Claude reads on demand via `Read` tool)
- Headings and structure extracted for navigation

**Code Index** (`.py`, `.js`, `.ts`, `.jsx`, `.tsx`):
- Function signatures and class names extracted via regex
- Injected into system prompt so Claude can jump to relevant code without Glob/Grep

**Skipped directories**: `node_modules`, `.venv`, `.git`, `__pycache__`, `dist`, `build`, `docker`, `backup`, `.next`, `coverage`, `.tox`

---

## MCP Integration

The bot auto-discovers MCP servers from `~/.claude/settings.json` and project `.mcp.json` files. At startup, it:

1. Connects a temporary session to enumerate all available tools
2. Builds a tool catalog with names and descriptions
3. Injects the catalog into every session's system prompt
4. Auto-approves all MCP tools via wildcard patterns (`mcp__<server>__*`)

**Supported MCP servers** (auto-discovered):
- **Jira** — ticket search, creation, updates, comments, transitions
- **Azure DevOps** — PRs, branches, repos, work items, builds
- **Slack** — message sending, thread management
- **Filesystem** — file operations via MCP protocol

---

## Session Management

### How Sessions Work

Each Slack thread gets its own Claude Code session:

1. **First message** → spawns a new Claude Code subprocess with a dedicated event loop
2. **Follow-ups** → reuses the same subprocess (no new spawn — fast)
3. **Bot restart** → next message restores from SQLite history (replays last 20 messages)
4. **Idle > 10 min** → session auto-killed (configurable via `SESSION_IDLE_TIMEOUT`)
5. **Session cap reached** → oldest idle session evicted (configurable via `MAX_ACTIVE_SESSIONS`)

### Auto-Compact

When Claude returns an empty response (context overload after many tool calls), the bot:
1. Kills the bloated session
2. Restores from database with clean message history (no tool-use noise)
3. Retries the query — effectively a "compact" operation
4. User sees no interruption

### Memory Optimization

- **Session cap**: Max 3 active sessions (each ~300-600MB). Oldest evicted on overflow.
- **Idle cleanup**: Background thread runs every 5 minutes, kills idle sessions.
- **Thread lock cleanup**: Stale locks removed for inactive threads.
- **Details store TTL**: Orphaned "Show Details" entries evicted after 24 hours.
- **Log rotation**: 10MB max with 3 backups.
- **SQLite safety**: All DB access uses `try/finally` to guarantee connection cleanup.

---

## App Home Dashboard

Click the bot's name in Slack → **Home** tab to see a live dashboard:

- **Active Sessions**: Count of running Claude Code subprocesses
- **Today's Cost**: Running total for the day
- **Budget Remaining**: How much of the daily budget is left
- **All-Time Cost**: Cumulative spend
- **Satisfaction**: Percentage based on 👍/👎 reactions
- **Recent Threads**: Last 10 threads with status, cost, query count, and direct links
- **Quick Actions**: Kill All Sessions, Refresh

---

## PR Review Workflow

### Manual Review

```
@bot review https://dev.azure.com/GoFynd/FyndPlatformCore/_git/brunt/pullrequest/243753
```

The bot:
1. Fetches PR details via Azure DevOps MCP tools
2. Gets the diff from local git repos
3. Analyzes: correctness, style, performance, security, test coverage
4. Posts a general review comment (`## Code Review by moksh.ai`)
5. Posts up to 5 inline comments on specific lines
6. Reports the verdict back in Slack

### Auto-Review on PR Creation

When Claude creates a PR (via `mcp__azure-devops__repo_create_pull_request`), it automatically:
1. Fetches the diff of its own changes
2. Self-reviews the code
3. Posts review comments on the PR
4. Reports findings in the Slack thread

All review comments are posted with **Active** status so they remain visible and require resolution.

---

## Troubleshooting

**Bot doesn't respond:**
- Check Socket Mode is enabled in Slack app settings
- Verify `SLACK_APP_TOKEN` starts with `xapp-`
- Make sure the bot is invited to the channel (`/invite @bot-name`)

**"Not authorized" error:**
- Your Slack user ID isn't in `ALLOWED_USER_IDS`
- Copy member ID from Slack profile → **⋯** menu → Copy member ID

**Claude Code errors:**
- Run `claude --version` in terminal to verify CLI works
- Make sure you're authenticated: run `claude` interactively first
- Check that `CLAUDE_WORK_DIR` exists

**Empty responses:**
- Session may be context-saturated. Auto-compact should handle this automatically.
- If persistent, try `kill all` and start a new thread.

**High memory usage:**
- Each Claude Code session uses 300-600MB. Reduce `MAX_ACTIVE_SESSIONS` (default: 3).
- Reduce `SESSION_IDLE_TIMEOUT` (default: 600s) to free sessions faster.

**MCP tools not working:**
- Check `~/.claude/settings.json` has MCP servers configured
- Verify startup logs show `[MCP] Tool catalog cached: N tools`
- Ensure the MCP server processes are running

**File uploads not working:**
- Add `files:read` scope to your Slack app → reinstall the app
- Check startup logs for `[FILES]` entries

**Import errors for `claude_agent_sdk`:**
- Install with `pip install claude-agent-sdk` (not the deprecated `claude-code-sdk`)
- Requires Python 3.10+

---

## Technical Details

### SQLite Schema

| Table | Purpose |
|---|---|
| `threads` | Thread tracking: thread_ts (PK), channel, status, timestamps, last_slack_ts |
| `messages` | All user/assistant messages: role, content, timestamps |
| `usage` | Cost tracking: cost_usd, duration, turns, session_id per request |
| `feedback` | Reaction tracking: message_ts, reaction, score (+1/-1) |

### Dependencies

```
slack-bolt>=1.18.0      # Slack framework
slack-sdk>=3.27.0       # Slack API client
claude-agent-sdk>=0.1.48 # Claude Code SDK
anyio>=4.0.0            # Async I/O
python-dotenv>=1.0.0    # Environment variables
httpx (transitive)      # HTTP client for file downloads
```

### File Structure

```
slack-claude-code-bot/
├── bot.py              # Single-file application (~2100 lines)
├── requirements.txt    # Python dependencies
├── .env                # Configuration (not committed)
├── slack_claude_bot.db # SQLite database (auto-created)
├── bot.log             # Rotating log file (auto-created)
├── CLAUDE.md           # Claude Code project instructions
└── readme.md           # This file
```
