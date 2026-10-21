"""Hermes "Tool Search" (tools.tool_search.enabled: on): what the park hook sees and must do.

With tool search on, Hermes hides MCP and plugin tools behind three bridge tools: ``tool_search`` and
``tool_describe`` (catalog reads) and ``tool_call`` (runs a hidden tool). Facts these tests rely on, read in
Hermes v0.22.3 and re-run against the real code on the proto VM (2026-10-21):

* ``tool_call`` with ONE valid entry is unwrapped BEFORE ``pre_tool_call`` runs (agent/tool_executor.py
  ``_unwrap_tool_search_call`` :399-441, called by ``_parse_tool_call`` :460-466): the hook gets the real tool
  name, the entry's arguments exactly as the model sent them (a JSON-string ``arguments`` is parsed to a dict,
  nothing is coerced), and the id of the outer ``tool_call``.
* In every other case the executor keeps the wrapper (``tool_call``, the raw ``calls``): a batch of two, an
  unparsable ``calls``, a core tool named through the bridge, a ``connectors__`` name. The hook then sees
  ``tool_call`` itself, and Hermes re-resolves the wrapper later with the hook switched off
  (model_tools.py ``handle_function_call`` :906-922), so durable-park must never let that name through.
"""

import json
from types import SimpleNamespace

import pytest
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from durable_park.gatecall import make_gate
from durable_park.ledger import Ledger, args_digest
from durable_park.park_hook import (
    BRIDGE_CALL_MESSAGE, BRIDGE_CALL_TOOL, BRIDGE_READ_TOOLS, BRIDGE_TOOLS, DEFAULT_EXEMPT_TOOLS,
    DEFAULT_FORM_CHECKED_TOOLS, fail_closed_hook, make_pre_tool_call)

import re

SESSION_KEY = "agent:main:telegram:dm:42"
RISKY = [re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS]
PAYMENT = "mcp__wh__quickbooks__create_payment"
READ = "mcp__wh__quickbooks__get_company_info"
BRIDGE_NAMES = {"tool_search", "tool_describe", "tool_call"}


def hermes_unwrap(name, args):
    """What Hermes hands the hook for a model tool call (tool_executor.py:399-441, tool_search.py:578-613,
    tool_search_validation.py:139-170), for the cases that matter here. Pinned to the real
    ``agent.tool_executor._parse_tool_call`` by tests/test_hermes_integration.py (runs where Hermes is importable):
    a single valid entry (``calls`` as a list, one object or a JSON string; ``arguments`` as an object or a JSON
    string; or the legacy ``{"name", "arguments"}`` shape) unwraps; a batch of two, ``{"calls": "nope"}``, a core tool,
    a connector name and a nested ``tool_call`` keep the wrapper."""
    if name != "tool_call":
        return name, args
    calls = args.get("calls")
    if calls is None and str(args.get("name") or "").strip():  # the legacy single shape
        calls = [{"name": args.get("name"), "arguments": args.get("arguments")}]
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return name, args
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return name, args
    entry_name = str(calls[0].get("name") or "").strip()
    raw = calls[0].get("arguments")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return name, args
    deferrable = entry_name.startswith("mcp__") or entry_name == "parked_action_execute"
    if not entry_name or entry_name in BRIDGE_NAMES or not isinstance(raw, dict) or not deferrable:
        return name, args  # includes connectors__* and core tools such as clarify
    return entry_name, raw


class RulesBroken:
    """A form checker that finds a problem with any amount that is not a number."""

    def check(self, tool, args):
        return None if isinstance(args.get("amount"), (int, float)) else "amount must be a number"


@pytest.fixture
def env(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    return SimpleNamespace(ledger=ledger, provider=FakeProvider(auto=None), tmp_path=tmp_path)


def build(env, *, provider=None, **kw):
    provider = provider or env.provider
    return make_pre_tool_call(
        env.ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5)), RISKY,
        session_key_fn=lambda: SESSION_KEY, **kw)


def as_hermes(hook, tool, args, call_id="call-1"):
    """One model tool call, the way Hermes's executor presents it to ``pre_tool_call``."""
    name, real_args = hermes_unwrap(tool, args)
    return hook(tool_name=name, args=real_args, session_id="sess-1", tool_call_id=call_id, task_id="",
                turn_id="t1", api_request_id="", middleware_trace=[])


def wrap(name, arguments):
    return {"calls": [{"name": name, "arguments": arguments}]}


def test_the_bridge_names_are_constants():
    assert BRIDGE_READ_TOOLS == ("tool_search", "tool_describe")
    assert BRIDGE_CALL_TOOL == "tool_call"
    assert set(BRIDGE_TOOLS) == BRIDGE_NAMES and len(BRIDGE_TOOLS) == 3
    assert DEFAULT_EXEMPT_TOOLS == ("parked_action_execute", "clarify", "tool_search", "tool_describe")
    assert BRIDGE_CALL_TOOL not in DEFAULT_EXEMPT_TOOLS


@pytest.mark.parametrize("tool, args", [("tool_search", {"queries": ["quickbooks payment"], "limit": 5}),
                                        ("tool_describe", {"names": [PAYMENT]})])
def test_catalog_reads_are_exempt_no_request_no_row(env, tool, args):
    hook = build(env)
    assert as_hermes(hook, tool, args) is None
    assert env.provider.requests == {}
    assert env.ledger.get_by_call("sess-1", "call-1") is None


@pytest.mark.parametrize("args", [
    wrap(PAYMENT, {"amount": 5}) | {"calls": wrap(PAYMENT, {"amount": 5})["calls"] * 2},  # a batch of two
    {"calls": "nope"},
    wrap("clarify", {"question": "x"}),                       # a core tool named through the bridge
    wrap("connectors__gmail__send_email", {"to": "a@b.c"}),   # hosted connector names
    {"calls": []},
    {},
])
def test_a_wrapper_that_reaches_the_hook_is_refused_never_asked(env, args):
    hook = build(env)
    assert hermes_unwrap("tool_call", args)[0] == "tool_call"  # Hermes did not unwrap it
    result = as_hermes(hook, "tool_call", args)
    assert result == {"action": "block", "message": BRIDGE_CALL_MESSAGE}
    assert env.provider.requests == {}
    assert env.ledger.get_by_call("sess-1", "call-1") is None


def test_the_refusal_names_the_way_out():
    assert "tool_call" in BRIDGE_CALL_MESSAGE and "NOT" in BRIDGE_CALL_MESSAGE
    assert "one entry" in BRIDGE_CALL_MESSAGE


def test_tool_call_is_refused_even_when_a_caller_lists_it_as_exempt(env):
    hook = build(env, exempt=(*DEFAULT_EXEMPT_TOOLS, "tool_call"))
    assert as_hermes(hook, "tool_call", {"calls": "nope"}) == {"action": "block", "message": BRIDGE_CALL_MESSAGE}
    assert env.provider.requests == {}
    closed = fail_closed_hook((*DEFAULT_EXEMPT_TOOLS, "tool_call"))
    assert closed(tool_name="tool_call")["action"] == "block"
    assert closed(tool_name="tool_search") is None


@pytest.mark.parametrize("as_text", [False, True], ids=["arguments object", "arguments JSON string"])
def test_wrapped_risky_call_parks_with_the_real_name_and_the_real_args_digest(env, as_text):
    args = {"amount": 5, "customer": "C-1"}
    hook = build(env)
    result = as_hermes(hook, "tool_call", wrap(PAYMENT, json.dumps(args) if as_text else args), call_id="outer-1")
    (row,) = env.ledger.list_by_status("parked")
    assert result["action"] == "block" and result["message"].startswith(f'PARKED {row.park_id}: "{PAYMENT}"')
    assert row.tool == PAYMENT and row.tool != "tool_call"
    assert row.tool_call_id == "outer-1" and row.server == "park:quickbooks"
    assert row.args_digest == args_digest(args)
    assert json.loads(row.args_json) == args
    stored = env.provider.only()
    assert stored.body["tool"] == PAYMENT and stored.body["arguments"] == args


def test_wrapped_and_direct_calls_are_the_same_call_to_withhuman(env, tmp_path):
    args = {"amount": 5}
    direct_env = SimpleNamespace(ledger=Ledger(str(tmp_path / "direct.db")), provider=FakeProvider(auto=None))
    as_hermes(build(direct_env), PAYMENT, args)
    as_hermes(build(env), "tool_call", wrap(PAYMENT, args))
    (a,), (b,) = direct_env.ledger.list_by_status("parked"), env.ledger.list_by_status("parked")
    assert (a.tool, a.args_digest, a.args_json, a.server) == (b.tool, b.args_digest, b.args_json, b.server)
    first, second = direct_env.provider.only().body, env.provider.only().body
    assert (first["tool"], first["arguments"], first["context"]["args_sha256"]) == (
        second["tool"], second["arguments"], second["context"]["args_sha256"])


def test_wrapped_risky_call_still_gets_the_form_check_first(env):
    hook = build(env, form_checker=RulesBroken())
    result = as_hermes(hook, "tool_call", wrap(PAYMENT, {"amount": "five"}))
    assert result["action"] == "block"
    assert f'"{PAYMENT}" was NOT run and no approval was requested' in result["message"]
    assert "amount must be a number" in result["message"]
    assert env.provider.requests == {} and env.ledger.get_by_call("sess-1", "call-1") is None


@pytest.mark.parametrize("auto, parked", [(None, True), ("approved", False)])
def test_wrapped_non_risky_call_is_decided_like_any_other_call(env, auto, parked):
    provider = FakeProvider(auto=auto)
    result = as_hermes(build(env, provider=provider), "tool_call", wrap(READ, {}))
    row = env.ledger.get_by_call("sess-1", "call-1")
    assert row.tool == READ and row.args_digest == args_digest({})
    assert provider.only().body["tool"] == READ
    if parked:
        assert result["message"].startswith("PARKED P-") and row.status == "parked"
    else:
        assert result is None and row.status == "allowed"


def test_the_legacy_single_shape_is_judged_like_the_batch_shape(env):
    result = as_hermes(build(env), "tool_call", {"name": PAYMENT, "arguments": {"amount": 5}})
    (row,) = env.ledger.list_by_status("parked")
    assert result["action"] == "block" and row.tool == PAYMENT and row.args_digest == args_digest({"amount": 5})


def test_the_parked_executor_is_exempt_when_reached_through_the_bridge(env):
    hook = build(env)
    assert as_hermes(hook, "tool_call", wrap("parked_action_execute", {"park_id": "P-1A2B3C4D"})) is None
    assert env.provider.requests == {}
