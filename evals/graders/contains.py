"""Content-based graders: check if response contains or excludes specific text."""

import re


def grade_contains(response: str, expected: list[str]) -> dict:
    """Check that response contains all expected strings."""
    results = {}
    for text in expected:
        found = text.lower() in response.lower()
        results[f"contains: {text}"] = {"pass": found, "detail": f"{'Found' if found else 'Missing'}: {text!r}"}
    return results


def grade_not_contains(response: str, forbidden: list[str]) -> dict:
    """Check that response does NOT contain any forbidden strings."""
    results = {}
    for text in forbidden:
        found = text.lower() in response.lower()
        results[f"not_contains: {text}"] = {"pass": not found, "detail": f"{'Found (BAD)' if found else 'Not found (good)'}: {text!r}"}
    return results


def grade_min_length(response: str, min_len: int) -> dict:
    actual = len(response.strip())
    return {"min_length": {"pass": actual >= min_len, "detail": f"Length {actual} vs min {min_len}"}}


def grade_max_length(response: str, max_len: int) -> dict:
    actual = len(response.strip())
    return {"max_length": {"pass": actual <= max_len, "detail": f"Length {actual} vs max {max_len}"}}


def grade_regex(response: str, patterns: list[str]) -> dict:
    """Check that response matches all regex patterns."""
    results = {}
    for pattern in patterns:
        match = bool(re.search(pattern, response, re.IGNORECASE))
        results[f"regex: {pattern}"] = {"pass": match, "detail": f"{'Matched' if match else 'No match'}: {pattern}"}
    return results
