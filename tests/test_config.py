"""Tests for config module."""

import os
import pytest
from unittest.mock import patch

from shadow_ai.config import BotConfig


class TestBotConfig:
    """Test BotConfig.from_env() with various env states."""

    def test_from_env_with_tokens(self):
        """Should work with token env vars set."""
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "ALLOWED_USER_IDS": "",
            "DAILY_BUDGET_USD": "0",
            "CLAUDE_WORK_DIR": "/tmp/test",
            "KNOWLEDGE_PATHS": "",
            "REQUEST_TIMEOUT": "600",
            "CLAUDE_MAX_TURNS": "30",
            "CLAUDE_PERMISSION_MODE": "acceptEdits",
            "MAX_ACTIVE_SESSIONS": "3",
        }
        with patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            assert config.slack_bot_token == "xoxb-test"
            assert config.slack_app_token == "xapp-test"
            assert config.max_concurrent == 5
            assert config.daily_budget_usd == 0

    def test_from_env_all_fields(self):
        """Should parse all env vars correctly."""
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "ALLOWED_USER_IDS": "U123,U456",
            "CLAUDE_WORK_DIR": "/tmp/test",
            "CLAUDE_MAX_TURNS": "50",
            "MAX_CONCURRENT": "10",
            "DAILY_BUDGET_USD": "100",
            "CLAUDE_MODEL": "claude-opus-4-6",
            "CLAUDE_THINKING": "enabled",
            "CLAUDE_PERMISSION_MODE": "bypassPermissions",
            "REQUEST_TIMEOUT": "300",
        }
        with patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            assert config.allowed_user_ids == ["U123", "U456"]
            assert config.claude_work_dir == "/tmp/test"
            assert config.max_turns == 50
            assert config.max_concurrent == 10
            assert config.daily_budget_usd == 100
            assert config.claude_model == "claude-opus-4-6"
            assert config.claude_thinking == "enabled"
            assert config.permission_mode == "bypassPermissions"
            assert config.request_timeout == 300

    def test_allowed_user_ids_populated(self):
        """ALLOWED_USER_IDS with values should parse correctly."""
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "ALLOWED_USER_IDS": "U123,U456",
        }
        with patch.dict(os.environ, env, clear=True):
            config = BotConfig.from_env()
            assert "U123" in config.allowed_user_ids
            assert "U456" in config.allowed_user_ids
