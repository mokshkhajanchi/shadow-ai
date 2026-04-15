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


def test_resume_used_when_claude_session_id_present(monkeypatch):
    """If DB has a claude_session_id, _resume_and_query is chosen over _restore_and_query."""
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[{"role": "user", "content": "old"}]),
        db_get_claude_session_id_fn=MagicMock(return_value="sess-xyz"),
        db_clear_claude_session_id_fn=MagicMock(),
        db_set_claude_session_id_fn=MagicMock(),
    )
    invoke_claude_code("new msg", "1.1", **kwargs)
    assert calls == ["_resume_and_query"]


def test_fallback_to_text_replay_when_resume_fails(monkeypatch):
    """If _resume_and_query raises, the stale session_id is cleared and _restore_and_query runs."""
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        if coro_factory.__name__ == "_resume_and_query":
            raise RuntimeError("session transcript missing")
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    clear_mock = MagicMock()
    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[{"role": "user", "content": "old"}]),
        db_get_claude_session_id_fn=MagicMock(return_value="sess-stale"),
        db_clear_claude_session_id_fn=clear_mock,
        db_set_claude_session_id_fn=MagicMock(),
    )
    invoke_claude_code("new msg", "1.1", **kwargs)
    assert calls == ["_resume_and_query", "_restore_and_query"]
    clear_mock.assert_called_once_with("1.1")


def test_text_replay_used_when_no_session_id(monkeypatch):
    """Null claude_session_id - skip resume, go straight to text-replay (existing behavior)."""
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[{"role": "user", "content": "old"}]),
        db_get_claude_session_id_fn=MagicMock(return_value=None),
        db_clear_claude_session_id_fn=MagicMock(),
        db_set_claude_session_id_fn=MagicMock(),
    )
    invoke_claude_code("new msg", "1.1", **kwargs)
    assert calls == ["_restore_and_query"]


def test_new_session_when_no_history_and_no_session_id(monkeypatch):
    """Empty history + null session_id - _new_session_and_query (existing behavior)."""
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[]),
        db_get_claude_session_id_fn=MagicMock(return_value=None),
        db_clear_claude_session_id_fn=MagicMock(),
        db_set_claude_session_id_fn=MagicMock(),
    )
    invoke_claude_code("first msg", "1.1", **kwargs)
    assert calls == ["_new_session_and_query"]


def test_missing_injection_fns_do_not_break_flow(monkeypatch):
    """If the new DI hooks aren't passed (legacy caller), behavior matches pre-change code path."""
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    # No db_get_claude_session_id_fn, no db_clear_claude_session_id_fn, no db_set...
    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[{"role": "user", "content": "hi"}]),
    )
    invoke_claude_code("msg", "1.1", **kwargs)
    assert calls == ["_restore_and_query"]


def test_resume_failure_with_empty_history_falls_through_to_new_session(monkeypatch):
    """Edge: session_id exists but resume fails AND history is empty → _new_session_and_query.

    Covers the (rare) case where the DB has a stale claude_session_id but the messages
    table has been pruned. Ensures we don't deadlock trying to text-replay nothing.
    """
    from shadow_ai import claude_runner

    calls = []

    def fake_run_in_new_loop(coro_factory, thread_ts, request_timeout, *args, **kwargs):
        calls.append(coro_factory.__name__)
        if coro_factory.__name__ == "_resume_and_query":
            raise RuntimeError("stale")
        return ("ok", None)

    monkeypatch.setattr(claude_runner, "_run_in_new_loop", fake_run_in_new_loop)

    clear_mock = MagicMock()
    kwargs = _make_session_kwargs(
        db_get_thread_messages_fn=MagicMock(return_value=[]),  # empty history
        db_get_claude_session_id_fn=MagicMock(return_value="sess-stale"),
        db_clear_claude_session_id_fn=clear_mock,
        db_set_claude_session_id_fn=MagicMock(),
    )
    invoke_claude_code("msg", "1.1", **kwargs)
    assert calls == ["_resume_and_query", "_new_session_and_query"]
    clear_mock.assert_called_once_with("1.1")
