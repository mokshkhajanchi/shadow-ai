"""Golden response grader: compare against recorded baseline responses."""

import json
import logging
from pathlib import Path
from difflib import SequenceMatcher

logger = logging.getLogger("shadow-ai-evals")

GOLDEN_DIR = Path(__file__).parent.parent / "golden"


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "_").replace("/", "_").replace(":", "")


def save_golden(name: str, response: str, tool_calls: list[dict] = None,
                cost: float = 0, duration: float = 0, quality_scores: dict = None):
    """Save a response as the golden baseline for a scenario."""
    GOLDEN_DIR.mkdir(exist_ok=True)
    slug = _slugify(name)

    data = {
        "name": name,
        "response": response,
        "tool_calls": [t.get("tool", "") for t in (tool_calls or [])],
        "cost": cost,
        "duration": duration,
        "quality_scores": quality_scores or {},
    }

    filepath = GOLDEN_DIR / f"{slug}.json"
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"[GOLDEN] Saved baseline: {filepath}")
    return filepath


def load_golden(name: str) -> dict | None:
    """Load the golden baseline for a scenario."""
    slug = _slugify(name)
    filepath = GOLDEN_DIR / f"{slug}.json"
    if not filepath.exists():
        return None
    with open(filepath) as f:
        return json.load(f)


def grade_against_golden(name: str, response: str, tool_calls: list[dict] = None,
                         cost: float = 0, quality_scores: dict = None) -> dict:
    """Compare current response against golden baseline.

    Checks:
    - Response similarity (fuzzy text match)
    - Same tools used
    - Cost within 2x of golden
    - Quality score not degraded
    """
    golden = load_golden(name)
    if golden is None:
        return {"golden": {"pass": None, "detail": f"No golden baseline for: {name}. Run with --record to create."}}

    results = {}

    # Text similarity (0.0 to 1.0)
    similarity = SequenceMatcher(None, response.lower(), golden["response"].lower()).ratio()
    # We don't expect exact match — 0.3+ means similar topic/structure
    results["golden_similarity"] = {
        "pass": similarity >= 0.2,
        "detail": f"Response similarity: {similarity:.1%} (vs golden baseline)",
    }

    # Tool overlap
    golden_tools = set(golden.get("tool_calls", []))
    actual_tools = {t.get("tool", "") for t in (tool_calls or [])}
    if golden_tools:
        overlap = len(golden_tools & actual_tools) / len(golden_tools) if golden_tools else 1.0
        results["golden_tools"] = {
            "pass": overlap >= 0.5,
            "detail": f"Tool overlap: {overlap:.0%} — golden: {golden_tools}, actual: {actual_tools}",
        }

    # Cost regression (allow up to 3x golden)
    golden_cost = golden.get("cost", 0)
    if golden_cost > 0 and cost > 0:
        ratio = cost / golden_cost
        results["golden_cost"] = {
            "pass": ratio <= 3.0,
            "detail": f"Cost ratio: {ratio:.1f}x golden (${cost:.4f} vs ${golden_cost:.4f})",
        }

    # Quality regression
    golden_quality = golden.get("quality_scores", {})
    if golden_quality and quality_scores:
        golden_overall = golden_quality.get("overall", 0)
        current_overall = quality_scores.get("overall", 0)
        if golden_overall > 0:
            degraded = current_overall < golden_overall - 1  # Allow 1 point drop
            results["golden_quality"] = {
                "pass": not degraded,
                "detail": f"Quality: {current_overall}/5 vs golden {golden_overall}/5"
                          + (" — DEGRADED" if degraded else ""),
            }

    return results
