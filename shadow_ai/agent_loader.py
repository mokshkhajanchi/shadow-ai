"""Load agent definitions from .md files for Claude Code sessions."""

import logging
import re
from pathlib import Path

logger = logging.getLogger("slack-claude-code")


def _parse_agent_md(filepath: Path) -> dict | None:
    """Parse a YAML-frontmatter + markdown agent file into a dict."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return None

    # Extract YAML frontmatter between --- delimiters
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return None

    frontmatter, body = match.group(1), match.group(2).strip()
    meta = {}
    for line in frontmatter.split("\n"):
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            val = val.strip()
            if val.startswith("[") or val.startswith("-"):
                continue  # Skip list values, handle below
            meta[key.strip()] = val

    # Parse list fields (tools, skills)
    for list_key in ("tools", "skills"):
        items = []
        in_list = False
        for line in frontmatter.split("\n"):
            if line.strip().startswith(f"{list_key}:"):
                in_list = True
                continue
            if in_list:
                if line.strip().startswith("- "):
                    items.append(line.strip()[2:].strip())
                else:
                    in_list = False
        if items:
            meta[list_key] = items

    if "name" not in meta or "description" not in meta:
        return None

    return {
        "name": meta["name"],
        "description": meta["description"],
        "prompt": body,
        "tools": meta.get("tools"),
        "model": meta.get("model"),
        "skills": meta.get("skills"),
        "maxTurns": int(meta["maxTurns"]) if "maxTurns" in meta else None,
    }


def load_agents(*agent_dirs: str | Path) -> dict:
    """Load all agent .md files from given directories.

    Returns dict[str, AgentDefinition] ready for ClaudeAgentOptions.agents.
    """
    from claude_agent_sdk import AgentDefinition

    agents = {}
    for agents_dir in agent_dirs:
        agents_path = Path(agents_dir)
        if not agents_path.is_dir():
            continue
        for md_file in sorted(agents_path.glob("*.md")):
            parsed = _parse_agent_md(md_file)
            if not parsed:
                logger.warning(f"[AGENTS] Skipped invalid agent file: {md_file}")
                continue

            kwargs = {
                "description": parsed["description"],
                "prompt": parsed["prompt"],
            }
            if parsed.get("tools"):
                kwargs["tools"] = parsed["tools"]
            if parsed.get("model"):
                kwargs["model"] = parsed["model"]
            if parsed.get("skills"):
                kwargs["skills"] = parsed["skills"]
            if parsed.get("maxTurns"):
                kwargs["maxTurns"] = parsed["maxTurns"]

            agents[parsed["name"]] = AgentDefinition(**kwargs)
            logger.info(f"[AGENTS] Loaded: {parsed['name']}")

    return agents
