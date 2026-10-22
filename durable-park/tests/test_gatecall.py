import copy
import hashlib

import pytest

from agentapprovalprotocol import Allow, Deny, Pending
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from agentapprovalprotocol._gate import submission
from durable_park.gatecall import (
    GateHolder, build_call, lift_gateway_context, make_gate, platform_of, redact_arguments, server_label)
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
