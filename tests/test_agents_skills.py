"""Tests for agent and skill loading."""

import tempfile
from pathlib import Path

import pytest

from shadow_ai.agent_loader import load_agents, _parse_agent_md
from shadow_ai.skill_loader import load_skills, build_skills_prompt, _parse_skill_md


VALID_AGENT_MD = """---
name: test-agent
description: A test agent for unit tests.
tools:
  - Read
  - Grep
model: haiku
maxTurns: 5
---

You are a test agent. Do test things.
"""

INVALID_AGENT_MD = """This has no YAML frontmatter at all."""

VALID_SKILL_MD = """---
name: test-skill
description: A test skill for unit tests.
---

# Test Skill

Follow these steps:
1. Step one
2. Step two
"""


class TestParseAgentMd:

    def test_valid_agent(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(VALID_AGENT_MD)
        result = _parse_agent_md(f)
        assert result is not None
        assert result["name"] == "test-agent"
        assert result["description"] == "A test agent for unit tests."
        assert result["tools"] == ["Read", "Grep"]
        assert result["model"] == "haiku"
        assert result["maxTurns"] == 5
        assert "test agent" in result["prompt"]

    def test_invalid_agent(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text(INVALID_AGENT_MD)
        result = _parse_agent_md(f)
        assert result is None

    def test_missing_name(self, tmp_path):
        f = tmp_path / "noname.md"
        f.write_text("---\ndescription: No name field\n---\nBody here")
        result = _parse_agent_md(f)
        assert result is None


class TestLoadAgents:

    def test_loads_from_directory(self, tmp_path):
        (tmp_path / "agent1.md").write_text(VALID_AGENT_MD)
        agents = load_agents(tmp_path)
        assert "test-agent" in agents
        assert agents["test-agent"].description == "A test agent for unit tests."
        assert agents["test-agent"].model == "haiku"

    def test_skips_invalid_files(self, tmp_path):
        (tmp_path / "good.md").write_text(VALID_AGENT_MD)
        (tmp_path / "bad.md").write_text(INVALID_AGENT_MD)
        agents = load_agents(tmp_path)
        assert len(agents) == 1

    def test_empty_directory(self, tmp_path):
        agents = load_agents(tmp_path)
        assert agents == {}

    def test_nonexistent_directory(self):
        agents = load_agents("/nonexistent/path")
        assert agents == {}

    def test_loads_bundled_agents(self):
        """Verify the bundled agents in knowledge/agents/ load correctly."""
        agents = load_agents("knowledge/agents")
        assert "code-reviewer" in agents
        assert "debugger" in agents
        assert "note-taker" in agents


class TestParseSkillMd:

    def test_valid_skill(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text(VALID_SKILL_MD)
        result = _parse_skill_md(f)
        assert result is not None
        assert result["name"] == "test-skill"
        assert result["description"] == "A test skill for unit tests."
        assert "Step one" in result["content"]

    def test_invalid_skill(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("No frontmatter here")
        result = _parse_skill_md(f)
        assert result is None


class TestLoadSkills:

    def test_loads_from_directory(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(VALID_SKILL_MD)
        skills = load_skills(tmp_path)
        assert "test-skill" in skills
        assert "test skill" in skills["test-skill"]["description"].lower()

    def test_skips_dirs_without_skill_md(self, tmp_path):
        (tmp_path / "empty-dir").mkdir()
        (tmp_path / "with-skill").mkdir()
        (tmp_path / "with-skill" / "SKILL.md").write_text(VALID_SKILL_MD)
        skills = load_skills(tmp_path)
        assert len(skills) == 1

    def test_loads_bundled_skills(self):
        """Verify the bundled skills in knowledge/skills/ load correctly."""
        skills = load_skills("knowledge/skills")
        assert "brainstorm" in skills
        assert "tdd" in skills
        assert "pr-review" in skills
        assert "summarize" in skills


class TestBuildSkillsPrompt:

    def test_builds_prompt_with_skills(self):
        skills = {
            "my-skill": {"description": "Does stuff", "content": "# Instructions\nDo the thing."},
        }
        prompt = build_skills_prompt(skills)
        assert "AVAILABLE SKILLS" in prompt
        assert "my-skill" in prompt
        assert "Does stuff" in prompt
        assert "Do the thing" in prompt

    def test_empty_skills_returns_empty(self):
        assert build_skills_prompt({}) == ""
