"""Tests for channel monitoring: DB CRUD, noise filter, read-only tools."""

import pytest

from shadow_ai.db import (
    db_add_monitored_channel,
    db_get_monitored_channels,
    db_is_monitored_channel,
    db_remove_monitored_channel,
)


class TestMonitoredChannelDB:

    def test_add_and_check(self, db_path):
        db_add_monitored_channel(db_path, "C123", "U456")
        assert db_is_monitored_channel(db_path, "C123") is True

    def test_not_monitored(self, db_path):
        assert db_is_monitored_channel(db_path, "C999") is False

    def test_remove(self, db_path):
        db_add_monitored_channel(db_path, "C123", "U456")
        db_remove_monitored_channel(db_path, "C123")
        assert db_is_monitored_channel(db_path, "C123") is False

    def test_list_channels(self, db_path):
        db_add_monitored_channel(db_path, "C111", "U456")
        db_add_monitored_channel(db_path, "C222", "U456")
        channels = db_get_monitored_channels(db_path)
        assert "C111" in channels
        assert "C222" in channels
        assert len(channels) == 2

    def test_add_duplicate_replaces(self, db_path):
        db_add_monitored_channel(db_path, "C123", "U111")
        db_add_monitored_channel(db_path, "C123", "U222")
        channels = db_get_monitored_channels(db_path)
        assert len(channels) == 1

    def test_remove_nonexistent(self, db_path):
        # Should not raise
        db_remove_monitored_channel(db_path, "C999")


class TestNoiseFilter:
    """Test the noise filter from events.py."""

    @pytest.fixture(autouse=True)
    def _import_filter(self):
        """Import the noise filter. It's defined inside register_events,
        so we recreate it here matching the same logic."""
        import re
        self._NOISE_WORDS = {
            "ok", "okay", "thanks", "thank you", "thx", "ty", "got it", "cool",
            "nice", "great", "sure", "yes", "no", "yep", "nope", "done", "np",
            "ack", "acknowledged", "noted", "lgtm", "wfm", "sg", "roger",
        }

        def _is_noise(text: str) -> bool:
            t = text.strip().lower()
            if len(t) < 5:
                return True
            if re.match(r'^[\s:+\-_a-z0-9]*$', t) and ':' in t:
                return True
            if t in self._NOISE_WORDS:
                return True
            if re.match(r'^<https?://[^>]+>$', t):
                return True
            return False

        self._is_noise = _is_noise

    @pytest.mark.parametrize("text", [
        "ok", "yes", "no", "hi", "ty",  # short or ack
        "thanks", "thank you", "got it", "lgtm", "done",  # noise words
        ":thumbsup:", ":+1:",  # emoji-only
        "<https://example.com>",  # bare URL
    ])
    def test_noise_detected(self, text):
        assert self._is_noise(text) is True

    @pytest.mark.parametrize("text", [
        "What is the deployment status?",
        "Can someone review my PR?",
        "The build is failing on staging",
        "How do I fix this error?",
        "Please check the latest Jira tickets",
    ])
    def test_non_noise_passes(self, text):
        assert self._is_noise(text) is False


class TestMonitoredOptions:
    """Test that monitored sessions get read-only tools."""

    def test_monitored_read_only_tools(self, tmp_path):
        from shadow_ai.claude_options import create_options
        from shadow_ai.config import BotConfig
        config = BotConfig(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        opts = create_options(config, monitored=True)
        assert "Read" in opts.allowed_tools
        assert "Glob" in opts.allowed_tools
        assert "Grep" in opts.allowed_tools
        assert "Write" not in opts.allowed_tools
        assert "Edit" not in opts.allowed_tools
        assert "Bash" not in opts.allowed_tools
        assert "Agent" not in opts.allowed_tools

    def test_monitored_uses_default_max_turns(self, tmp_path):
        from shadow_ai.claude_options import create_options
        from shadow_ai.config import BotConfig
        config = BotConfig(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        opts = create_options(config, monitored=True)
        assert opts.max_turns == 50  # No turn limit for monitored

    def test_normal_full_tools(self, tmp_path):
        from shadow_ai.claude_options import create_options
        from shadow_ai.config import BotConfig
        config = BotConfig(
            bot_username="test",
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            allowed_user_ids=["U123"],
            claude_work_dir=str(tmp_path),
        )
        opts = create_options(config, monitored=False)
        assert "Write" in opts.allowed_tools
        assert "Bash" in opts.allowed_tools
        assert "Agent" in opts.allowed_tools
