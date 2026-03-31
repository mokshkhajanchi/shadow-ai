"""Install bundled and custom skills into Claude Code's skill discovery path."""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("slack-claude-code")

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def install_skills(*skill_dirs: str | Path) -> int:
    """Symlink skill directories into ~/.claude/skills/ for Claude Code discovery.

    Each subdirectory in the given skill_dirs that contains a SKILL.md is symlinked.
    Existing symlinks are updated. Non-symlink conflicts are skipped.

    Returns the number of skills installed.
    """
    CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0

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

            target = CLAUDE_SKILLS_DIR / skill_dir.name

            # Already correctly linked
            if target.is_symlink() and target.resolve() == skill_dir.resolve():
                installed += 1
                continue

            # Update stale symlink
            if target.is_symlink():
                target.unlink()
                target.symlink_to(skill_dir.resolve())
                logger.info(f"[SKILLS] Updated: {skill_dir.name}")
                installed += 1
                continue

            # Skip if a real directory exists (user's own skill with same name)
            if target.exists():
                logger.warning(f"[SKILLS] Skipped {skill_dir.name} — already exists at {target}")
                continue

            # Create new symlink
            target.symlink_to(skill_dir.resolve())
            logger.info(f"[SKILLS] Installed: {skill_dir.name}")
            installed += 1

    return installed
