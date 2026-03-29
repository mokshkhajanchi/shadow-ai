"""CLI entry point for shadow.ai."""

import sys


def main():
    """Route CLI commands: shadow-ai [init|doctor|run]."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "init":
        from shadow_ai.setup_wizard import run_wizard
        run_wizard()
    elif cmd == "doctor":
        from shadow_ai.doctor import run_doctor
        run_doctor()
    elif cmd == "--version":
        from shadow_ai import __version__
        print(f"shadow.ai v{__version__}")
    elif cmd in ("--help", "-h"):
        print("shadow.ai — Your AI co-worker via Slack")
        print()
        print("Usage:")
        print("  shadow-ai          Start the bot")
        print("  shadow-ai init     Interactive setup wizard")
        print("  shadow-ai doctor   Check prerequisites and configuration")
        print("  shadow-ai --version")
        print("  shadow-ai --help")
    else:
        from shadow_ai.app import main as run_bot
        run_bot()
