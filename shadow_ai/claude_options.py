"""
Claude Code SDK option builder.

Constructs ClaudeAgentOptions with system prompt, MCP tool approvals,
thinking mode, model selection, and knowledge base injection.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("slack-claude-code")


def build_base_system_prompt(
    config,
    gitnexus_available: bool = False,
    knowledge_index_file: str = "",
    mcp_tool_catalog: str = "",
) -> str:
    """
    Build the universal portion of the system prompt — Slack formatting rules,
    MCP tool usage instructions, "lead with answer" guidelines.

    These apply regardless of which user is talking to the bot.

    config keys used:
        (none currently — kept for future per-instance overrides)
    """
    parts = [
        "\n\n--- RESPONSE GUIDELINES ---\n"
        "You are replying in a Slack thread.\n"
        "- Lead with the answer or action taken, not the process.\n"
        "- Give complete, useful responses. Include all relevant details the user needs.\n"
        "- For data lookups (tickets, PRs, etc.): show the full results with key fields — don't just summarize.\n"
        "- For code changes: state what you changed, the file(s), and the PR link.\n"
        "- Skip preamble like \"I'd be happy to help\". Answer directly.\n"
        "- Use Slack formatting: *bold*, `code`, bullet points. No markdown headers (# ##).\n"
        "- When the user asks for data from external systems, "
        "ALWAYS use your MCP tools to fetch real data. Never guess — call the tool.\n"
        "- URL ACCESS PRIORITY: When given a URL, ALWAYS try the relevant MCP tool first "
        "(Azure DevOps MCP for dev.azure.com URLs, Jira MCP for atlassian.net URLs, "
        "Sentry MCP for sentry URLs, Grafana MCP for grafana URLs, etc.). "
        "Only use Chrome browser/WebFetch as a LAST RESORT if no MCP tool can handle the URL.\n"
        "--- END RESPONSE GUIDELINES ---\n"
    ]

    # GitNexus code intelligence instructions
    if gitnexus_available:
        parts.append(
            "\n\n--- CODE INTELLIGENCE (GitNexus) ---\n"
            "You have GitNexus MCP tools for deep code intelligence. ALWAYS prefer these over "
            "Grep/Glob for understanding code architecture, tracing execution flows, and finding "
            "symbol relationships.\n"
            "Key tools:\n"
            "- gitnexus_query({query: \"concept\"}) — find execution flows and related symbols\n"
            "- gitnexus_context({name: \"symbolName\"}) — 360° view: callers, callees, processes\n"
            "- gitnexus_impact({target: \"symbolName\", direction: \"upstream\"}) — blast radius before editing\n"
            "- gitnexus_detect_changes({}) — pre-commit scope check\n"
            "Use these FIRST for any code exploration or architecture question. "
            "Fall back to Grep/Glob only if GitNexus returns no results.\n"
        )
        if knowledge_index_file:
            parts.append(
                f"A knowledge index (docs, no code signatures) is at: {knowledge_index_file}\n"
                "Use Read on it for domain knowledge and document references.\n"
            )
        parts.append("--- END CODE INTELLIGENCE ---\n")
    elif knowledge_index_file:
        parts.append(
            "\n\n--- KNOWLEDGE & CODEBASE ---\n"
            f"A knowledge index and codebase index is saved at: {knowledge_index_file}\n"
            "When a question relates to domain knowledge, code architecture, or you need to find "
            "relevant files, read this index first using the Read tool. "
            "Then use Read/Grep on the actual files listed there. "
            "Do NOT guess — always check the index and read source files.\n"
            "--- END KNOWLEDGE & CODEBASE ---\n"
        )

    if mcp_tool_catalog:
        parts.append(mcp_tool_catalog)

    return "".join(parts)


def build_custom_prompt(system_prompt_file: str) -> str:
    """
    Read a user's custom system prompt file (e.g. a .md file with
    user-specific conventions, signature rules, repo paths, etc.).

    Returns the file content as a string, or empty string if the file
    doesn't exist or can't be read.
    """
    if not system_prompt_file:
        return ""

    path = Path(system_prompt_file).expanduser().resolve()
    if not path.exists():
        logger.warning(f"[OPTIONS] Custom system prompt file not found: {system_prompt_file}")
        return ""

    try:
        content = path.read_text(encoding="utf-8").strip()
        if content:
            logger.info(f"[OPTIONS] Loaded custom system prompt from {path} ({len(content)} chars)")
        return content
    except Exception as e:
        logger.warning(f"[OPTIONS] Failed to read custom system prompt {system_prompt_file}: {e}")
        return ""


def create_options(
    config,
    model: str | None = None,
    thinking_override: str | None = None,
    mcp_server_names: list[str] | None = None,
    mcp_tool_catalog: str = "",
    knowledge_index_file: str = "",
    gitnexus_available: bool = False,
    knowledge_dirs: list[str] | None = None,
):
    """
    Build a ClaudeAgentOptions instance for a new or restored SDK session.

    config keys used:
        ALLOWED_TOOLS       — list[str] of built-in tool names
        PERMISSION_MODE     — str, e.g. "acceptEdits"
        CLAUDE_WORK_DIR     — str, working directory for Claude
        MAX_TURNS           — int, max conversation turns
        CLAUDE_MODEL        — str | None, default model from env
        CLAUDE_THINKING     — str, "off" | "adaptive" | "enabled"
        CLAUDE_THINKING_BUDGET — int, token budget for thinking mode
        SYSTEM_PROMPT_FILE  — str, path to user-specific system prompt .md file
    """
    from claude_agent_sdk import ClaudeAgentOptions

    allowed_tools = list(getattr(config, "allowed_tools", ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent"]))
    permission_mode = getattr(config, "permission_mode", "acceptEdits")
    cwd = os.path.expanduser(getattr(config, "claude_work_dir", "~/Projects"))
    max_turns = getattr(config, "max_turns", 30)
    default_model = getattr(config, "claude_model", None)
    default_thinking = getattr(config, "claude_thinking", "off")
    thinking_budget = getattr(config, "claude_thinking_budget", 10000)
    system_prompt_file = getattr(config, "system_prompt_file", "")

    # Build allowed_tools: built-in tools + wildcard for every MCP server
    if mcp_server_names:
        for server_name in mcp_server_names:
            wildcard = f"mcp__{server_name}__*"
            if wildcard not in allowed_tools:
                allowed_tools.append(wildcard)

    opts = ClaudeAgentOptions(
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        cwd=cwd,
        max_turns=max_turns,
        setting_sources=["user", "project"],
        max_buffer_size=10 * 1024 * 1024,  # 10MB (default 1MB too small for large fixtures)
    )

    # Model selection: inline override > env var > SDK default
    effective_model = model or default_model
    if effective_model:
        opts.model = effective_model

    # Thinking mode: inline override > env var
    thinking_mode = thinking_override or default_thinking
    if thinking_mode == "enabled":
        opts.thinking = {"type": "enabled", "budget_tokens": thinking_budget}
    elif thinking_mode == "adaptive":
        opts.thinking = {"type": "adaptive"}

    # Build system prompt: base (universal) + custom (user-specific)
    base_prompt = build_base_system_prompt(
        config,
        gitnexus_available=gitnexus_available,
        knowledge_index_file=knowledge_index_file,
        mcp_tool_catalog=mcp_tool_catalog,
    )

    custom_prompt = build_custom_prompt(system_prompt_file)

    append_text = base_prompt
    if custom_prompt:
        append_text += (
            "\n\n--- CUSTOM INSTRUCTIONS ---\n"
            + custom_prompt
            + "\n--- END CUSTOM INSTRUCTIONS ---\n"
        )

    # Inline summarized notes from knowledge/notes/ into system prompt
    notes_dir = os.path.join(cwd, "knowledge", "notes")
    if os.path.isdir(notes_dir):
        note_parts = []
        total_size = 0
        max_notes_size = 50_000  # 50KB hard limit for notes
        total_notes = 0
        skipped_notes = 0

        note_files = sorted(
            Path(notes_dir).glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for nf in note_files:
            total_notes += 1
            try:
                content = nf.read_text(encoding="utf-8", errors="ignore").strip()
                # Extract summary: header + source + first user msg + first assistant msg
                lines = content.split("\n")
                header = lines[0] if lines else nf.name
                summary_parts = [header]
                for line in lines[1:]:
                    if line.startswith("Source:") or line.startswith("Updated:") or line.startswith("Date:"):
                        summary_parts.append(line)
                    elif line.startswith("**User**:"):
                        summary_parts.append(line[:200])
                        break
                    elif line.startswith("**Assistant**:"):
                        summary_parts.append(line[:200])
                        break
                summary = "\n".join(summary_parts)

                if total_size + len(summary) > max_notes_size:
                    skipped_notes += 1
                    continue
                note_parts.append(summary)
                total_size += len(summary)
            except Exception:
                continue

        if skipped_notes > 0:
            logger.warning(
                f"[NOTES] {skipped_notes}/{total_notes} notes skipped — "
                f"system prompt notes budget ({max_notes_size // 1000}KB) exceeded. "
                f"Consider cleaning up old notes in knowledge/notes/"
            )

        if note_parts:
            logger.info(f"[NOTES] Injecting {len(note_parts)}/{total_notes} note summaries into system prompt ({total_size} chars)")
            append_text += (
                "\n\n--- NOTES FROM PREVIOUS SESSIONS ---\n"
                "These are summaries of notes saved from earlier conversations. Use them as context.\n\n"
                + "\n\n".join(note_parts)
                + "\n--- END NOTES ---\n"
            )

    logger.info(f"[SYSTEM PROMPT] Total length: {len(append_text)} chars")
    logger.debug(f"[SYSTEM PROMPT] Full content:\n{'=' * 80}\n{append_text}\n{'=' * 80}")

    opts.system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": append_text,
    }

    if knowledge_dirs:
        opts.add_dirs = list(knowledge_dirs)

    return opts
