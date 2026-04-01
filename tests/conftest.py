"""Shared test fixtures for shadow-ai."""

import os
import tempfile

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from shadow_ai.db import init_db


@pytest.fixture
def db_path():
    """Create a temporary SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
def mock_slack_client():
    """Mocked Slack WebClient."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1234567890.123456"}
    client.reactions_add.return_value = {"ok": True}
    client.reactions_remove.return_value = {"ok": True}
    client.conversations_replies.return_value = {
        "ok": True,
        "messages": [
            {"user": "U123", "text": "Hello world", "ts": "1234567890.000001"},
            {"user": "U456", "text": "Bot reply", "ts": "1234567890.000002", "bot_id": "B123"},
        ],
    }
    return client


@pytest.fixture
def mock_config(tmp_path):
    """BotConfig with test defaults."""
    from shadow_ai.config import BotConfig
    return BotConfig(
        bot_username="test",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        allowed_user_ids=["U123"],
        claude_work_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def knowledge_dir(tmp_path):
    """Temp knowledge directory with notes/, conversations/, agents/, skills/."""
    for subdir in ("notes", "conversations", "agents", "skills"):
        (tmp_path / subdir).mkdir()
    return tmp_path
