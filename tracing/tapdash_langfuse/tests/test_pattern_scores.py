"""Tests for HERMES_LANGFUSE_SCORE_PATTERNS, the pattern-driven trace score (TapDash patch, see PATCHES.md).

The plugin itself knows nothing about durable-park; these tests use the exact proto-agents setting (SCORE_PATTERNS
below) against the texts durable-park's wake really sends, so a change on either side fails here.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import logging
import sys
from pathlib import Path

import pytest

SCORE_ENV = "HERMES_LANGFUSE_SCORE_PATTERNS"

# The exact proto-agents value (plan amendment A4; also in README.md). Raw string: backslashes and quotes are literal.
SCORE_PATTERNS = r'''approval_decision=(?m)^(?:\[[^\]\n]*\] )?\s*\[durable-park\] (?P<comment>P-[0-9A-F]{8}) (?:\("[^"]*"\) )?was (?P<value>APPROVED|DENIED|CANCELLED) in withHuman(?: by (?P<by>[^:]+?))?(?:: (?P<note>.+?))?\. (?:It did not run\.|Call parked_action_execute );approval_decision=(?m)^(?:\[[^\]\n]*\] )?\s*\[durable-park\] (?P<comment>P-[0-9A-F]{8}) \("[^"]*"\) got no decision before its deadline\.=>expired'''

# The three format strings below are copied from durable-park/plugin/durable_park/wake.py (APPROVED_TEXT, DENIED_TEXT,
# EXPIRED_TEXT). test_wake_texts_have_not_drifted fails when wake.py changes them.
APPROVED_TEXT = (
    '[durable-park] {park_id} was APPROVED in withHuman{by}. Call parked_action_execute with park_id "{park_id}" '
    'now to run it exactly as saved. If parked_action_execute is not in your tool list, call it through tool_call. '
    'Do not call "{tool}" directly.')
DENIED_TEXT = (
    '[durable-park] {park_id} ("{tool}") was {status} in withHuman{by}{note}. It did not run. '
    "Tell the user and continue without it.")
EXPIRED_TEXT = (
    '[durable-park] {park_id} ("{tool}") got no decision before its deadline. It did not run. '
    "Ask the user if they want to request approval again.")

PARK_ID = "P-1A2B3C4D"
TOOL = "mcp__wh__quickbooks__create_bill"
DECIDER = "Yaakov Yehuda (web)"


def approved(decider=None):
    return APPROVED_TEXT.format(park_id=PARK_ID, tool=TOOL, by=f" by {decider}" if decider else "")


def denied(status="DENIED", decider=None, note=None):
    return DENIED_TEXT.format(park_id=PARK_ID, tool=TOOL, status=status, by=f" by {decider}" if decider else "",
                              note=f": {note}" if note else "")


def expired():
    return EXPIRED_TEXT.format(park_id=PARK_ID, tool=TOOL)


@pytest.fixture
def plugin(monkeypatch):
    """Fresh plugin module wired to a fake Langfuse client that records every ``create_score`` call."""
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    mod.score_calls = []  # kwargs of every create_score call, read by tests
    mod.root_calls = []
    mod.score_error = None  # set to an exception to make create_score raise

    @contextlib.contextmanager
    def _propagate(**kw):
        yield

    class _Span:
        def update(self, **kw): pass
        def update_trace(self, **kw): pass
        def set_trace_io(self, **kw): pass
        def end(self, **kw): pass
        def start_observation(self, **kw): return _Span()

    class _Otel:
        def set_attribute(self, key, value): pass

    class _Root(_Span):
        def __init__(self): self._otel_span = _Otel()

    class _Client:
        def create_trace_id(self, seed=None): return f"trace::{seed}"

        def start_observation(self, **kw):
            mod.root_calls.append(dict(kw))
            return _Root()

        def create_score(self, **kw):
            if mod.score_error is not None:
                raise mod.score_error
            mod.score_calls.append(dict(kw))

        def flush(self): pass

    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client())
    return mod


def _open(mod, original, *, request_text=None, call=1, turn="t1"):
    """Like Hermes's pre_api_request: ``user_message`` is the original, ``request_messages`` the API copy."""
    mod.on_pre_llm_request(turn_id=turn, task_id="k", session_id="s", platform="telegram", api_call_count=call,
                           user_message=original,
                           request_messages=[{"role": "user", "content": request_text if request_text is not None
                                              else original}])


def _trace_id(mod, turn="t1"):
    return f"trace::s::{turn}"


# ---- the wake texts are copied from durable-park: fail when they drift ----

def _find_wake_py() -> Path:
    """Walk up from this test file to the first directory that has durable-park/plugin/durable_park/wake.py.
    run_tests.sh runs the tests in place (tracing/tapdash_langfuse/tests inside the checkout), so the repo root is
    an ancestor. Not found is a failure, never a skip: a silent skip would turn the drift guard off."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "durable-park" / "plugin" / "durable_park" / "wake.py"
        if candidate.is_file():
            return candidate
    pytest.fail("durable-park/plugin/durable_park/wake.py not found above " + str(Path(__file__).resolve()))


def test_wake_texts_have_not_drifted():
    tree = ast.parse(_find_wake_py().read_text(encoding="utf-8"))  # parsed, not imported: no durable_park deps
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants[node.targets[0].id] = node.value
    for name, fixture in (("APPROVED_TEXT", APPROVED_TEXT), ("DENIED_TEXT", DENIED_TEXT),
                          ("EXPIRED_TEXT", EXPIRED_TEXT)):
        assert name in constants, f"{name} is gone from wake.py"
        assert ast.literal_eval(constants[name]) == fixture, f"{name} in wake.py no longer matches this test"


def test_inject_text_glue_has_not_drifted():
    """The by/note glue (`` by {decider}``, ``: {note}``) lives in wake.inject_text, not in the three constants, and
    the setting's regex depends on it. Run the real inject_text (functions and constants lifted by ast, not
    imported: no durable_park deps) on stub rows and compare with what this file's helpers build."""
    from types import SimpleNamespace

    wanted_consts = {"MAX_NOTE_CHARS", "MAX_DECIDER_CHARS", "MAX_CHANNEL_CHARS", "MAX_DECIDED_BY_CHARS",
                     "_DECIDER_EXTRA_CHARS", "APPROVED_TEXT", "DENIED_TEXT", "EXPIRED_TEXT"}
    wanted_funcs = {"one_line", "clean_decider", "inject_text"}
    tree = ast.parse(_find_wake_py().read_text(encoding="utf-8"))
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in wanted_consts:
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            body.append(node)
    assert {n.name for n in body if isinstance(n, ast.FunctionDef)} == wanted_funcs, "wake.py helpers moved or renamed"
    namespace: dict = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "wake.py", "exec"), namespace)

    def row(status, decided_by=None, note=None):
        return SimpleNamespace(park_id=PARK_ID, tool=TOOL, status=status, decided_by=decided_by, note=note)

    inject_text = namespace["inject_text"]
    assert inject_text(row("approved")) == approved()
    assert inject_text(row("approved", DECIDER)) == approved(DECIDER)
    assert inject_text(row("denied")) == denied()
    assert inject_text(row("denied", DECIDER, "wrong amount")) == denied(decider=DECIDER, note="wrong amount")
    assert inject_text(row("cancelled", DECIDER)) == denied("CANCELLED", decider=DECIDER)
    assert inject_text(row("cancelled", note="duplicate")) == denied("CANCELLED", note="duplicate")
    assert inject_text(row("expired")) == expired()


# ---- parsing ----

def test_parse_splits_name_on_first_equals_and_literal_on_last_arrow(plugin):
    parsed = plugin._parse_score_patterns("a=x=y=>b=>c;plain=foo")
    assert [(n, p.pattern, lit) for n, p, lit in parsed] == [("a", "x=y=>b", "c"), ("plain", "foo", None)]


def test_parse_bad_entries_warn_once_and_are_skipped(plugin, monkeypatch, caplog):
    raw = r"ok=\bfoo\b=>one;nolabel;Bad Name=x;broken=(;empty=;ok2=bar"
    monkeypatch.setenv(SCORE_ENV, raw)
    with caplog.at_level("WARNING", logger="langfuse_plugin"):
        parsed = plugin._score_patterns()
        assert [name for name, _, _ in parsed] == ["ok", "ok2"]
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 4
        assert all("HERMES_LANGFUSE_SCORE_PATTERNS" in r.getMessage() for r in warnings)
        caplog.clear()
        assert [name for name, _, _ in plugin._score_patterns()] == ["ok", "ok2"]
        assert not caplog.records  # a distinct bad value warns once (cached)


def test_other_entries_still_score_when_one_is_bad(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"broken=(;kind=\bfoo\b=>seen")
    assert plugin._pattern_scores("a foo b") == [("kind", "seen", None)]


def test_bad_name_is_skipped(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"Bad Name=foo;" + "x" * 41 + r"=foo;good_name-1=foo")
    assert [name for name, _, _ in plugin._score_patterns()] == ["good_name-1"]


def test_unset_means_nothing(plugin, monkeypatch):
    monkeypatch.delenv(SCORE_ENV, raising=False)
    assert plugin._score_patterns() == []
    assert plugin._pattern_scores(approved()) == []


# ---- the proto-agents setting against the real wake texts (A4) ----

@pytest.mark.parametrize("text, value, comment", [
    pytest.param(approved(), "approved", PARK_ID, id="approved"),
    pytest.param(approved(DECIDER), "approved", f"{PARK_ID}; by: {DECIDER}", id="approved-by"),
    pytest.param(denied(), "denied", PARK_ID, id="denied"),
    pytest.param(denied(decider=DECIDER, note="wrong amount: see bill 12. Ask again later"), "denied",
                 f"{PARK_ID}; by: {DECIDER}; note: wrong amount: see bill 12. Ask again later", id="denied-by-note"),
    pytest.param(denied(decider=DECIDER, note="wrong amount"), "denied",
                 f"{PARK_ID}; by: {DECIDER}; note: wrong amount", id="denied-by-note-simple"),
    pytest.param(denied("CANCELLED", decider=DECIDER), "cancelled", f"{PARK_ID}; by: {DECIDER}", id="cancelled-by"),
    pytest.param(denied("CANCELLED", note="duplicate"), "cancelled", f"{PARK_ID}; note: duplicate",
                 id="cancelled-note"),
    pytest.param(expired(), "expired", PARK_ID, id="expired"),
])
def test_proto_agents_setting_scores_each_wake_text(plugin, monkeypatch, text, value, comment):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    assert plugin._pattern_scores(text) == [("approval_decision", value, comment)]


@pytest.mark.parametrize("text", [
    "hello",
    "please pay the invoice",
    "[durable-park] P-1A2B3C4D was APPROVED in withHuman",  # no tail: not a wake text
    "I read that " + approved(),  # not at the start of the message
    "[Yaakov Yehuda] I read that " + approved(),  # a sender prefix does not open the door to mid-line text
    "Earlier in the chat: " + denied(),  # mid-line mention, not a line of its own
    "[durable-park] P-1A2B3C4D (\"x\") was PENDING in withHuman. It did not run. ",
    "",
])
def test_non_matching_message_gives_no_score(plugin, monkeypatch, text):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    assert plugin._pattern_scores(text) == []


@pytest.mark.parametrize("text, value, comment", [
    pytest.param(f"[Yaakov Yehuda] {approved()}", "approved", PARK_ID, id="sender-prefix-approved"),
    pytest.param(f"[Yaakov Yehuda] {denied(decider=DECIDER, note='wrong amount')}", "denied",
                 f"{PARK_ID}; by: {DECIDER}; note: wrong amount", id="sender-prefix-denied-by-note"),
    pytest.param(f"[Yaakov Yehuda] {expired()}", "expired", PARK_ID, id="sender-prefix-expired"),
    pytest.param(f"Alice: hi\nBob: ok\n\n[New message]\n{approved()}", "approved", PARK_ID,
                 id="observed-group-backfill"),
    pytest.param(f"Alice: hi\n\n[New message]\n[Yaakov Yehuda] {denied('CANCELLED')}", "cancelled", PARK_ID,
                 id="backfill-and-sender-prefix"),
])
def test_wake_text_still_scores_behind_the_sender_prefix_or_backfill(plugin, monkeypatch, text, value, comment):
    """Hermes rewrites a shared-session message to ``[<user name>] <text>`` and puts observed-group backfill before
    ``[New message]``; the hook's user_message is taken after that (final review of TAP2-125)."""
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    assert plugin._pattern_scores(text) == [("approval_decision", value, comment)]


# ---- value and comment rules (A1, A2) ----

def test_value_priority_named_then_literal_then_group_one_then_whole_match(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"named=(?P<value>Y\w+) (\w+)=>lit;lit=(\w+) (\w+)=>fixed;grp=(\w+) (\w+);whole=\w+ \w+")
    assert plugin._pattern_scores("Yes no") == [
        ("named", "yes", None), ("lit", "fixed", None), ("grp", "yes", None), ("whole", "yes no", None)]


def test_value_is_stripped_and_lower_cased(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"v=state: (?P<value>[A-Za-z ]+)$")
    assert plugin._pattern_scores("state:  Mixed Case  ") == [("v", "mixed case", None)]


def test_empty_or_non_participating_value_group_falls_through(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"v=a(?P<value> *)b=>lit;w=a(?P<value>x)? *b")
    assert plugin._pattern_scores("a  b") == [("v", "lit", None), ("w", "a  b", None)]


def test_value_length_limit_is_100_and_a_later_entry_can_still_win(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"v=(?P<value>x+);v=y=>short")
    assert plugin._pattern_scores("x" * 100 + " y") == [("v", "x" * 100, None)]
    assert plugin._pattern_scores("x" * 101 + " y") == [("v", "short", None)]
    monkeypatch.setenv(SCORE_ENV, r"v=(?P<value>x+)")
    assert plugin._pattern_scores("x" * 101) == []


def test_comment_is_built_from_named_groups_in_pattern_order(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<note>n\w*) (?P<value>v\w*) (?P<comment>c\w*) (?P<by>b\w*)")
    assert plugin._pattern_scores("nn vv cc bb") == [("d", "vv", "note: nn; cc; by: bb")]


def test_comment_skips_empty_groups_and_strips(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<value>v)(?P<comment>-*)(?P<by> *x *)?(?P<note>.*)")
    assert plugin._pattern_scores("v x end") == [("d", "v", "by: x; note: end")]
    assert plugin._pattern_scores("v") == [("d", "v", None)]


def test_no_named_groups_besides_value_means_no_comment(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<value>v\w*) (extra)")
    assert plugin._pattern_scores("vv extra") == [("d", "vv", None)]


def test_comment_is_cut_at_500_characters(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<value>v) (?P<comment>.+)")
    [(_, _, comment)] = plugin._pattern_scores("v " + "c" * 600)
    assert comment == "c" * 500


# ---- first matching entry per name wins; one result per name ----

def test_first_matching_entry_per_name_wins(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=nomatch=>never;d=foo=>first;d=foo=>second;e=foo=>other")
    assert plugin._pattern_scores("foo") == [("d", "first", None), ("e", "other", None)]


# ---- capture mode, size cap ----

def test_metadata_capture_mode_emits_no_score(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "metadata")
    assert plugin._pattern_scores(approved()) == []
    _open(plugin, approved())
    assert plugin.score_calls == []
    assert plugin.root_calls  # the trace itself still starts


@pytest.mark.parametrize("mode", ["sanitized", "full"])
def test_other_capture_modes_score(plugin, monkeypatch, mode):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", mode)
    assert plugin._pattern_scores(approved()) == [("approval_decision", "approved", PARK_ID)]


def test_scan_is_cut_at_the_scan_size_cap(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<value>needle)")
    near = "x" * (plugin._MAX_SCAN_CHARS - 6) + "needle"
    assert plugin._pattern_scores(near) == [("d", "needle", None)]
    assert plugin._pattern_scores("x" * plugin._MAX_SCAN_CHARS + "needle") == []


# ---- emission when the trace starts ----

def test_trace_start_emits_the_score_with_the_exact_arguments(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, denied(decider=DECIDER, note="wrong amount"))
    trace_id = _trace_id(plugin)
    assert plugin.score_calls == [{
        "name": "approval_decision", "value": "denied", "trace_id": trace_id, "data_type": "CATEGORICAL",
        "comment": f"{PARK_ID}; by: {DECIDER}; note: wrong amount", "score_id": f"{trace_id}-approval_decision"}]
    assert plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")].scored_names == {"approval_decision"}


def test_expired_score_through_the_hook(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, expired())
    [call] = plugin.score_calls
    assert (call["value"], call["comment"]) == ("expired", PARK_ID)


def test_non_matching_message_creates_no_score(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, "please pay the invoice")
    assert plugin.score_calls == []
    assert plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")].scored_names == set()


def test_no_setting_creates_no_score(plugin, monkeypatch):
    monkeypatch.delenv(SCORE_ENV, raising=False)
    _open(plugin, approved())
    assert plugin.score_calls == []


def test_a_name_is_scored_once_per_trace(plugin, monkeypatch):
    # two entries with the same name that both match: one score; a later hook call on the same trace: no second one
    monkeypatch.setenv(SCORE_ENV, r"d=(?P<value>foo)=>x;d=foo=>bar;other=foo=>seen")
    _open(plugin, "foo")
    assert [(c["name"], c["value"]) for c in plugin.score_calls] == [("d", "foo"), ("other", "seen")]
    _open(plugin, "foo", call=2)
    _open(plugin, "foo", call=3)
    assert len(plugin.score_calls) == 2
    assert plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")].scored_names == {"d", "other"}


def test_a_new_trace_scores_again(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, approved(), turn="t1")
    _open(plugin, approved(), turn="t2")
    assert [c["score_id"] for c in plugin.score_calls] == [
        "trace::s::t1-approval_decision", "trace::s::t2-approval_decision"]


def test_scores_read_the_original_user_message_not_the_api_copy(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, "pay the invoice", request_text=approved() + "\n\n[memory] earlier note")
    assert plugin.score_calls == []
    _open(plugin, approved(), request_text="[memory] recall\n\n" + approved(), turn="t2")
    assert [c["value"] for c in plugin.score_calls] == ["approved"]


def test_request_copy_is_the_fallback_when_there_is_no_original(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, None, request_text=expired())
    assert [c["value"] for c in plugin.score_calls] == ["expired"]


def test_tool_arguments_and_results_are_not_scanned_for_scores(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    _open(plugin, "pay the invoice")
    kw = dict(tool_name="parked_action_execute", args={"note": approved()}, task_id="k", session_id="s",
              tool_call_id="c1", turn_id="t1")
    plugin.on_pre_tool_call(**kw)
    plugin.on_post_tool_call(result=approved(), **kw)
    assert plugin.score_calls == []


def test_a_failing_create_score_never_breaks_the_trace(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    plugin.score_error = RuntimeError("langfuse is down")
    _open(plugin, approved())
    assert len(plugin.root_calls) == 1
    state = plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")]
    assert state.scored_names == set()  # nothing was recorded as sent
    _open(plugin, approved(), call=2)  # later calls on the trace still work
    assert len(plugin.root_calls) == 1


def test_one_failing_create_score_does_not_block_the_next_name(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, r"a=foo=>one;b=foo=>two")
    sent = []
    real = plugin._get_langfuse()

    class _Flaky:
        def __getattr__(self, name): return getattr(real, name)

        def create_score(self, **kw):
            if kw["name"] == "a":
                raise RuntimeError("boom")
            sent.append(kw["name"])

    monkeypatch.setattr(plugin, "_get_langfuse", lambda: _Flaky())
    _open(plugin, "foo")
    assert sent == ["b"]
    assert plugin._TRACE_STATE[plugin._trace_key("k", "s", turn_id="t1")].scored_names == {"b"}


def test_a_scan_that_raises_is_swallowed(plugin, monkeypatch):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)

    def boom(value):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(plugin, "_pattern_scores", boom)
    _open(plugin, approved())
    assert plugin.score_calls == [] and len(plugin.root_calls) == 1


def test_scoring_logs_nothing_at_warning_level(plugin, monkeypatch, caplog):
    monkeypatch.setenv(SCORE_ENV, SCORE_PATTERNS)
    with caplog.at_level(logging.WARNING, logger="langfuse_plugin"):
        _open(plugin, denied(decider=DECIDER, note="secret-looking note"))
    assert not caplog.records
