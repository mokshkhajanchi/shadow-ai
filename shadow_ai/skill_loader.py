"""Load and manage skills for shadow-ai Claude Code sessions."""

import logging
import re
from pathlib import Path

logger = logging.getLogger("slack-claude-code")


def _parse_skill_md(filepath: Path) -> dict | None:
    """Parse a SKILL.md file with YAML frontmatter + markdown body."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return None

    frontmatter, body = match.group(1), match.group(2).strip()
    meta = {}
    for line in frontmatter.split("\n"):
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip('"')

    if "name" not in meta or "description" not in meta:
        return None

    return {
        "name": meta["name"],
        "description": meta["description"],
        "content": body,
    }


def load_skills(*skill_dirs: str | Path) -> dict[str, dict]:
    """Load all skills from given directories.

    Each directory should contain subdirectories with SKILL.md files.
    Returns dict[name, {description, content}].
    """
    skills = {}
    for source_dir in skill_dirs:
        source_path = Path(source_dir)
        if not source_path.is_dir():
            continue

        for skill_dir in sorted(source_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            parsed = _parse_skill_md(skill_file)
            if not parsed:
                logger.warning(f"[SKILLS] Skipped invalid skill: {skill_dir.name}")
                continue

            skills[parsed["name"]] = {
                "description": parsed["description"],
                "content": parsed["content"],
            }
            logger.info(f"[SKILLS] Loaded: {parsed['name']}")

    return skills


def build_skills_prompt(skills: dict[str, dict]) -> str:
    """Build a system prompt section documenting all available skills.

    Includes the full skill content so Claude knows exactly how to use each one.
    """
    if not skills:
        return ""

    parts = [
        "\n\n--- AVAILABLE SKILLS ---\n"
        "You have the following skills. When a task matches a skill, follow its instructions.\n\n"
    ]

    for name, skill in skills.items():
        parts.append(f"### Skill: {name}\n")
        parts.append(f"**When to use:** {skill['description']}\n\n")
        parts.append(skill["content"])
        parts.append("\n\n")

    parts.append("--- END SKILLS ---\n")
    return "".join(parts)


def install_skills_to_claude(*skill_dirs: str | Path) -> int:
    """Symlink skill directories into ~/.claude/skills/ for native Claude Code discovery.

    Returns the number of skills installed.
    """
    claude_skills_dir = Path.home() / ".claude" / "skills"
    claude_skills_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    for source_dir in skill_dirs:
        source_path = Path(source_dir)
        if not source_path.is_dir():
            continue

        for skill_dir in sorted(source_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue

            target = claude_skills_dir / skill_dir.name

            if target.is_symlink() and target.resolve() == skill_dir.resolve():
                installed += 1
                continue

            if target.is_symlink():
                target.unlink()
                target.symlink_to(skill_dir.resolve())
                installed += 1
                continue

            if target.exists():
                logger.warning(f"[SKILLS] Skipped symlink for {skill_dir.name} — already exists")
                continue

            target.symlink_to(skill_dir.resolve())
            installed += 1

    return installed
