"""Tests for channel_rules.py + the events-level mandatory-rules gate +
prompt injection of `## When to invoke` into the monitored prompt.

There is NO separate classifier — rules enforcement happens inside the
main Claude invocation via the NO_RESPONSE suppression path. Tests
therefore cover:
 1. `extract_invoke_rules` parsing
 2. `find_channel_rules_file` lookup
 3. events.py: missing rules → silent; present rules → invoke
 4. handlers.py: `## When to invoke` section is injected into the prompt
"""

from unittest.mock import MagicMock

import pytest


# ─── extract_invoke_rules ────────────────────────────────────────────────────

class TestExtractInvokeRules:

    def test_returns_none_for_empty_input(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        assert extract_invoke_rules("") is None
        assert extract_invoke_rules(None) is None

    def test_missing_section_returns_none(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        rules = "## How to answer\n- Be concise\n\n## Domain\n- Foo"
        assert extract_invoke_rules(rules) is None

    def test_extracts_section_body(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        rules = (
            "# Channel X\n\n"
            "## When to invoke\n"
            "Invoke for PR URLs and questions.\n"
            "Skip FYIs.\n\n"
            "## How to answer\n- Be concise"
        )
        body = extract_invoke_rules(rules)
        assert body is not None
        assert "Invoke for PR URLs" in body
        assert "Skip FYIs" in body
        # Stops at the next `## ` heading
        assert "How to answer" not in body
        assert "Be concise" not in body

    def test_case_insensitive_heading(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        rules = "## WHEN TO INVOKE\nAlways invoke for foo.\n"
        assert extract_invoke_rules(rules) == "Always invoke for foo."

    def test_empty_section_returns_none(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        rules = "## When to invoke\n\n## Other\nbody"
        assert extract_invoke_rules(rules) is None

    def test_section_at_end_of_file(self):
        from shadow_ai.channel_rules import extract_invoke_rules
        rules = "# Header\n\n## When to invoke\nFinal body content."
        assert extract_invoke_rules(rules) == "Final body content."


# ─── find_channel_rules_file ─────────────────────────────────────────────────

class TestFindChannelRulesFile:

    def test_returns_none_for_empty_name(self, tmp_path):
        from shadow_ai.channel_rules import find_channel_rules_file
        assert find_channel_rules_file("", str(tmp_path)) is None

    def test_finds_file_in_work_dir(self, tmp_path):
        from shadow_ai.channel_rules import find_channel_rules_file
        channels_dir = tmp_path / "channels"
        channels_dir.mkdir()
        rule_file = channels_dir / "mychan.md"
        rule_file.write_text("## When to invoke\nfoo\n")
        found = find_channel_rules_file("mychan", str(tmp_path))
        assert found is not None
        assert found.resolve() == rule_file.resolve()

    def test_returns_none_when_missing(self, tmp_path):
        from shadow_ai.channel_rules import find_channel_rules_file
        assert find_channel_rules_file("no-such-channel-xyz", str(tmp_path)) is None


# ─── Monitored-channel gate in events.py ─────────────────────────────────────

class _FakeApp:
    def __init__(self):
        self.event_handlers = {}
        self.action_handlers = {}

    def event(self, name):
        def decorator(fn):
            self.event_handlers[name] = fn
            return fn
        return decorator

    def action(self, name):
        def decorator(fn):
            self.action_handlers[name] = fn
            return fn
        return decorator


@pytest.fixture
def cfg_with_rules(db_path, tmp_path):
    """Config pointing at a tmp work dir so rules files land there."""
    from shadow_ai.config import BotConfig
    (tmp_path / "channels").mkdir()
    return BotConfig(
        bot_username="test",
        slack_bot_token="xoxb",
        slack_app_token="xapp",
        allowed_user_ids=["U123"],
        claude_work_dir=str(tmp_path),
        db_path=db_path,
    )


def _register(app, config):
    from shadow_ai import events as events_mod
    events_mod.register_events(
        app, config, MagicMock(), MagicMock(), bot_user_id="UBOT",
        get_thread_lock_fn=MagicMock(),
        remove_thread_lock_fn=MagicMock(),
        create_options_fn=MagicMock(),
    )


class TestMonitoredChannelRulesGate:
    """Monitored channels without a `## When to invoke` section must stay
    silent. With a section present, routing proceeds to handle_user_message
    where Claude reads the injected rules and may emit NO_RESPONSE."""

    def _setup(self, cfg, monkeypatch):
        from shadow_ai import events as events_mod
        hum_mock = MagicMock()
        monkeypatch.setattr(events_mod, "handle_user_message", hum_mock)
        app = _FakeApp()
        _register(app, cfg)
        return hum_mock, app.event_handlers["message"]

    def _write_rules(self, cfg, channel_name, content):
        from pathlib import Path
        p = Path(cfg.claude_work_dir) / "channels" / f"{channel_name}.md"
        p.write_text(content)

    def _monitor(self, cfg, channel_id, channel_name):
        from shadow_ai.db import db_add_monitored_channel
        db_add_monitored_channel(
            cfg.db_path, channel_id, "U123", channel_name=channel_name,
        )

    def test_valid_rules_route_to_handle(self, cfg_with_rules, monkeypatch):
        hum_mock, msg_handler = self._setup(cfg_with_rules, monkeypatch)
        self._write_rules(
            cfg_with_rules, "rulechan",
            "## When to invoke\nInvoke for questions.\n",
        )
        self._monitor(cfg_with_rules, "CRULECH1", "rulechan")

        event = {
            "user": "U999", "channel": "CRULECH1", "channel_type": "channel",
            "ts": "1700.100", "text": "what is the deploy status?",
        }
        msg_handler(event, MagicMock())
        hum_mock.assert_called_once()
        assert hum_mock.call_args.kwargs.get("monitored") is True

    def test_missing_rules_section_blocks_silently(self, cfg_with_rules, monkeypatch):
        hum_mock, msg_handler = self._setup(cfg_with_rules, monkeypatch)
        self._write_rules(
            cfg_with_rules, "badchan",
            "## How to answer\nBe concise\n",
        )
        self._monitor(cfg_with_rules, "CBADCHN1", "badchan")

        event = {
            "user": "U999", "channel": "CBADCHN1", "channel_type": "channel",
            "ts": "1700.100", "text": "anything",
        }
        msg_handler(event, MagicMock())
        hum_mock.assert_not_called()

    def test_missing_rules_file_blocks_silently(self, cfg_with_rules, monkeypatch):
        hum_mock, msg_handler = self._setup(cfg_with_rules, monkeypatch)
        self._monitor(cfg_with_rules, "CNOFILE1", "nofile-chan")

        event = {
            "user": "U999", "channel": "CNOFILE1", "channel_type": "channel",
            "ts": "1700.100", "text": "anything",
        }
        msg_handler(event, MagicMock())
        hum_mock.assert_not_called()

    def test_thread_reply_bypasses_rules_check(self, cfg_with_rules, monkeypatch):
        """Replies within an existing thread bypass the rules check — the
        bot already committed to that conversation at top level."""
        hum_mock, msg_handler = self._setup(cfg_with_rules, monkeypatch)
        # Deliberately NO rules file written — thread reply should still
        # route because the top-level check doesn't apply.
        self._monitor(cfg_with_rules, "CRULECH1", "rulechan")

        event = {
            "user": "U999", "channel": "CRULECH1", "channel_type": "channel",
            "ts": "1700.200", "thread_ts": "1700.100",
            "text": "follow-up chatter",
        }
        msg_handler(event, MagicMock())
        hum_mock.assert_called_once()


# ─── handlers.py: `## When to invoke` is injected into the monitored prompt ─

class TestMonitoredPromptInjectsRules:
    """When a monitored channel has a `## When to invoke` section, the
    text is injected into the prompt as AUTHORITATIVE rules with an
    explicit NO_RESPONSE instruction for non-matches."""

    def test_invoke_rules_text_present_in_prompt(self, cfg_with_rules, monkeypatch):
        from shadow_ai import handlers as handlers_mod
        from shadow_ai.db import db_create_thread, db_add_monitored_channel
        from pathlib import Path

        # Write rules with a distinctive marker
        (Path(cfg_with_rules.claude_work_dir) / "channels" / "promptchan.md").write_text(
            "## When to invoke\nDISTINCTIVE_INVOKE_MARKER_XYZ: invoke for foo.\n"
        )
        db_add_monitored_channel(
            cfg_with_rules.db_path, "CPROMPT1", "U123", channel_name="promptchan",
        )
        db_create_thread(cfg_with_rules.db_path, "1700.000", "CPROMPT1")

        captured_prompts = []

        def fake_invoke(prompt, thread_ts, **kwargs):
            captured_prompts.append(prompt)
            return ("NO_RESPONSE", None)

        monkeypatch.setattr(handlers_mod, "invoke_claude_code", fake_invoke)
        monkeypatch.setattr(
            handlers_mod, "fetch_thread_messages",
            lambda *a, **k: ("", None),
        )

        slack_client = MagicMock()
        slack_client.chat_postMessage.return_value = {"ok": True, "ts": "progress.1"}
        lock = MagicMock()
        lock.acquire.return_value = True

        handlers_mod._process_message(
            user_id="U999",
            channel="CPROMPT1",
            thread_ts="1700.000",
            message_ts="1700.100",
            text="the deploy is done",
            monitored=True,
            config=cfg_with_rules,
            slack_client=slack_client,
            bot_user_id="UBOT",
            get_thread_lock_fn=lambda _ts: lock,
            create_options_fn=MagicMock(),
        )

        assert captured_prompts, "invoke_claude_code was never called"
        prompt = captured_prompts[0]
        # The rule body must appear in the prompt
        assert "DISTINCTIVE_INVOKE_MARKER_XYZ" in prompt
        # The authoritative gate block must reference NO_RESPONSE
        assert "NO_RESPONSE" in prompt
        assert "CHANNEL INVOCATION RULES" in prompt

    def test_no_rules_section_uses_fallback_block(self, cfg_with_rules, monkeypatch):
        """When no `## When to invoke` section exists, the prompt falls
        back to the generic NO_RESPONSE instruction (no CHANNEL INVOCATION
        RULES block). This path is reachable from thread replies or when
        events.py decides to invoke anyway."""
        from shadow_ai import handlers as handlers_mod
        from shadow_ai.db import db_create_thread, db_add_monitored_channel
        from pathlib import Path

        (Path(cfg_with_rules.claude_work_dir) / "channels" / "genericchan.md").write_text(
            "## How to answer\nBe concise.\n"
        )
        db_add_monitored_channel(
            cfg_with_rules.db_path, "CGENERIC1", "U123", channel_name="genericchan",
        )
        db_create_thread(cfg_with_rules.db_path, "1700.000", "CGENERIC1")

        captured_prompts = []

        def fake_invoke(prompt, thread_ts, **kwargs):
            captured_prompts.append(prompt)
            return ("NO_RESPONSE", None)

        monkeypatch.setattr(handlers_mod, "invoke_claude_code", fake_invoke)
        monkeypatch.setattr(
            handlers_mod, "fetch_thread_messages",
            lambda *a, **k: ("", None),
        )

        slack_client = MagicMock()
        slack_client.chat_postMessage.return_value = {"ok": True, "ts": "progress.1"}
        lock = MagicMock()
        lock.acquire.return_value = True

        handlers_mod._process_message(
            user_id="U999",
            channel="CGENERIC1",
            thread_ts="1700.000",
            message_ts="1700.100",
            text="any message",
            monitored=True,
            config=cfg_with_rules,
            slack_client=slack_client,
            bot_user_id="UBOT",
            get_thread_lock_fn=lambda _ts: lock,
            create_options_fn=MagicMock(),
        )

        assert captured_prompts, "invoke_claude_code was never called"
        prompt = captured_prompts[0]
        # The fallback block still mentions NO_RESPONSE but NOT the
        # authoritative CHANNEL INVOCATION RULES header.
        assert "NO_RESPONSE" in prompt
        assert "CHANNEL INVOCATION RULES" not in prompt


# ─── monitor command rejects channels without rules ─────────────────────────

class TestMonitorCommandValidatesRules:

    def test_monitor_rejects_channel_without_rules_section(self, cfg_with_rules):
        from shadow_ai.handlers import _process_message
        from pathlib import Path

        (Path(cfg_with_rules.claude_work_dir) / "channels" / "targetchan.md").write_text(
            "## How to answer\nBe concise\n"
        )

        slack_client = MagicMock()
        slack_client.chat_postMessage.return_value = {"ok": True, "ts": "progress.1"}
        slack_client.conversations_info.return_value = {
            "ok": True, "channel": {"name": "targetchan"},
        }

        lock = MagicMock()
        lock.acquire.return_value = True

        _process_message(
            user_id="U123",
            channel="CSOURCE1",
            thread_ts="1700.000",
            message_ts="1700.100",
            text="<@UBOT> monitor <#CTARGET1|targetchan>",
            monitored=False,
            config=cfg_with_rules,
            slack_client=slack_client,
            bot_user_id="UBOT",
            get_thread_lock_fn=lambda _ts: lock,
            create_options_fn=MagicMock(),
        )

        posted_texts = [
            c.kwargs.get("text", "") for c in slack_client.chat_postMessage.call_args_list
        ]
        assert any("Cannot monitor" in t for t in posted_texts), posted_texts

        from shadow_ai.db import db_is_monitored_channel
        assert db_is_monitored_channel(cfg_with_rules.db_path, "CTARGET1") is False

    def test_monitor_accepts_channel_with_valid_rules(self, cfg_with_rules):
        from shadow_ai.handlers import _process_message
        from pathlib import Path

        (Path(cfg_with_rules.claude_work_dir) / "channels" / "goodchan.md").write_text(
            "## When to invoke\nInvoke for questions.\n"
        )

        slack_client = MagicMock()
        slack_client.chat_postMessage.return_value = {"ok": True, "ts": "progress.1"}
        slack_client.conversations_info.return_value = {
            "ok": True, "channel": {"name": "goodchan"},
        }
        slack_client.conversations_join.return_value = {"ok": True}

        lock = MagicMock()
        lock.acquire.return_value = True

        _process_message(
            user_id="U123",
            channel="CSOURCE1",
            thread_ts="1700.000",
            message_ts="1700.100",
            text="<@UBOT> monitor <#CGOOD123|goodchan>",
            monitored=False,
            config=cfg_with_rules,
            slack_client=slack_client,
            bot_user_id="UBOT",
            get_thread_lock_fn=lambda _ts: lock,
            create_options_fn=MagicMock(),
        )

        from shadow_ai.db import db_is_monitored_channel
        assert db_is_monitored_channel(cfg_with_rules.db_path, "CGOOD123") is True
