"""
Special command handlers: PR review, test runner, summary.

These are self-contained workflows that parse user input and
delegate to invoke_claude_code from claude_runner.
"""

import logging
import re
import time
import traceback
from pathlib import Path

logger = logging.getLogger("slack-claude-code")

# ─── Azure DevOps PR URL Parsing ─────────────────────────────────────────────

ADO_PR_PATTERN = re.compile(
    r"https?://dev\.azure\.com/GoFynd/FyndPlatformCore/_git/([^/]+)/pullrequest/(\d+)"
)


def parse_ado_pr_url(text: str) -> tuple[str, int] | None:
    """
    Extract (repo_name, pr_id) from an Azure DevOps PR URL in the text.

    Also handles shorthand: "review PR #12345 reponame" or "review #12345 reponame".
    """
    match = ADO_PR_PATTERN.search(text)
    if match:
        return match.group(1), int(match.group(2))
    # Also handle "review PR #12345" with repo context
    match = re.match(r"^review\s+(?:pr\s+)?#?(\d+)\s+(\w+)$", text.strip(), re.IGNORECASE)
    if match:
        return match.group(2), int(match.group(1))
    return None


# ─── PR Review ────────────────────────────────────────────────────────────────


def review_pr(
    repo_name: str,
    pr_id: int,
    channel: str,
    thread_ts: str,
    *,
    repo_paths: dict[str, str],
    slack_client,
    invoke_claude_code_fn,
    remove_session_fn,
    db_save_usage_fn,
    send_response_fn,
):
    """
    Review an Azure DevOps PR: fetch details, get diff, analyze with Claude, post comments.
    Runs in the thread pool.

    Args:
        repo_name: Name of the Azure DevOps repository.
        pr_id: Pull request ID.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.
        repo_paths: Dict mapping repo names to local filesystem paths.
        slack_client: Slack WebClient.
        invoke_claude_code_fn: The invoke_claude_code function (with deps bound).
        remove_session_fn: Function to remove a session by key.
        db_save_usage_fn: Function to save usage data.
        send_response_fn: Function to send response with stop button.
    """
    try:
        # Step 1: Post progress
        progress_msg = slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":mag: Reviewing PR #{pr_id} in *{repo_name}*...",
        )
        progress_ts = progress_msg.get("ts")

        # Step 2: Build review prompt and let Claude do the review with MCP tools
        local_path = repo_paths.get(repo_name, repo_name)

        review_prompt = f"""Review Azure DevOps PR #{pr_id} in the **{repo_name}** repository (project: FyndPlatformCore).

Follow these steps:

1. **Fetch PR details**: Use `mcp__azure-devops__repo_get_pull_request_by_id` with repositoryId="{repo_name}", pullRequestId={pr_id}, project="FyndPlatformCore", includeWorkItemRefs=true.

2. **Get the diff**: The source branch and target branch are in the PR response (sourceRefName, targetRefName — strip "refs/heads/" prefix). Use the Bash tool to run:
   ```
   cd {local_path} && git fetch origin <source_branch> <target_branch> 2>&1 && git diff origin/<target_branch>...origin/<source_branch>
   ```
   If the diff is empty, fall back to: `git fetch origin <lastMergeSourceCommit.commitId> && git show <commitId> --format=""`

3. **Analyze the diff thoroughly**:
   - Overview: what the PR does
   - Code quality: style, patterns, DRY
   - Correctness: logic errors, edge cases
   - Performance: N+1 queries, unnecessary work
   - Security: injection risks, secrets
   - Test coverage: are changes tested?
   - Specific suggestions with file:line references

4. **Post review comments on the PR**:
   a. Post a general review comment using `mcp__azure-devops__repo_create_pull_request_thread` with:
      - repositoryId: "{repo_name}", pullRequestId: {pr_id}, project: "FyndPlatformCore"
      - content: Full review in markdown, starting with "## Code Review by moksh.ai"
      - status: "Active" (always keep review threads active for visibility)

   b. For specific issues (max 5), post inline comments using the same tool with:
      - filePath, rightFileStartLine, rightFileStartOffset=1, rightFileEndLine, rightFileEndOffset=1
      - status: "Active"

5. **Respond with**: PR title, verdict (Approve/Request Changes), number of comments posted, any critical issues.

Be thorough but concise. Reference exact file paths and line numbers."""

        # Use a temporary session for the review
        review_key = f"review_{pr_id}_{int(time.time())}"
        response, cost_info = invoke_claude_code_fn(
            review_prompt, review_key,
            progress_ts=progress_ts,
        )

        if cost_info:
            db_save_usage_fn(thread_ts, cost_info)

        # Clean up temporary session
        remove_session_fn(review_key)

        # Post the review result to Slack
        send_response_fn(channel, thread_ts, response)

    except Exception as e:
        logger.error(f"[PR REVIEW] Error reviewing PR #{pr_id}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":x: Failed to review PR #{pr_id}:\n```\n{type(e).__name__}: {e}\n```",
        )


# ─── PR Diff Helper ──────────────────────────────────────────────────────────


def get_pr_diff(
    repo_name: str,
    source_branch: str,
    target_branch: str,
    source_commit: str,
    *,
    repo_paths: dict[str, str],
) -> str:
    """
    Get PR diff using local git repo. Falls back to commit-level diff.

    Args:
        repo_name: Repository name.
        source_branch: Source branch name.
        target_branch: Target branch name.
        source_commit: Source commit hash.
        repo_paths: Dict mapping repo names to local filesystem paths.
    """
    import subprocess

    local_path = repo_paths.get(repo_name)
    if not local_path or not Path(local_path).exists():
        # Try to find it
        for candidate in Path.home().iterdir():
            if candidate.name == repo_name and (candidate / ".git").exists():
                local_path = str(candidate)
                break
            sub = candidate / repo_name
            if sub.exists() and (sub / ".git").exists():
                local_path = str(sub)
                break

    if not local_path:
        return f"(Could not find local repo for '{repo_name}'. Diff unavailable.)"

    try:
        # Fetch both branches
        subprocess.run(
            ["git", "fetch", "origin", source_branch, target_branch],
            cwd=local_path, capture_output=True, timeout=30,
        )

        # Try branch diff first
        result = subprocess.run(
            ["git", "diff", f"origin/{target_branch}...origin/{source_branch}"],
            cwd=local_path, capture_output=True, text=True, timeout=30,
        )
        if result.stdout.strip():
            return result.stdout

        # Branches may be merged — fall back to commit diff
        subprocess.run(
            ["git", "fetch", "origin", source_commit],
            cwd=local_path, capture_output=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "show", source_commit, "--format="],
            cwd=local_path, capture_output=True, text=True, timeout=30,
        )
        return result.stdout if result.stdout.strip() else "(Empty diff — PR may already be merged.)"

    except Exception as e:
        return f"(Failed to get diff: {type(e).__name__}: {e})"


# ─── Test Runner ──────────────────────────────────────────────────────────────


def run_test_command(
    prompt: str,
    channel: str,
    thread_ts: str,
    *,
    repo_test_config: dict[str, dict],
    slack_client,
    invoke_claude_code_fn,
    remove_session_fn,
    db_save_usage_fn,
    send_response_fn,
) -> bool:
    """
    Handle "test <repo> [module]" commands.

    Returns True if the command was handled, False if it wasn't a valid test command
    (e.g. missing repo name).

    Args:
        prompt: The cleaned user prompt (e.g. "test avis module_createorder").
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.
        repo_test_config: Dict mapping repo names to test config dicts with
            keys: "path", "cmd", "cmd_specific".
        slack_client: Slack WebClient.
        invoke_claude_code_fn: The invoke_claude_code function (with deps bound).
        remove_session_fn: Function to remove a session by key.
        db_save_usage_fn: Function to save usage data.
        send_response_fn: Function to send response with stop button.
    """
    parts = prompt.strip().split(None, 2)  # ["test", "repo", "module"]
    repo_name = parts[1].lower() if len(parts) > 1 else None
    module = parts[2] if len(parts) > 2 else None

    if not repo_name or repo_name not in repo_test_config:
        available = ", ".join(repo_test_config.keys())
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":test_tube: Usage: `test <repo> [module]`\nAvailable repos: `{available}`\nExample: `test avis module_createorder`",
        )
        return True  # Handled (showed usage)

    config = repo_test_config[repo_name]
    if module:
        test_cmd = config["cmd_specific"].format(module=module)
    else:
        test_cmd = config["cmd"]

    progress_msg = slack_client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f":test_tube: Running tests for *{repo_name}*{f' ({module})' if module else ''}...",
    )
    progress_ts = progress_msg.get("ts")

    test_prompt = (
        f"Run the following test command and report the results:\n\n"
        f"```\ncd {config['path']} && {test_cmd}\n```\n\n"
        f"After running:\n"
        f"1. Report the total tests run, passed, failed, skipped\n"
        f"2. If any tests failed, show the failure details with file paths and line numbers\n"
        f"3. If all passed, just say 'All tests passed' with the count\n"
        f"Keep the response concise."
    )

    test_key = f"test_{repo_name}_{int(time.time())}"
    response, cost_info = invoke_claude_code_fn(test_prompt, test_key, progress_ts=progress_ts)
    if cost_info:
        db_save_usage_fn(thread_ts, cost_info)
    remove_session_fn(test_key)
    send_response_fn(channel, thread_ts, response)
    return True


# ─── Summary Command ──────────────────────────────────────────────────────────


def run_summary_command(
    channel: str,
    thread_ts: str,
    *,
    slack_client,
    invoke_claude_code_fn,
    remove_session_fn,
    db_get_thread_messages_fn,
    db_save_usage_fn,
    send_response_fn,
) -> bool:
    """
    Generate a conversation summary for the current thread.

    Returns True if handled, False if no history found.

    Args:
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.
        slack_client: Slack WebClient.
        invoke_claude_code_fn: The invoke_claude_code function (with deps bound).
        remove_session_fn: Function to remove a session by key.
        db_get_thread_messages_fn: Function to get thread messages from DB.
        db_save_usage_fn: Function to save usage data.
        send_response_fn: Function to send response with stop button.
    """
    messages = db_get_thread_messages_fn(thread_ts)
    if not messages:
        slack_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":warning: No conversation history found for this thread.",
        )
        return True  # Handled (showed warning)

    convo_parts = []
    for msg in messages:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        convo_parts.append(f"{prefix}: {msg['content'][:500]}")
    conversation_text = "\n\n".join(convo_parts)
    if len(conversation_text) > 30000:
        conversation_text = "...(earlier messages truncated)...\n\n" + conversation_text[-30000:]

    summary_prompt = (
        "Summarize this conversation thread. Highlight:\n"
        "1. Key decisions made\n"
        "2. Actions taken (files changed, PRs created, etc.)\n"
        "3. Outcomes and current status\n"
        "4. Any open items or follow-ups needed\n\n"
        "Keep it concise but complete.\n\n"
        f"--- CONVERSATION ---\n{conversation_text}\n--- END CONVERSATION ---"
    )

    progress_msg = slack_client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=":memo: Generating summary...",
    )
    progress_ts = progress_msg.get("ts")

    summary_key = f"summary_{thread_ts}_{int(time.time())}"
    response, cost_info = invoke_claude_code_fn(summary_prompt, summary_key, progress_ts=progress_ts)
    if cost_info:
        db_save_usage_fn(thread_ts, cost_info)
    remove_session_fn(summary_key)
    send_response_fn(channel, thread_ts, response)
    return True
