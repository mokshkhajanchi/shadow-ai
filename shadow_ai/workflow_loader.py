"""Load and execute workflow templates from knowledge/workflows/."""

import logging
import re
from pathlib import Path

logger = logging.getLogger("slack-claude-code")


def _parse_workflow_md(filepath: Path) -> dict | None:
    """Parse a workflow .md file with YAML frontmatter + markdown body."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return None

    frontmatter, body = match.group(1), match.group(2).strip()

    # Parse simple YAML fields
    meta = {}
    for line in frontmatter.split("\n"):
        if ":" in line and not line.startswith(" ") and not line.startswith("-"):
            key, val = line.split(":", 1)
            val = val.strip()
            if val and not val.startswith("[") and not val.startswith("-"):
                meta[key.strip()] = val

    # Parse parameters list
    params = []
    in_params = False
    current_param = {}
    for line in frontmatter.split("\n"):
        stripped = line.strip()
        if stripped.startswith("parameters:"):
            in_params = True
            if stripped == "parameters: []":
                break
            continue
        if in_params:
            if stripped.startswith("- name:"):
                if current_param:
                    params.append(current_param)
                current_param = {"name": stripped[7:].strip()}
            elif stripped.startswith("required:"):
                current_param["required"] = stripped[9:].strip().lower() == "true"
            elif stripped.startswith("description:"):
                current_param["description"] = stripped[12:].strip()
            elif stripped.startswith("default:"):
                current_param["default"] = stripped[8:].strip()
            elif not stripped.startswith("-") and not stripped.startswith(" ") and stripped:
                in_params = False
    if current_param:
        params.append(current_param)

    if "name" not in meta:
        meta["name"] = filepath.stem

    return {
        "name": meta.get("name", filepath.stem),
        "description": meta.get("description", ""),
        "usage": meta.get("usage", ""),
        "parameters": params,
        "body": body,
    }


def load_workflows(*workflow_dirs: str | Path) -> dict[str, dict]:
    """Load all workflow .md files from given directories.

    Returns dict[name, {description, parameters, body}].
    """
    workflows = {}
    for source_dir in workflow_dirs:
        source_path = Path(source_dir)
        if not source_path.is_dir():
            continue
        for md_file in sorted(source_path.glob("*.md")):
            if md_file.name == "example.md":
                continue
            parsed = _parse_workflow_md(md_file)
            if not parsed:
                continue
            workflows[parsed["name"]] = parsed
            logger.info(f"[WORKFLOWS] Loaded: {parsed['name']}")
    return workflows


def build_workflow_prompt(workflow: dict, params: dict[str, str]) -> str:
    """Build the prompt for a workflow, substituting parameters."""
    body = workflow["body"]

    # Substitute parameters
    for param in workflow.get("parameters", []):
        name = param["name"]
        value = params.get(name, param.get("default", f"<{name}>"))
        body = body.replace(f"{{{name}}}", value)

    return (
        f"[WORKFLOW: {workflow['name']}]\n"
        "Execute this workflow step by step. Report progress after each step.\n"
        "If a step fails, STOP and report the failure.\n\n"
        + body
    )


def parse_workflow_command(text: str) -> tuple[str, dict[str, str]]:
    """Parse 'run <workflow-name> key=value key2=value2' into (name, params).

    Returns (workflow_name, params_dict).
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return "", {}

    # Skip "run"
    name = parts[1] if parts[0].lower() == "run" else parts[0]

    # Parse key=value pairs
    params = {}
    for part in parts[2:] if parts[0].lower() == "run" else parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip()] = v.strip()

    return name, params


def format_workflow_list(workflows: dict[str, dict]) -> str:
    """Format a list of available workflows for display."""
    if not workflows:
        return ":file_folder: No workflows found. Add `.md` files to `knowledge/workflows/`."

    lines = [":rocket: *Available Workflows:*\n"]
    for name, wf in sorted(workflows.items()):
        desc = wf.get("description", "No description")
        usage = wf.get("usage", f"@bot run {name}")
        lines.append(f"• *{name}* — _{desc}_\n  `{usage}`")

    lines.append("")
    return "\n".join(lines)
