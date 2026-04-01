"""Live eval runner: sends real messages to Slack and grades real responses.

Usage:
    python -m evals.live                          # Run all scenarios
    python -m evals.live --category guardrails    # Run specific category
    python -m evals.live --channel C0AQ61HQ550    # Use specific channel
    python -m evals.live --dry-run                # Show scenarios without sending

Requires:
    - Bot running (shadow-ai)
    - SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env
    - A test channel where the bot is a member
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

from evals.runner import load_scenarios, grade_scenario
from evals.graders.quality import grade_quality_with_llm
from evals.reporter import print_report

load_dotenv()
logger = logging.getLogger("shadow-ai-evals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Default test channel — override with --channel
DEFAULT_TEST_CHANNEL = os.environ.get("EVAL_CHANNEL", "")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

# How long to wait for bot response (seconds)
RESPONSE_TIMEOUT = 180
POLL_INTERVAL = 3


def get_bot_user_id(client: WebClient) -> str:
    """Get the bot's own user ID."""
    resp = client.auth_test()
    return resp["user_id"]


def send_message(client: WebClient, channel: str, text: str, thread_ts: str = None) -> str:
    """Send a message to Slack. Returns the message timestamp."""
    kwargs = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    return resp["ts"]


def wait_for_bot_reply(client: WebClient, channel: str, thread_ts: str, bot_user_id: str,
                       timeout: int = RESPONSE_TIMEOUT) -> dict | None:
    """Poll a Slack thread for the bot's reply.

    Returns dict with: text, ts, tool_summary (from reactions/context)
    """
    start = time.time()
    seen_ts = set()

    while time.time() - start < timeout:
        try:
            resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
            messages = resp.get("messages", [])

            for msg in messages:
                if msg.get("ts") in seen_ts:
                    continue
                seen_ts.add(msg["ts"])

                # Check if this is a bot reply (not the original message)
                if msg.get("bot_id") and msg["ts"] != thread_ts:
                    return {
                        "text": msg.get("text", ""),
                        "ts": msg["ts"],
                        "blocks": msg.get("blocks", []),
                    }
        except Exception as e:
            logger.warning(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)

    return None


def extract_tool_calls_from_log(thread_ts: str, log_file: str = "bot.log") -> list[dict]:
    """Extract tool calls from bot.log for a specific thread."""
    tool_calls = []
    if not Path(log_file).exists():
        return tool_calls

    pattern = re.compile(rf"\[CC:{re.escape(thread_ts)}\] 🔧 (\w+)")
    with open(log_file) as f:
        for line in f:
            match = pattern.search(line)
            if match:
                tool_calls.append({"tool": match.group(1), "input": {}})
    return tool_calls


def extract_cost_from_log(thread_ts: str, log_file: str = "bot.log") -> tuple[float, float]:
    """Extract cost and duration from bot.log for a specific thread."""
    cost = 0.0
    duration = 0.0
    if not Path(log_file).exists():
        return cost, duration

    pattern = re.compile(rf"\[CC:{re.escape(thread_ts)}\] RESULT.*cost=\$([0-9.]+).*duration=([0-9.]+)s")
    with open(log_file) as f:
        for line in f:
            match = pattern.search(line)
            if match:
                cost = float(match.group(1))
                duration = float(match.group(2))
    return cost, duration


def run_live_scenario(client: WebClient, channel: str, bot_user_id: str, scenario: dict) -> dict:
    """Run a single scenario against the live bot."""
    name = scenario.get("name", "unnamed")
    input_data = scenario.get("input", {})
    text = input_data.get("text", "")
    monitored = input_data.get("monitored", False)

    logger.info(f"[EVAL] Running: {name}")

    # For non-monitored scenarios, prepend @bot mention
    if not monitored:
        text = f"<@{bot_user_id}> {text}"

    # Send message
    msg_ts = send_message(client, channel, text)
    logger.info(f"[EVAL] Sent message: {msg_ts}")

    # Wait for reply
    reply = wait_for_bot_reply(client, channel, msg_ts, bot_user_id)

    if reply is None:
        logger.warning(f"[EVAL] No reply received for: {name}")
        return {
            "name": name,
            "category": scenario.get("category", "unknown"),
            "severity": scenario.get("severity", "normal"),
            "passed": False,
            "critical_failed": scenario.get("severity") == "critical",
            "checks": {"bot_replied": {"pass": False, "detail": "No reply within timeout"}},
        }

    response = reply["text"]
    logger.info(f"[EVAL] Got reply: {response[:100]}...")

    # Extract tool calls and cost from logs
    tool_calls = extract_tool_calls_from_log(msg_ts)
    cost, duration = extract_cost_from_log(msg_ts)

    # Grade with standard checks
    result = grade_scenario(scenario, response, tool_calls, cost, duration)

    # LLM-as-judge quality grading
    quality = grade_quality_with_llm(input_data["text"], response)
    if quality:
        result["checks"]["llm_quality"] = quality
        result["quality_scores"] = quality.get("scores", {})

    result["response"] = response[:500]
    result["cost"] = cost
    result["duration"] = duration
    result["tool_calls"] = len(tool_calls)

    return result


def run_live_evals(channel: str, category: str = None, scenario_dir: str = "evals/scenarios") -> list[dict]:
    """Run all scenarios against the live bot."""
    client = WebClient(token=BOT_TOKEN)
    bot_user_id = get_bot_user_id(client)
    scenarios = load_scenarios(scenario_dir)

    if category:
        scenarios = [s for s in scenarios if s.get("category") == category]

    logger.info(f"[EVAL] Running {len(scenarios)} scenarios against live bot in <#{channel}>")
    logger.info(f"[EVAL] Bot user: {bot_user_id}")
    print()

    results = []
    for i, scenario in enumerate(scenarios, 1):
        print(f"  [{i}/{len(scenarios)}] {scenario.get('name', 'unnamed')}...", end=" ", flush=True)
        result = run_live_scenario(client, channel, bot_user_id, scenario)
        status = "✅" if result["passed"] else "❌"
        print(status)
        results.append(result)

        # Brief pause between scenarios to avoid rate limiting
        if i < len(scenarios):
            time.sleep(2)

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="shadow.ai live eval runner")
    parser.add_argument("--channel", type=str, default=DEFAULT_TEST_CHANNEL,
                        help="Slack channel ID to run evals in")
    parser.add_argument("--category", type=str, help="Only run scenarios in this category")
    parser.add_argument("--scenario-dir", default="evals/scenarios", help="Scenario directory")
    parser.add_argument("--dry-run", action="store_true", help="Show scenarios without sending")
    args = parser.parse_args()

    if not BOT_TOKEN:
        print("Error: SLACK_BOT_TOKEN not set in .env")
        sys.exit(1)

    if not args.channel:
        print("Error: No channel specified. Use --channel C0AQ61HQ550 or set EVAL_CHANNEL in .env")
        sys.exit(1)

    if args.dry_run:
        scenarios = load_scenarios(args.scenario_dir)
        if args.category:
            scenarios = [s for s in scenarios if s.get("category") == args.category]
        print(f"\n  {len(scenarios)} scenarios would run:\n")
        for s in scenarios:
            sev = "🔴" if s.get("severity") == "critical" else "⚪"
            print(f"  {sev} [{s.get('category')}] {s.get('name')}")
            print(f"     Input: {s['input']['text'][:80]}")
        print()
        return

    results = run_live_evals(args.channel, args.category, args.scenario_dir)
    print()
    print_report(results)

    # Save results to JSON
    output_file = Path("evals") / "results" / f"live_{int(time.time())}.json"
    output_file.parent.mkdir(exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Results saved to: {output_file}")

    if any(r.get("critical_failed") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
