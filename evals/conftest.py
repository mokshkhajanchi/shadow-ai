"""Pytest integration for evals: load scenarios as test cases."""

import pytest
import yaml
from pathlib import Path


def load_all_scenarios():
    """Load all scenario YAML files."""
    scenarios = []
    scenario_dir = Path(__file__).parent / "scenarios"
    for f in sorted(scenario_dir.glob("*.yaml")):
        with open(f) as fh:
            data = yaml.safe_load(fh)
            if isinstance(data, list):
                for s in data:
                    s["_file"] = f.stem
                    scenarios.append(s)
    return scenarios


ALL_SCENARIOS = load_all_scenarios()


def pytest_generate_tests(metafunc):
    """Dynamically generate test cases from scenarios."""
    if "scenario" in metafunc.fixturenames:
        ids = [s.get("name", f"scenario_{i}") for i, s in enumerate(ALL_SCENARIOS)]
        metafunc.parametrize("scenario", ALL_SCENARIOS, ids=ids)
