"""Tests for the doctor command."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from shadow_ai.doctor import _check, run_doctor


class TestCheckOutput:

    def test_pass(self, capsys):
        result = _check("Test", True, "all good")
        assert result is True
        out = capsys.readouterr().out
        assert "✓" in out
        assert "Test" in out
        assert "all good" in out

    def test_fail_with_fix(self, capsys):
        result = _check("Test", False, "broken", fix="do this")
        assert result is False
        out = capsys.readouterr().out
        assert "✗" in out
        assert "broken" in out
        assert "do this" in out

    def test_fail_without_fix(self, capsys):
        result = _check("Test", False, "broken")
        assert result is False
        out = capsys.readouterr().out
        assert "✗" in out
        assert "→" not in out


class TestRunDoctor:

    def test_detects_missing_env(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_doctor()
        out = capsys.readouterr().out
        assert ".env" in out
        assert "not found" in out

    def test_python_version_check(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_doctor()
        out = capsys.readouterr().out
        # Python version should always show
        assert f"{sys.version_info.major}.{sys.version_info.minor}" in out

    def test_with_env_file(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create .env with required vars
        (tmp_path / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-test\n"
            "SLACK_APP_TOKEN=xapp-test\n"
            "ALLOWED_USER_IDS=U123\n"
            "CLAUDE_WORK_DIR=.\n"
        )
        (tmp_path / "knowledge" / "notes").mkdir(parents=True)
        run_doctor()
        out = capsys.readouterr().out
        assert "3 required vars set" in out
