"""Tool usage graders: check which tools were used/not used."""

import fnmatch


def grade_tools_used(tool_calls: list[dict], expected_tools: list[str]) -> dict:
    """Check that all expected tools were called at least once."""
    used_tools = {call.get("tool", "") for call in tool_calls}
    results = {}
    for tool in expected_tools:
        # Support wildcards like "mcp__azure-devops__*"
        if "*" in tool:
            found = any(fnmatch.fnmatch(t, tool) for t in used_tools)
        else:
            found = tool in used_tools
        results[f"tool_used: {tool}"] = {"pass": found, "detail": f"{'Used' if found else 'NOT used'}: {tool}"}
    return results


def grade_tools_not_used(tool_calls: list[dict], forbidden_tools: list[str]) -> dict:
    """Check that none of the forbidden tools were called."""
    used_tools = {call.get("tool", "") for call in tool_calls}
    results = {}
    for tool in forbidden_tools:
        if "*" in tool:
            found = any(fnmatch.fnmatch(t, tool) for t in used_tools)
        else:
            found = tool in used_tools
        results[f"tool_not_used: {tool}"] = {"pass": not found, "detail": f"{'Used (BAD)' if found else 'Not used (good)'}: {tool}"}
    return results


def grade_tool_count(tool_calls: list[dict], min_calls: int = 0, max_calls: int = 0) -> dict:
    """Check tool call count is within expected range."""
    count = len(tool_calls)
    results = {}
    if min_calls > 0:
        results["min_tool_calls"] = {"pass": count >= min_calls, "detail": f"{count} calls vs min {min_calls}"}
    if max_calls > 0:
        results["max_tool_calls"] = {"pass": count <= max_calls, "detail": f"{count} calls vs max {max_calls}"}
    return results
