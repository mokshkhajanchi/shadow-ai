"""Tests for database module."""

import os
import tempfile
import pytest
from shadow_ai.db import (
    init_db,
    db_create_thread,
    db_is_active_thread,
    db_stop_thread,
    db_get_active_thread_count,
    db_save_message,
    db_get_thread_messages,
    db_get_thread_channel,
)


@pytest.fixture
def db_path():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    init_db(path)
    yield path
    os.unlink(path)
    # Clean up WAL/SHM files
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


class TestThreadOperations:
    def test_create_thread(self, db_path):
        db_create_thread(db_path, "1234.5678", "C0001")
        assert db_is_active_thread(db_path, "1234.5678")

    def test_stop_thread(self, db_path):
        db_create_thread(db_path, "1234.5678", "C0001")
        db_stop_thread(db_path, "1234.5678")
        assert not db_is_active_thread(db_path, "1234.5678")

    def test_active_thread_count(self, db_path):
        assert db_get_active_thread_count(db_path) == 0
        db_create_thread(db_path, "1.1", "C0001")
        db_create_thread(db_path, "2.2", "C0001")
        assert db_get_active_thread_count(db_path) == 2
        db_stop_thread(db_path, "1.1")
        assert db_get_active_thread_count(db_path) == 1

    def test_get_channel(self, db_path):
        db_create_thread(db_path, "1.1", "C_TEST")
        assert db_get_thread_channel(db_path, "1.1") == "C_TEST"

    def test_nonexistent_thread(self, db_path):
        assert not db_is_active_thread(db_path, "nope")
        assert db_get_thread_channel(db_path, "nope") is None


class TestMessageOperations:
    def test_save_and_get_messages(self, db_path):
        db_create_thread(db_path, "1.1", "C0001")
        db_save_message(db_path, "1.1", "user", "hello", user_id="U001")
        db_save_message(db_path, "1.1", "assistant", "hi back")

        messages = db_get_thread_messages(db_path, "1.1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "hi back"

    def test_empty_thread_messages(self, db_path):
        db_create_thread(db_path, "1.1", "C0001")
        messages = db_get_thread_messages(db_path, "1.1")
        assert messages == []
