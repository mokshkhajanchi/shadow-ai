"""Tests for note-taking: saving, auto-save, and prompt injection."""

import os
import tempfile
from pathlib import Path

import pytest

from shadow_ai.knowledge import save_learned_knowledge, save_conversation


class TestSaveLearnedKnowledge:
    """Test saving curated notes to knowledge/notes/."""

    def test_saves_file(self, tmp_path):
        notes_dir = str(tmp_path / "notes")
        filepath = save_learned_knowledge("Test content", "test topic", "123.456", notes_dir)
        assert os.path.exists(filepath)
        content = Path(filepath).read_text()
        assert "# Learned: test topic" in content
        assert "Test content" in content
        assert "123.456" in content

    def test_sanitizes_topic(self, tmp_path):
        notes_dir = str(tmp_path / "notes")
        filepath = save_learned_knowledge("content", "test/bad:chars!", "123.456", notes_dir)
        assert os.path.exists(filepath)
        assert "/" not in Path(filepath).name.replace(notes_dir, "")

    def test_avoids_overwrite(self, tmp_path):
        notes_dir = str(tmp_path / "notes")
        f1 = save_learned_knowledge("content 1", "topic", "123.456", notes_dir)
        f2 = save_learned_knowledge("content 2", "topic", "123.456", notes_dir)
        assert f1 != f2
        assert os.path.exists(f1)
        assert os.path.exists(f2)

    def test_creates_directory(self, tmp_path):
        notes_dir = str(tmp_path / "nonexistent" / "notes")
        filepath = save_learned_knowledge("content", "topic", "123.456", notes_dir)
        assert os.path.exists(filepath)


class TestSaveConversation:
    """Test saving raw conversations to knowledge/conversations/."""

    def test_saves_with_thread_ts_filename(self, tmp_path):
        convo_dir = str(tmp_path / "conversations")
        filepath = save_conversation("convo text", "topic", "1234567890.123456", convo_dir)
        assert "1234567890-123456.md" in filepath
        assert os.path.exists(filepath)

    def test_overwrites_same_thread(self, tmp_path):
        convo_dir = str(tmp_path / "conversations")
        f1 = save_conversation("version 1", "topic", "123.456", convo_dir)
        f2 = save_conversation("version 2", "topic", "123.456", convo_dir)
        assert f1 == f2
        content = Path(f1).read_text()
        assert "version 2" in content
        assert "version 1" not in content

    def test_different_threads_different_files(self, tmp_path):
        convo_dir = str(tmp_path / "conversations")
        f1 = save_conversation("convo 1", "topic", "111.111", convo_dir)
        f2 = save_conversation("convo 2", "topic", "222.222", convo_dir)
        assert f1 != f2


class TestNoteSummaryInjection:
    """Test that notes are injected into the system prompt."""

    def test_notes_injected_into_prompt(self, tmp_path):
        notes_dir = tmp_path / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "test-note.md").write_text(
            "# Learned: Test Topic\nDate: 2026-03-31\nSource: Slack thread 123\n\n"
            "**User**: What is the API endpoint?\n\n**Assistant**: It's /api/v1/orders"
        )

        from shadow_ai.claude_options import create_options
        from shadow_ai.config import BotConfig
        config = BotConfig(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        opts = create_options(config)
        prompt_text = opts.system_prompt["append"]
        assert "SAVED NOTES" in prompt_text
        assert "Test Topic" in prompt_text

    def test_notes_include_full_content(self, tmp_path):
        """Notes should be injected with FULL content, not summaries."""
        notes_dir = tmp_path / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "test.md").write_text(
            "# Learned: Rate Limit\nDate: 2026-04-02\n\n"
            "**User**: The API rate limit is 100 requests per minute. Remember this."
        )
        from shadow_ai.claude_options import create_options
        from shadow_ai.config import BotConfig
        config = BotConfig(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        opts = create_options(config)
        prompt_text = opts.system_prompt["append"]
        assert "100 requests per minute" in prompt_text


class TestNoteInstructionInPrompt:
    """Note-taking is handled by Claude naturally — no explicit instruction needed."""

    def test_no_note_taking_section_in_prompt(self):
        """NOTE-TAKING section was removed to prevent Claude from over-saving."""
        from shadow_ai.claude_options import build_base_system_prompt
        prompt = build_base_system_prompt(None)
        assert "NOTE-TAKING" not in prompt
