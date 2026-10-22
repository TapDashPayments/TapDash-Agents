"""Tests for the tools-offered metadata on each "LLM call N" generation (TapDash patch, see PATCHES.md item 7).

The provider body's ``tools`` list is reduced to a sorted list of tool NAMES (never descriptions or schemas).
"""

from __future__ import annotations

import contextlib
import importlib
import sys

import pytest


@pytest.fixture
def plugin(monkeypatch):
    """Fresh plugin module wired to a fake Langfuse client; records every child observation's kwargs."""
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    mod.children = []  # kwargs of every child observation (generations, tools) started under the root

    @contextlib.contextmanager
    def _propagate(**kw):
        yield

    class _Span:
        def update(self, **kw): pass
        def update_trace(self, **kw): pass
        def set_trace_io(self, **kw): pass
        def end(self, **kw): pass

        def start_observation(self, **kw):
            mod.children.append(dict(kw))
            return _Span()

    class _Client:
        def create_trace_id(self, seed=None): return f"trace::{seed}"
        def start_observation(self, **kw): return _Span()
        def flush(self): pass

    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client())
    return mod


def _call(mod, tools=..., *, call=1, body_extra=None, request=...):
    """Start one LLM call. ``tools`` is the provider body's tools value (``...`` = leave the key out)."""
    if request is ...:
        body = {"model": "m", **(body_extra or {})}
        if tools is not ...:
            body["tools"] = tools
        request = {"body": body}
    mod.on_pre_llm_request(turn_id="t1", task_id="k", session_id="s", platform="telegram", api_call_count=call,
                           request_messages=[{"role": "user", "content": "hi"}], request=request)
    return mod.children[-1]


def _generation(mod):
    gens = [c for c in mod.children if c["name"].startswith("LLM call")]
    assert gens, "no LLM call generation was started"
    return gens[-1]


def _fn(name):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object"}}}


def _flat(name):
    return {"name": name, "description": "d", "input_schema": {"type": "object"}}


def test_openai_style(plugin):
    meta = _call(plugin, [_fn("terminal"), _fn("read_file")])["metadata"]
    assert meta["tools_offered"] == ["read_file", "terminal"]  # sorted
    assert meta["tools_offered_count"] == 2


def test_anthropic_style(plugin):
    meta = _call(plugin, [_flat("terminal"), _flat("read_file")])["metadata"]
    assert meta["tools_offered"] == ["read_file", "terminal"]
    assert meta["tools_offered_count"] == 2


def test_mixed_styles(plugin):
    meta = _call(plugin, [_fn("b_tool"), _flat("a_tool"), _fn("c_tool")])["metadata"]
    assert meta["tools_offered"] == ["a_tool", "b_tool", "c_tool"]
    assert meta["tools_offered_count"] == 3


def test_names_only_never_descriptions_or_schemas(plugin):
    meta = _call(plugin, [_fn("terminal"), _flat("read_file")])["metadata"]
    assert all(isinstance(n, str) for n in meta["tools_offered"])
    blob = repr(meta)
    assert "parameters" not in blob and "input_schema" not in blob and "'d'" not in blob


def test_names_also_recorded_in_metadata_capture_mode(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "metadata")
    meta = _call(plugin, [_fn("terminal")])["metadata"]
    assert meta["tools_offered"] == ["terminal"] and meta["tools_offered_count"] == 1


def test_missing_tools_leaves_both_keys_out(plugin):
    meta = _call(plugin)["metadata"]
    assert "tools_offered" not in meta and "tools_offered_count" not in meta
    assert meta["provider"] == "" and meta["message_count"] == 0  # the rest of the metadata is untouched


@pytest.mark.parametrize("tools", [None, "terminal", {"name": "terminal"}, 7, ("a", "b")])
def test_tools_not_a_list_leaves_both_keys_out(plugin, tools):
    meta = _call(plugin, tools)["metadata"]
    assert "tools_offered" not in meta and "tools_offered_count" not in meta


def test_empty_list_leaves_both_keys_out(plugin):
    meta = _call(plugin, [])["metadata"]
    assert "tools_offered" not in meta and "tools_offered_count" not in meta


@pytest.mark.parametrize("request_value", [None, "body", [], {}, {"body": None}, {"body": "x"}, {"body": ["tools"]}])
def test_odd_request_shapes_leave_both_keys_out(plugin, request_value):
    meta = _call(plugin, request=request_value)["metadata"]
    assert "tools_offered" not in meta and "tools_offered_count" not in meta


def test_garbage_entries_are_skipped(plugin):
    garbage = [
        None, 7, "terminal", ["terminal"],                      # not dicts
        {"name": 5}, {"name": None}, {"name": ["x"]},            # non-str flat names
        {"name": ""},                                            # empty flat name
        {"type": "function", "function": {"name": 5}},           # non-str OpenAI name
        {"type": "function", "function": {"name": ""}},          # empty OpenAI name
        {"type": "function", "function": "terminal"},            # function is not a dict
        {"type": "function", "function": {}},                    # function has no name
        {"description": "no name at all"},                       # neither shape
        {},
    ]
    meta = _call(plugin, garbage + [_fn("good_one"), _flat("good_two")])["metadata"]
    assert meta["tools_offered"] == ["good_one", "good_two"]
    assert meta["tools_offered_count"] == 2


def test_only_garbage_leaves_both_keys_out(plugin):
    meta = _call(plugin, [None, 7, {"name": ""}, {"description": "x"}])["metadata"]
    assert "tools_offered" not in meta and "tools_offered_count" not in meta


def test_cap_200_names_count_250(plugin):
    tools = [_fn(f"tool_{i:03d}") for i in range(250)]
    meta = _call(plugin, tools)["metadata"]
    assert len(meta["tools_offered"]) == 200
    assert meta["tools_offered_count"] == 250
    assert meta["tools_offered"] == sorted(meta["tools_offered"]) == [f"tool_{i:03d}" for i in range(200)]


def test_exactly_200_is_not_cut(plugin):
    meta = _call(plugin, [_flat(f"t{i:03d}") for i in range(200)])["metadata"]
    assert len(meta["tools_offered"]) == 200 and meta["tools_offered_count"] == 200


def test_long_names_are_cut_to_64_chars(plugin):
    long_name = "mcp__" + "x" * 100
    meta = _call(plugin, [_fn(long_name), _flat("a" * 64), _flat("b" * 65)])["metadata"]
    assert meta["tools_offered"] == ["a" * 64, "b" * 64, long_name[:64]]
    assert all(len(n) <= 64 for n in meta["tools_offered"])


def test_duplicates_are_removed(plugin):
    meta = _call(plugin, [_fn("terminal"), _flat("terminal"), _fn("terminal"), _flat("read_file")])["metadata"]
    assert meta["tools_offered"] == ["read_file", "terminal"]
    assert meta["tools_offered_count"] == 2


def test_dedupe_on_full_name_then_again_after_cut(plugin):
    """Dedupe happens on the full name (count), then names are cut to 64 chars and deduped again, so the list
    never holds a duplicate, even when two long names share their first 64 characters."""
    prefix = "p" * 64
    meta = _call(plugin, [_fn(prefix + "_one"), _fn(prefix + "_two"), _fn(prefix + "_one"), _fn("short")])["metadata"]
    assert meta["tools_offered"] == [prefix, "short"]
    assert meta["tools_offered_count"] == 3  # three distinct full names, before the cut


def test_names_are_not_altered(plugin):
    meta = _call(plugin, [_fn(" spaced "), _fn("Upper_Case"), _fn("mcp__qb__create_bill")])["metadata"]
    assert meta["tools_offered"] == [" spaced ", "Upper_Case", "mcp__qb__create_bill"]


def test_metadata_reaches_the_llm_call_generation(plugin):
    _call(plugin, [_fn("terminal")], call=1)
    gen = _generation(plugin)
    assert gen["name"] == "LLM call 1" and gen["as_type"] == "generation"
    assert gen["metadata"]["tools_offered"] == ["terminal"]
    assert gen["metadata"]["tools_offered_count"] == 1
    assert gen["metadata"]["message_count"] == 0  # existing keys are still there


def test_each_llm_call_records_its_own_offered_set(plugin):
    _call(plugin, [_fn("a_tool"), _fn("b_tool")], call=1)
    _call(plugin, [_fn("b_tool")], call=2)
    _call(plugin, call=3)  # no tools this time
    gens = [c for c in plugin.children if c["name"].startswith("LLM call")]
    assert [g["name"] for g in gens] == ["LLM call 1", "LLM call 2", "LLM call 3"]
    assert gens[0]["metadata"]["tools_offered"] == ["a_tool", "b_tool"]
    assert gens[1]["metadata"]["tools_offered"] == ["b_tool"]
    assert "tools_offered" not in gens[2]["metadata"]


def test_helper_exception_is_swallowed_and_generation_still_starts(plugin, monkeypatch):
    def boom(body):
        raise RuntimeError("odd shape")

    monkeypatch.setattr(plugin, "_tools_offered", boom)
    gen = _call(plugin, [_fn("terminal")])
    assert gen["name"] == "LLM call 1"
    assert "tools_offered" not in gen["metadata"] and "tools_offered_count" not in gen["metadata"]
    assert gen["metadata"]["message_count"] == 0


def test_a_tool_list_that_cannot_be_iterated_safely_never_breaks_the_call(plugin):
    class Hostile(list):
        def __iter__(self):
            raise RuntimeError("no iterating")

    gen = _call(plugin, Hostile([_fn("terminal")]))
    assert gen["name"] == "LLM call 1"
    assert "tools_offered" not in gen["metadata"]
