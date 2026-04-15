"""Tests for SDK-native session resume in claude_runner."""

from unittest.mock import MagicMock
from shadow_ai.claude_runner import invoke_claude_code


def _make_session_kwargs(**overrides):
    """Minimal kwargs dict for invoke_claude_code. All DI hooks are MagicMocks unless overridden."""
    defaults = dict(
        config=MagicMock(request_timeout=5, verbose_progress=False),
        slack_client=MagicMock(),
        get_session_fn=MagicMock(return_value=None),
        remove_session_fn=MagicMock(),
        touch_session_fn=MagicMock(),
        mark_session_processing_fn=MagicMock(),
        store_session_fn=MagicMock(),
        db_get_thread_messages_fn=MagicMock(return_value=[]),
        db_get_thread_channel_fn=MagicMock(return_value="C001"),
        create_options_fn=MagicMock(),
        mcp_server_names=[],
        mcp_tool_catalog="",
        knowledge_index_file="",
        knowledge_dirs=[],
    )
    defaults.update(overrides)
    return defaults


def test_db_set_claude_session_id_called_on_successful_turn(monkeypatch):
    """After a turn completes, the session_id from ResultMessage must be persisted to DB.

    We simulate _collect_response calling the injected setter by stubbing _run_in_new_loop
    to invoke the db_set_claude_session_id_fn kwarg with a fake session_id. This verifies
    the kwarg is correctly threaded all the way from invoke_claude_code down to the
    _collect_response layer.
    """
    from shadow_ai import claude_runner

    recorded = []

    def fake_db_set_claude_session_id_fn(ts, sid):
        recorded.append((ts, sid))

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        # simulate the setter being called by _collect_response
        setter = kwargs.get("db_set_claude_session_id_fn")
        if setter:
            setter(thread_ts, "sess-abc-123")
        return ("response text", {"session_id": "sess-abc-123"})

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    kwargs = _make_session_kwargs(
        db_set_claude_session_id_fn=fake_db_set_claude_session_id_fn,
    )
    invoke_claude_code("hello", "1.1", **kwargs)
    assert recorded == [("1.1", "sess-abc-123")]
