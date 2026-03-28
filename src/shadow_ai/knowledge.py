"""
Knowledge base and codebase indexing functions.
Extracted from bot.py lines 139-337.

All functions accept parameters instead of reading globals so the module
has zero dependency on any global state.
"""

import logging
import re
import subprocess
from pathlib import Path

from .config import (
    ALL_INDEX_EXTENSIONS,
    CODEBASE_INDEX_EXTENSIONS,
    CODEBASE_PATTERNS,
    DOC_EXTENSIONS,
    INDEXABLE_EXTENSIONS,
    SKIP_DIRS,
    _human_size,
    _get_file_description,
)

logger = logging.getLogger("slack-claude-code")


# ─── Knowledge index ─────────────────────────────────────────────────────────

def _build_knowledge_index(
    paths: list[str],
    inline_threshold: int = 10_000,
    total_inline_limit: int = 20_000,
    index_max_entries: int = 100,
) -> tuple[str, str, list[str]]:
    """
    Scan knowledge paths and build:
    - index_text: compact file index for the system prompt
    - inline_text: content of small files loaded inline
    - dirs: list of directory paths to add to add_dirs
    """
    all_files = []  # (absolute_path, display_path, size_bytes)
    dirs = []

    for raw_path in paths:
        p = Path(raw_path).expanduser().resolve()
        if not p.exists():
            logger.warning(f"Knowledge path not found: {raw_path}")
            continue

        if p.is_file():
            if p.suffix.lower() in INDEXABLE_EXTENSIONS:
                all_files.append((p, p.name, p.stat().st_size))
            dirs.append(str(p.parent))
        elif p.is_dir():
            dirs.append(str(p))
            for fp in sorted(p.rglob("*")):
                if not fp.is_file():
                    continue
                if fp.suffix.lower() not in INDEXABLE_EXTENSIONS:
                    continue
                if any(part in SKIP_DIRS for part in fp.parts):
                    continue
                all_files.append((fp, str(fp.relative_to(p)), fp.stat().st_size))

    if not all_files:
        return "", "", dirs

    inline_parts = []
    index_entries = []
    total_inline = 0

    for abs_path, display_path, size in all_files:
        # Only inline doc files from knowledge dirs, not entire codebases
        is_doc = abs_path.suffix.lower() in {".md", ".txt", ".rst"}
        if is_doc and size <= inline_threshold and (total_inline + size) <= total_inline_limit:
            try:
                content = abs_path.read_text(encoding="utf-8", errors="ignore").strip()
                inline_parts.append(f"### {display_path}\n{content}\n")
                total_inline += size
            except Exception:
                if len(index_entries) < index_max_entries:
                    desc = _get_file_description(abs_path)
                    index_entries.append((display_path, _human_size(size), desc, str(abs_path)))
        else:
            if len(index_entries) < index_max_entries:
                desc = _get_file_description(abs_path)
                index_entries.append((display_path, _human_size(size), desc, str(abs_path)))

    index_text = ""
    if index_entries:
        lines = [
            "| File | Size | Description | Path |",
            "|------|------|-------------|------|",
        ]
        for display, size, desc, abspath in index_entries:
            lines.append(f"| {display} | {size} | {desc} | {abspath} |")
        index_text = "\n".join(lines)

    inline_text = "\n".join(inline_parts) if inline_parts else ""
    dirs = list(dict.fromkeys(dirs))  # deduplicate, preserve order

    return index_text, inline_text, dirs


# ─── Document outline extraction ─────────────────────────────────────────────

def _extract_doc_outline(filepath: Path, max_headings: int = 15) -> list[str]:
    """Extract headings and key structure from a document file."""
    headings = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip()
                if not line:
                    continue
                # Markdown headings
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                    if heading:
                        depth = len(line) - len(line.lstrip("#"))
                        indent = "  " * min(depth - 1, 3)
                        headings.append(f"{indent}{heading}")
                # YAML top-level keys
                elif filepath.suffix.lower() in (".yaml", ".yml") and re.match(r'^[a-zA-Z_]\w*:', line):
                    headings.append(f"  {line.split(':')[0]}")
                # JSON top-level structure hint (first few keys)
                elif filepath.suffix.lower() == ".json" and re.match(r'^\s{2}"[a-zA-Z_]\w*":', line) and len(headings) < 10:
                    key = line.strip().split('"')[1]
                    headings.append(f"  {key}")

                if len(headings) >= max_headings:
                    break
    except Exception:
        pass
    return headings


# ─── Codebase index ──────────────────────────────────────────────────────────

def _build_codebase_index(
    paths: list[str],
    max_size: int = 50_000,
    extensions: set[str] | None = None,
    patterns: dict | None = None,
) -> str:
    """Scan directories and extract code signatures + document outlines for the system prompt."""
    if not paths:
        return ""

    if extensions is None:
        extensions = CODEBASE_INDEX_EXTENSIONS
    if patterns is None:
        patterns = CODEBASE_PATTERNS

    file_entries = {}
    total_chars = 0
    truncated = False

    for raw_path in paths:
        root = Path(raw_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            logger.warning(f"[CODEBASE INDEX] Path not found: {raw_path}")
            continue

        for fp in sorted(root.rglob("*")):
            if not fp.is_file():
                continue
            ext = fp.suffix.lower()
            if ext not in ALL_INDEX_EXTENSIONS:
                continue
            if any(part in SKIP_DIRS for part in fp.parts):
                continue

            rel_path = str(fp.relative_to(root))
            lines_out = []

            if ext in extensions:
                # Code file — extract signatures
                file_patterns = patterns.get(ext)
                if not file_patterns:
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line_stripped = line.rstrip()
                            for pattern in file_patterns:
                                if pattern.match(line_stripped):
                                    sig = line_stripped.strip().rstrip(":{").rstrip()
                                    if len(sig) > 120:
                                        sig = sig[:117] + "..."
                                    lines_out.append(f"  {sig}")
                                    break
                except Exception:
                    continue
            elif ext in DOC_EXTENSIONS:
                # Document file — extract outline
                lines_out = _extract_doc_outline(fp)

            if lines_out:
                entry = f"## {rel_path}\n" + "\n".join(lines_out) + "\n"
                if total_chars + len(entry) > max_size:
                    truncated = True
                    break
                file_entries[rel_path] = entry
                total_chars += len(entry)

        if truncated:
            break

    if not file_entries:
        return ""

    return (
        "--- CODEBASE & DOCS INDEX ---\n"
        "Pre-indexed view of code and documents. Use this to find relevant files, "
        "classes, functions, and topics without Glob/Grep. Use Read for full details.\n\n"
        + "\n".join(file_entries.values())
        + "\n--- END CODEBASE & DOCS INDEX ---\n"
    )


# ─── GitNexus availability check ─────────────────────────────────────────────

def _check_gitnexus_available() -> bool:
    """Check if GitNexus CLI is installed and has indexed repos."""
    try:
        result = subprocess.run(
            ["npx", "-y", "gitnexus", "list"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except Exception:
        return False


# ─── Knowledge Index Rebuild ──────────────────────────────────────────────────

def rebuild_knowledge_index(knowledge_paths, cwd, inline_threshold=10_000, total_inline_limit=20_000, index_max_entries=100, codebase_max_size=50_000, gitnexus_available=False):
    """Rebuild the .knowledge-index.md file from current knowledge paths.

    Call this after saving new knowledge files so they're immediately discoverable.
    Returns (knowledge_index_file_path, knowledge_dirs).
    """
    from pathlib import Path

    index_str, inline_str, dirs = _build_knowledge_index(
        knowledge_paths, inline_threshold, total_inline_limit, index_max_entries,
    )

    codebase_index = ""
    if not gitnexus_available:
        codebase_index = _build_codebase_index(knowledge_paths or [cwd], codebase_max_size)

    if index_str or inline_str or codebase_index:
        index_path = Path(cwd).resolve() / ".knowledge-index.md"
        parts = [
            "# Knowledge & Codebase Index\n\n"
            "Generated dynamically. Use Read tool on the file paths listed below for details.\n"
        ]
        if inline_str:
            parts.append(f"\n## Inline Knowledge\n\n{inline_str}\n")
        if index_str:
            parts.append(f"\n## Knowledge File Index\n\n{index_str}\n")
        if codebase_index:
            parts.append(f"\n## Codebase Signatures\n\n{codebase_index}\n")
        index_path.write_text("".join(parts), encoding="utf-8")
        logger.info(f"[INDEX] Rebuilt at {index_path} ({index_path.stat().st_size / 1024:.1f} KB)")
        return str(index_path), dirs

    return "", dirs


# ─── Self-Learning Knowledge ─────────────────────────────────────────────────

def save_learned_knowledge(content: str, topic: str, thread_ts: str, learned_dir: str = "knowledge/learned") -> str:
    """Save learned knowledge from a conversation to a markdown file.

    Returns the path of the saved file.
    """
    import re
    from datetime import datetime

    os.makedirs(learned_dir, exist_ok=True)

    # Sanitize topic for filename
    safe_topic = re.sub(r'[^\w\s-]', '', topic).strip().replace(' ', '-')[:50]
    if not safe_topic:
        safe_topic = "conversation"
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}_{safe_topic}.md"
    filepath = os.path.join(learned_dir, filename)

    # Avoid overwriting — append number if exists
    counter = 1
    while os.path.exists(filepath):
        filename = f"{date_str}_{safe_topic}_{counter}.md"
        filepath = os.path.join(learned_dir, filename)
        counter += 1

    with open(filepath, "w") as f:
        f.write(f"# Learned: {topic}\n")
        f.write(f"Date: {date_str}\n")
        f.write(f"Source: Slack thread {thread_ts}\n\n")
        f.write(content)

    return filepath


def save_feedback_lessons(content: str, learned_dir: str = "knowledge/learned") -> str:
    """Save distilled feedback lessons to a persistent file.

    Overwrites the previous feedback_lessons.md — it's regenerated each time.
    """
    os.makedirs(learned_dir, exist_ok=True)
    filepath = os.path.join(learned_dir, "feedback_lessons.md")

    with open(filepath, "w") as f:
        f.write("# Feedback Lessons\n")
        f.write("Auto-generated from user reactions. Do NOT edit manually.\n\n")
        f.write(content)

    return filepath
