"""Tests for TapDash's patches to the vendored Langfuse plugin (see PATCHES.md).

Upstream behaviour is covered by test_langfuse_plugin.py; this file covers only what we changed.
"""

from __future__ import annotations

import contextlib
import importlib
import os
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

    mod.otel_calls = []  # (root span, attribute name, value) for every root OTel set_attribute

    class _Otel:
        def __init__(self, root): self.root = root
        def set_attribute(self, key, value): mod.otel_calls.append((self.root, key, value))

    class _Root(_Span):
        def __init__(self): self._otel_span = _Otel(self)

    mod.root_spans = []

    class _Client:
        def create_trace_id(self, seed=None): return f"trace::{seed}"
        def start_observation(self, **kw):
            mod.root_calls.append(dict(kw))
            root = _Root()
            mod.root_spans.append(root)
            return root
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
    assert plugin._ascii_attr("123") == "123"
    assert plugin._ascii_attr(" 123 ") is None  # never altered: a value with outer whitespace is left out
    assert plugin._ascii_attr("123\n") is None
    assert plugin._ascii_attr("") is None
    assert plugin._ascii_attr("   ") is None
    assert plugin._ascii_attr(None) is None
    assert plugin._ascii_attr(123) is None
    assert plugin._ascii_attr("é") is None
    assert plugin._ascii_attr("a" * 200) == "a" * 200
    assert plugin._ascii_attr("a" * 201) is None


def test_remember_empty_sender_drops_stale_entry(plugin):
    plugin._remember_sender("k", "1", "A")
    plugin._remember_sender("k", "", "")
    assert plugin._pop_sender("k") == ("", "")


# ---- HERMES_LANGFUSE_TAG_PATTERNS (spec D4-D6) ----

PARK = r"park=\bP-[0-9A-F]{8}\b"


def _open(mod, text="hi", *, task="k", turn="t1", session="s", call=1):
    mod.on_pre_llm_request(turn_id=turn, task_id=task, session_id=session, platform="telegram", api_call_count=call,
                           request_messages=[{"role": "user", "content": text}])


def _tool(mod, args, result, *, task="k", turn="t1", session="s", name="parked_action_execute", call_id="c1"):
    kw = dict(tool_name=name, args=args, task_id=task, session_id=session, tool_call_id=call_id, turn_id=turn)
    mod.on_pre_tool_call(**kw)
    mod.on_post_tool_call(result=result, **kw)


def _tag_values(mod, prefix="park:"):
    return [t for t in mod.propagate_calls[0]["tags"] if t.startswith(prefix)]


def test_patterns_parse_and_skip_bad_entries(plugin, monkeypatch, caplog):
    raw = r"park=\bP-[0-9A-F]{8}\b;nolabel;Bad Label=x;broken=(;inv=invoice (\d+)"
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", raw)
    with caplog.at_level("WARNING", logger="langfuse_plugin"):
        parsed = plugin._tag_patterns()
        assert [label for label, _ in parsed] == ["park", "inv"]
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 3
        caplog.clear()
        assert [label for label, _ in plugin._tag_patterns()] == ["park", "inv"]
        assert not caplog.records


def test_patterns_unset_means_nothing(plugin, monkeypatch):
    monkeypatch.delenv("HERMES_LANGFUSE_TAG_PATTERNS", raising=False)
    assert plugin._tag_patterns() == []
    assert plugin._pattern_tags(["P-420E1D2C"]) == []


def test_root_tagged_from_user_message(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    _open(plugin, "[durable-park] P-420E1D2C was APPROVED in withHuman.")
    tags = plugin.propagate_calls[0]["tags"]
    assert "park:P-420E1D2C" in tags
    assert tags[:2] == ["hermes", "langfuse"] and "platform:telegram" in tags  # upstream tags first, unchanged


def test_late_tag_from_tool_result_reaches_root_and_later_children(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    _open(plugin, "pay the invoice")
    assert _tag_values(plugin) == []
    _tool(plugin, {"amount": 5}, 'PARKED P-0A9C15CE: "mcp__wh__quickbooks__create_payment" needs human approval in '
                                 "withHuman and has NOT run.", name="mcp__wh__quickbooks__create_payment")
    root = plugin.root_spans[0]
    assert plugin.otel_calls, "root OTel span never got the tag"
    owner, key, value = plugin.otel_calls[-1]
    assert owner is root and key == "langfuse.trace.tags"
    assert value[-1] == "park:P-0A9C15CE" and value[:2] == ["hermes", "langfuse"]
    n = len(plugin.propagate_calls)
    _open(plugin, "pay the invoice", call=2)  # next LLM call of the same turn: a child under the same root
    assert len(plugin.root_spans) == 1
    assert "park:P-0A9C15CE" in plugin.propagate_calls[n]["tags"]
    assert _tag_values(plugin) == []  # the call recorded at root start was not rewritten after the fact


def test_tool_args_are_scanned(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    _open(plugin)
    _tool(plugin, {"park_id": "P-420E1D2C"}, {"status": "executed", "park_id": "P-420E1D2C"})
    values = [v for _, _, v in plugin.otel_calls]
    assert values and all(v.count("park:P-420E1D2C") == 1 for v in values)
    assert values[-1].count("park:P-420E1D2C") == 1
    assert len(plugin.otel_calls) == 1  # the second sighting changes nothing, so no second write


def test_group_capture(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", r"inv=invoice (\d+)")
    _open(plugin, "pay invoice 1016")
    assert plugin.propagate_calls[0]["tags"][-1] == "inv:1016"


def test_tag_limits(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", r"n=\bn\d+\b;long=L\w+;u=u\S+")
    _open(plugin, " ".join(f"n{i}" for i in range(15)))
    assert len(_tag_values(plugin, "n:")) == 10
    assert plugin._pattern_tags(["L" + "x" * 250]) == []  # tag would be > 200 chars
    assert plugin._pattern_tags(["L" + "x" * 193]) == ["long:L" + "x" * 193]  # exactly 200 chars: kept
    assert plugin._pattern_tags(["u\u00e9"]) == []  # non-ASCII value skipped


def test_ten_tag_cap_holds_across_later_matches(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", r"n=\bn\d+\b")
    _open(plugin, " ".join(f"n{i}" for i in range(8)))
    _tool(plugin, {}, " ".join(f"n{i}" for i in range(8, 20)))
    final = plugin.otel_calls[-1][2]
    assert len([t for t in final if t.startswith("n:")]) == 10


def test_scan_limit(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    far = "x" * 250_000 + " P-0A9C15CE " + "x" * 50_000
    near = "x" * 1_000 + " P-0A9C15CE " + "x" * 290_000
    assert len(far) > 300_000
    assert plugin._pattern_tags([far]) == []
    assert plugin._pattern_tags([near]) == ["park:P-0A9C15CE"]
    assert plugin._pattern_tags([{"k": far}]) == []  # structured values are cut the same way
    assert plugin._pattern_tags([{"k": near}]) == ["park:P-0A9C15CE"]


def test_metadata_capture_adds_no_tags(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "metadata")
    _open(plugin, "P-420E1D2C")
    assert _tag_values(plugin) == []
    _tool(plugin, {"park_id": "P-0A9C15CE"}, "P-0A9C15CE")
    assert plugin.otel_calls == []


def test_two_chats_do_not_share_tags(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    _open(plugin, "chat A", task="kA", turn="tA", session="sA")
    _open(plugin, "chat B", task="kB", turn="tB", session="sB")
    root_a, root_b = plugin.root_spans
    before_b = list(plugin._TRACE_STATE[plugin._trace_key("kB", "sB", turn_id="tB")].propagated["tags"])
    _tool(plugin, {}, "PARKED P-0A9C15CE: x", task="kA", turn="tA", session="sA")
    assert [o for o, _, _ in plugin.otel_calls] == [root_a]
    state_b = plugin._TRACE_STATE[plugin._trace_key("kB", "sB", turn_id="tB")]
    assert state_b.propagated["tags"] == before_b
    _open(plugin, "chat B", task="kB", turn="tB", session="sB", call=2)
    assert all("park:P-0A9C15CE" not in c["tags"] for c in plugin.propagate_calls[1:] if c["session_id"] == "sB")


def test_root_without_otel_span_is_fine(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    _open(plugin)
    del plugin.root_spans[0]._otel_span
    _tool(plugin, {}, "PARKED P-0A9C15CE: x")
    state = plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")]
    assert "park:P-0A9C15CE" in state.propagated["tags"]


def test_no_patterns_changes_nothing(plugin, monkeypatch):
    monkeypatch.delenv("HERMES_LANGFUSE_TAG_PATTERNS", raising=False)
    _open(plugin, "P-420E1D2C")
    _tool(plugin, {}, "P-0A9C15CE")
    assert plugin.propagate_calls[0]["tags"] == plugin._trace_tags("telegram")
    assert plugin.otel_calls == []


# ---- the root scan reads the turn's ORIGINAL user message (strong review m3) ----

TAG_ENV = "HERMES_LANGFUSE_TAG_PATTERNS"


def _open_with_original(mod, *, original, request_text, call=1):
    """Like Hermes's pre_api_request: ``user_message`` is the original, ``request_messages`` the API copy that
    carries injected context (memory recall, plugin context, gateway notes)."""
    mod.on_pre_llm_request(turn_id="t1", task_id="k", session_id="s", platform="telegram", api_call_count=call,
                           user_message=original,
                           request_messages=[{"role": "user", "content": request_text}])


def test_injected_park_id_in_request_copy_does_not_tag(plugin, monkeypatch):
    monkeypatch.setenv(TAG_ENV, PARK)
    _open_with_original(plugin, original="pay the invoice",
                        request_text="pay the invoice\n\n[memory] earlier park P-0A9C15CE was approved")
    assert _tag_values(plugin) == []
    assert plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")].pattern_tags == []


def test_park_id_in_original_user_message_tags(plugin, monkeypatch):
    monkeypatch.setenv(TAG_ENV, PARK)
    _open_with_original(plugin, original="[durable-park] P-420E1D2C was APPROVED in withHuman.",
                        request_text="[durable-park] P-420E1D2C was APPROVED in withHuman.\n\n[memory] P-0A9C15CE")
    assert _tag_values(plugin) == ["park:P-420E1D2C"]  # the injected id is not tagged


def test_request_copy_is_scanned_only_when_original_is_absent(plugin, monkeypatch):
    monkeypatch.setenv(TAG_ENV, PARK)
    _open_with_original(plugin, original=None, request_text="[durable-park] P-420E1D2C was APPROVED")
    assert _tag_values(plugin) == ["park:P-420E1D2C"]


def test_legacy_pre_llm_call_scans_original_too(plugin, monkeypatch):
    monkeypatch.setenv(TAG_ENV, PARK)
    plugin.on_pre_llm_call(turn_id="t1", task_id="k", session_id="s", platform="telegram", user_message="hello",
                           messages=[{"role": "user", "content": "hello\n\n[memory] P-0A9C15CE"}])
    assert _tag_values(plugin) == []


# ---- structured values are scanned as JSON: a JSON escape glued to an id hides it (strong review m4) ----

def test_json_escape_before_an_id_hides_it_documented_limit(plugin, monkeypatch):
    monkeypatch.setenv(TAG_ENV, PARK)
    # "\n" is written as backslash + n in the JSON text, so the \b in the pattern sees "nP-..." and finds no boundary.
    assert plugin._pattern_tags([{"a": "x\nP-0A9C15CE"}]) == []
    # An id after a space, a quote or other punctuation is found. Live ids sit in text like this.
    assert plugin._pattern_tags([{"a": "x P-0A9C15CE"}]) == ["park:P-0A9C15CE"]
    assert plugin._pattern_tags([{"park_id": "P-0A9C15CE"}]) == ["park:P-0A9C15CE"]
    assert plugin._pattern_tags(["x\nP-0A9C15CE"]) == ["park:P-0A9C15CE"]  # a plain string has a real newline


# ---- the real Langfuse SDK, in memory (strong review m7) ----
#
# Every test above replaces ``propagate_attributes`` and the client with fakes. This one runs the real SDK
# (pinned in plugin.yaml) with an in-memory span exporter and no network, so an SDK upgrade that changes how
# ``user.id`` or trace tags reach the exported spans, or that renames the private ``_otel_span`` the late-tag
# patch writes through (spec D5), fails here and not silently in production.

def _real_sdk_turn(monkeypatch, *, user_name="Yaäkov ✡"):
    import threading

    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    for name in list(os.environ):
        if name.startswith(("LANGFUSE_", "HERMES_LANGFUSE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_LANGFUSE_TAG_PATTERNS", PARK)
    monkeypatch.setattr("gateway.session_context.get_session_env",
                        lambda name, default="": user_name if name == "HERMES_SESSION_USER_NAME" else default)
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    exporter = InMemorySpanExporter()
    # Obvious fake keys and a base URL nothing listens on; ``span_exporter`` replaces the OTLP exporter, so
    # nothing here can leave the machine.
    client = Langfuse(public_key="pk-lf-fake-in-memory-test", secret_key="sk-lf-fake-in-memory-test",
                      base_url="http://127.0.0.1:9", span_exporter=exporter, tracing_enabled=True)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)

    turn = dict(turn_id="t-real", task_id="k", session_id="s-real", platform="telegram")
    mod.on_pre_llm_call(sender_id="8675309", **turn)
    mod.on_pre_llm_request(api_call_count=1, user_message="pay the invoice", request_messages=[
        {"role": "user", "content": "pay the invoice"}], **turn)
    mod.on_post_llm_call(api_call_count=1, assistant_message=None, assistant_tool_call_count=1,
                         assistant_content_chars=0, **turn)
    tool = dict(tool_name="mcp__wh__quickbooks__create_payment", args={"amount": 5}, tool_call_id="c1", **turn)

    def run_tool():  # Hermes runs tools on worker threads
        mod.on_pre_tool_call(**tool)
        mod.on_post_tool_call(result='PARKED P-0A9C15CE: "create_payment" needs human approval and has NOT run.',
                              **tool)

    worker = threading.Thread(target=run_tool)
    worker.start()
    worker.join()
    mod.on_pre_llm_request(api_call_count=2, user_message="pay the invoice", request_messages=[
        {"role": "user", "content": "pay the invoice"}], **turn)
    mod.on_post_llm_call(api_call_count=2, assistant_response="It needs approval.", assistant_tool_call_count=0,
                         assistant_content_chars=18, **turn)
    client.flush()
    return mod, exporter, client


def test_real_sdk_user_id_name_and_late_tag_reach_exported_spans(monkeypatch):
    mod, exporter, client = _real_sdk_turn(monkeypatch)
    try:
        # Safety: the only span processor exports to the in-memory exporter (no network exporter in this test).
        # The SDK wraps a given exporter in its own transforming exporter; unwrap it before comparing.
        processors = client._resources.tracer_provider._active_span_processor._span_processors
        wrapped = [getattr(p, "span_exporter", None) for p in processors]
        assert wrapped and all(getattr(e, "_exporter", e) is exporter for e in wrapped)

        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert set(spans) == {"telegram-turn", "LLM call 1", "LLM call 2", "Tool: mcp__wh__quickbooks__create_payment"}
        for span in spans.values():
            attrs = dict(span.attributes)
            assert attrs["user.id"] == "8675309", span.name
            assert attrs["langfuse.trace.name"] == "telegram-turn", span.name
        root = dict(spans["telegram-turn"].attributes)
        late = "park:P-0A9C15CE"
        # The tag was found in a tool result after the root opened: only the root and later children carry it.
        assert late in root["langfuse.trace.tags"]
        assert list(root["langfuse.trace.tags"])[:2] == ["hermes", "langfuse"]
        assert late in spans["LLM call 2"].attributes["langfuse.trace.tags"]
        for name in ("LLM call 1", "Tool: mcp__wh__quickbooks__create_payment"):
            assert late not in spans[name].attributes["langfuse.trace.tags"], name
        # user_name is ROOT OBSERVATION metadata (not trace metadata), non-ASCII intact.
        assert root["langfuse.observation.metadata.user_name"] == "Yaäkov ✡"
        assert not any(k.startswith("langfuse.trace.metadata") and k.endswith("user_name") for k in root)
        assert mod._TURN_SENDERS == {}
        assert mod._TRACE_STATE == {}
    finally:
        client.shutdown()
