"""Tests for local Claude Code session discovery and resolution."""

import json
import os

import pytest

from shadow_ai import local_sessions
from shadow_ai.local_sessions import (
    LocalSession,
    format_session_list,
    resolve_session,
    scan_local_sessions,
)


def _write_transcript(dir_path, session_id, *, cwd=None, branch=None,
                      summary=None, first_user=None, mtime=None):
    """Write a minimal .jsonl transcript and return its path."""
    path = os.path.join(dir_path, f"{session_id}.jsonl")
    lines = []
    if summary is not None:
        lines.append({"type": "summary", "summary": summary, "sessionId": session_id})
    user_msg = {
        "type": "user",
        "cwd": cwd,
        "gitBranch": branch,
        "timestamp": "2026-06-30T10:00:00.000Z",
        "message": {"role": "user", "content": first_user or "hello"},
    }
    lines.append(user_msg)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def fake_projects(tmp_path, monkeypatch):
    """Point the module's glob at a temp ~/.claude/projects layout."""
    projects = tmp_path / "projects"
    proj_a = projects / "-Users-me-repo-a"
    proj_b = projects / "-Users-me-repo-b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    monkeypatch.setattr(
        local_sessions, "PROJECTS_GLOB", str(projects / "*" / "*.jsonl")
    )
    return {"projects": projects, "a": proj_a, "b": proj_b}


def test_scan_parses_metadata(fake_projects):
    _write_transcript(
        fake_projects["a"], "aaaa1111-2222-3333",
        cwd="/Users/me/repo-a", branch="feature/x",
        summary="Built the charges feature", first_user="add charges",
    )
    sessions = scan_local_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "aaaa1111-2222-3333"
    assert s.short_id == "aaaa1111"
    assert s.cwd == "/Users/me/repo-a"
    assert s.git_branch == "feature/x"
    assert s.summary == "Built the charges feature"
    assert s.label == "Built the charges feature"


def test_scan_sorts_newest_first(fake_projects):
    _write_transcript(fake_projects["a"], "old11111", cwd="/r/a", mtime=1000)
    _write_transcript(fake_projects["b"], "new22222", cwd="/r/b", mtime=9000)
    sessions = scan_local_sessions()
    assert sessions[0].session_id == "new22222"
    assert sessions[1].session_id == "old11111"


def test_scan_filter_matches_branch_and_cwd(fake_projects):
    _write_transcript(fake_projects["a"], "aaaa1111", cwd="/Users/me/avis", branch="feature/x", mtime=2)
    _write_transcript(fake_projects["b"], "bbbb2222", cwd="/Users/me/brunt", branch="main", mtime=1)
    assert [s.session_id for s in scan_local_sessions(filter_text="avis")] == ["aaaa1111"]
    assert [s.session_id for s in scan_local_sessions(filter_text="brunt")] == ["bbbb2222"]
    assert [s.session_id for s in scan_local_sessions(filter_text="feature/x")] == ["aaaa1111"]


def test_scan_limit(fake_projects):
    for i in range(5):
        _write_transcript(fake_projects["a"], f"sess{i:04d}", cwd="/r", mtime=i)
    assert len(scan_local_sessions(limit=3)) == 3


def test_label_falls_back_to_first_user_msg(fake_projects):
    _write_transcript(fake_projects["a"], "ccccdddd", cwd="/r", first_user="why does X fail?")
    s = scan_local_sessions()[0]
    assert s.summary is None
    assert s.label == "why does X fail?"


def test_scan_keeps_first_cwd_when_session_cd_s_midway(fake_projects):
    """Claude may `cd` into a subdir mid-session; we must keep the ORIGINAL cwd.

    Claude Code indexes a session under the directory it *started* in. Resuming
    must launch from that original cwd or the SDK's connect handshake exits 1.
    """
    path = os.path.join(fake_projects["a"], "cd11cd22.jsonl")
    records = [
        {"type": "summary", "summary": "migrations fix", "sessionId": "cd11cd22"},
        # session starts in the monorepo root
        {"type": "user", "cwd": "/Users/me/Commerce/Commerce", "gitBranch": "master",
         "message": {"role": "user", "content": "fix the migrations"}},
        # ...then Claude cd's into a submodule; a later record carries the new cwd
        {"type": "assistant", "cwd": "/Users/me/Commerce/Commerce/services/avis",
         "gitBranch": "master", "message": {"role": "assistant", "content": "done"}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    s = scan_local_sessions()[0]
    assert s.cwd == "/Users/me/Commerce/Commerce"  # first, not the avis subdir
    assert s.git_branch == "master"


def test_resolve_exact_id(fake_projects):
    _write_transcript(fake_projects["a"], "aaaa1111-2222", cwd="/r", mtime=2)
    _write_transcript(fake_projects["b"], "aaaa9999-8888", cwd="/r", mtime=1)
    res = resolve_session("aaaa1111-2222")
    assert res.session is not None
    assert res.session.session_id == "aaaa1111-2222"
    assert res.ambiguous is None and res.error is None


def test_resolve_unique_prefix(fake_projects):
    _write_transcript(fake_projects["a"], "abcd1111", cwd="/r", mtime=2)
    _write_transcript(fake_projects["b"], "wxyz2222", cwd="/r", mtime=1)
    res = resolve_session("abcd")
    assert res.session is not None
    assert res.session.session_id == "abcd1111"


def test_resolve_ambiguous_prefix(fake_projects):
    _write_transcript(fake_projects["a"], "abcd1111", cwd="/r", mtime=2)
    _write_transcript(fake_projects["b"], "abcd2222", cwd="/r", mtime=1)
    res = resolve_session("abcd")
    assert res.session is None
    assert res.ambiguous is not None
    assert {s.session_id for s in res.ambiguous} == {"abcd1111", "abcd2222"}


def test_resolve_exact_wins_over_prefix(fake_projects):
    # "abcd" is both an exact id AND a prefix of "abcd1111" — exact must win.
    _write_transcript(fake_projects["a"], "abcd", cwd="/r", mtime=2)
    _write_transcript(fake_projects["b"], "abcd1111", cwd="/r", mtime=1)
    res = resolve_session("abcd")
    assert res.session is not None
    assert res.session.session_id == "abcd"


def test_resolve_no_match(fake_projects):
    _write_transcript(fake_projects["a"], "abcd1111", cwd="/r")
    res = resolve_session("zzzz")
    assert res.session is None and res.ambiguous is None
    assert "zzzz" in res.error


def test_resolve_empty_ref(fake_projects):
    res = resolve_session("   ")
    assert res.error is not None


def test_resolve_strips_slack_code_span(fake_projects):
    # Slack renders a pasted id as inline code, sending literal backticks.
    _write_transcript(fake_projects["a"], "abcd1111", cwd="/r", mtime=2)
    res = resolve_session("`abcd1111`")
    assert res.session is not None
    assert res.session.session_id == "abcd1111"


def test_resolve_strips_slack_angle_link(fake_projects):
    # Slack may auto-link a bare token as <token>.
    _write_transcript(fake_projects["a"], "abcd1111", cwd="/r", mtime=2)
    res = resolve_session("<abcd>")
    assert res.session is not None
    assert res.session.session_id == "abcd1111"


def test_format_session_list_empty():
    assert "No local" in format_session_list([])


def test_format_session_list_renders_fields():
    s = LocalSession(
        session_id="aaaa1111-2222", cwd="/Users/me/avis", git_branch="feature/x",
        mtime=1_700_000_000.0, summary="charges feature", first_user_msg="x", path="/p",
    )
    out = format_session_list([s])
    assert "aaaa1111" in out
    assert "avis" in out
    assert "feature/x" in out
    assert "charges feature" in out


def test_malformed_transcript_is_skipped(fake_projects):
    # A file with garbage lines should not crash the scan.
    bad = os.path.join(fake_projects["a"], "garbage1.jsonl")
    with open(bad, "w") as fh:
        fh.write("not json\n{also not\n")
    _write_transcript(fake_projects["b"], "good1111", cwd="/r")
    sessions = scan_local_sessions()
    ids = {s.session_id for s in sessions}
    assert "good1111" in ids
    assert "garbage1" in ids  # parsed with empty metadata, still listed
