"""Tests for workflow loading, parsing, and prompt building."""

import pytest
from pathlib import Path

from shadow_ai.workflow_loader import (
    _parse_workflow_md,
    load_workflows,
    build_workflow_prompt,
    parse_workflow_command,
    format_workflow_list,
)


VALID_WORKFLOW = """---
name: test-workflow
description: A test workflow.
parameters:
  - name: branch
    required: true
    description: The branch
  - name: env
    required: false
    description: Target environment
    default: staging
---

# Test Workflow

## Step 1: Check
Verify branch `{branch}` exists.

## Step 2: Deploy
Deploy to `{env}`.
"""


class TestParseWorkflowMd:

    def test_valid_workflow(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(VALID_WORKFLOW)
        result = _parse_workflow_md(f)
        assert result is not None
        assert result["name"] == "test-workflow"
        assert result["description"] == "A test workflow."
        assert len(result["parameters"]) == 2
        assert result["parameters"][0]["name"] == "branch"
        assert result["parameters"][0]["required"] is True
        assert result["parameters"][1]["name"] == "env"
        assert result["parameters"][1].get("default") == "staging"
        assert "Step 1" in result["body"]

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("No frontmatter here")
        assert _parse_workflow_md(f) is None

    def test_empty_parameters(self, tmp_path):
        f = tmp_path / "simple.md"
        f.write_text("---\nname: simple\ndescription: Simple.\nparameters: []\n---\n\nDo stuff.")
        result = _parse_workflow_md(f)
        assert result is not None
        assert result["parameters"] == []


class TestLoadWorkflows:

    def test_loads_from_directory(self, tmp_path):
        (tmp_path / "test.md").write_text(VALID_WORKFLOW)
        workflows = load_workflows(tmp_path)
        assert "test-workflow" in workflows

    def test_skips_example(self, tmp_path):
        (tmp_path / "example.md").write_text(VALID_WORKFLOW)
        workflows = load_workflows(tmp_path)
        assert len(workflows) == 0

    def test_loads_bundled_workflows(self):
        workflows = load_workflows("workflows")
        assert "hello-world" in workflows
        assert "deploy-to-staging" in workflows
        assert "create-release-notes" in workflows


class TestBuildWorkflowPrompt:

    def test_substitutes_parameters(self):
        wf = {
            "name": "test",
            "body": "Deploy `{branch}` to `{env}`.",
            "parameters": [
                {"name": "branch"},
                {"name": "env", "default": "staging"},
            ],
        }
        prompt = build_workflow_prompt(wf, {"branch": "feature/xyz"})
        assert "feature/xyz" in prompt
        assert "staging" in prompt
        assert "{branch}" not in prompt

    def test_includes_workflow_header(self):
        wf = {"name": "deploy", "body": "Do things.", "parameters": []}
        prompt = build_workflow_prompt(wf, {})
        assert "[WORKFLOW: deploy]" in prompt
        assert "step by step" in prompt.lower()


class TestParseWorkflowCommand:

    def test_simple(self):
        name, params = parse_workflow_command("run deploy-to-staging branch=feature/xyz")
        assert name == "deploy-to-staging"
        assert params == {"branch": "feature/xyz"}

    def test_multiple_params(self):
        name, params = parse_workflow_command("run deploy branch=main env=prod service=avis")
        assert name == "deploy"
        assert params == {"branch": "main", "env": "prod", "service": "avis"}

    def test_no_params(self):
        name, params = parse_workflow_command("run hello-world")
        assert name == "hello-world"
        assert params == {}

    def test_empty(self):
        name, params = parse_workflow_command("run")
        assert name == ""


class TestFormatWorkflowList:

    def test_formats_list(self):
        workflows = {
            "deploy": {"description": "Deploy stuff", "usage": "@bot run deploy branch=main", "parameters": [{"name": "branch", "required": True}]},
            "test": {"description": "Run tests", "usage": "@bot run test", "parameters": []},
        }
        result = format_workflow_list(workflows)
        assert "deploy" in result
        assert "test" in result
        assert "@bot run deploy" in result

    def test_empty(self):
        result = format_workflow_list({})
        assert "No workflows" in result
