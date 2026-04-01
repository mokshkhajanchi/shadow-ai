"""Run eval scenarios as pytest test cases.

Each scenario from evals/scenarios/*.yaml becomes a test.
Tests check scenario structure and grading logic.
For live evaluation, use: python -m evals.runner --live
"""

import pytest
from pathlib import Path

from evals.runner import grade_scenario


class TestScenarioStructure:
    """Verify all scenarios have required fields."""

    def test_scenario_has_name(self, scenario):
        assert "name" in scenario, f"Scenario missing 'name': {scenario}"

    def test_scenario_has_category(self, scenario):
        assert "category" in scenario, f"Scenario missing 'category': {scenario.get('name')}"

    def test_scenario_has_input(self, scenario):
        assert "input" in scenario, f"Scenario missing 'input': {scenario.get('name')}"

    def test_scenario_has_expected(self, scenario):
        assert "expected" in scenario, f"Scenario missing 'expected': {scenario.get('name')}"

    def test_scenario_input_has_text(self, scenario):
        assert "text" in scenario["input"], f"Scenario input missing 'text': {scenario.get('name')}"


class TestGradingLogic:
    """Test that grading functions work correctly with sample responses."""

    def test_passing_response(self):
        scenario = {
            "name": "test",
            "category": "test",
            "severity": "normal",
            "expected": {
                "response_contains": ["hello"],
                "response_not_contains": ["secret", "xoxb-"],
                "min_length": 5,
            },
        }
        result = grade_scenario(scenario, "hello world, this is a test response")
        assert result["passed"] is True

    def test_failing_contains(self):
        scenario = {
            "name": "test",
            "category": "test",
            "expected": {"response_contains": ["missing_word"]},
        }
        result = grade_scenario(scenario, "this response has nothing")
        assert result["passed"] is False

    def test_failing_not_contains(self):
        scenario = {
            "name": "test",
            "category": "test",
            "expected": {"response_not_contains": ["secret"]},
        }
        result = grade_scenario(scenario, "here is a secret value")
        assert result["passed"] is False

    def test_secret_detection(self):
        scenario = {"name": "test", "category": "test", "expected": {}}
        result = grade_scenario(scenario, "token is xoxb-12345-67890-abcdef")
        assert result["passed"] is False

    def test_path_detection(self):
        scenario = {"name": "test", "category": "test", "expected": {}}
        result = grade_scenario(scenario, "file is at /Users/moksh/secret.txt")
        assert result["passed"] is False

    def test_clean_response_passes(self):
        scenario = {"name": "test", "category": "test", "expected": {}}
        result = grade_scenario(scenario, "The project has 5 modules and uses Python 3.11.")
        assert result["passed"] is True

    def test_critical_severity(self):
        scenario = {
            "name": "test",
            "category": "test",
            "severity": "critical",
            "expected": {"response_contains": ["missing"]},
        }
        result = grade_scenario(scenario, "no match here")
        assert result["critical_failed"] is True

    def test_tool_usage_grading(self):
        scenario = {
            "name": "test",
            "category": "test",
            "expected": {
                "tools_used": ["Bash", "Read"],
                "tools_not_used": ["WebFetch"],
            },
        }
        tool_calls = [
            {"tool": "Bash", "input": {"command": "ls"}},
            {"tool": "Read", "input": {"file_path": "README.md"}},
        ]
        result = grade_scenario(scenario, "response", tool_calls)
        assert result["passed"] is True

    def test_destructive_command_detection(self):
        scenario = {"name": "test", "category": "test", "expected": {}}
        tool_calls = [{"tool": "Bash", "input": {"command": "rm -rf /"}}]
        result = grade_scenario(scenario, "done", tool_calls)
        assert result["passed"] is False
