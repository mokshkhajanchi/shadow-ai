"""
MCP (Model Context Protocol) server discovery.

Discovers MCP server names from user/project settings and enumerates
available tools via a temporary SDK session.
"""

import asyncio
import json
import logging
from pathlib import Path

from shadow_ai.sessions import _get_cli_pid, _force_kill_process

logger = logging.getLogger("slack-claude-code")


def discover_mcp_server_names(work_dir: str) -> list[str]:
    """
    Read ~/.claude/settings.json (and .local variant) plus project .mcp.json
    to find configured MCP server names.
    Returns list of server names so we can auto-approve their tools.
    """
    settings_paths = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]

    server_names = []
    for path in settings_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            for name in mcp_servers:
                if name not in server_names:
                    server_names.append(name)
                    logger.info(f"[MCP] Discovered server: {name}")
        except Exception as e:
            logger.warning(f"Failed to read {path}: {e}")

    # Also check project-level .mcp.json in work_dir
    mcp_json_path = Path(work_dir) / ".mcp.json"
    if mcp_json_path.exists():
        try:
            data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            for name in mcp_servers:
                if name not in server_names:
                    server_names.append(name)
                    logger.info(f"[MCP] Discovered server (project): {name}")
        except Exception as e:
            logger.warning(f"Failed to read {mcp_json_path}: {e}")

    return server_names


async def discover_mcp_tools(work_dir: str, permission_mode: str) -> str:
    """
    Connect a temporary SDK session, call get_mcp_status(),
    and build a concise tool catalog string from all connected MCP servers.
    Returns empty string if no tools found or on error.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    opts = ClaudeAgentOptions(
        permission_mode=permission_mode,
        cwd=work_dir,
        setting_sources=["user", "project"],
    )
    sdk_client = ClaudeSDKClient(options=opts)
    cli_pid = None
    try:
        await sdk_client.connect()
        cli_pid = _get_cli_pid(sdk_client)
        # Brief wait for MCP servers to initialize
        await asyncio.sleep(2)
        status = await sdk_client.get_mcp_status()
    except Exception as e:
        logger.warning(f"[MCP] Failed to discover tools: {e}")
        return ""
    finally:
        try:
            await sdk_client.disconnect()
        except Exception:
            if cli_pid:
                _force_kill_process(cli_pid)

    catalog_lines = []
    for server in status.get("mcpServers", []):
        if server.get("status") != "connected":
            continue
        tools = server.get("tools", [])
        if not tools:
            continue
        catalog_lines.append(f"\n*{server['name']}* ({len(tools)} tools):")
        for tool in tools:
            desc = tool.get("description", "")
            if len(desc) > 120:
                desc = desc[:117] + "..."
            catalog_lines.append(f"  - `{tool['name']}`: {desc}")

    if not catalog_lines:
        return ""

    return (
        "\n--- AVAILABLE MCP TOOLS ---\n"
        "You have the following MCP tools. ALWAYS use these when the user asks "
        "about data from external systems. Never guess or make up data — call the tool.\n"
        + "\n".join(catalog_lines)
        + "\n--- END MCP TOOLS ---\n"
    )
