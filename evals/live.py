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
USER_TOKEN = os.environ.get("EVAL_USER_TOKEN", "")

# How long to wait for bot response (seconds)
RESPONSE_TIMEOUT = 120
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
    """Poll a Slack thread for the bot's final reply.

    Skips progress messages (Working...) and waits for the actual response.
    Returns dict with: text, ts, blocks
    """
    start = time.time()
    last_bot_msg = None

    while time.time() - start < timeout:
        try:
            resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
            messages = resp.get("messages", [])

            for msg in messages:
                # Skip the original message
                if msg["ts"] == thread_ts:
                    continue
                # Only look at bot messages
                if not msg.get("bot_id"):
                    continue

                text = msg.get("text", "")
                # Skip progress/busy messages
                if "Working..." in text or "Taking a look" in text or ":hourglass:" in text:
                    continue
                # Skip empty messages
                if not text.strip():
                    continue

                last_bot_msg = {
                    "text": text,
                    "ts": msg["ts"],
                    "blocks": msg.get("blocks", []),
                }

            # If we found a real reply, wait a bit more for edits then return
            if last_bot_msg:
                time.sleep(5)  # Wait for message to be fully posted/edited
                # Re-fetch to get the latest version (bot may edit the message)
                resp2 = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                for msg in resp2.get("messages", []):
                    if msg.get("ts") == last_bot_msg["ts"]:
                        last_bot_msg["text"] = msg.get("text", "")
                        last_bot_msg["blocks"] = msg.get("blocks", [])
                        break
                return last_bot_msg

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


def _run_multi_step_scenario(sender: WebClient, reader: WebClient, channel: str,
                              bot_user_id: str, scenario: dict, record: bool = False) -> dict:
    """Run a multi-step scenario (e.g., save a fact, then recall it in a new thread)."""
    from evals.graders.golden import grade_against_golden, save_golden

    name = scenario.get("name", "unnamed")
    steps = scenario.get("steps", [])
    expected = scenario.get("expected", {})

    logger.info(f"[EVAL] Running multi-step: {name} ({len(steps)} steps)")

    response = ""
    tool_calls = []
    cost = 0
    duration = 0

    for i, step in enumerate(steps):
        action = step.get("action", "message")
        text = step.get("text", "")
        wait = step.get("wait", 15)

        # All steps use @mention (not monitored)
        msg_text = f"<@{bot_user_id}> {text}"
        msg_ts = send_message(sender, channel, msg_text)
        logger.info(f"[EVAL] Step {i+1}/{len(steps)} ({action}): sent {msg_ts}")

        if action == "save":
            # Claude now handles saves via Write tool — wait for the actual reply
            # to confirm the note was saved before proceeding to recall
            logger.info(f"[EVAL] Step {i+1}: save action — waiting for Claude to save and respond")
            reply = wait_for_bot_reply(reader, channel, msg_ts, bot_user_id)
            if reply:
                logger.info(f"[EVAL] Step {i+1}: save confirmed — {reply['text'][:80]}...")
            else:
                logger.warning(f"[EVAL] Step {i+1}: save may have timed out — no reply")
            # Extra wait for file to be flushed to disk
            time.sleep(5)
        else:
            # Non-save steps: wait for actual reply
            reply = wait_for_bot_reply(reader, channel, msg_ts, bot_user_id)

            if reply:
                response = reply["text"]
                logger.info(f"[EVAL] Step {i+1} reply: {response[:80]}...")
            else:
                logger.warning(f"[EVAL] Step {i+1} no reply")
                if action == "recall":
                    return {
                        "name": name, "category": scenario.get("category", "unknown"),
                        "severity": scenario.get("severity", "normal"),
                        "passed": False, "critical_failed": scenario.get("severity") == "critical",
                        "checks": {"recall_replied": {"pass": False, "detail": "No reply to recall question"}},
                    }

        # Extra pause between steps
        if i < len(steps) - 1:
            time.sleep(5)

    # Grade the LAST step's response against expected
    from evals.runner import grade_scenario
    result = grade_scenario(scenario, response, tool_calls, cost, duration)

    if record and response:
        save_golden(name, response, tool_calls, cost, duration)

    result["response"] = response[:500]
    return result


def run_live_scenario(sender: WebClient, reader: WebClient, channel: str, bot_user_id: str,
                      scenario: dict, record: bool = False) -> dict:
    """Run a single scenario against the live bot."""
    from evals.graders.golden import grade_against_golden, save_golden
    from evals.graders.side_effects import (
        grade_file_created, grade_file_not_modified, grade_log_contains, grade_tool_sequence,
    )

    name = scenario.get("name", "unnamed")

    # Multi-step scenarios (save → recall)
    steps = scenario.get("steps")
    if steps:
        return _run_multi_step_scenario(sender, reader, channel, bot_user_id, scenario, record)

    input_data = scenario.get("input", {})
    text = input_data.get("text", "")
    monitored = input_data.get("monitored", False)
    expected = scenario.get("expected", {})
    verify = scenario.get("verify", {})

    logger.info(f"[EVAL] Running: {name}")

    # Timestamp before sending (for side-effect checks)
    before_ts = time.time()

    # For non-monitored scenarios, prepend @bot mention
    if not monitored:
        text = f"<@{bot_user_id}> {text}"

    # Send message (as user)
    msg_ts = send_message(sender, channel, text)
    logger.info(f"[EVAL] Sent message: {msg_ts}")

    # Wait for reply (read as bot to see all messages)
    reply = wait_for_bot_reply(reader, channel, msg_ts, bot_user_id)

    if reply is None:
        # Check if no-reply is expected (noise filter scenarios with empty response_contains)
        expected_contains = expected.get("response_contains", None)
        no_reply_expected = (expected_contains is not None and len(expected_contains) == 0
                            and "response_not_contains" not in expected
                            and "min_length" not in expected)
        if no_reply_expected:
            logger.info(f"[EVAL] No reply (expected — noise filtered): {name}")
            return {
                "name": name,
                "category": scenario.get("category", "unknown"),
                "severity": scenario.get("severity", "normal"),
                "passed": True,
                "checks": {"no_reply_expected": {"pass": True, "detail": "No reply (correctly suppressed by noise filter)"}},
            }
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
    quality_scores = {}
    if quality:
        result["checks"]["llm_quality"] = quality
        quality_scores = quality.get("scores", {})
        result["quality_scores"] = quality_scores

    # Side-effect verification
    if "file_created" in verify:
        result["checks"].update(
            grade_file_created(verify["file_created"]["dir"], verify["file_created"].get("pattern", "*.md"), before_ts)
        )
    if "file_not_modified" in verify:
        for fp in verify["file_not_modified"]:
            result["checks"].update(grade_file_not_modified(fp, before_ts))
    if "log_contains" in verify:
        result["checks"].update(grade_log_contains("bot.log", verify["log_contains"], before_ts))
    if "tool_sequence" in verify:
        result["checks"].update(grade_tool_sequence("bot.log", msg_ts, verify["tool_sequence"]))

    # Golden comparison (or record)
    if record:
        save_golden(name, response, tool_calls, cost, duration, quality_scores)
        logger.info(f"[EVAL] Recorded golden baseline for: {name}")
    else:
        golden_results = grade_against_golden(name, response, tool_calls, cost, quality_scores)
        result["checks"].update(golden_results)

    # Recompute passed after all checks
    result["passed"] = all(
        c["pass"] for c in result["checks"].values() if c.get("pass") is not None
    )
    result["critical_failed"] = not result["passed"] and scenario.get("severity") == "critical"

    result["response"] = response[:500]
    result["cost"] = cost
    result["duration"] = duration
    result["tool_calls"] = len(tool_calls)

    return result


def run_live_evals(channel: str, category: str = None, record: bool = False,
                    concurrency: int = 1, scenario_dir: str = "evals/scenarios") -> list[dict]:
    """Run all scenarios against the live bot.

    Scenarios run one at a time by default. Use --concurrency N to run N
    scenarios in parallel (should be max_active_sessions - 2).
    """
    # User token sends messages (as you), bot token reads replies
    sender = WebClient(token=USER_TOKEN) if USER_TOKEN else WebClient(token=BOT_TOKEN)
    reader = WebClient(token=BOT_TOKEN)
    bot_user_id = get_bot_user_id(reader)
    scenarios = load_scenarios(scenario_dir)

    if category:
        scenarios = [s for s in scenarios if s.get("category") == category]

    mode = "RECORD" if record else "EVAL"
    token_type = "user token" if USER_TOKEN else "bot token (⚠️ bot may ignore own messages)"
    logger.info(f"[{mode}] Running {len(scenarios)} scenarios against live bot in <#{channel}>")
    logger.info(f"[{mode}] Bot user: {bot_user_id}, sending via: {token_type}")
    print()

    results = []
    for i, scenario in enumerate(scenarios, 1):
        print(f"  [{i}/{len(scenarios)}] {scenario.get('name', 'unnamed')}...", end=" ", flush=True)
        result = run_live_scenario(sender, reader, channel, bot_user_id, scenario, record=record)
        if record:
            print("📝 recorded")
        else:
            status = "✅" if result["passed"] else "❌"
            print(status)
        results.append(result)

        # Pause between scenarios to let bot finish processing
        if i < len(scenarios):
            time.sleep(5)

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="shadow.ai live eval runner")
    parser.add_argument("--channel", type=str, default=DEFAULT_TEST_CHANNEL,
                        help="Slack channel ID to run evals in")
    parser.add_argument("--category", type=str, help="Only run scenarios in this category")
    parser.add_argument("--scenario-dir", default="evals/scenarios", help="Scenario directory")
    parser.add_argument("--dry-run", action="store_true", help="Show scenarios without sending")
    parser.add_argument("--record", action="store_true",
                        help="Record golden baselines (run once, then compare future runs)")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Number of scenarios to run in parallel (default: 1, recommended: max_sessions - 2)")
    args = parser.parse_args()

    if not BOT_TOKEN:
        print("Error: SLACK_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not USER_TOKEN:
        print("Warning: EVAL_USER_TOKEN not set in .env — using bot token (bot may ignore own messages)")
        print("Add EVAL_USER_TOKEN=xoxp-... to .env for reliable evals\n")

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

    results = run_live_evals(args.channel, args.category, record=args.record,
                             concurrency=args.concurrency, scenario_dir=args.scenario_dir)
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
