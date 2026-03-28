"""CLI entry point for shadow.ai."""

import sys


def main():
    """Route CLI commands: shadow-ai [init|run]."""
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from shadow_ai.setup_wizard import run_wizard
        run_wizard()
    elif len(sys.argv) > 1 and sys.argv[1] == "--version":
        from shadow_ai import __version__
        print(f"shadow.ai v{__version__}")
    elif len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("shadow.ai — Your AI co-worker via Slack")
        print()
        print("Usage:")
        print("  shadow-ai          Start the bot")
        print("  shadow-ai init     Interactive setup wizard")
        print("  shadow-ai --version")
        print("  shadow-ai --help")
    else:
        from shadow_ai.app import main as run_bot
        run_bot()
