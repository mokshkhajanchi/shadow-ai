"""Tests for system prompt assembly."""

from pathlib import Path

import pytest

from shadow_ai.claude_options import build_base_system_prompt, build_custom_prompt, create_options
from shadow_ai.config import BotConfig


class TestBuildBaseSystemPrompt:

    def test_contains_response_guidelines(self):
        prompt = build_base_system_prompt(None)
        assert "RESPONSE GUIDELINES" in prompt
        assert "Slack thread" in prompt

    def test_contains_mcp_instructions(self):
        prompt = build_base_system_prompt(None)
        assert "MCP tools" in prompt

    def test_gitnexus_included_when_available(self):
        prompt = build_base_system_prompt(None, gitnexus_available=True)
        assert "GitNexus" in prompt

    def test_gitnexus_excluded_when_unavailable(self):
        prompt = build_base_system_prompt(None, gitnexus_available=False)
        assert "GitNexus" not in prompt

    def test_knowledge_index_included(self):
        prompt = build_base_system_prompt(None, knowledge_index_file="/path/to/index.md")
        assert "/path/to/index.md" in prompt

    def test_mcp_catalog_included(self):
        catalog = "\n--- MCP TOOLS ---\njira: search_issues\n--- END ---\n"
        prompt = build_base_system_prompt(None, mcp_tool_catalog=catalog)
        assert "jira: search_issues" in prompt

    def test_agents_mentioned(self):
        prompt = build_base_system_prompt(None)
        assert "AGENTS" in prompt

    def test_skills_mentioned(self):
        prompt = build_base_system_prompt(None)
        assert "SKILLS" in prompt


class TestBuildCustomPrompt:

    def test_empty_path(self):
        assert build_custom_prompt("") == ""

    def test_nonexistent_file(self):
        assert build_custom_prompt("/nonexistent/file.md") == ""

    def test_valid_file(self, tmp_path):
        f = tmp_path / "custom.md"
        f.write_text("# My Custom Prompt\nDo this and that.")
        result = build_custom_prompt(str(f))
        assert "My Custom Prompt" in result
        assert "Do this and that" in result


class TestCreateOptions:

    def _make_config(self, tmp_path, **overrides):
        defaults = dict(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        defaults.update(overrides)
        return BotConfig(**defaults)

    def test_system_prompt_has_response_guidelines(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config)
        assert "RESPONSE GUIDELINES" in opts.system_prompt["append"]

    def test_system_prompt_preset_is_claude_code(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config)
        assert opts.system_prompt["preset"] == "claude_code"

    def test_custom_prompt_included(self, tmp_path):
        custom_file = tmp_path / "custom.md"
        custom_file.write_text("Always be concise.")
        config = self._make_config(tmp_path, system_prompt_file=str(custom_file))
        opts = create_options(config)
        assert "CUSTOM INSTRUCTIONS" in opts.system_prompt["append"]
        assert "Always be concise" in opts.system_prompt["append"]

    def test_notes_injected(self, tmp_path):
        notes_dir = tmp_path / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "test.md").write_text(
            "# Learned: API endpoint\nDate: 2026-03-31\n\n**User**: Where is the API?"
        )
        config = self._make_config(tmp_path)
        opts = create_options(config)
        assert "SAVED NOTES" in opts.system_prompt["append"]

    def test_skills_injected(self, tmp_path):
        skills_dir = tmp_path / "knowledge" / "skills" / "test-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test.\n---\n\nDo the test thing."
        )
        config = self._make_config(tmp_path)
        opts = create_options(config)
        assert "AVAILABLE SKILLS" in opts.system_prompt["append"]
        assert "test-skill" in opts.system_prompt["append"]

    def test_monitored_restricts_tools_without_rules(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config, monitored=True)
        assert opts.allowed_tools == ["Read", "Glob", "Grep"]

    def test_monitored_full_tools_with_rules(self, tmp_path):
        config = self._make_config(tmp_path)
        config._has_channel_rules = True
        opts = create_options(config, monitored=True)
        assert "Write" in opts.allowed_tools
        assert "Bash" in opts.allowed_tools

    def test_normal_has_full_tools(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config, monitored=False)
        assert "Write" in opts.allowed_tools
        assert "Bash" in opts.allowed_tools

    def test_model_override(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config, model="opus")
        assert opts.model == "opus"

    def test_thinking_enabled(self, tmp_path):
        config = self._make_config(tmp_path)
        opts = create_options(config, thinking_override="enabled")
        assert opts.thinking["type"] == "enabled"
