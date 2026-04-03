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
    Build the universal portion of the system prompt — identity, guidelines,
    and references to external resources (read on demand, not inlined).
    """
    parts = [
        "\n\n--- YOUR IDENTITY ---\n"
        "You are shadow.ai — a Slack bot that runs Claude Code on the user's local machine.\n"
        "Your capabilities: Slack messaging, MCP tool access, knowledge notes, "
        "channel monitoring, custom agents, and skills.\n"
        "You are NOT a platform. You do NOT have built-in Grafana, Sentry, MongoDB, "
        "NewRelic, or database features. You ACCESS those via MCP tools when configured.\n"
        "When asked 'what are you' or 'what can you do', describe ONLY the above capabilities.\n"
        "--- END IDENTITY ---\n"
        "\n\n--- RESPONSE GUIDELINES ---\n"
        "You are replying in a Slack thread.\n"
        "- Lead with the answer or action taken, not the process.\n"
        "- Give complete, useful responses. Include all relevant details the user needs.\n"
        "- For data lookups (tickets, PRs, etc.): show the full results with key fields — don't just summarize.\n"
        "- For code changes: state what you changed, the file(s), and the PR link.\n"
        "- Skip preamble like \"I'd be happy to help\". Answer directly.\n"
        "- Use Slack formatting: *bold*, `code`, bullet points. No markdown headers (# ##).\n"
        "- NEVER guess or hallucinate. If you don't know, say so. If you need data, use a tool to fetch it.\n"
        "- NEVER describe tools or MCP servers as your 'features'. They are external services you can access.\n"
        "- URL ACCESS PRIORITY: When given a URL, ALWAYS try the relevant MCP tool first. "
        "Only use Chrome browser/WebFetch as a LAST RESORT.\n"
        "- AGENTS: Use 'code-reviewer' for PR reviews, 'debugger' for errors, 'note-taker' for notes.\n"
        "- SKILLS: When a task matches a skill, follow its instructions precisely.\n"
        "- PRIVACY: NEVER expose absolute file paths (/Users/*, /home/*). Use relative paths.\n"
        "--- END RESPONSE GUIDELINES ---\n"
        "\n\n--- NOTE-TAKING ---\n"
        "ONLY save a note when the user's CURRENT message explicitly asks you to "
        "remember/save/note/learn something. Look for words like 'remember this', "
        "'save this', 'take note', 'learn' in the current message.\n"
        "NEVER save based on thread context or previous messages. "
        "NEVER save when the user is asking you to DO something (share, create, review, list, etc.).\n"
        "When in doubt: do the task, don't save.\n"
        "To save: Write tool → knowledge/notes/<date>_<topic>.md\n"
        "Format: # Learned: <topic>\\nDate: <YYYY-MM-DD>\\n\\n<content>\n"
        "If the message has both a task AND 'remember this': do the task FIRST, then save.\n"
        "--- END NOTE-TAKING ---\n"
    ]

    # Reference to knowledge index (READ ON DEMAND, not inlined)
    if knowledge_index_file:
        if gitnexus_available:
            parts.append(
                "\n\n--- CODE INTELLIGENCE ---\n"
                "You have GitNexus MCP tools for code intelligence. Use gitnexus_query, "
                "gitnexus_context, gitnexus_impact for code exploration.\n"
                f"Knowledge index (docs only): {knowledge_index_file} — Read it when needed.\n"
                "--- END CODE INTELLIGENCE ---\n"
            )
        else:
            parts.append(
                "\n\n--- CODEBASE REFERENCE ---\n"
                f"The user's project files are indexed at: {knowledge_index_file}\n"
                "Read this file ONLY when you need to look up code, files, or project structure.\n"
                "Do NOT assume you know the codebase — always Read the index first.\n"
                "--- END CODEBASE REFERENCE ---\n"
            )

    # NOTE: MCP tool catalog is intentionally NOT included in the system prompt.
    # Claude already has the tool list from the SDK. Including it here causes
    # hallucination (Claude describes MCP tools as its own features).

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
    monitored: bool = False,
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
    from shadow_ai.agent_loader import load_agents
    from shadow_ai.skill_loader import load_skills, build_skills_prompt

    if monitored:
        # If channel rules exist, give full tools (owner defined a workflow)
        # Otherwise, read-only for basic auto-replies
        if getattr(config, '_has_channel_rules', False):
            allowed_tools = list(getattr(config, "allowed_tools", ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent"]))
        else:
            allowed_tools = ["Read", "Glob", "Grep"]
        max_turns = getattr(config, "max_turns", 100)
    else:
        allowed_tools = list(getattr(config, "allowed_tools", ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent"]))
        max_turns = getattr(config, "max_turns", 30)
    permission_mode = getattr(config, "permission_mode", "acceptEdits")
    cwd = os.path.expanduser(getattr(config, "claude_work_dir", "~/Projects"))
    default_model = getattr(config, "claude_model", None)
    default_thinking = getattr(config, "claude_thinking", "off")
    thinking_budget = getattr(config, "claude_thinking_budget", 10000)
    system_prompt_file = getattr(config, "system_prompt_file", "")

    # Build allowed_tools: built-in tools + wildcard for every MCP server
    # Exclude browser tools for monitored channels (security guardrail)
    blocked_servers_for_monitored = {"claude-in-chrome", "playwright"}
    if mcp_server_names:
        for server_name in mcp_server_names:
            if monitored and server_name in blocked_servers_for_monitored:
                continue
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

    # Code-level guardrails for monitored channels
    if monitored:
        from shadow_ai.guardrails import monitored_tool_guard
        opts.can_use_tool = monitored_tool_guard

    # Load agents from knowledge/agents/ (check multiple locations)
    repo_root = Path(__file__).parent.parent
    agents_dir = next((d for d in [
        Path(cwd) / "knowledge" / "agents",
        Path.cwd() / "knowledge" / "agents",
        repo_root / "knowledge" / "agents",
    ] if d.is_dir()), Path(cwd) / "knowledge" / "agents")
    agents = load_agents(agents_dir)
    if agents:
        opts.agents = agents

    # Load skills from knowledge/skills/ (check multiple locations)
    skills_dir = next((d for d in [
        Path(cwd) / "knowledge" / "skills",
        Path.cwd() / "knowledge" / "skills",
        repo_root / "knowledge" / "skills",
    ] if d.is_dir()), Path(cwd) / "knowledge" / "skills")
    loaded_skills = load_skills(skills_dir)

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

    # Inject skills into system prompt
    if loaded_skills:
        skills_prompt = build_skills_prompt(loaded_skills)
        append_text += skills_prompt
        logger.info(f"[SKILLS] Injected {len(loaded_skills)} skills into system prompt ({len(skills_prompt)} chars)")

    if custom_prompt:
        append_text += (
            "\n\n--- CUSTOM INSTRUCTIONS ---\n"
            + custom_prompt
            + "\n--- END CUSTOM INSTRUCTIONS ---\n"
        )

    # Inline FULL notes from knowledge/notes/ into system prompt
    # Check multiple locations: CLAUDE_WORK_DIR, cwd, repo root
    notes_candidates = [
        os.path.join(cwd, "knowledge", "notes"),
        os.path.join(os.getcwd(), "knowledge", "notes"),
        os.path.join(str(Path(__file__).parent.parent), "knowledge", "notes"),
    ]
    notes_dir = next((d for d in notes_candidates if os.path.isdir(d)), "")
    if notes_dir:
        note_parts = []
        total_size = 0
        max_notes_size = 100_000  # 100KB budget — notes are the most important context
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
                if total_size + len(content) > max_notes_size:
                    skipped_notes += 1
                    continue
                note_parts.append(f"### {nf.name}\n{content}")
                total_size += len(content)
            except Exception:
                continue

        if skipped_notes > 0:
            logger.warning(
                f"[NOTES] {skipped_notes}/{total_notes} notes skipped — "
                f"system prompt notes budget ({max_notes_size // 1000}KB) exceeded. "
                f"Consider cleaning up old notes in knowledge/notes/"
            )

        if note_parts:
            logger.info(f"[NOTES] Injecting {len(note_parts)}/{total_notes} notes into system prompt ({total_size} chars)")
            append_text += (
                "\n\n--- SAVED NOTES (from previous conversations) ---\n"
                "These notes were saved by the user. They contain facts, decisions, and context "
                "from earlier conversations. When answering questions, check these notes FIRST — "
                "they are authoritative. If a note contains the answer, use it directly.\n\n"
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
