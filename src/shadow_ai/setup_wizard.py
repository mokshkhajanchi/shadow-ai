"""Interactive setup wizard for shadow.ai."""

import os
import re
from pathlib import Path

from shadow_ai import __version__


def _prompt(label: str, default: str = "", required: bool = False, validate=None) -> str:
    """Prompt user for input with optional default and validation."""
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
    """Run the interactive setup wizard."""
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
    print("  Step 1/5: Your Identity")
    print("  ───────────────────────")
    print("  Choose a username for your bot. It will appear as:")
    print("    username.shadow.ai")
    print()
    username = _prompt("Username (e.g. moksh, john, dev-team)", required=True, validate=_validate_username)
    bot_identity = f"{username}.shadow.ai"
    print(f"\n    Your bot: {bot_identity}")
    print()

    # ── Step 2: Create Slack App ──
    print("  Step 2/5: Create Slack App")
    print("  ──────────────────────────")
    print(f"  Let's create your Slack bot named '{bot_identity}'.")
    print()
    print("  1. Go to https://api.slack.com/apps")
    print("  2. Click 'Create New App' > 'From an app manifest'")
    print("  3. Select your workspace")
    print("  4. Paste this JSON manifest:")
    print()

    manifest = SLACK_APP_MANIFEST.replace("BOT_DISPLAY_NAME", bot_identity)
    print("  ─── Copy below ───")
    print(manifest)
    print("  ─── End copy ─────")
    print()
    print("  5. Click 'Create'")
    print("  6. Go to 'Install App' > Install to workspace")
    print()

    input("  Press Enter when your Slack app is created...")
    print()

    print("  Now get your tokens:")
    print("  • Bot Token: OAuth & Permissions > Bot User OAuth Token (xoxb-...)")
    print("  • App Token: Basic Information > App-Level Tokens > Generate (scope: connections:write)")
    print()
    bot_token = _prompt("Bot Token (xoxb-...)", required=True, validate=_validate_bot_token)
    app_token = _prompt("App Token (xapp-...)", required=True, validate=_validate_app_token)
    print()

    # ── Step 3: Access Control ──
    print("  Step 3/5: Access Control")
    print("  ────────────────────────")
    print("  Who can use the bot? Find Slack User IDs:")
    print("  Click a profile > More > Copy member ID")
    print()
    user_id = _prompt("Your Slack User ID", required=True)
    extra_ids = _prompt("Additional user IDs (comma-separated, or empty)")
    allowed = user_id
    if extra_ids:
        allowed = f"{user_id},{extra_ids}"
    print()

    # ── Step 4: Claude Code ──
    print("  Step 4/5: Claude Code")
    print("  ─────────────────────")
    work_dir = _prompt("Working directory (where Claude reads/writes)", default="~/Projects")
    permission = _prompt("Permission mode (acceptEdits/bypassPermissions)", default="acceptEdits")
    budget = _prompt("Daily budget in USD (0 = unlimited)", default="0")
    max_turns = _prompt("Max conversation turns", default="30")
    print()

    # ── Step 5: Optional ──
    print("  Step 5/5: Optional")
    print("  ──────────────────")
    knowledge = _prompt("Knowledge base paths (comma-separated, or empty)")
    prompt_file = _prompt("Custom system prompt file (or empty)")
    print()

    # ── Write .env ──
    lines = [
        f"# {bot_identity} — generated by shadow-ai init",
        f"",
        f"# Bot identity",
        f"BOT_USERNAME={username}",
        f"",
        f"# Slack (required)",
        f"SLACK_BOT_TOKEN={bot_token}",
        f"SLACK_APP_TOKEN={app_token}",
        f"ALLOWED_USER_IDS={allowed}",
        f"",
        f"# Claude Code",
        f"CLAUDE_WORK_DIR={work_dir}",
        f"CLAUDE_PERMISSION_MODE={permission}",
        f"CLAUDE_MAX_TURNS={max_turns}",
        f"DAILY_BUDGET_USD={budget}",
    ]

    if knowledge:
        lines.append(f"KNOWLEDGE_PATHS={knowledge}")
    if prompt_file:
        lines.append(f"SYSTEM_PROMPT_FILE={prompt_file}")

    lines.append("")

    env_path.write_text("\n".join(lines))

    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │  {bot_identity:<40s}│")
    print(f"  │                                         │")
    print(f"  │  Configuration saved to .env             │")
    print(f"  │  Run `shadow-ai` to start!               │")
    print(f"  └─────────────────────────────────────────┘")
    print()
