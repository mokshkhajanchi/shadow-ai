"""Eval runner: loads scenarios, grades responses, reports results."""

import logging
import os
import sys
from pathlib import Path

import yaml

from evals.graders.contains import grade_contains, grade_not_contains, grade_min_length, grade_regex
from evals.graders.safety import grade_no_secrets, grade_no_absolute_paths, grade_no_destructive_commands
from evals.graders.tool_usage import grade_tools_used, grade_tools_not_used
from evals.reporter import print_report

logger = logging.getLogger("shadow-ai-evals")


def load_scenarios(scenario_dir: str = "evals/scenarios") -> list[dict]:
    """Load all YAML scenario files."""
    scenarios = []
    for f in sorted(Path(scenario_dir).glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
            if isinstance(data, list):
                for s in data:
                    s.setdefault("_file", f.name)
                    scenarios.append(s)
    return scenarios


def grade_scenario(scenario: dict, response: str, tool_calls: list[dict] = None, cost: float = 0, duration: float = 0) -> dict:
    """Grade a single scenario against actual results.

    Returns dict with: name, category, severity, checks (list of pass/fail), passed (bool)
    """
    expected = scenario.get("expected", {})
    tool_calls = tool_calls or []
    checks = {}

    # Content checks
    if "response_contains" in expected:
        checks.update(grade_contains(response, expected["response_contains"]))
    if "response_not_contains" in expected:
        checks.update(grade_not_contains(response, expected["response_not_contains"]))
    if "min_length" in expected:
        checks.update(grade_min_length(response, expected["min_length"]))
    if "regex_match" in expected:
        checks.update(grade_regex(response, expected["regex_match"]))

    # Tool checks
    if "tools_used" in expected:
        checks.update(grade_tools_used(tool_calls, expected["tools_used"]))
    if "tools_not_used" in expected:
        checks.update(grade_tools_not_used(tool_calls, expected["tools_not_used"]))

    # Safety checks (always run)
    checks.update(grade_no_secrets(response))
    checks.update(grade_no_absolute_paths(response))
    if tool_calls:
        checks.update(grade_no_destructive_commands(tool_calls))

    # Cost/duration checks
    if "max_cost_usd" in expected and cost > 0:
        ok = cost <= expected["max_cost_usd"]
        checks["max_cost"] = {"pass": ok, "detail": f"${cost:.4f} vs max ${expected['max_cost_usd']}"}
    if "max_duration_sec" in expected and duration > 0:
        ok = duration <= expected["max_duration_sec"]
        checks["max_duration"] = {"pass": ok, "detail": f"{duration:.1f}s vs max {expected['max_duration_sec']}s"}

    all_passed = all(c["pass"] for c in checks.values())
    critical_failed = not all_passed and scenario.get("severity") == "critical"

    return {
        "name": scenario.get("name", "unnamed"),
        "category": scenario.get("category", "unknown"),
        "severity": scenario.get("severity", "normal"),
        "checks": checks,
        "passed": all_passed,
        "critical_failed": critical_failed,
    }


def run_recorded_evals(scenario_dir: str = "evals/scenarios", responses_dir: str = "evals/responses") -> list[dict]:
    """Run evals against recorded responses."""
    scenarios = load_scenarios(scenario_dir)
    results = []

    for scenario in scenarios:
        name = scenario.get("name", "unnamed")
        # Look for recorded response file
        response_file = Path(responses_dir) / f"{name.replace(' ', '_').lower()}.txt"
        if not response_file.exists():
            results.append({
                "name": name,
                "category": scenario.get("category", "unknown"),
                "severity": scenario.get("severity", "normal"),
                "passed": None,
                "checks": {},
                "skipped": True,
                "skip_reason": f"No recorded response: {response_file}",
            })
            continue

        response = response_file.read_text()
        result = grade_scenario(scenario, response)
        results.append(result)

    return results


def main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="shadow.ai eval runner")
    parser.add_argument("--live", action="store_true", help="Run live evals (sends real Slack messages)")
    parser.add_argument("--category", type=str, help="Only run scenarios in this category")
    parser.add_argument("--scenario-dir", default="evals/scenarios", help="Scenario directory")
    args = parser.parse_args()

    if args.live:
        from evals.live import main as live_main
        live_main()
        return

    results = run_recorded_evals(args.scenario_dir)
    if args.category:
        results = [r for r in results if r.get("category") == args.category]

    print_report(results)

    # Exit with failure if any critical eval failed
    if any(r.get("critical_failed") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
