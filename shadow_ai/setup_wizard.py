"""Interactive setup wizard for shadow.ai."""

import re
from pathlib import Path

from shadow_ai import __version__


def _prompt(label: str, default: str = "", required: bool = False, validate=None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"  {label}{suffix}: ").strip()
        if not value and default:
            value = default
        if required and not value:
            print("    This field is required.")
            continue
        if validate and value and not validate(value):
            continue
        return value


def _validate_bot_token(token: str) -> bool:
    if not token.startswith("xoxb-"):
        print("    Must start with 'xoxb-'. Get it from Slack App > OAuth & Permissions.")
        return False
    return True


def _validate_app_token(token: str) -> bool:
    if not token.startswith("xapp-"):
        print("    Must start with 'xapp-'. Get it from Slack App > Basic Information > App-Level Tokens.")
        return False
    return True


def _validate_username(name: str) -> bool:
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', name):
        print("    Use only letters, numbers, hyphens, underscores. Must start with a letter.")
        return False
    return True


SLACK_APP_MANIFEST = """{
  "display_information": {
    "name": "BOT_DISPLAY_NAME",
    "description": "AI co-worker powered by shadow.ai",
    "background_color": "#1a1a2e"
  },
  "features": {
    "app_home": {
      "home_tab_enabled": true,
      "messages_tab_enabled": true,
      "messages_tab_read_only_enabled": false
    },
    "bot_user": {
      "display_name": "BOT_DISPLAY_NAME",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "channels:history",
        "channels:read",
        "chat:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "reactions:read",
        "reactions:write",
        "users:read",
        "files:read"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": [
        "app_home_opened",
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim",
        "reaction_added",
        "reaction_removed"
      ]
    },
    "interactivity": {
      "is_enabled": true
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": true
  }
}"""


def run_wizard():
    """Run the interactive setup wizard — 3 simple steps."""
    env_path = Path(".env")

    print()
    print("  ┌─────────────────────────────────────────┐")
    print(f"  │  shadow.ai v{__version__:<29s}│")
    print("  │  Created by Moksh Khajanchi              │")
    print("  │  Setup Wizard                            │")
    print("  └─────────────────────────────────────────┘")
    print()

    if env_path.exists():
        overwrite = input("  .env already exists. Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("  Keeping existing .env. Run `shadow-ai` to start.")
            return

    # ── Step 1: Identity ──
    print("  Step 1/3: Your Identity")
    print("  ───────────────────────")
    print("  Choose a username. Your bot will be: username.shadow.ai")
    print()
    username = _prompt("Username (e.g. moksh, john, dev-team)", required=True, validate=_validate_username)
    bot_identity = f"{username}.shadow.ai"
    print(f"\n    Your bot: {bot_identity}\n")

    # ── Step 2: Create Slack App ──
    print("  Step 2/3: Create Slack App")
    print("  ──────────────────────────")
    print(f"  Create '{bot_identity}' on Slack:")
    print()
    print("  1. Go to https://api.slack.com/apps")
    print("  2. Click 'Create New App' > 'From an app manifest'")
    print("  3. Select your workspace, paste this manifest:")
    print()

    manifest = SLACK_APP_MANIFEST.replace("BOT_DISPLAY_NAME", bot_identity)
    print("  ─── Copy below ───")
    print(manifest)
    print("  ─── End copy ─────")
    print()
    print("  4. Click 'Create' > 'Install App' > Install to workspace")
    print()

    input("  Press Enter when done...")
    print()

    print("  Get your tokens:")
    print("  • Bot Token: OAuth & Permissions > Bot User OAuth Token")
    print("  • App Token: Basic Information > App-Level Tokens > Generate (scope: connections:write)")
    print()
    bot_token = _prompt("Bot Token (xoxb-...)", required=True, validate=_validate_bot_token)
    app_token = _prompt("App Token (xapp-...)", required=True, validate=_validate_app_token)
    print()

    # ── Step 3: Access Control ──
    print("  Step 3/3: Who Can Use the Bot?")
    print("  ──────────────────────────────")
    print("  Find Slack User IDs: click profile > More > Copy member ID")
    print()
    user_id = _prompt("Your Slack User ID", required=True)
    extra_ids = _prompt("Additional user IDs (comma-separated, or empty)")
    allowed = user_id
    if extra_ids:
        allowed = f"{user_id},{extra_ids}"
    print()

    # ── Write .env with opinionated defaults ──
    lines = [
        f"# {bot_identity} — generated by shadow-ai init",
        f"",
        f"# Identity",
        f"BOT_USERNAME={username}",
        f"",
        f"# Slack",
        f"SLACK_BOT_TOKEN={bot_token}",
        f"SLACK_APP_TOKEN={app_token}",
        f"ALLOWED_USER_IDS={allowed}",
        f"",
        f"# Claude Code (opinionated defaults — edit if needed)",
        f"CLAUDE_WORK_DIR=~/Projects",
        f"CLAUDE_PERMISSION_MODE=bypassPermissions",
        f"CLAUDE_MAX_TURNS=50",
        f"DAILY_BUDGET_USD=500",
        f"REQUEST_TIMEOUT=3600",
        f"",
        f"# System prompt (ships with a default — customize for your org)",
        f"SYSTEM_PROMPT_FILE=knowledge/system_prompt.example.md",
        f"",
        f"# MCP Servers — auto-discovered from ~/.claude/settings.json",
        f"# Configure MCP servers in Claude Code settings to extend the bot with",
        f"# Jira, Azure DevOps, Sentry, Grafana, GitHub, Slack, and more.",
        f"# See: https://docs.claude.com/en/mcp",
        f"",
        f"# Knowledge base — auto-learns from conversations. Add extra paths here:",
        f"# KNOWLEDGE_PATHS=/path/to/docs,/path/to/code",
        f"",
    ]

    env_path.write_text("\n".join(lines))

    # Auto-create required directories
    Path("knowledge/notes").mkdir(parents=True, exist_ok=True)
    Path("knowledge/conversations").mkdir(parents=True, exist_ok=True)
    Path("~/Projects").expanduser().mkdir(parents=True, exist_ok=True)

    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │  {bot_identity:<40s}│")
    print(f"  │                                         │")
    print(f"  │  Configuration saved to .env             │")
    print(f"  │  Run `shadow-ai` to start!               │")
    print(f"  │                                         │")
    print(f"  │  Tip: Add MCP servers to extend the bot  │")
    print(f"  │  with Jira, Sentry, GitHub, and more.    │")
    print(f"  └─────────────────────────────────────────┘")
    print()
