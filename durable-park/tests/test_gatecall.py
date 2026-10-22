import copy
import hashlib
import json
import logging
import threading

import pytest

from agentapprovalprotocol import Allow, Deny, Pending
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from agentapprovalprotocol._gate import submission
from durable_park.gatecall import (
    GateHolder, LiftStatus, MAX_LIFT_ENTRIES, build_call, find_leftover_notes, is_note_key, lift_gateway_context,
    make_gate, platform_of, redact_arguments, server_label, text_form_note_keys)
from durable_park.ledger import Ledger


def make_row(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    return ledger.park(
        session_key="agent:main:telegram:dm:42",
        session_id="sess-1",
        tool_call_id="call-1",
        tool="mcp__wh__quickbooks__create_payment",
        args={"amount": 12.5, "customer": {"id": "7"}},
        call_key=hashlib.sha256(b"sess-1:call-1").hexdigest(),
        server="park:quickbooks",
    )


def test_build_call_context_and_resume(tmp_path):
    row = make_row(tmp_path)
    call = build_call(row)
    assert call.key == row.call_key
    assert call.tool == "mcp__wh__quickbooks__create_payment"
    assert dict(call.arguments) == {"amount": 12.5, "customer": {"id": "7"}}
    assert call.server == "park:quickbooks"
    assert dict(call.context) == {
        "args_sha256": row.args_digest, "platform": "telegram", "session_key": "agent:main:telegram:dm:42"}
    assert dict(call.resume) == {"framework": "hermes", "park_id": row.park_id, "session_key": row.session_key}


def test_build_call_is_pure_function_of_row(tmp_path):
    row = make_row(tmp_path)
    gate = make_gate(BASE_URL, TOKEN, client=FakeProvider(auto=None).client())
    bodies = [submission(build_call(row), gate.timeout, gate.context) for _ in range(2)]
    assert bodies[0] == bodies[1]


def test_build_call_marks_truncation(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    row = ledger.park(session_key="agent:main:slack:dm:1", session_id="s", tool_call_id="c", tool="web_search",
                      args={"q": "x" * 20000}, call_key="k" * 64, server="hermes")
    call = build_call(row)
    assert len(call.arguments["q"]) == 1024
    assert call.context["args_truncated"] is True
    assert call.context["args_sha256"] == row.args_digest


def test_non_json_arguments_raise(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    row = ledger.park(session_key="agent:main:telegram:dm:1", session_id="s", tool_call_id="c", tool="web_search",
                      args={"x": float("nan")}, call_key="n" * 64, server="hermes")
    with pytest.raises(ValueError):
        build_call(row)


@pytest.mark.parametrize("tool,label", [
    ("mcp__wh__quickbooks__create_payment", "park:quickbooks"),
    ("mcp__wh__docusign__createEnvelope", "park:docusign"),
    ("mcp__github__get_issue", "mcp:github"),
    ("mcp__my_srv__x_y", "mcp:my_srv"),
    ("web_search", "hermes"),
    ("connectors__slack__post", "hermes"),
])
def test_server_label(tool, label):
    assert server_label(tool) == label


def test_platform_of():
    assert platform_of("agent:main:telegram:dm:8169087659") == "telegram"
    assert platform_of("") == "unknown"
    assert platform_of("x:y") == "unknown"


def test_redaction_any_depth_and_case():
    args = {"Password": "p", "nested": {"api_key": "k", "list": [{"access_token": "t"}]}, "amount": 5}
    snapshot = copy.deepcopy(args)
    out, truncated = redact_arguments(args)
    assert out == {"Password": "[redacted]", "nested": {"api_key": "[redacted]",
                                                        "list": [{"access_token": "[redacted]"}]}, "amount": 5}
    assert args == snapshot
    assert truncated is False


def test_truncation_over_16_kib():
    out, truncated = redact_arguments({"body": "x" * 20000, "n": 1})
    assert len(out["body"]) == 1024 and out["n"] == 1 and truncated is True
    out, truncated = redact_arguments({"body": "x" * 1000})
    assert out == {"body": "x" * 1000} and truncated is False


def test_gate_holder_reuses_and_rebuilds_on_token_change(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("tok-one")
    holder = GateHolder("http://127.0.0.1:1", str(token_file))
    first = holder.get()
    assert holder.get() is first
    token_file.write_text("tok-two-longer")
    second = holder.get()
    assert second is not first
    assert holder.get() is second
    token_file.unlink()
    with pytest.raises(OSError):
        holder.get()


def test_gate_holder_closes_a_retired_client_one_rebuild_later(tmp_path):
    """A rebuilt gate's old httpx client is closed, but only at the NEXT rebuild: a call that got the old gate
    just before the token changed may still be using it."""
    token_file = tmp_path / "token"
    token_file.write_text("tok-1")
    holder = GateHolder("http://127.0.0.1:1", str(token_file))
    first = holder.get()
    token_file.write_text("tok-22")
    second = holder.get()
    assert not first.client._http.is_closed  # retired, maybe still in use
    token_file.write_text("tok-333")
    holder.get()
    assert first.client._http.is_closed
    assert not second.client._http.is_closed


def test_gate_pending_then_allow_then_consumed(tmp_path):
    row = make_row(tmp_path)
    provider = FakeProvider(auto=None)
    gate = make_gate(BASE_URL, TOKEN, client=provider.client())
    call = build_call(row)

    first = gate.check(call)
    assert isinstance(first, Pending)

    stored = provider.only()
    assert stored.body["timeout"] == "168h"
    assert stored.body["server"] == "park:quickbooks"
    assert stored.body["context"]["call_id"] == row.call_key
    assert stored.body["context"]["resume"] == {
        "framework": "hermes", "park_id": row.park_id, "session_key": row.session_key}

    # still undecided: same request, still pending (idempotent create)
    assert gate.check(call).request_id == first.request_id
    assert len(provider.requests) == 1

    provider.decide(first.request_id, "approved")
    allowed = gate.check(call)
    assert isinstance(allowed, Allow)
    assert allowed.request_id == first.request_id

    again = gate.check(call)
    assert isinstance(again, Deny)
    assert again.status == "consumed"
    assert not again


def test_gate_does_not_report_outcomes(tmp_path):
    row = make_row(tmp_path)
    provider = FakeProvider(auto="approved")
    gate = make_gate(BASE_URL, TOKEN, client=provider.client())
    assert gate.mode == "async"
    assert gate.report_outcomes is False
    with gate.guard(build_call(row)) as verdict:
        assert isinstance(verdict, Allow)
    assert provider.only().outcome is None


def test_gate_denied(tmp_path):
    row = make_row(tmp_path)
    provider = FakeProvider(auto=None)
    gate = make_gate(BASE_URL, TOKEN, client=provider.client())
    pending = gate.check(build_call(row))
    provider.decide(pending.request_id, "denied", note="no")
    verdict = gate.check(build_call(row))
    assert isinstance(verdict, Deny) and verdict.status == "denied"


# ---- lifting a nested withhuman_context ------------------------------------------------------------

GW_SCHEMA = {"type": "object", "properties": {
    "params": {"type": "object", "properties": {"criteria": {"type": "string"}}},
    "withhuman_context": {"type": "string"}}}


def test_lift_moves_nested_note_to_top():
    args = {"params": {"criteria": "x", "withhuman_context": "Find customer 58"}}
    assert lift_gateway_context(args, GW_SCHEMA) == {
        "params": {"criteria": "x"}, "withhuman_context": "Find customer 58"}


def test_lift_existing_top_level_note_wins_and_nested_is_dropped():
    args = {"params": {"criteria": "x", "withhuman_context": "nested"}, "withhuman_context": "top"}
    assert lift_gateway_context(args, GW_SCHEMA) == {"params": {"criteria": "x"}, "withhuman_context": "top"}


def test_lift_non_string_nested_is_dropped_not_moved():
    for value in (5, None, {"a": 1}, ["x"], True, ""):
        args = {"params": {"criteria": "x", "withhuman_context": value}}
        assert lift_gateway_context(args, GW_SCHEMA) == {"params": {"criteria": "x"}}, value


@pytest.mark.parametrize("schema", [
    None, "nope", [], {}, {"properties": None}, {"properties": []},
    {"properties": {"params": {"type": "object"}}},  # the tool does not advertise withhuman_context
])
def test_lift_leaves_other_tools_untouched(schema):
    args = {"params": {"withhuman_context": "note"}}
    assert lift_gateway_context(args, schema) == {"params": {"withhuman_context": "note"}}


def test_lift_skips_a_key_whose_own_schema_declares_the_field():
    schema = {"properties": {
        "params": {"type": "object", "properties": {"withhuman_context": {"type": "string"}}},
        "other": {"type": "object"},
        "withhuman_context": {"type": "string"}}}
    args = {"params": {"withhuman_context": "the server wants this"}, "other": {"withhuman_context": "lift me"}}
    assert lift_gateway_context(args, schema) == {
        "params": {"withhuman_context": "the server wants this"}, "other": {}, "withhuman_context": "lift me"}


def test_lift_two_nested_dicts_sorted_first_string_wins_both_removed():
    args = {"zeta": {"withhuman_context": "from zeta", "k": 1}, "alpha": {"withhuman_context": "from alpha"}}
    assert lift_gateway_context(args, GW_SCHEMA) == {
        "zeta": {"k": 1}, "alpha": {}, "withhuman_context": "from alpha"}


def test_lift_sorted_first_non_string_is_dropped_and_next_string_wins():
    args = {"alpha": {"withhuman_context": 7}, "beta": {"withhuman_context": "from beta"}}
    assert lift_gateway_context(args, GW_SCHEMA) == {"alpha": {}, "beta": {}, "withhuman_context": "from beta"}


@pytest.mark.parametrize("args", [None, "text", 5, ["params"], ({"withhuman_context": "x"},)])
def test_lift_non_dict_args_unchanged(args):
    assert lift_gateway_context(args, GW_SCHEMA) == args


def test_lift_leaves_json_string_values_and_deeper_levels_alone():
    string_valued = {"params": '{"criteria": "x", "withhuman_context": "note"}'}
    assert lift_gateway_context(string_valued, GW_SCHEMA) == string_valued
    deeper = {"params": {"inner": {"withhuman_context": "too deep"}}}
    assert lift_gateway_context(deeper, GW_SCHEMA) == deeper


def test_lift_is_idempotent_and_does_not_mutate_its_input():
    args = {"params": {"criteria": "x", "withhuman_context": "note"}, "other": {"withhuman_context": 1}}
    before = copy.deepcopy(args)
    once = lift_gateway_context(args, GW_SCHEMA)
    assert args == before
    assert once == {"params": {"criteria": "x"}, "other": {}, "withhuman_context": "note"}
    assert lift_gateway_context(once, GW_SCHEMA) == once


def park_with_args(tmp_path, args):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    return ledger.park(session_key="agent:main:telegram:dm:42", session_id="s", tool_call_id="c",
                       tool="mcp__wh__quickbooks__create_payment", args=args, call_key="r" * 64,
                       server="park:quickbooks")


def test_build_call_passes_the_note_as_reasoning(tmp_path):
    row = park_with_args(tmp_path, {"params": {"criteria": "x"}, "withhuman_context": "Find customer 58"})
    call = build_call(row)
    assert call.reasoning == "Find customer 58"
    assert dict(call.arguments)["withhuman_context"] == "Find customer 58"


def test_build_call_cuts_reasoning_at_2000_characters(tmp_path):
    row = park_with_args(tmp_path, {"withhuman_context": "n" * 2500})
    assert build_call(row).reasoning == "n" * 2000


@pytest.mark.parametrize("args", [
    {"params": {"criteria": "x"}},
    {"withhuman_context": ""},
    {"withhuman_context": 5},
    {"params": {"withhuman_context": "nested only"}},
])
def test_build_call_without_a_top_level_note_has_no_reasoning(tmp_path, args):
    assert build_call(park_with_args(tmp_path, args)).reasoning is None


def test_build_call_with_a_note_is_still_a_pure_function_of_the_row(tmp_path):
    row = park_with_args(tmp_path, {"withhuman_context": "Find customer 58"})
    gate = make_gate(BASE_URL, TOKEN, client=FakeProvider(auto=None).client())
    bodies = [submission(build_call(row), gate.timeout, gate.context) for _ in range(2)]
    assert bodies[0] == bodies[1]
    assert bodies[0]["agent_reasoning"] == "Find customer 58"


def ctx_schema(nested, **extra):
    """A gateway schema whose ``params`` rules are ``nested``; ``extra`` goes next to ``properties`` at the root."""
    return {"properties": {"params": nested, "withhuman_context": {"type": "string"}}, **extra}


NOTE_ARGS = {"params": {"criteria": "x", "withhuman_context": "the note"}}
LIFTED_ARGS = {"params": {"criteria": "x"}, "withhuman_context": "the note"}
DECLARING = {"type": "object", "properties": {"withhuman_context": {"type": "string"}}}
PLAIN = {"type": "object", "properties": {"criteria": {"type": "string"}}}


def lift_report(args, schema):
    problems = []
    return lift_gateway_context(args, schema, on_problem=lambda kind, key, detail: problems.append((kind, key, detail))), problems


@pytest.mark.parametrize("schema", [
    ctx_schema({"$ref": "#/$defs/P"}, **{"$defs": {"P": DECLARING}}),
    ctx_schema({"$ref": "#/definitions/P"}, definitions={"P": DECLARING}),
    ctx_schema({"$ref": "#/$defs/a~1b"}, **{"$defs": {"a/b": DECLARING}}),
    ctx_schema({"$ref": "#/$defs/a~0b"}, **{"$defs": {"a~b": DECLARING}}),
    ctx_schema({"$ref": "#/$defs/Outer"}, **{"$defs": {"Outer": {"$ref": "#/$defs/P"}, "P": DECLARING}}),
    ctx_schema({"allOf": [PLAIN, DECLARING]}),
    ctx_schema({"allOf": [{"$ref": "#/$defs/P"}]}, **{"$defs": {"P": DECLARING}}),
    ctx_schema({"anyOf": [PLAIN, DECLARING]}),
    ctx_schema({"oneOf": [DECLARING, {"$ref": "https://example.com/x.json"}]}),  # True beats unknown
    ctx_schema({"patternProperties": {"^withhuman_": {"type": "string"}}}),
    ctx_schema({"type": "object", "properties": {"criteria": {}}, "allOf": [DECLARING]}),  # siblings combine
], ids=["defs", "definitions", "tilde1", "tilde0", "ref-chain", "allOf", "allOf-ref", "anyOf", "oneOf-true-wins",
        "pattern", "siblings"])
def test_lift_skips_a_key_whose_rules_declare_the_field(schema):
    result, problems = lift_report(NOTE_ARGS, schema)
    assert result == NOTE_ARGS and problems == [("declared", "params", "")]


@pytest.mark.parametrize("schema", [
    ctx_schema({"$ref": "#/$defs/P"}, **{"$defs": {"P": PLAIN}}),
    ctx_schema({"$ref": "#/definitions/P"}, definitions={"P": PLAIN}),
    ctx_schema({"$ref": "#/$defs/a~1b"}, **{"$defs": {"a/b": PLAIN}}),
    ctx_schema({"allOf": [PLAIN, {"type": "object"}]}),
    ctx_schema({"anyOf": [PLAIN, {"type": "object"}]}),
    ctx_schema({"oneOf": [{"type": "object"}, True, False]}),
    ctx_schema({"patternProperties": {"^x_": {"type": "string"}}}),
    ctx_schema({"type": "object", "properties": {"criteria": {}}, "allOf": [PLAIN]}),
], ids=["defs", "definitions", "tilde1", "allOf", "anyOf", "oneOf-bools", "pattern", "siblings"])
def test_lift_lifts_when_the_rules_clearly_do_not_declare_the_field(schema):
    result, problems = lift_report(NOTE_ARGS, schema)
    assert result == LIFTED_ARGS and problems == []


def _cycle():
    return ctx_schema({"$ref": "#/$defs/A"}, **{"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}})


def _deep():
    defs = {f"D{i}": {"$ref": f"#/$defs/D{i + 1}"} for i in range(30)}
    defs["D30"] = PLAIN
    return ctx_schema({"$ref": "#/$defs/D0"}, **{"$defs": defs})


@pytest.mark.parametrize("schema", [
    ctx_schema({"$ref": "https://example.com/x.json"}),
    ctx_schema({"$ref": "other.json#/P"}),
    ctx_schema({"$ref": "#/$defs/missing"}, **{"$defs": {}}),
    ctx_schema({"$ref": "#/$defs/P/properties/x"}, **{"$defs": {"P": PLAIN}}),
    ctx_schema({"$ref": 5}),
    ctx_schema({"oneOf": [PLAIN, {"$ref": "https://example.com/x.json"}]}),  # no True, one unknown
    ctx_schema({"anyOf": "not a list"}),
    ctx_schema({"patternProperties": {"([": {"type": "string"}}}),  # invalid regex
    ctx_schema({"$ref": "#/$defs/A"}, **{"$defs": {"A": {"$ref": "#/$defs/A"}}}),  # self reference
    _cycle(),
    _deep(),  # past the depth limit
], ids=["remote", "relative-file", "missing", "pointer-into-nothing", "non-string-ref", "oneOf-unknown",
        "bad-anyOf", "bad-regex", "self-ref", "cycle", "too-deep"])
def test_lift_lifts_a_key_it_cannot_judge_and_reports_nothing(schema):
    """Only a positive declaration skips a key: rules that cannot be judged are treated as not declaring it."""
    result, problems = lift_report(NOTE_ARGS, schema)
    assert result == LIFTED_ARGS and problems == []


def test_lift_reports_declared_only_for_keys_that_carry_a_note_and_never_values():
    schema = ctx_schema(DECLARING)
    quiet, problems = lift_report({"params": {"criteria": "x"}}, schema)
    assert quiet == {"params": {"criteria": "x"}} and problems == []
    schema["properties"]["zz"] = DECLARING
    args = {"params": {"withhuman_context": "private note"}, "zz": {"withhuman_context": "other"}}
    result, problems = lift_report(args, schema)
    assert result == args and problems == [("declared", "params", ""), ("declared", "zz", "")]


def test_lift_survives_a_failing_on_problem_callback():
    def boom(kind, key, detail):
        raise RuntimeError("callback bug")

    assert lift_gateway_context(NOTE_ARGS, ctx_schema(DECLARING), on_problem=boom) == NOTE_ARGS


def test_lift_treats_an_unexpected_error_while_judging_as_not_declared(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr("durable_park.gatecall._declares_context_in", boom)
    result, problems = lift_report(NOTE_ARGS, ctx_schema(PLAIN))
    assert result == LIFTED_ARGS and problems == []


def test_lift_ignores_a_non_dict_or_boolean_nested_schema():
    for nested in (True, False, None, "object", []):
        result, problems = lift_report(NOTE_ARGS, ctx_schema(nested))
        assert result == LIFTED_ARGS and problems == [], nested


def test_lift_still_lifts_when_the_nested_rules_are_plain():
    schema = {"properties": {"params": {"type": "object", "properties": {"criteria": {"type": "string"}}},
                             "withhuman_context": {"type": "string"}}}
    assert lift_gateway_context({"params": {"withhuman_context": "n"}}, schema) == {
        "params": {}, "withhuman_context": "n"}


# ---- dropped notes, leftovers, the sanitized-key case ---------------------------------------------------------


@pytest.mark.parametrize("args,reason,key", [
    ({"params": {"withhuman_context": {"why": "x"}}}, "not text", "params"),
    ({"params": {"withhuman_context": 7}}, "not text", "params"),
    ({"params": {"withhuman_context": None}}, "not text", "params"),
    ({"params": {"withhuman_context": ""}}, "empty", "params"),
    ({"params": {"withhuman_context": "nested"}, "withhuman_context": "top"}, "another note was already at the top", "params"),
    ({"params": {"withhuman_context": "nested"}, "withhuman_context": ""}, "another note was already at the top", "params"),
    ({"a": {"withhuman_context": "first"}, "b": {"withhuman_context": "second"}}, "more than one note", "b"),
])
def test_lift_reports_a_note_that_is_removed_but_not_moved(args, reason, key):
    result, problems = lift_report(args, GW_SCHEMA_FOR_DROPS)
    assert ("note_dropped", key, reason) in problems
    assert all(kind == "note_dropped" for kind, _, _ in problems)
    assert not any(isinstance(v, dict) and "withhuman_context" in v for v in result.values())


GW_SCHEMA_FOR_DROPS = {"properties": {"params": {"type": "object"}, "withhuman_context": {"type": "string"}}}


def test_lift_does_not_report_a_note_that_is_the_one_shown_or_an_exact_copy_of_it():
    assert lift_report({"params": {"withhuman_context": "same"}, "withhuman_context": "same"},
                       GW_SCHEMA_FOR_DROPS) == ({"params": {}, "withhuman_context": "same"}, [])
    assert lift_report({"a": {"withhuman_context": "same"}, "b": {"withhuman_context": "same"}},
                       GW_SCHEMA_FOR_DROPS) == ({"a": {}, "b": {}, "withhuman_context": "same"}, [])
    result, problems = lift_report({"a": {"withhuman_context": "same"}, "b": {"withhuman_context": "other"}},
                                   GW_SCHEMA_FOR_DROPS)
    assert problems == [("note_dropped", "b", "more than one note")] and result["withhuman_context"] == "same"


def test_lift_judges_a_key_the_rules_do_not_list_by_the_other_properties():
    """Hermes may show the model a sanitized spelling of a property name: a key the rules do not list is skipped when
    another property declares the field (when in doubt, leave it alone and say so)."""
    schema = {"properties": {"wire_name": DECLARING, "withhuman_context": {"type": "string"}}}
    args = {"sanitized_name": {"withhuman_context": "x"}}
    assert lift_report(args, schema) == (args, [("declared", "sanitized_name", "")])
    plain = {"properties": {"wire_name": PLAIN, "withhuman_context": {"type": "string"}}}
    assert lift_report(args, plain) == ({"sanitized_name": {}, "withhuman_context": "x"}, [])


@pytest.mark.parametrize("name", ["withhuman_context", "WithHuman_Context", "_withhuman_context", "withhuman_context__",
                                  "WITHHUMAN_CONTEXT"])
def test_note_key_spellings(name):
    assert is_note_key(name)


@pytest.mark.parametrize("name", ["withhuman_scope", "withhuman_context_x", "context", "", 5, None])
def test_other_keys_are_not_the_note(name):
    assert not is_note_key(name)


@pytest.mark.parametrize("args,paths", [
    ({"params": {"criteria": {"withhuman_context": "x"}}}, ["params.criteria.withhuman_context"]),
    ({"params": [{"a": 1, "withhuman_context": "x"}]}, ["params[0].withhuman_context"]),
    ({"params": {"items": [{"a": 1}, {"b": {"withhuman_context": "x"}}]}}, ["params.items[1].b.withhuman_context"]),
    ({"params": "{'a':1,'withhuman_context':'x'}"}, ["params"]),  # not strict JSON, still contains the name
    ({"params": '{"a":1,"withhuman_context":"x"'}, ["params"]),  # truncated
    ({"params": '{"a":1,"withhuman_context":"x"}'}, ["params.withhuman_context"]),
    ({"params": '{"a":1,"_withhuman_context_":"x"}'}, ["params._withhuman_context_"]),
    ({"params": '[{"withhuman_context":"x"}]'}, ["params[0].withhuman_context"]),
    ({"params": '"{\\"withhuman_context\\": \\"x\\"}"'}, ["params.withhuman_context"]),  # double encoded
    ({"params": '{"\\u0077ithhuman_context": "x"}'}, ["params.withhuman_context"]),  # escaped spelling
    ({"params": {"WithHuman_Context": "x"}}, ["params.WithHuman_Context"]),
    ({"_withhuman_context": "x"}, ["_withhuman_context"]),  # a top-level misspelling is not the gateway's field
    ({"params": {"a": 1}, "withhuman_context": "the real top-level note"}, []),
    ({"params": {"a": "plain text"}}, []),
    ({"params": "7", "q": "[1, 2]", "r": "null"}, []),
])
def test_find_leftover_notes_finds_every_shape_with_a_path_and_no_values(args, paths):
    hits, incomplete = find_leftover_notes(args)
    assert hits == paths and incomplete is False


def test_find_leftover_notes_paths_never_hold_values():
    args = {"params": {"criteria": {"withhuman_context": "SECRET-NOTE"}, "memo": "SECRET-DATA"}}
    hits, _ = find_leftover_notes(args)
    assert hits == ["params.criteria.withhuman_context"] and "SECRET" not in repr(hits)


def test_find_leftover_notes_skips_only_the_declared_locations():
    args = {"params": {"withhuman_context": "owned"}, "other": {"withhuman_context": "stray"}}
    assert find_leftover_notes(args, {"params.withhuman_context"}) == (["other.withhuman_context"], False)


def test_find_leftover_notes_does_not_look_inside_a_note_or_flag_the_top_level_text():
    args = {"withhuman_context": "talks about withhuman_context in prose", "params": {"a": 1}}
    assert find_leftover_notes(args) == ([], False)


def test_find_leftover_notes_is_bounded_in_depth_nodes_and_hits():
    deep = current = {}
    for _ in range(60):
        current["x"] = {}
        current = current["x"]
    current["withhuman_context"] = "n"
    assert find_leftover_notes({"params": deep}) == ([], True)  # too deep: reported as incomplete, not silently fine
    wide = {"params": [{"i": i} for i in range(11_000)]}
    assert find_leftover_notes(wide)[1] is True
    many = {"params": [{"withhuman_context": "x"} for _ in range(50)]}
    hits, incomplete = find_leftover_notes(many)
    assert len(hits) == 20 and incomplete is True


def test_find_leftover_notes_survives_absurd_json_nesting():
    text = "[" * 100_000 + "withhuman_context" + "]" * 100_000
    assert find_leftover_notes({"params": text}) == (["params"], False)


def test_text_form_note_keys_lists_only_json_objects_holding_the_note():
    args = {"b": '{"withhuman_context": "n"}', "a": '{"withhuman_context": "n"}', "c": '{"x": 1}',
            "d": "withhuman_context", "e": {"withhuman_context": "n"}, "f": '[{"withhuman_context": "n"}]'}
    assert text_form_note_keys(args) == ["a", "b"]
    assert text_form_note_keys("nope") == []


# ---- the lift problem recorder (lift_status.json) ---------------------------------------------------------


def read_entries(path):
    return json.loads(path.read_text())["entries"]


FIELDS = {"tool", "kind", "key", "detail", "first_seen", "last_seen", "count"}


def test_lift_status_dedupes_per_tool_kind_key_detail_and_counts(tmp_path):
    path = tmp_path / "lift_status.json"
    status = LiftStatus(str(path))
    for _ in range(3):
        status.record("tool_a", "declared", "params")
    status.record("tool_a", "declared", "other")
    status.record("tool_a", "schema_unavailable")
    status.record("tool_b", "declared", "params")
    status.record("tool_a", "note_dropped", "params", "empty")
    status.record("tool_a", "note_dropped", "params", "not text")
    status.flush()
    entries = read_entries(path)
    assert len(entries) == 6
    first = next(e for e in entries if (e["tool"], e["kind"], e["key"]) == ("tool_a", "declared", "params"))
    assert first["count"] == 3 and first["first_seen"] <= first["last_seen"]
    assert set(first) == FIELDS
    assert next(e for e in entries if e["kind"] == "schema_unavailable")["key"] == ""
    assert sorted(e["detail"] for e in entries if e["kind"] == "note_dropped") == ["empty", "not text"]


def test_lift_status_keeps_at_most_200_entries_and_the_newest_always_gets_in(tmp_path):
    path = tmp_path / "lift_status.json"
    status = LiftStatus(str(path))
    for i in range(MAX_LIFT_ENTRIES + 25):
        status.record(f"tool_{i}", "declared", "params")
    entries = read_entries(path)
    assert MAX_LIFT_ENTRIES == 200 and len(entries) == 200
    assert f"tool_{MAX_LIFT_ENTRIES + 24}" in {e["tool"] for e in entries}  # the newest is kept
    assert "tool_0" not in {e["tool"] for e in entries}  # the oldest made room


def test_lift_status_file_holds_names_and_times_only_never_values(tmp_path):
    path = tmp_path / "lift_status.json"
    LiftStatus(str(path)).record("mcp__wh__quickbooks__create_payment", "declared", "params")
    data = json.loads(path.read_text())
    assert set(data) == {"entries", "updated_at"}
    assert set(data["entries"][0]) == FIELDS


def test_lift_status_is_reloaded_after_a_restart(tmp_path):
    path = tmp_path / "lift_status.json"
    first = LiftStatus(str(path))
    first.record("t", "declared", "params")
    again = LiftStatus(str(path))
    again.record("t", "declared", "params")
    again.flush()
    (entry,) = read_entries(path)
    assert entry["count"] == 2


def test_lift_status_keeps_a_corrupt_old_file_aside_instead_of_losing_it(tmp_path):
    path = tmp_path / "lift_status.json"
    path.write_text("{not json")
    LiftStatus(str(path)).record("t", "lift_error")
    assert [e["kind"] for e in read_entries(path)] == ["lift_error"]
    assert (tmp_path / "lift_status.json.corrupt").read_text() == "{not json"


def test_lift_status_merges_with_what_another_process_wrote(tmp_path):
    path = tmp_path / "lift_status.json"
    gateway, cli = LiftStatus(str(path)), LiftStatus(str(path))
    gateway.record("t", "declared", "params")
    cli.record("u", "lift_error")  # written after the gateway's: must not drop it
    gateway.record("t", "declared", "params")
    gateway.flush()
    cli.record("t", "declared", "params")
    cli.flush()
    by_tool = {(e["tool"], e["kind"]): e["count"] for e in read_entries(path)}
    assert by_tool == {("t", "declared"): 3, ("u", "lift_error"): 1}


def test_lift_status_write_failure_keeps_the_entries_and_retries_later(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="durable_park")
    directory = tmp_path / "later"
    path = directory / "lift_status.json"
    status = LiftStatus(str(path))
    status.record("t", "declared", "params")
    status.record("t", "declared", "params")
    status.record("u", "lift_error")
    errors = [r.getMessage() for r in caplog.records if "lift_status" in r.getMessage()]
    assert errors == ["could not write lift_status.json (FileNotFoundError)"]  # one error per outage
    directory.mkdir()
    status.flush()  # the self-check thread's retry
    assert {(e["tool"], e["kind"]): e["count"] for e in read_entries(path)} == {("t", "declared"): 2, ("u", "lift_error"): 1}


def test_lift_status_retries_on_the_next_record_after_a_failure(tmp_path):
    directory = tmp_path / "later"
    path = directory / "lift_status.json"
    status = LiftStatus(str(path))
    status.record("t", "declared", "params")
    directory.mkdir()
    status.record("u", "lift_error")
    assert {e["tool"] for e in read_entries(path)} == {"t", "u"}


def test_lift_status_holds_back_the_write_for_a_repeat_until_the_interval_passes(tmp_path):
    now = [100.0]
    path = tmp_path / "lift_status.json"
    status = LiftStatus(str(path), clock=lambda: now[0])
    status.record("t", "declared", "params")
    status.record("t", "declared", "params")
    assert read_entries(path)[0]["count"] == 1  # a repeat of a known problem does not rewrite the file
    now[0] += 61
    status.record("t", "declared", "params")
    assert read_entries(path)[0]["count"] == 3
    status.record("u", "lift_error")  # a new problem always writes at once
    assert {e["tool"] for e in read_entries(path)} == {"t", "u"}


def test_lift_status_without_a_path_only_remembers(tmp_path):
    status = LiftStatus(None)
    status.record("t", "declared", "params")  # no file, no error
    status.flush()


def test_lift_status_is_thread_safe(tmp_path):
    path = tmp_path / "lift_status.json"
    status = LiftStatus(str(path))
    threads = [threading.Thread(target=lambda: [status.record("t", "declared", "params") for _ in range(25)])
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    status.flush()
    (entry,) = read_entries(path)
    assert entry["count"] == 200
