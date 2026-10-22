"""Tests for TapDash's patches to the vendored Langfuse plugin (see PATCHES.md).

Upstream behaviour is covered by test_langfuse_plugin.py; this file covers only what we changed.
"""

from __future__ import annotations

import contextlib
import importlib
import sys

import pytest


@pytest.fixture
def plugin(monkeypatch):
    """Fresh plugin module wired to a fake Langfuse client; records propagate + root kwargs."""
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    mod.propagate_calls = []  # attribute on the module object, read by tests
    mod.root_calls = []

    @contextlib.contextmanager
    def _propagate(**kw):
        mod.propagate_calls.append(dict(kw))
        yield

    class _Span:
        def update(self, **kw): pass
        def update_trace(self, **kw): pass
        def set_trace_io(self, **kw): pass
        def end(self, **kw): pass
        def start_observation(self, **kw): return _Span()

    class _Client:
        def create_trace_id(self, seed=None): return f"trace::{seed}"
        def start_observation(self, **kw):
            mod.root_calls.append(dict(kw))
            return _Span()
        def flush(self): pass

    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client())
    return mod


def _open_turn(mod, *, sender=None, platform="telegram", turn_id="t1"):
    if sender is not None:
        mod.on_pre_llm_call(turn_id=turn_id, task_id="k", session_id="s", platform=platform, sender_id=sender)
    mod.on_pre_llm_request(turn_id=turn_id, task_id="k", session_id="s", platform=platform, api_call_count=1,
                           request_messages=[{"role": "user", "content": "hi"}])


def test_trace_name_per_platform(plugin):
    assert plugin._trace_name("telegram") == "telegram-turn"
    assert plugin._trace_name("Teams") == "teams-turn"
    assert plugin._trace_name("cron") == "cron-run"
    assert plugin._trace_name("") == "hermes-turn"
    assert plugin._trace_name("wei!rd plat") == "weirdplat-turn"
    assert plugin._trace_name("!!!") == "hermes-turn"


def test_root_gets_sender_as_user_id_and_name(plugin, monkeypatch):
    seen = []

    def fake_env(name, default=""):
        seen.append(name)
        return "Yaakov" if name == "HERMES_SESSION_USER_NAME" else default

    monkeypatch.setattr("gateway.session_context.get_session_env", fake_env)
    _open_turn(plugin, sender="123456789")
    assert "HERMES_SESSION_USER_NAME" in seen
    assert plugin.propagate_calls[0]["user_id"] == "123456789"
    assert plugin.propagate_calls[0]["trace_name"] == "telegram-turn"
    root = plugin.root_calls[0]
    assert root["name"] == "telegram-turn"
    assert root["metadata"]["user_name"] == "Yaakov"
    assert "t1" not in plugin._TURN_SENDERS


def test_no_sender_no_user_id(plugin):
    _open_turn(plugin, sender=None, platform="cron")
    assert "user_id" not in plugin.propagate_calls[0]
    assert plugin.propagate_calls[0]["trace_name"] == "cron-run"
    assert plugin.root_calls[0]["name"] == "cron-run"
    assert "user_name" not in plugin.root_calls[0]["metadata"]


@pytest.mark.parametrize("sender", ["Ünïcode", "x" * 201])
def test_bad_sender_left_out(plugin, sender):
    _open_turn(plugin, sender=sender)
    assert "user_id" not in plugin.propagate_calls[0]
    assert plugin.propagate_calls[0]["trace_name"] == "telegram-turn"


def test_sender_map_is_bounded(plugin):
    for i in range(300):
        plugin._remember_sender(f"k{i}", f"s{i}", "")
    assert len(plugin._TURN_SENDERS) == 256
    assert all(f"k{i}" not in plugin._TURN_SENDERS for i in range(44))
    assert "k44" in plugin._TURN_SENDERS and "k299" in plugin._TURN_SENDERS


def test_pop_sender_unknown_and_known(plugin):
    assert plugin._pop_sender("nope") == ("", "")
    plugin._remember_sender("a", "42", "Bo")
    assert plugin._pop_sender("a") == ("42", "Bo")
    assert plugin._pop_sender("a") == ("", "")


def test_ascii_attr(plugin):
    assert plugin._ascii_attr("  abc  ") == "abc"
    assert plugin._ascii_attr("") is None
    assert plugin._ascii_attr("   ") is None
    assert plugin._ascii_attr(None) is None
    assert plugin._ascii_attr(123) is None
    assert plugin._ascii_attr("é") is None
    assert plugin._ascii_attr("a" * 200) == "a" * 200
    assert plugin._ascii_attr("a" * 201) is None
