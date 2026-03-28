"""Tests for slack_helpers module."""

import pytest
from shadow_ai.slack_helpers import (
    markdown_to_slack,
    chunk_message,
    parse_model_prefix,
    clean_message_text,
)
from shadow_ai.config import MODEL_ALIASES


class TestMarkdownToSlack:
    """Test markdown to Slack mrkdwn conversion."""

    def test_bold(self):
        assert "**bold**" not in markdown_to_slack("This is **bold** text")
        assert "*bold*" in markdown_to_slack("This is **bold** text")

    def test_headers(self):
        result = markdown_to_slack("# Header 1\n## Header 2")
        assert "#" not in result or "*" in result  # Headers converted to bold

    def test_code_blocks_preserved(self):
        md = "```python\ndef foo():\n    pass\n```"
        result = markdown_to_slack(md)
        assert "```" in result
        assert "def foo():" in result

    def test_links(self):
        result = markdown_to_slack("[click here](https://example.com)")
        assert "<https://example.com|click here>" in result

    def test_empty_input(self):
        assert markdown_to_slack("") == ""

    def test_plain_text(self):
        assert markdown_to_slack("just plain text") == "just plain text"


class TestChunkMessage:
    """Test message chunking for Slack's character limits."""

    def test_short_message(self):
        chunks = chunk_message("short message")
        assert len(chunks) == 1
        assert chunks[0] == "short message"

    def test_long_message(self):
        text = "x" * 8000
        chunks = chunk_message(text, max_length=3900)
        assert len(chunks) >= 2
        assert all(len(c) <= 3900 for c in chunks)

    def test_preserves_content(self):
        text = "line1\nline2\nline3"
        chunks = chunk_message(text, max_length=100)
        combined = "".join(chunks)
        assert "line1" in combined
        assert "line3" in combined

    def test_empty_message(self):
        chunks = chunk_message("")
        assert chunks == [""]


class TestParseModelPrefix:
    """Test model prefix extraction from messages."""

    def test_no_prefix(self):
        model, text = parse_model_prefix("hello world", MODEL_ALIASES)
        assert text == "hello world"
        assert model is None

    def test_opus_prefix(self):
        model, text = parse_model_prefix("opus: do something", MODEL_ALIASES)
        assert text == "do something"
        assert model == "claude-opus-4-6"

    def test_sonnet_prefix(self):
        model, text = parse_model_prefix("sonnet: fast test", MODEL_ALIASES)
        assert text == "fast test"
        assert model == "claude-sonnet-4-6"

    def test_haiku_prefix(self):
        model, text = parse_model_prefix("haiku: quick answer", MODEL_ALIASES)
        assert text == "quick answer"
        assert model == "claude-haiku-4-5-20251001"


class TestCleanMessageText:
    """Test message text cleaning."""

    def test_removes_bot_mention(self):
        text = clean_message_text("<@U12345> do something", "U12345")
        assert "<@U12345>" not in text
        assert "do something" in text

    def test_strips_whitespace(self):
        text = clean_message_text("  hello  ", "UBOT")
        assert text == "hello"

    def test_empty_after_cleaning(self):
        text = clean_message_text("<@UBOT>", "UBOT")
        assert text.strip() == ""
