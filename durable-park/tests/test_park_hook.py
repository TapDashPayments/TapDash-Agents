import copy
import hashlib
import logging
import os
import re
import sys
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

import json
from datetime import datetime, timedelta, timezone

import durable_park
from durable_park import POLICY_MESSAGE, foreign_pre_tool_call_hooks, policy_problem, register
from durable_park.executor import EXECUTE_SCHEMA
from durable_park.gatecall import GateHolder, build_call, bounded_http, make_gate
from durable_park.ledger import Ledger, call_key
from agentapprovalprotocol import Gate

from durable_park.breaker import Breaker
from durable_park.formcheck import FormChecker
from durable_park.park_hook import DEFAULT_FORM_CHECKED_TOOLS, make_pre_tool_call

REAL_POLICY_PROBLEM = policy_problem
REAL_IS_GATEWAY = durable_park.is_gateway_process if hasattr(durable_park, "is_gateway_process") else None


@pytest.fixture(autouse=True)
def _policy_passes(monkeypatch):
    """The register tests are about everything else: a passing tool policy report is assumed (the tests of the
    report itself put the real ``policy_problem`` back)."""
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: None)


@pytest.fixture(autouse=True)
def _is_the_gateway(monkeypatch):
    """The register tests run as the gateway process (the only one that writes the load verdict); the tests of
    that distinction put their own answer back."""
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: True)


SESSION_KEY = "agent:main:telegram:dm:42"
COULD_NOT_OPEN = "durable-park: could not open an approval request; the call was not run"
RISKY = [re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS]
UNREACHABLE = 'durable-park: withHuman could not be reached; "{tool}" did not run. Try again later.'
REFUSED = ('durable-park: "{tool}" was refused by withHuman policy{note}. It did not run. '
           "Do not retry it or work around it; tell the user.")
NO_SESSION = "durable-park: no chat session; the tool call was refused"
NO_CALL_ID = "durable-park: no tool call id; the tool call was refused"
FOREIGN = ("durable-park: another plugin hook could change tool arguments after approval ({names}); every tool "
           "call is refused until an admin removes it or lists it in observer_hooks.")


def already_waiting(park_id):
    return (f"durable-park: {park_id} is already waiting for approval in this chat; "
            f"this call was not run. Do not retry it; wait until {park_id} is decided.")


def park_message(park_id, tool):
    return (f'PARKED {park_id}: "{tool}" needs human approval in withHuman and has NOT run. '
            "Do not retry it or try another way to do it. Tell the user it is waiting for approval; "
            "you will be told here when it is decided.")


@pytest.fixture
def env(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    provider = FakeProvider(auto=None)
    hook = make_pre_tool_call(
        ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5)), RISKY,
        session_key_fn=lambda: SESSION_KEY)
    return SimpleNamespace(ledger=ledger, provider=provider, hook=hook, tmp_path=tmp_path)


def make_hook(env, *, provider=None, ledger=None, session_key_fn=lambda: SESSION_KEY, **kw):
    provider = provider or env.provider
    return make_pre_tool_call(
        ledger or env.ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5)), RISKY,
        session_key_fn=session_key_fn, **kw)


def call(hook, tool, args=None, session_id="sess-1", tool_call_id="call-1"):
    return hook(tool_name=tool, args=args or {"amount": 5}, session_id=session_id,
                tool_call_id=tool_call_id, task_id="", turn_id="t1", api_request_id="", middleware_trace=[])


def test_exempt_tools_pass_without_request(env):
    assert call(env.hook, "clarify") is None
    assert call(env.hook, "parked_action_execute", tool_call_id="call-2") is None
    assert env.provider.requests == {}
    assert env.ledger.get_by_call("sess-1", "call-1") is None
    assert env.ledger.get_by_call("sess-1", "call-2") is None
    # any other tool is asked about: it gets a row and a request
    assert call(env.hook, "web_search", {"query": "x"}, tool_call_id="call-3")["message"].startswith("PARKED P-")
    assert len(env.provider.requests) == 1
    assert env.provider.only().body["tool"] == "web_search"


def test_risky_call_parks_and_blocks(env):
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(env.hook, tool)
    (row,) = env.ledger.list_by_status("parked")
    assert result == {"action": "block", "message": park_message(row.park_id, tool)}
    assert row.server == "park:quickbooks"
    assert row.request_id
    assert row.session_key == SESSION_KEY
    assert row.call_key == call_key("sess-1", "call-1") == hashlib.sha256(b"sess-1:call-1").hexdigest()
    stored = env.provider.only()
    assert stored.id == row.request_id
    assert stored.body["context"]["resume"]["park_id"] == row.park_id


def test_hyphenated_bill_tool_is_risky(env):
    result = call(env.hook, "mcp__wh__quickbooks__create-bill")
    assert result["action"] == "block" and result["message"].startswith("PARKED P-")
    assert len(env.ledger.list_by_status("parked")) == 1


def test_docusign_create_envelope_is_risky(env):
    result = call(env.hook, "mcp__wh__docusign__createEnvelope")
    assert result["action"] == "block" and result["message"].startswith("PARKED P-")
    (row,) = env.ledger.list_by_status("parked")
    assert row.server == "park:docusign"


def test_same_tool_call_id_is_idempotent(env):
    tool = "mcp__wh__quickbooks__create_payment"
    first = call(env.hook, tool)
    second = call(env.hook, tool)
    assert first == second
    assert len(env.ledger.list_by_status("parked")) == 1
    assert len(env.provider.requests) == 1


def test_second_risky_call_blocked_while_parked(env):
    first = call(env.hook, "mcp__wh__quickbooks__create_payment")
    (row,) = env.ledger.list_by_status("parked")
    second = call(env.hook, "mcp__wh__quickbooks__create_bill", tool_call_id="call-2")
    assert first["message"].startswith("PARKED ")
    assert second == {"action": "block", "message": already_waiting(row.park_id)}
    assert env.ledger.list_by_status("parked") == [env.ledger.get(row.park_id)]
    # the second call's request was created (withHuman had no instant answer) and then withdrawn
    assert len(env.provider.requests) == 2
    loser = env.ledger.get_by_call("sess-1", "call-2")
    assert loser.status == "cancelled" and loser.request_id is None
    (rid,) = [r for r in env.provider.requests if r != row.request_id]
    assert env.provider.requests[rid].status == "cancelled"
    assert rid in loser.note and row.park_id in loser.note


def test_no_session_key_blocks(env):
    hook = make_hook(env, session_key_fn=lambda: "")
    result = call(hook, "mcp__wh__quickbooks__create_payment")
    assert result == {"action": "block", "message": NO_SESSION}
    assert env.ledger.list_by_status("parked") == []
    assert env.provider.requests == {}


def test_provider_error_blocks(env):
    env.provider.intercept = lambda request: httpx.Response(401, json={"error": "bad token"})
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(env.hook, tool)
    assert result == {"action": "block", "message": UNREACHABLE.format(tool=tool)}
    assert env.ledger.list_by_status("parked") == []
    (row,) = env.ledger.list_by_status("refused")
    assert row.request_id is None and row.note.startswith("withHuman error")
    # the failed park does not wedge the chat: the next risky call can park
    env.provider.intercept = None
    again = call(env.hook, "mcp__wh__quickbooks__create_bill", tool_call_id="call-2")
    assert again["message"].startswith("PARKED ")


def test_gate_factory_failure_fails_closed(env):
    def broken_factory():
        raise OSError("token file missing")

    hook = make_pre_tool_call(env.ledger, broken_factory, RISKY, session_key_fn=lambda: SESSION_KEY)
    assert call(hook, "mcp__wh__quickbooks__create_payment") == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.ledger.list_by_status("parked") == []


def test_instant_allow_runs_and_records_allowed(env):
    env.provider.auto = "approved"  # a pipeline rule approved it at once
    result = call(env.hook, "mcp__wh__quickbooks__create_payment")
    assert result is None
    stored = env.provider.only()
    assert stored.consumed_at is not None  # the approval was consumed exactly once, by this call
    assert [c for c in env.provider.calls if c[1].endswith("/consume")] == [("POST", f"/{stored.id}/consume")]
    (row,) = env.ledger.list_by_status("allowed")
    assert row.request_id == stored.id and row.note == "withHuman: approved"
    assert env.ledger.list_by_status("parked", "cancelled", "refused") == []


def test_missing_tool_call_id_blocks(env):
    result = call(env.hook, "mcp__wh__quickbooks__create_payment", tool_call_id="")
    assert result == {"action": "block", "message": NO_CALL_ID}
    assert env.ledger.list_by_status("parked") == []
    assert env.provider.requests == {}


def test_concurrent_loser_is_cancelled(env):
    """Another call of this chat got its request attached while ours was being asked: ours backs out."""
    class Racy(Ledger):
        raced = False

        def active_for_session(self, session_key):
            if not self.raced:
                self.raced = True
                other = self.park(session_key=session_key, session_id="sess-1", tool_call_id="call-0",
                                  tool="mcp__wh__quickbooks__create_bill", args={"a": 1},
                                  call_key=call_key("sess-1", "call-0"), server="park:quickbooks")
                assert self.set_request_id(other.park_id, "req-other")
            return super().active_for_session(session_key)

    ledger = Racy(str(env.tmp_path / "racy.db"))
    hook = make_hook(env, ledger=ledger)
    result = call(hook, "mcp__wh__quickbooks__create_payment", tool_call_id="call-1")
    first = ledger.get_by_call("sess-1", "call-0")
    assert result == {"action": "block", "message": already_waiting(first.park_id)}
    assert first.status == "parked" and first.request_id == "req-other"
    (loser,) = ledger.list_by_status("cancelled")
    assert loser.tool_call_id == "call-1" and loser.request_id is None
    assert env.provider.only().status == "cancelled"


def test_parked_row_without_request_does_not_make_the_chat_busy(env):
    """A row still being asked (no request id yet) does not count as waiting (D11)."""
    env.ledger.park(session_key=SESSION_KEY, session_id="sess-1", tool_call_id="call-0",
                    tool="mcp__wh__quickbooks__create_bill", args={"a": 1},
                    call_key=call_key("sess-1", "call-0"), server="park:quickbooks")
    result = call(env.hook, "mcp__wh__quickbooks__create_payment", tool_call_id="call-1")
    assert result["message"].startswith("PARKED P-")


class FakeCtx:
    """A Hermes PluginContext stand-in. ``_manager.iter_hook_callbacks`` mirrors Hermes v0.22.2
    (hermes_cli/plugins_dispatch.py:523): a snapshot tuple of the callables registered for a hook.
    ``_manager._middleware`` mirrors the middleware registry (hermes_cli/plugins.py:1183, a dict of lists
    keyed by kind; ``register_middleware`` appends the callable itself, hermes_cli/plugins.py:937-952).
    It imitates Hermes's unwrapped callback storage (``register_hook`` appends the callable itself, so ``id()``
    identifies durable-park's own hooks); ``compat.DEPENDS_ON`` is the guard if Hermes ever changes that."""

    def __init__(self, data_dir, config=None, *, manager=True):
        self.config = config or {}
        self.state = SimpleNamespace(data_dir=data_dir)
        self.hooks = {}
        self.hook_lists = {}  # every callback in registration order, as the Hermes registry keeps them
        self.tools = {}
        self.asked = []
        if manager:
            self._manager = SimpleNamespace(
                iter_hook_callbacks=lambda name: tuple(self.hook_lists.get(name, ())), _middleware={})

    def register_middleware(self, kind, callback):
        self._manager._middleware.setdefault(kind, []).append(callback)

    def get_config(self, key, default=None):
        self.asked.append(key)
        return self.config.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback
        self.hook_lists.setdefault(name, []).append(callback)

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = SimpleNamespace(toolset=toolset, schema=schema, handler=handler, **kwargs)

    def dispatch_tool(self, tool_name, args, **kwargs):
        raise AssertionError("not expected in registration tests")


def foreign_hook(**kw):
    """Another plugin's pre_tool_call hook (it could return a ``modify`` directive)."""
    return None


FOREIGN_NAME = f"{foreign_hook.__module__}.{foreign_hook.__qualname__}"


@pytest.fixture
def with_session(monkeypatch):
    monkeypatch.setattr("durable_park.park_hook.default_session_key", lambda: SESSION_KEY)


@pytest.fixture
def fake_withhuman(monkeypatch, tmp_path):
    """The plugin's shared gate talks to a FakeProvider; ``token`` is a readable token file."""
    provider = FakeProvider(auto=None)
    real_make_gate = make_gate
    monkeypatch.setattr("durable_park.gatecall.make_gate",
                        lambda base_url, token: real_make_gate(BASE_URL, TOKEN,
                                                               client=provider.client(retry_budget=0.5)))
    token = tmp_path / "token"
    token.write_text("tok\n")
    return SimpleNamespace(provider=provider, token=str(token))


def test_register_wires_pre_tool_call_with_default_settings(tmp_path, with_session):
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "pre_gateway_dispatch"}
    assert set(ctx.tools) == {"parked_action_execute"}
    tool = ctx.tools["parked_action_execute"]
    assert tool.toolset == "durable_park" and tool.schema == EXECUTE_SCHEMA and callable(tool.handler)
    assert {"exempt_tools", "form_checked_tools", "observer_hooks", "aap_base_url", "token_file",
            "ledger_path"} <= set(ctx.asked)
    assert (tmp_path / "data" / "ledger.db").exists()  # default ledger_path under state.data_dir
    # every non-exempt tool is asked about: with no token and no network it is refused, never let through
    result = ctx.hooks["pre_tool_call"](tool_name="web_search", args={"query": "x"}, session_id="s",
                                        tool_call_id="c")
    assert result is not None and result["action"] == "block"
    assert result["message"] in (COULD_NOT_OPEN, UNREACHABLE.format(tool="web_search"))
    for exempt in ("clarify", "parked_action_execute"):
        assert ctx.hooks["pre_tool_call"](tool_name=exempt, args={}, session_id="s", tool_call_id="c2") is None


def test_register_without_a_chat_session_refuses(tmp_path):
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    result = ctx.hooks["pre_tool_call"](tool_name="mcp__wh__quickbooks__search_customers", args={},
                                        session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": NO_SESSION}


def test_register_honours_settings_and_fails_closed_without_token(tmp_path, with_session):
    ctx = FakeCtx(tmp_path, {
        "ledger_path": str(tmp_path / "custom.db"),
        "token_file": str(tmp_path / "no-such-token"),
        "exempt_tools": ["parked_action_execute", "web_search"],
        "form_checked_tools": [r"mcp__wh__custom__do_it"],
    })
    register(ctx)
    assert (tmp_path / "custom.db").exists()
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="web_search", args={}, session_id="s", tool_call_id="c0") is None  # exempt now
    # clarify is no longer exempt: it is asked about (and the unreadable token file refuses it)
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c1") == {
        "action": "block", "message": COULD_NOT_OPEN}
    result = hook(tool_name="mcp__wh__custom__do_it", args={}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    rows = Ledger(str(tmp_path / "custom.db")).list_by_status("cancelled")
    assert {row.tool for row in rows} == {"clarify", "mcp__wh__custom__do_it"}


def test_register_form_checks_the_configured_tools(tmp_path, monkeypatch, with_session):
    seen = []

    class Spy(FormChecker):
        def check(self, tool, args):
            seen.append(tool)
            return None

    monkeypatch.setattr("durable_park.formcheck.FormChecker", Spy)
    ctx = FakeCtx(tmp_path, {"form_checked_tools": [r"mcp__wh__custom__do_it"],
                             "token_file": str(tmp_path / "no-such-token")})
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    hook(tool_name="mcp__wh__custom__do_it", args={}, session_id="s", tool_call_id="c1")
    hook(tool_name="mcp__wh__quickbooks__create_payment", args={}, session_id="s", tool_call_id="c2")
    assert seen == ["mcp__wh__custom__do_it"]


def test_register_invalid_form_checked_pattern_keeps_the_defaults(tmp_path, monkeypatch, caplog):
    captured = {}

    def spy(ledger, gate_factory, form_checked, **kw):
        captured["patterns"] = [p.pattern for p in form_checked]
        return lambda **k: None

    monkeypatch.setattr("durable_park.make_pre_tool_call", spy)
    with caplog.at_level("ERROR", logger="durable_park"):
        register(FakeCtx(tmp_path, {"form_checked_tools": ["(", r"mcp__wh__x__y"]}))
    assert captured["patterns"] == [r"mcp__wh__x__y", *DEFAULT_FORM_CHECKED_TOOLS]
    assert any("form_checked_tools" in r.getMessage() for r in caplog.records)


def test_register_fails_closed_when_ledger_cannot_open(tmp_path):
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path)})  # a directory, not a database file
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    for tool in ("web_search", "mcp__wh__quickbooks__create_payment", "mcp__wh__quickbooks__search_customers"):
        assert hook(tool_name=tool, args={}, session_id="s", tool_call_id="c") == {
            "action": "block", "message": COULD_NOT_OPEN}
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None
    assert "pre_gateway_dispatch" in ctx.hooks  # wakes are dropped, never reach the agent
    assert ctx.tools == {}  # without a ledger there is nothing to execute


def test_register_fails_closed_with_the_default_exempt_tools_only(tmp_path):
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path), "exempt_tools": ["web_search"]})
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="web_search", args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": COULD_NOT_OPEN}  # a configured extra is not exempt when failing closed
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None


def test_foreign_hook_detected_through_manager(tmp_path, with_session, fake_withhuman):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    register(ctx)
    ctx.register_hook("pre_tool_call", foreign_hook)  # another plugin, loaded after durable-park
    hook = ctx.hook_lists["pre_tool_call"][0]
    result = hook(tool_name="web_search", args={"q": 1}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": FOREIGN.format(names=FOREIGN_NAME)}
    assert len(fake_withhuman.provider.requests) == 0
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c2") is None


def test_foreign_hook_listed_in_observer_hooks_is_allowed(tmp_path, with_session, fake_withhuman):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token, "observer_hooks": [FOREIGN_NAME]})
    register(ctx)
    ctx.register_hook("pre_tool_call", foreign_hook)
    hook = ctx.hook_lists["pre_tool_call"][0]
    result = hook(tool_name="web_search", args={"q": 1}, session_id="s", tool_call_id="c")
    assert result["message"].startswith("PARKED P-")  # it went on to ask withHuman
    assert len(fake_withhuman.provider.requests) == 1


def test_foreign_hook_registered_later_is_caught_on_the_next_call(tmp_path, with_session, fake_withhuman):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    assert call(hook, "web_search", tool_call_id="c1")["message"].startswith("PARKED P-")
    ctx.register_hook("pre_tool_call", foreign_hook)
    assert call(hook, "web_search", session_id="s2", tool_call_id="c2") == {
        "action": "block", "message": FOREIGN.format(names=FOREIGN_NAME)}


@pytest.mark.parametrize("manager", ["missing", "no_method", "raises"])
def test_missing_hook_registry_fails_closed(tmp_path, with_session, fake_withhuman, manager):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token}, manager=manager != "missing")
    if manager == "no_method":
        ctx._manager = SimpleNamespace()
    elif manager == "raises":
        def boom(name):
            raise RuntimeError("registry broke")
        ctx._manager = SimpleNamespace(iter_hook_callbacks=boom)
    register(ctx)
    result = ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": FOREIGN.format(names="<hook registry unavailable>")}
    assert len(fake_withhuman.provider.requests) == 0


def test_foreign_pre_tool_call_hooks_names_and_filters():
    def mine(**kw):
        return None

    class Callable_:
        def __call__(self, **kw):
            return None

    obj = Callable_()
    ctx = SimpleNamespace(_manager=SimpleNamespace(
        iter_hook_callbacks=lambda name: (mine, foreign_hook, obj) if name == "pre_tool_call" else (),
        _middleware={}))
    obj_name = f"{type(obj).__module__}.{type(obj).__qualname__}"
    assert foreign_pre_tool_call_hooks(ctx, {id(mine)}, set()) == [FOREIGN_NAME, obj_name]
    assert foreign_pre_tool_call_hooks(ctx, {id(mine)}, {FOREIGN_NAME}) == [obj_name]
    assert foreign_pre_tool_call_hooks(ctx, {id(mine), id(obj)}, {FOREIGN_NAME}) == []
    assert foreign_pre_tool_call_hooks(SimpleNamespace(), set(), set()) == ["<hook registry unavailable>"]


def foreign_middleware(**kw):
    """Another plugin's tool middleware: it could hand different args to ``next_call``."""
    return kw["next_call"](kw["args"])


FOREIGN_MW_NAME = f"{foreign_middleware.__module__}.{foreign_middleware.__qualname__}"


@pytest.mark.parametrize("kind", ["tool_execution", "tool_request"])
def test_foreign_tool_middleware_blocks_every_non_exempt_call(tmp_path, with_session, fake_withhuman, kind):
    """Hermes v0.22.2 runs tool_execution middleware AFTER pre_tool_call (agent_runtime_helpers.py:2619-2624,
    middleware.py _run_execution_chain); a middleware can pass other args to next_call than were approved."""
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    register(ctx)
    ctx.register_middleware(kind, foreign_middleware)
    hook = ctx.hooks["pre_tool_call"]
    for i, tool in enumerate(("web_search", "mcp__wh__quickbooks__search_customers", "terminal")):
        assert call(hook, tool, tool_call_id=f"c{i}") == {
            "action": "block", "message": FOREIGN.format(names=FOREIGN_MW_NAME)}
    assert len(fake_withhuman.provider.requests) == 0
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="cx") is None


def test_foreign_tool_middleware_listed_in_observer_hooks_proceeds(tmp_path, with_session, fake_withhuman):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token, "observer_hooks": [FOREIGN_MW_NAME]})
    register(ctx)
    ctx.register_middleware("tool_execution", foreign_middleware)
    assert call(ctx.hooks["pre_tool_call"], "web_search")["message"].startswith("PARKED P-")
    assert len(fake_withhuman.provider.requests) == 1


def test_other_middleware_kinds_are_not_foreign_tool_middleware(tmp_path, with_session, fake_withhuman):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    register(ctx)
    ctx.register_middleware("llm_request", foreign_middleware)
    assert call(ctx.hooks["pre_tool_call"], "web_search")["message"].startswith("PARKED P-")


@pytest.mark.parametrize("registry", ["missing", "not_a_mapping"])
def test_missing_middleware_registry_fails_closed(tmp_path, with_session, fake_withhuman, registry):
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    if registry == "missing":
        del ctx._manager._middleware
    else:
        ctx._manager._middleware = ["not", "a", "dict"]
    register(ctx)
    assert call(ctx.hooks["pre_tool_call"], "web_search") == {
        "action": "block", "message": FOREIGN.format(names="<hook registry unavailable>")}
    assert len(fake_withhuman.provider.requests) == 0


def test_hook_and_executor_share_one_gate(tmp_path, monkeypatch):
    holders, gets, factories = [], [], {}
    real_init = GateHolder.__init__

    def init_spy(self, *a, **kw):
        holders.append(self)
        real_init(self, *a, **kw)

    monkeypatch.setattr(GateHolder, "__init__", init_spy)
    monkeypatch.setattr(GateHolder, "get", lambda self: gets.append(self) or object())

    def hook_spy(ledger, gate_factory, form_checked, **kw):
        factories["hook"] = gate_factory
        factories["breaker"] = kw.get("breaker")
        return lambda **k: None

    def executor_spy(ledger, gate_factory, dispatch, **kw):
        factories["executor"] = gate_factory
        return lambda args, **k: "{}"

    monkeypatch.setattr("durable_park.make_pre_tool_call", hook_spy)
    monkeypatch.setattr("durable_park.make_executor", executor_spy)
    register(FakeCtx(tmp_path / "data"))
    factories["hook"]()
    factories["executor"]()
    assert len(holders) == 1 and gets == [holders[0], holders[0]]
    assert isinstance(factories["breaker"], Breaker)
    assert factories["breaker"]._path == str(tmp_path / "data" / "breaker_status.json")


BAD_SETTINGS = [
    ("exempt_tools", 5), ("exempt_tools", {"clarify": True}), ("exempt_tools", ["clarify", 3]),
    ("exempt_tools", None), ("observer_hooks", True), ("observer_hooks", {"a": "b"}),
    ("form_checked_tools", 7), ("form_checked_tools", [r"mcp__x", None]),
]


def _assert_fails_closed_with_default_exempt(ctx):
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="web_search", args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": COULD_NOT_OPEN}
    for exempt in ("clarify", "parked_action_execute"):
        assert hook(tool_name=exempt, args={}, session_id="s", tool_call_id="c") is None
    assert "pre_gateway_dispatch" in ctx.hooks  # wakes never reach the agent


@pytest.mark.parametrize("key, value", BAD_SETTINGS)
def test_bad_setting_type_fails_closed(tmp_path, key, value, caplog):
    """A setting of the wrong type must never make register raise: Hermes would drop every registration and
    run with no pre_tool_call hook (hermes_cli/plugins_loader.py:525-537), i.e. every tool unchecked."""
    ctx = FakeCtx(tmp_path / "data", {key: value})
    with caplog.at_level("ERROR", logger="durable_park"):
        register(ctx)  # does not raise
    _assert_fails_closed_with_default_exempt(ctx)
    assert any(key in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert ctx.tools == {}


@pytest.mark.parametrize("target", ["durable_park.GateHolder", "durable_park.make_pre_tool_call",
                                    "durable_park.make_executor"])
def test_error_after_the_ledger_opens_fails_closed(tmp_path, monkeypatch, target):
    def boom(*a, **kw):
        raise RuntimeError("wiring broke")

    monkeypatch.setattr(target, boom)
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)  # does not raise
    for cb in ctx.hook_lists["pre_tool_call"]:  # every pre_tool_call hook Hermes would run
        if cb(tool_name="web_search", args={}, session_id="s", tool_call_id="c") is not None:
            break
    else:
        raise AssertionError("no hook blocked web_search")
    _assert_fails_closed_with_default_exempt(ctx)


def test_risky_tools_warns_even_when_the_ledger_fails(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="durable_park"):
        register(FakeCtx(tmp_path, {"ledger_path": str(tmp_path), "risky_tools": ["x"]}))
    assert "risky_tools is renamed form_checked_tools; ignoring it" in [r.getMessage() for r in caplog.records]


def test_risky_tools_setting_warns(tmp_path, monkeypatch, caplog):
    captured = {}

    def spy(ledger, gate_factory, form_checked, **kw):
        captured["patterns"] = [p.pattern for p in form_checked]
        return lambda **k: None

    monkeypatch.setattr("durable_park.make_pre_tool_call", spy)
    with caplog.at_level("WARNING", logger="durable_park"):
        register(FakeCtx(tmp_path, {"risky_tools": [r"mcp__wh__old__x"]}))
    assert "risky_tools is renamed form_checked_tools; ignoring it" in [r.getMessage() for r in caplog.records]
    assert captured["patterns"] == list(DEFAULT_FORM_CHECKED_TOOLS)


def test_no_risky_tools_setting_no_warning(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="durable_park"):
        register(FakeCtx(tmp_path))
    assert not any("risky_tools" in r.getMessage() for r in caplog.records)


REUSED = "durable-park: this tool call id was already used for a different call; the call was not run"


def test_replay_of_decided_row_is_blocked_without_new_request(env):
    tool = "mcp__wh__quickbooks__create_payment"
    call(env.hook, tool)
    (row,) = env.ledger.list_by_status("parked")
    assert env.ledger.transition(row.park_id, "parked", "denied")
    result = call(env.hook, tool)
    assert result["action"] == "block"
    assert row.park_id in result["message"] and "denied" in result["message"]
    assert not result["message"].startswith("PARKED")
    assert len(env.provider.requests) == 1
    assert len(env.ledger.list_by_status("parked", "denied", "cancelled")) == 1


def test_same_id_different_args_is_refused(env):
    tool = "mcp__wh__quickbooks__create_payment"
    first = call(env.hook, tool, {"amount": 5})
    (row,) = env.ledger.list_by_status("parked")
    result = call(env.hook, tool, {"amount": 9999})
    assert result == {"action": "block", "message": REUSED}
    assert first["message"].startswith("PARKED ")
    assert len(env.provider.requests) == 1
    assert env.ledger.get(row.park_id).status == "parked"
    assert len(env.ledger.list_by_status("parked")) == 1


def test_same_id_different_tool_is_refused(env):
    call(env.hook, "mcp__wh__quickbooks__create_payment")
    result = call(env.hook, "mcp__wh__quickbooks__create_bill")
    assert result == {"action": "block", "message": REUSED}
    assert len(env.provider.requests) == 1
    assert len(env.ledger.list_by_status("parked")) == 1


def test_same_id_same_args_replay_keeps_parked_message(env):
    first = call(env.hook, "mcp__wh__quickbooks__create_payment", {"b": 2, "a": 1})
    again = call(env.hook, "mcp__wh__quickbooks__create_payment", {"a": 1, "b": 2})
    assert again == first
    assert len(env.provider.requests) == 1


def test_transport_error_blocks_and_refuses(env):
    def boom(request):
        raise httpx.ConnectError("network down")

    env.provider.intercept = boom
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(env.hook, tool)
    assert result == {"action": "block", "message": UNREACHABLE.format(tool=tool)}
    assert env.ledger.list_by_status("parked") == []
    (row,) = env.ledger.list_by_status("refused")
    assert row.request_id is None and row.note.startswith("withHuman error")


def test_make_gate_bounds_network_waits():
    gate = make_gate(BASE_URL, TOKEN)
    client = gate.client
    assert client.retry_budget == 8
    assert client._http.timeout == httpx.Timeout(10)
    assert client._http.follow_redirects is False


def test_bounded_http_clamps_per_request_timeouts():
    seen = []

    class Recorder(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append(dict(request.extensions["timeout"]))
            return httpx.Response(200, json={})

    http = bounded_http(Recorder())
    http.request("GET", "https://example.test/a", timeout=40.0)          # SDK default per attempt
    http.request("GET", "https://example.test/b", timeout=35.0)          # SDK poll: wait(25) + 10
    http.request("GET", "https://example.test/c", timeout=3.0)           # smaller values are kept
    assert all(v == 10.0 for v in seen[0].values())
    assert all(v == 10.0 for v in seen[1].values())
    assert all(v == 3.0 for v in seen[2].values())


@pytest.mark.parametrize("env_value, expected", [("1", True), ("0", False), (None, False)])
def test_register_passes_crash_flag_and_dispatch_to_executor(tmp_path, monkeypatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("DURABLE_PARK_CRASH_AFTER_CONSUME", raising=False)
    else:
        monkeypatch.setenv("DURABLE_PARK_CRASH_AFTER_CONSUME", env_value)
    seen = {}

    def spy(ledger, gate_factory, dispatch, **kwargs):
        seen.update(dispatch=dispatch, **kwargs)
        return lambda args, **kw: "{}"

    monkeypatch.setattr("durable_park.make_executor", spy)
    ctx = FakeCtx(tmp_path)
    register(ctx)
    assert seen["crash_after_consume"] is expected
    assert seen["dispatch"] == ctx.dispatch_tool


# ---- form check before parking ---------------------------------------------------------------


PAY_RULES = {"type": "object", "required": ["params"], "additionalProperties": False,
             "properties": {"params": {"type": "object", "required": ["customer_ref", "total_amt"],
                                       "properties": {"customer_ref": {"type": "string"},
                                                      "total_amt": {"type": "number"}}}}}
GOOD_PAY = {"params": {"customer_ref": "58", "total_amt": 5}}
CHECK_UNAVAILABLE = ('durable-park: the arguments of "mcp__wh__quickbooks__create_payment" could not be '
                     "checked against the tool's rules; the call was not run and no approval was requested")


def checked_env(env, schema_fn):
    return make_hook(env, form_checker=FormChecker(schema_fn))


def test_invalid_args_blocked_before_parking(env):
    hook = checked_env(env, lambda tool: PAY_RULES)
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(hook, tool, args={"params": {"customer_ref": "58"}})
    assert result["action"] == "block"
    msg = result["message"]
    assert msg.startswith(f'durable-park: "{tool}" was NOT run and no approval was requested')
    assert "total_amt" in msg and f'call "{tool}" again' in msg
    assert env.ledger.list_by_status("parked", "cancelled") == []
    assert env.provider.requests == {}


def test_valid_args_park_as_before(env):
    hook = checked_env(env, lambda tool: PAY_RULES)
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(hook, tool, args=GOOD_PAY)
    (row,) = env.ledger.list_by_status("parked")
    assert result == {"action": "block", "message": park_message(row.park_id, tool)}
    assert len(env.provider.requests) == 1


def test_gateway_context_field_does_not_block(env):
    hook = checked_env(env, lambda tool: PAY_RULES)
    result = call(hook, "mcp__wh__quickbooks__create_payment",
                  args={**GOOD_PAY, "withhuman_context": "paying invoice 1016"})
    assert result["message"].startswith("PARKED P-")


def test_rules_unavailable_fails_closed(env):
    hook = checked_env(env, lambda tool: None)
    result = call(hook, "mcp__wh__quickbooks__create_payment", args=GOOD_PAY)
    assert result == {"action": "block", "message": CHECK_UNAVAILABLE}
    assert env.ledger.list_by_status("parked", "cancelled") == []
    assert env.provider.requests == {}


def test_rules_lookup_crash_fails_closed(env):
    def boom(tool):
        raise RuntimeError("registry moved")
    result = call(checked_env(env, boom), "mcp__wh__quickbooks__create_payment", args=GOOD_PAY)
    assert result == {"action": "block", "message": CHECK_UNAVAILABLE}


def test_only_form_checked_tools_are_form_checked(env):
    def boom(tool):
        raise AssertionError("tools outside form_checked must not be form-checked")
    result = call(checked_env(env, boom), "mcp__wh__quickbooks__search_customers", args={})
    # not form-checked, but still asked: withHuman had no instant answer, so it parks
    assert result["message"].startswith("PARKED P-")
    assert len(env.provider.requests) == 1


def test_fixed_retry_after_invalid_parks_once(env):
    hook = checked_env(env, lambda tool: PAY_RULES)
    tool = "mcp__wh__quickbooks__create_payment"
    first = call(hook, tool, args={"params": {"customer_ref": "58"}}, tool_call_id="c1")
    assert "was NOT run" in first["message"]
    second = call(hook, tool, args=GOOD_PAY, tool_call_id="c2")
    assert second["message"].startswith("PARKED P-")
    assert len(env.ledger.list_by_status("parked")) == 1 and len(env.provider.requests) == 1


def test_register_checks_forms_with_the_registry_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("durable_park.formcheck.registry_schema", lambda tool: PAY_RULES)
    monkeypatch.setattr("durable_park.park_hook.default_session_key", lambda: SESSION_KEY)
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    result = ctx.hooks["pre_tool_call"](tool_name="mcp__wh__quickbooks__create_payment",
                                        args={"params": {}}, session_id="s", tool_call_id="c")
    assert "was NOT run and no approval was requested" in result["message"]


def test_register_starts_selfcheck_next_to_the_ledger(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck",
                        lambda checker, risky, path, on_tick=None: started.append((checker, len(risky), path)) or True)
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    ((checker, n_risky, path),) = started
    assert isinstance(checker, FormChecker) and n_risky == len(DEFAULT_FORM_CHECKED_TOOLS)
    assert path == str(tmp_path / "data" / "formcheck_status.json")


def test_register_without_ledger_does_not_start_selfcheck(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck", lambda *a, **kw: started.append(a) or True)
    register(FakeCtx(tmp_path, {"ledger_path": str(tmp_path)}))
    assert started == []



def test_replay_of_an_already_parked_call_is_not_rechecked(env):
    tool = "mcp__wh__quickbooks__create_payment"
    first = call(checked_env(env, lambda tool: PAY_RULES), tool, args=GOOD_PAY)
    assert first["message"].startswith("PARKED P-")
    # rules vanish (MCP reconnect); the same call id replays: it must still say PARKED, never "not requested"
    again = call(checked_env(env, lambda tool: None), tool, args=GOOD_PAY)
    assert again == first
    assert len(env.provider.requests) == 1


def test_request_id_not_attached_to_a_row_the_sweep_cancelled(env, monkeypatch):
    real = Ledger.set_request_id

    def sweep_got_there_first(self, park_id, request_id):
        assert self.transition(park_id, "parked", "cancelled", note="cancelled by the sweep")
        return real(self, park_id, request_id)

    monkeypatch.setattr(Ledger, "set_request_id", sweep_got_there_first)
    result = call(env.hook, "mcp__wh__quickbooks__create_payment")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.provider.only().status == "cancelled"
    assert env.ledger.list_by_status("parked") == []


# ---- every tool call is asked (spec 4.2) ----------------------------------------------------------

class NotingProvider(FakeProvider):
    """Decides every new request at once with a reviewer note (FakeProvider's ``auto`` carries no note)."""

    def __init__(self, status, note):
        super().__init__(auto=None)
        self.decide_on_create = (status, note)

    def _handle(self, request):
        response = super()._handle(request)
        if request.method == "POST" and response.status_code == 201:
            stored = self.requests[response.json()["id"]]
            self._decide(stored, *self.decide_on_create)
            return httpx.Response(201, json=stored.wire())
        return response


def test_instant_deny_records_refused_with_note(env):
    provider = NotingProvider("denied", "too big")
    hook = make_hook(env, provider=provider)
    tool = "mcp__wh__quickbooks__create_payment"
    result = call(hook, tool)
    assert result == {"action": "block", "message": REFUSED.format(tool=tool, note=": too big")}
    (row,) = env.ledger.list_by_status("refused")
    stored = provider.only()
    assert row.request_id == stored.id and stored.consumed_at is None
    assert row.note == "withHuman denied: refused by withHuman policy: too big"


def test_instant_deny_ledger_note_names_policy_not_a_reviewer(env):
    provider = NotingProvider("denied", "over limit")
    hook = make_hook(env, provider=provider)
    call(hook, "mcp__wh__quickbooks__create_payment")
    (row,) = env.ledger.list_by_status("refused")
    assert row.note == "withHuman denied: refused by withHuman policy: over limit"
    assert "reviewer" not in row.note.lower()


def test_instant_deny_without_note_ledger_says_policy(env):
    hook = make_hook(env, provider=NotingProvider("denied", None))
    call(hook, "web_search", {"query": "x"})
    (row,) = env.ledger.list_by_status("refused")
    assert row.note == "withHuman denied: refused by withHuman policy"


def test_instant_deny_note_is_one_line_and_capped(env):
    hook = make_hook(env, provider=NotingProvider("denied", "a\nb " + "x" * 500))
    result = call(hook, "web_search", {"query": "x"})
    assert "\n" not in result["message"]
    (row,) = env.ledger.list_by_status("refused")
    assert "\n" not in row.note and len(row.note) < 400


def test_instant_deny_without_note_has_no_note(env):
    hook = make_hook(env, provider=NotingProvider("denied", None))
    result = call(hook, "web_search", {"query": "x"})
    assert result == {"action": "block", "message": REFUSED.format(tool="web_search", note="")}


def test_every_tool_gets_a_request_with_server_label(env):
    env.provider.auto = "approved"
    assert call(env.hook, "web_search", {"query": "x"}) is None
    assert call(env.hook, "mcp__github__x", {"a": 1}, tool_call_id="call-2") is None
    servers = {s.body["tool"]: s.body["server"] for s in env.provider.requests.values()}
    assert servers == {"web_search": "hermes", "mcp__github__x": "mcp:github"}
    assert {r.server for r in env.ledger.list_by_status("allowed")} == {"hermes", "mcp:github"}


def test_request_body_is_redacted_and_carries_context(env):
    call(env.hook, "web_search", {"query": "x", "password": "hunter2"})
    (row,) = env.ledger.list_by_status("parked")
    body = env.provider.only().body
    assert body["arguments"] == {"query": "x", "password": "[redacted]"}
    assert body["context"]["args_sha256"] == row.args_digest
    assert "hunter2" in row.args_json  # the ledger keeps the real arguments for the executor


def test_breaker_open_refuses_without_request(env):
    breaker = Breaker(None)
    hook = make_hook(env, breaker=breaker)
    attempts = []

    def boom(request):
        attempts.append(request)
        raise httpx.ConnectError("network down")

    env.provider.intercept = boom
    for i in range(3):
        result = call(hook, "web_search", {"query": "x"}, tool_call_id=f"c{i}")
        assert result == {"action": "block", "message": UNREACHABLE.format(tool="web_search")}
    assert not breaker.allow_attempt()
    env.provider.intercept = None  # withHuman is back, but the breaker is still cooling down
    seen = len(attempts)
    result = call(hook, "web_search", {"query": "x"}, tool_call_id="c3")
    assert result == {"action": "block", "message": UNREACHABLE.format(tool="web_search")}
    assert env.provider.requests == {} and len(attempts) == seen
    row = env.ledger.get_by_call("sess-1", "c3")
    assert row.status == "refused" and row.request_id is None and "breaker" in row.note


def test_breaker_success_after_policy_decisions(env):
    breaker = Breaker(None)
    hook = make_hook(env, breaker=breaker, provider=NotingProvider("denied", None))
    breaker.record_failure()
    breaker.record_failure()
    call(hook, "web_search", {"query": "x"})
    breaker.record_failure()
    assert breaker.allow_attempt()  # the deny was an answer from withHuman: the failure count was reset


def test_foreign_hook_blocks_every_non_exempt_call(env, caplog):
    hook = make_hook(env, foreign_hooks_fn=lambda: ["x.y"])
    assert call(hook, "web_search") == {"action": "block", "message": FOREIGN.format(names="x.y")}
    assert call(hook, "mcp__wh__quickbooks__create_payment", tool_call_id="c2") == {
        "action": "block", "message": FOREIGN.format(names="x.y")}
    assert call(hook, "clarify", tool_call_id="c3") is None
    assert env.provider.requests == {}
    assert env.ledger.get_by_call("sess-1", "call-1") is None
    assert any("x.y" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.parametrize("tool", ["web_search", "terminal", "mcp__github__x", "mcp__wh__quickbooks__create_payment"])
def test_no_session_blocks_every_non_exempt_tool(env, tool):
    hook = make_hook(env, session_key_fn=lambda: "")
    assert call(hook, tool) == {"action": "block", "message": NO_SESSION}
    assert call(hook, "clarify", tool_call_id="c2") is None
    assert env.provider.requests == {}
    assert env.ledger.get_by_call("sess-1", "call-1") is None


def test_reused_call_id_after_allow_is_not_run_again(env):
    env.provider.auto = "approved"
    assert call(env.hook, "web_search", {"query": "x"}) is None
    (row,) = env.ledger.list_by_status("allowed")
    again = call(env.hook, "web_search", {"query": "x"})
    assert again == {"action": "block",
                     "message": f"durable-park: {row.park_id} is already allowed; this call was not run again"}
    assert len(env.provider.requests) == 1


def test_reused_call_id_after_refuse_is_not_asked_again(env):
    hook = make_hook(env, provider=NotingProvider("denied", None))
    call(hook, "web_search", {"query": "x"})
    (row,) = env.ledger.list_by_status("refused")
    again = call(hook, "web_search", {"query": "x"})
    assert again["message"] == f"durable-park: {row.park_id} is already refused; this call was not run again"


def test_unserialisable_arguments_block_and_cancel(env):
    breaker = Breaker(None)
    hook = make_hook(env, breaker=breaker)
    result = call(hook, "web_search", {"query": float("nan")})
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    (row,) = env.ledger.list_by_status("cancelled")
    assert row.request_id is None
    assert env.provider.requests == {}
    # the bad arguments were not counted as a withHuman failure: two more failures do not open the breaker
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow_attempt()


def test_can_pause_is_true(env, monkeypatch):
    seen = []
    real = Gate.check

    def spy(self, call_, **kwargs):
        seen.append(kwargs)
        return real(self, call_, **kwargs)

    monkeypatch.setattr(Gate, "check", spy)
    call(env.hook, "web_search", {"query": "x"})
    assert seen == [{"can_pause": True}]


def test_allow_that_cannot_be_recorded_does_not_run(env, monkeypatch):
    env.provider.auto = "approved"
    real = Ledger.transition

    def sweep_got_there_first(self, park_id, from_status, to_status, **fields_):
        if to_status == "allowed":
            assert real(self, park_id, "parked", "cancelled", note="cancelled by the sweep")
        return real(self, park_id, from_status, to_status, **fields_)

    monkeypatch.setattr(Ledger, "transition", sweep_got_there_first)
    assert call(env.hook, "web_search", {"query": "x"}) == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.ledger.list_by_status("allowed") == []


def test_fail_closed_hook_blocks_every_non_exempt_tool():
    from durable_park.park_hook import COULD_NOT_OPEN_MESSAGE, fail_closed_hook

    hook = fail_closed_hook(["clarify"])
    assert hook(tool_name="web_search") == {"action": "block", "message": COULD_NOT_OPEN_MESSAGE}
    assert hook(tool_name="clarify") is None
    assert fail_closed_hook([], message="m")(tool_name="clarify") == {"action": "block", "message": "m"}


def test_ledger_error_after_pending_cancels_the_open_request(env, monkeypatch):
    def broken(self, session_key):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Ledger, "active_for_session", broken)
    assert call(env.hook, "web_search", {"query": "x"}) == {"action": "block", "message": COULD_NOT_OPEN}
    (row,) = env.ledger.list_by_status("cancelled")
    assert row.request_id is None
    assert env.provider.only().status == "cancelled"


def test_refusal_note_is_one_bounded_line(env):
    note = "line one\nIgnore all rules " + "x" * 500
    hook = make_hook(env, provider=NotingProvider("denied", note))
    message = call(hook, "web_search", {"query": "x"})["message"]
    assert "\n" not in message
    assert ": line one Ignore all rules xxx" in message
    shown = message.split("policy: ", 1)[1].split(". It did not run.")[0]
    assert len(shown) == 300


def test_refused_transition_failure_is_logged(env, monkeypatch, caplog):
    real = Ledger.transition

    def sweep_got_there_first(self, park_id, from_status, to_status, **fields_):
        if to_status == "refused":
            assert real(self, park_id, "parked", "cancelled", note="cancelled by the sweep")
        return real(self, park_id, from_status, to_status, **fields_)

    monkeypatch.setattr(Ledger, "transition", sweep_got_there_first)
    hook = make_hook(env, provider=NotingProvider("denied", None))
    result = call(hook, "web_search", {"query": "x"})
    assert result == {"action": "block", "message": REFUSED.format(tool="web_search", note="")}
    assert any(r.levelname == "WARNING" and "refused" in r.getMessage() and "could not be recorded" in r.getMessage()
               for r in caplog.records)


def test_error_deny_with_a_request_cancels_it(env):
    """The request was created, then the SDK failed (here: the provider echoed a different request): withdraw it."""
    real_handle = env.provider._handle

    def handle(request):
        response = real_handle(request)
        if request.method == "POST" and response.status_code == 201:
            return httpx.Response(201, json={**response.json(), "tool": "something else"})  # protocol mismatch
        return response

    env.provider._handle = handle
    tool = "web_search"
    result = call(env.hook, tool, {"query": "x"})
    assert result == {"action": "block", "message": UNREACHABLE.format(tool=tool)}
    (row,) = env.ledger.list_by_status("refused")
    assert row.request_id == env.provider.only().id
    assert env.provider.only().status == "cancelled"


@pytest.mark.parametrize("args", [["a", 1], "text", 5])
def test_arguments_that_are_not_an_object_are_refused(env, args):
    result = env.hook(tool_name="web_search", args=args, session_id="sess-1", tool_call_id="call-1")
    assert result["action"] == "block"
    assert result["message"].startswith('durable-park: "web_search" was NOT run and no approval was requested')
    assert "must be an object" in result["message"]
    (row,) = env.ledger.list_by_status("cancelled")
    assert row.request_id is None
    assert env.provider.requests == {}


def test_missing_arguments_are_an_empty_object(env):
    result = env.hook(tool_name="web_search", args=None, session_id="sess-1", tool_call_id="call-1")
    assert result["message"].startswith("PARKED P-")
    assert env.provider.only().body["arguments"] == {}


def test_exempt_name_outside_the_defaults_logs_an_error(tmp_path, caplog):
    """Anything in exempt_tools runs with no approval and no ledger row: an extra name is loud (ERROR)."""
    ctx = FakeCtx(tmp_path / "data", {"exempt_tools": ["parked_action_execute", "clarify", "web_search"]})
    with caplog.at_level("WARNING", logger="durable_park"):
        register(ctx)
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("web_search" in m and "no approval" in m for m in errors)
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s", tool_call_id="c") is None
    assert "parked_action_execute" in ctx.tools  # not refused: loaded normally


def test_default_exempt_names_log_no_error(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="durable_park"):
        register(FakeCtx(tmp_path / "data"))
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.parametrize("exempt, extra", [
    (["parked_action_execute", "clarify", "mcp__github__create_issue"], {}),
    (["parked_action_execute", "mcp__wh__quickbooks__search_customers"], {}),
    (["parked_action_execute", "clarify", "custom_pay"], {"form_checked_tools": [r"custom_pay"]}),
    (["parked_action_execute", "clarify", "tool_call"], {}),  # Tool Search's tool_call runs hidden tools: never exempt
])
def test_exempt_mcp_or_form_checked_name_is_refused_as_a_bad_setting(tmp_path, caplog, exempt, extra):
    ctx = FakeCtx(tmp_path / "data", {"exempt_tools": exempt, **extra})
    with caplog.at_level("ERROR", logger="durable_park"):
        register(ctx)  # does not raise
    _assert_fails_closed_with_default_exempt(ctx)
    assert ctx.tools == {}
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name=exempt[-1], args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": COULD_NOT_OPEN}
    assert any("exempt_tools" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_fail_closed_modes_let_only_the_read_only_bridge_tools_through(tmp_path):
    """Ledger unusable: every call is refused except the default exempt tools, which now include the two
    read-only Tool Search catalog tools; tool_call is refused."""
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path)})
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    for name in ("tool_search", "tool_describe", "clarify", "parked_action_execute"):
        assert hook(tool_name=name, args={}, session_id="s", tool_call_id="c") is None
    assert hook(tool_name="tool_call", args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": COULD_NOT_OPEN}


def test_default_exempt_setting_logs_no_error_for_the_bridge_read_tools(tmp_path, caplog):
    ctx = FakeCtx(tmp_path / "data", {"exempt_tools": ["parked_action_execute", "clarify", "tool_search",
                                                       "tool_describe"]})
    with caplog.at_level("WARNING", logger="durable_park"):
        register(ctx)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "parked_action_execute" in ctx.tools


def test_exempt_default_form_checked_tool_is_refused(tmp_path):
    """A name the default form-checked patterns match is refused even without the mcp__ check (belt and braces)."""
    ctx = FakeCtx(tmp_path / "data", {"exempt_tools": ["parked_action_execute",
                                                       "mcp__wh__quickbooks__create_payment"]})
    register(ctx)
    _assert_fails_closed_with_default_exempt(ctx)


def test_missing_executor_in_exempt_tools_still_warns(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="durable_park"):
        register(FakeCtx(tmp_path / "data", {"exempt_tools": ["clarify"]}))
    assert any(r.levelname == "WARNING" and "parked_action_execute" in r.getMessage() for r in caplog.records)


def write_policy_report(path, *, ok=True, age_minutes=0, problems=()):
    at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    path.write_text(json.dumps({"ok": ok, "checked_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "problems": list(problems), "allowed": []}))


UNREADABLE_CHECKED_AT = "unreadable checked_at"
FUTURE_CHECKED_AT = "checked_at in the future"


def test_policy_problem(tmp_path):
    path = tmp_path / "status.json"
    now = datetime.now(timezone.utc)
    assert policy_problem(str(path), now) == "no tool policy report"  # missing
    path.write_text("{not json")
    assert policy_problem(str(path), now) == "no tool policy report"
    path.write_text("[1]")
    assert policy_problem(str(path), now) == "no tool policy report"
    write_policy_report(path, ok=False, problems=["terminal is open", "wh missing"])
    assert policy_problem(str(path), now) == "terminal is open; wh missing"
    write_policy_report(path, ok=True, age_minutes=16)
    assert policy_problem(str(path), now) == "stale"
    write_policy_report(path, ok=True, age_minutes=14)
    assert policy_problem(str(path), now) is None
    write_policy_report(path, ok=True, age_minutes=-30)  # a clock far in the future is not "fresh"
    assert policy_problem(str(path), now) == FUTURE_CHECKED_AT
    path.write_text(json.dumps({"ok": True, "checked_at": "garbage"}))
    assert policy_problem(str(path), now) == UNREADABLE_CHECKED_AT
    path.write_text(json.dumps({"ok": True}))  # no checked_at at all
    assert policy_problem(str(path), now) == UNREADABLE_CHECKED_AT
    path.write_text(json.dumps({"ok": "yes", "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    assert policy_problem(str(path), now) is not None  # only a real true passes


def test_policy_problem_is_judged_against_process_start_not_wall_clock(tmp_path):
    path = tmp_path / "status.json"
    start = datetime.now(timezone.utc) - timedelta(hours=3)  # a long-lived gateway
    now = datetime.now(timezone.utc)
    write_policy_report(path, age_minutes=180)  # written at the gateway start, 3 hours ago
    assert policy_problem(str(path), now, process_start=start) is None  # uptime does not matter
    write_policy_report(path, age_minutes=180 + 14)  # written 14 minutes before this process started
    assert policy_problem(str(path), now, process_start=start) is None
    write_policy_report(path, age_minutes=180 + 16)  # 16 minutes before: the start check did not run for it
    assert policy_problem(str(path), now, process_start=start) == "stale"
    write_policy_report(path, age_minutes=60)  # written after this process started is fine
    assert policy_problem(str(path), now, process_start=start) is None
    write_policy_report(path, age_minutes=-30)  # future-dated beyond a minute is its own verdict, not "stale"
    assert policy_problem(str(path), now, process_start=start) == FUTURE_CHECKED_AT


def test_policy_problem_defaults_to_this_process_start(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(durable_park, "PROCESS_START", now - timedelta(minutes=18))
    write_policy_report(path, age_minutes=18)  # the 07:57 case: 18 minutes after the gateway start
    assert policy_problem(str(path), now) is None
    monkeypatch.setattr(durable_park, "PROCESS_START", now)
    write_policy_report(path, age_minutes=16)
    assert policy_problem(str(path), now) == "stale"


def test_policy_message_text():
    assert POLICY_MESSAGE == ("durable-park: the tool policy check did not pass at gateway start; tool calls are "
                              "refused until an admin fixes it.")


@pytest.mark.parametrize("report", [None, "failed", "stale"])
def test_register_fails_closed_on_policy_problem(tmp_path, monkeypatch, report):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    data = tmp_path / "data"
    data.mkdir()
    if report == "failed":
        write_policy_report(data / "tool_policy_status.json", ok=False, problems=["x"])
    elif report == "stale":
        write_policy_report(data / "tool_policy_status.json", age_minutes=20)
    ctx = FakeCtx(data)  # default location: next to the ledger
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    for tool in ("web_search", "mcp__wh__quickbooks__search_customers"):
        assert hook(tool_name=tool, args={}, session_id="s", tool_call_id="c") == {
            "action": "block", "message": POLICY_MESSAGE}
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None
    assert hook(tool_name="parked_action_execute", args={}, session_id="s", tool_call_id="c") is None
    assert "pre_gateway_dispatch" in ctx.hooks  # wakes are dropped, never reach the agent
    assert set(ctx.tools) == {"parked_action_execute"}
    reply = json.loads(ctx.tools["parked_action_execute"].handler({"park_id": "P-1"}))
    assert reply["status"] == "refused" and reply["message"] == POLICY_MESSAGE
    assert not (data / "ledger.db").exists()  # nothing opened, nothing consumed


def _normal_hook_reached(ctx, fake_withhuman):
    result = ctx.hooks["pre_tool_call"](tool_name="web_search", args={"q": 1}, session_id="s", tool_call_id="c")
    assert result["message"].startswith("PARKED P-")  # the normal hook asked withHuman and parked the call
    assert len(fake_withhuman.provider.requests) == 1


def test_register_passes_with_a_fresh_passing_report(tmp_path, monkeypatch, with_session, fake_withhuman):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    report = tmp_path / "elsewhere.json"
    write_policy_report(report)
    ctx = FakeCtx(tmp_path / "data", {"tool_policy_status": str(report), "token_file": fake_withhuman.token})
    register(ctx)
    _normal_hook_reached(ctx, fake_withhuman)
    assert "tool_policy_status" in ctx.asked


def test_register_uses_the_default_status_path(tmp_path, monkeypatch, with_session, fake_withhuman):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    data = tmp_path / "data"
    data.mkdir()
    write_policy_report(data / "tool_policy_status.json")  # next to the ledger, no setting given
    ctx = FakeCtx(data, {"token_file": fake_withhuman.token})
    register(ctx)
    _normal_hook_reached(ctx, fake_withhuman)


def test_register_bad_policy_setting_does_not_fail_open(tmp_path):
    ctx = FakeCtx(tmp_path / "data", {"tool_policy_status": 5})
    register(ctx)
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s",
                                      tool_call_id="c") is not None


def test_policy_failure_hook_ignores_the_configured_exempt_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    data = tmp_path / "data"
    data.mkdir()
    write_policy_report(data / "tool_policy_status.json", ok=False, problems=["x"])
    ctx = FakeCtx(data, {"exempt_tools": ["parked_action_execute", "clarify", "web_search"]})
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="web_search", args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": POLICY_MESSAGE}  # the message says every call is refused: it must be true
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None


def _load_verdict(directory):
    return json.loads((directory / "tool_policy_load.json").read_text())


@pytest.mark.parametrize("report,problem", [(None, "no tool policy report"), ("failed", "x"), ("stale", "stale")])
def test_register_writes_its_load_verdict_when_the_policy_fails(tmp_path, monkeypatch, report, problem):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    data = tmp_path / "data"
    data.mkdir()
    if report == "failed":
        write_policy_report(data / "tool_policy_status.json", ok=False, problems=["x"])
    elif report == "stale":
        write_policy_report(data / "tool_policy_status.json", age_minutes=20)
    register(FakeCtx(data))
    verdict = _load_verdict(data)
    assert verdict["ok"] is False and verdict["problem"] == problem
    assert verdict["checked_at"].endswith("Z") and len(verdict["checked_at"]) == 20
    assert [p.name for p in data.iterdir() if p.name.startswith(".")] == []  # no temp file left


def test_register_writes_an_ok_load_verdict_when_the_policy_passes(tmp_path, monkeypatch, with_session, fake_withhuman):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    data = tmp_path / "data"
    data.mkdir()
    write_policy_report(data / "tool_policy_status.json")
    register(FakeCtx(data, {"token_file": fake_withhuman.token}))
    verdict = _load_verdict(data)
    assert verdict["ok"] is True and verdict["problem"] is None


def test_the_load_verdict_write_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    data = tmp_path / "data"
    data.mkdir()
    (data / "tool_policy_load.json").mkdir()  # cannot be replaced by a file
    ctx = FakeCtx(data)
    register(ctx)  # must not raise, and must still fail closed
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s", tool_call_id="c")["action"] == "block"


def test_is_gateway_process_detects_gateway_run_in_argv(monkeypatch):
    real = REAL_IS_GATEWAY
    for argv, expected in [
        (["hermes", "gateway", "run"], True),
        (["/x/python", "-m", "hermes_cli.main", "-p", "leaddesk", "gateway", "run", "--replace"], True),
        (["hermes", "cron", "tick"], False),
        (["hermes", "config", "set", "gateway", "x"], False),
        (["hermes", "gateway", "status"], False),
        (["hermes", "gateway"], False),
        ([], False),
    ]:
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")  # inherited env must not make a cron process the gateway
        assert real() is expected, argv


@pytest.mark.parametrize("gateway", [True, False])
def test_only_the_gateway_writes_the_load_verdict(tmp_path, monkeypatch, caplog, gateway):
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: gateway)
    monkeypatch.setattr(durable_park, "_logged_not_gateway", False)
    data = tmp_path / "data"
    data.mkdir()
    ctx = FakeCtx(data)
    with caplog.at_level("INFO", logger="durable_park"):
        register(ctx)
        register(FakeCtx(data))
    assert (data / "tool_policy_load.json").exists() is gateway
    # either way this process still fails closed
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s", tool_call_id="c")["action"] == "block"
    msgs = [r.getMessage() for r in caplog.records
            if r.getMessage() == "tool policy: load verdict not written (not the gateway process)"]
    assert len(msgs) == (0 if gateway else 1)  # once per process


STALE_SIDE_PROCESS = ("durable-park: tool policy report predates this side process (expected for the cron sweep "
                      "and CLI runs); refusing every tool call in this process")
STALE_ERROR = "durable-park tool policy check failed (stale); refusing every tool call"


def _durable_park_records(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "durable_park"]


def _assert_refuses_everything(ctx):
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="web_search", args={}, session_id="s", tool_call_id="c") == {
        "action": "block", "message": POLICY_MESSAGE}
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None
    assert "pre_gateway_dispatch" in ctx.hooks
    assert set(ctx.tools) == {"parked_action_execute"}
    reply = json.loads(ctx.tools["parked_action_execute"].handler({"park_id": "P-1"}))
    assert reply["status"] == "refused" and reply["message"] == POLICY_MESSAGE


def test_stale_report_in_a_side_process_logs_info_not_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: False)
    monkeypatch.setattr(durable_park, "_logged_not_gateway", False)
    monkeypatch.setattr(durable_park, "_logged_stale_side_process", False)
    # Without Hermes installed the version check warns; that is not what this test is about.
    monkeypatch.setattr(durable_park.compat, "check_hermes_version", lambda log: None)
    data = tmp_path / "data"
    data.mkdir()
    write_policy_report(data / "tool_policy_status.json", age_minutes=20)  # older than 15 min before this process
    ctx = FakeCtx(data)
    with caplog.at_level("DEBUG", logger="durable_park"):
        register(ctx)
        register(FakeCtx(data))  # once per process
    records = _durable_park_records(caplog)
    assert records.count(("INFO", STALE_SIDE_PROCESS)) == 1
    assert not [r for r in records if r[0] in ("WARNING", "ERROR", "CRITICAL")]  # errors.log gets WARNING and up
    assert not any("tool policy check failed" in m for _, m in records)
    _assert_refuses_everything(ctx)  # behaviour is unchanged: still fails closed
    assert not (data / "tool_policy_load.json").exists()  # the gateway's verdict (and the sweep alert) untouched


def test_stale_report_in_the_gateway_still_logs_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: True)
    monkeypatch.setattr(durable_park, "_logged_stale_side_process", False)
    data = tmp_path / "data"
    data.mkdir()
    write_policy_report(data / "tool_policy_status.json", age_minutes=20)
    ctx = FakeCtx(data)
    with caplog.at_level("DEBUG", logger="durable_park"):
        register(ctx)
    records = _durable_park_records(caplog)
    assert ("ERROR", STALE_ERROR) in records
    assert not any(m == STALE_SIDE_PROCESS for _, m in records)
    _assert_refuses_everything(ctx)
    verdict = _load_verdict(data)  # the gateway still records the verdict the sweep alerts on
    assert verdict["ok"] is False and verdict["problem"] == "stale"


@pytest.mark.parametrize("report,problem", [(None, "no tool policy report"), ("failed", "x")])
def test_other_policy_problems_in_a_side_process_still_log_error(tmp_path, monkeypatch, caplog, report, problem):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: False)
    monkeypatch.setattr(durable_park, "_logged_not_gateway", False)
    monkeypatch.setattr(durable_park, "_logged_stale_side_process", False)
    data = tmp_path / "data"
    data.mkdir()
    if report == "failed":
        write_policy_report(data / "tool_policy_status.json", ok=False, problems=["x"])
    ctx = FakeCtx(data)
    with caplog.at_level("DEBUG", logger="durable_park"):
        register(ctx)
    records = _durable_park_records(caplog)
    assert ("ERROR", f"durable-park tool policy check failed ({problem}); refusing every tool call") in records
    assert not any(m == STALE_SIDE_PROCESS for _, m in records)
    _assert_refuses_everything(ctx)


@pytest.mark.parametrize("anomaly,problem", [("garbage", UNREADABLE_CHECKED_AT), ("future", FUTURE_CHECKED_AT)])
@pytest.mark.parametrize("gateway", [False, True])
def test_a_report_anomaly_logs_error_in_every_process(tmp_path, monkeypatch, caplog, anomaly, problem, gateway):
    """Only a report that is merely too old may be quiet in a side process: an unparsable or future-dated
    ``checked_at`` (clock skew, tampering) is a real anomaly and stays an ERROR everywhere."""
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: gateway)
    monkeypatch.setattr(durable_park, "_logged_not_gateway", False)
    monkeypatch.setattr(durable_park, "_logged_stale_side_process", False)
    data = tmp_path / "data"
    data.mkdir()
    report = data / "tool_policy_status.json"
    if anomaly == "future":
        write_policy_report(report, age_minutes=-30)
    else:
        report.write_text(json.dumps({"ok": True, "checked_at": "garbage"}))
    ctx = FakeCtx(data)
    with caplog.at_level("DEBUG", logger="durable_park"):
        register(ctx)
    records = _durable_park_records(caplog)
    assert ("ERROR", f"durable-park tool policy check failed ({problem}); refusing every tool call") in records
    assert not any(m == STALE_SIDE_PROCESS for _, m in records)
    _assert_refuses_everything(ctx)


def test_a_non_gateway_process_does_not_overwrite_the_gateways_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: False)
    data = tmp_path / "data"
    data.mkdir()
    (data / "tool_policy_load.json").write_text('{"ok": true, "problem": null, "checked_at": "2026-10-21T07:00:00Z"}')
    register(FakeCtx(data))
    assert _load_verdict(data)["ok"] is True


# --- plugin load proof (plugin_load.json): written last on every path that ends with a pre_tool_call hook ----

def _proof(directory):
    return json.loads((directory / "plugin_load.json").read_text())


def _assert_proof(directory, mode, ctx):
    proof = _proof(directory)
    assert set(proof) == {"pid", "written_at", "mode", "hooks"}
    assert proof["pid"] == os.getpid()
    assert proof["mode"] == mode
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", proof["written_at"])
    assert proof["hooks"] == list(ctx.hook_lists) and "pre_tool_call" in proof["hooks"]
    assert [p.name for p in directory.iterdir() if p.name.startswith(".plugin-load-")] == []  # no temp file left


def test_register_writes_the_load_proof_when_asking(tmp_path, with_session, fake_withhuman):
    data = tmp_path / "data"
    ctx = FakeCtx(data, {"token_file": fake_withhuman.token})
    register(ctx)
    _assert_proof(data, "asking", ctx)
    assert set(_proof(data)["hooks"]) == {"pre_gateway_dispatch", "pre_tool_call"}


def test_register_writes_the_load_proof_when_the_policy_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    data = tmp_path / "data"
    ctx = FakeCtx(data)
    register(ctx)
    _assert_proof(data, "refusing-policy", ctx)


def test_register_writes_the_load_proof_when_the_ledger_is_unavailable(tmp_path):
    data = tmp_path / "data"
    (data / "ledger.db").mkdir(parents=True)  # a directory, not a database file
    ctx = FakeCtx(tmp_path, {"ledger_path": str(data / "ledger.db")})
    register(ctx)
    assert ctx.hooks["pre_tool_call"](tool_name="web_search", args={}, session_id="s", tool_call_id="c")["action"] == "block"
    _assert_proof(data, "refusing-error", ctx)


def test_register_writes_the_load_proof_on_the_except_path(tmp_path):
    data = tmp_path / "data"
    ctx = FakeCtx(data, {"exempt_tools": 5})  # BadSetting -> the refusing hook
    register(ctx)
    _assert_proof(data, "refusing-error", ctx)


def test_register_writes_the_load_proof_when_a_setting_lookup_raises(tmp_path):
    data = tmp_path / "data"
    ctx = FakeCtx(data)
    real_get = ctx.get_config
    calls = []

    def flaky(key, default=None):
        calls.append(key)
        if key == "risky_tools":  # the very first lookup inside _register
            raise RuntimeError("boom")
        return real_get(key, default)

    ctx.get_config = flaky
    register(ctx)
    _assert_proof(data, "refusing-error", ctx)


def test_the_load_proof_is_written_after_the_hook_is_registered(tmp_path, monkeypatch, with_session, fake_withhuman):
    seen = []
    real = durable_park.write_plugin_load
    monkeypatch.setattr(durable_park, "write_plugin_load",
                        lambda path, mode, hooks: seen.append((mode, list(hooks))) or real(path, mode, hooks))
    ctx = FakeCtx(tmp_path / "data", {"token_file": fake_withhuman.token})
    register(ctx)
    assert len(seen) == 1 and "pre_tool_call" in seen[0][1] and "pre_tool_call" in ctx.hooks


@pytest.mark.parametrize("gateway", [True, False])
def test_only_the_gateway_writes_the_load_proof(tmp_path, monkeypatch, gateway):
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: gateway)
    data = tmp_path / "data"
    register(FakeCtx(data))
    assert (data / "plugin_load.json").exists() is gateway


def test_a_failed_load_proof_write_is_logged_not_raised(tmp_path, caplog):
    data = tmp_path / "data"
    data.mkdir()
    (data / "plugin_load.json").mkdir()  # cannot be replaced by a file
    ctx = FakeCtx(data)
    with caplog.at_level("WARNING", logger="durable_park"):
        register(ctx)  # must not raise
    assert "pre_tool_call" in ctx.hooks
    assert any("could not write plugin_load.json" in r.getMessage() for r in caplog.records)


class NoopHookCtx(FakeCtx):
    """What Hermes gives an abandoned load: register_hook silently does nothing."""

    def register_hook(self, name, callback):
        pass


@pytest.mark.parametrize("config", [{}, {"exempt_tools": 5}], ids=["asking", "except-path"])
def test_no_load_proof_when_the_load_was_abandoned(tmp_path, config, with_session, fake_withhuman, caplog):
    # Hermes run_with_load_deadline (hermes_cli/plugins_loader.py:142-148) set ctx._load_abandoned after the
    # deadline; register() still ran to its end, but its registrations were ignored.
    data = tmp_path / "data"
    ctx = FakeCtx(data, {"token_file": fake_withhuman.token, **config})
    ctx._load_abandoned = True
    with caplog.at_level("WARNING", logger="durable_park"):
        register(ctx)
    assert not (data / "plugin_load.json").exists()
    assert any("not writing plugin_load.json" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("config", [{}, {"exempt_tools": 5}], ids=["asking", "except-path"])
def test_no_load_proof_when_our_hook_never_reached_the_registry(tmp_path, config, with_session, fake_withhuman):
    data = tmp_path / "data"
    ctx = NoopHookCtx(data, {"token_file": fake_withhuman.token, **config})
    register(ctx)
    assert not (data / "plugin_load.json").exists()


def test_no_load_proof_when_the_hook_registry_is_unreadable(tmp_path, with_session, fake_withhuman):
    data = tmp_path / "data"
    register(FakeCtx(data, {"token_file": fake_withhuman.token}, manager=False))
    assert not (data / "plugin_load.json").exists()  # fail closed: nothing proves the hook is live


def test_no_load_proof_when_only_a_foreign_hook_is_registered(tmp_path, with_session, fake_withhuman):
    data = tmp_path / "data"
    ctx = NoopHookCtx(data, {"token_file": fake_withhuman.token})
    ctx.hook_lists["pre_tool_call"] = [foreign_hook]  # someone else's hook is there, ours is not
    register(ctx)
    assert not (data / "plugin_load.json").exists()


# ---- lifting a nested withhuman_context --------------------------------------------------------------

GW_TOOL = "mcp__wh__quickbooks__create_payment"
GW_SCHEMA = {"type": "object", "properties": {"params": {"type": "object"}, "withhuman_context": {"type": "string"}}}
NESTED = {"params": {"customer_ref": "58", "withhuman_context": "Find customer 58"}}
LIFTED = {"params": {"customer_ref": "58"}, "withhuman_context": "Find customer 58"}


def lifting_hook(env, schema_fn=lambda tool: GW_SCHEMA, **kw):
    return make_hook(env, schema_fn=schema_fn, **kw)


def test_allowed_gateway_call_with_nested_note_is_modified_to_the_lifted_args(env):
    env.provider.auto = "approved"
    hook = lifting_hook(env)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) == {"action": "modify", "args": LIFTED}
    assert env.provider.only().body["arguments"] == LIFTED  # withHuman was asked about what will run
    (row,) = env.ledger.list_by_status("allowed")
    assert json.loads(row.args_json) == LIFTED


def test_allowed_call_with_nothing_to_lift_returns_none(env):
    env.provider.auto = "approved"
    hook = lifting_hook(env, schema_fn=lambda tool: GW_SCHEMA if tool == GW_TOOL else {"type": "object"})
    assert call(hook, GW_TOOL, args={"params": {"customer_ref": "58"}, "withhuman_context": "top"}) is None
    assert call(hook, "web_search", args={"q": {"withhuman_context": "x"}}, tool_call_id="call-2") is None
    assert env.provider.requests and len(env.ledger.list_by_status("allowed")) == 2


def test_parked_call_stores_the_lifted_args_and_shows_the_note_to_the_approver(env):
    hook = lifting_hook(env)
    result = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    (row,) = env.ledger.list_by_status("parked")
    assert result == {"action": "block", "message": park_message(row.park_id, GW_TOOL)}
    stored_args = json.loads(row.args_json)
    assert stored_args == LIFTED and "withhuman_context" not in stored_args["params"]
    body = env.provider.only().body
    assert body["agent_reasoning"] == "Find customer 58"
    assert body["arguments"] == LIFTED
    assert build_call(row).reasoning == "Find customer 58"  # the executor replays the same body


def test_replay_of_the_same_nested_call_is_the_same_call(env):
    hook = lifting_hook(env)
    first = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    second = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    assert first == second and first["message"].startswith("PARKED P-")
    assert len(env.ledger.list_by_status("parked")) == 1 and len(env.provider.requests) == 1


def test_a_different_call_on_the_same_id_is_still_refused_after_a_lift(env):
    hook = lifting_hook(env)
    call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    other = {"params": {"customer_ref": "99", "withhuman_context": "Find customer 58"}}
    assert call(hook, GW_TOOL, args=other)["message"].startswith("durable-park: this tool call id was already used")


def test_failing_schema_lookup_uses_the_original_args(env):
    def broken(tool):
        raise RuntimeError("registry gone")

    hook = lifting_hook(env, schema_fn=broken)
    result = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    (row,) = env.ledger.list_by_status("parked")
    assert result == {"action": "block", "message": park_message(row.park_id, GW_TOOL)}
    assert json.loads(row.args_json) == NESTED
    assert env.provider.only().body["arguments"] == NESTED


def test_failing_schema_lookup_on_an_allowed_call_changes_nothing(env):
    env.provider.auto = "approved"

    def broken(tool):
        raise RuntimeError("registry gone")

    assert call(lifting_hook(env, schema_fn=broken), GW_TOOL, args=copy.deepcopy(NESTED)) is None


def test_lift_never_runs_for_exempt_tools(env):
    seen = []
    hook = lifting_hook(env, schema_fn=lambda tool: seen.append(tool) or GW_SCHEMA)
    assert call(hook, "clarify", args=copy.deepcopy(NESTED)) is None
    assert seen == [] and env.provider.requests == {}


def test_hermes_merge_of_the_modify_equals_the_lifted_args(env):
    """Hermes shallow-merges ``result["args"]`` into the original args (it can add or replace a key, never delete):
    the merge must be exactly the lifted call, with two nested dicts and a dropped non-string value."""
    env.provider.auto = "approved"
    original = {"zeta": {"k": 1, "withhuman_context": "from zeta"}, "alpha": {"withhuman_context": 7, "j": 2},
                "plain": {"x": [1, 2]}, "n": 5}
    expected = {"zeta": {"k": 1}, "alpha": {"j": 2}, "plain": {"x": [1, 2]}, "n": 5,
                "withhuman_context": "from zeta"}
    result = call(lifting_hook(env), GW_TOOL, args=copy.deepcopy(original))
    assert result["action"] == "modify"
    assert {**original, **result["args"]} == expected == result["args"]
    assert env.provider.only().body["arguments"] == expected


def test_default_schema_lookup_is_resolved_when_called(env, monkeypatch):
    env.provider.auto = "approved"
    seen = []
    monkeypatch.setattr("durable_park.formcheck.registry_schema", lambda tool: seen.append(tool) or GW_SCHEMA)
    assert call(make_hook(env), GW_TOOL, args=copy.deepcopy(NESTED)) == {"action": "modify", "args": LIFTED}
    assert GW_TOOL in seen  # (the form check reads the same lookup)


def test_default_hook_does_not_log_a_lift_failure(env, caplog):
    caplog.set_level(logging.WARNING, logger="durable_park")
    assert call(env.hook, "web_search", args={"query": "x"})["message"].startswith("PARKED P-")
    assert [r for r in caplog.records if "could not lift" in r.getMessage()] == []


def test_lift_failure_warning_has_no_note_and_no_argument_values(env, caplog):
    def broken(tool):
        raise RuntimeError("secret-detail-7788 " + NESTED["params"]["withhuman_context"])

    caplog.set_level(logging.DEBUG, logger="durable_park")
    args = {"params": {"customer_ref": "cust-ref-9911", "withhuman_context": "private note 4455"}}
    assert call(lifting_hook(env, schema_fn=broken), GW_TOOL, args=args)["message"].startswith("PARKED P-")
    warnings = [r.getMessage() for r in caplog.records if "could not lift" in r.getMessage()]
    assert warnings == [f"could not lift withhuman_context for tool={GW_TOOL} (RuntimeError)"]
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("private note 4455", "cust-ref-9911", "secret-detail-7788", "Find customer 58"):
        assert secret not in everything


DECLARES = {"type": "object", "properties": {
    "params": {"type": "object", "properties": {"withhuman_context": {"type": "string"}}},
    "withhuman_context": {"type": "string"}}}
TEXT_ARGS = {"params": '{"customer_ref": "58", "withhuman_context": "Find customer 58"}'}


def status_hook(env, schema_fn=lambda tool: GW_SCHEMA, **kw):
    return lifting_hook(env, schema_fn=schema_fn, lift_status_path=str(env.tmp_path / "lift_status.json"), **kw)


def recorded(env):
    path = env.tmp_path / "lift_status.json"
    if not path.exists():
        return set()
    return {(e["tool"], e["kind"], e["key"]) for e in json.loads(path.read_text())["entries"]}


def json_coerce(tool, args):
    """Stands in for Hermes's type fixing: a JSON string where an object belongs becomes the object."""
    return {k: json.loads(v) if isinstance(v, str) and v.startswith("{") else v for k, v in args.items()}


@pytest.mark.parametrize("params_rules", [
    {"$ref": "https://example.com/params.json"},
    {"$ref": "#/nowhere"},
    {"patternProperties": {"([": {"type": "string"}}},
    {"allOf": "broken"},
])
def test_rules_the_lift_cannot_judge_still_lift_and_report_nothing(env, params_rules):
    env.provider.auto = "approved"
    schema = {"type": "object", "properties": {"params": params_rules, "withhuman_context": {"type": "string"}}}
    hook = status_hook(env, schema_fn=lambda tool: schema)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) == {"action": "modify", "args": LIFTED}
    assert recorded(env) == set()


def test_a_key_whose_rules_declare_the_field_is_left_alone_and_recorded(env):
    env.provider.auto = "approved"
    hook = status_hook(env, schema_fn=lambda tool: DECLARES)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None  # nothing lifted, the server owns that field
    assert recorded(env) == {(GW_TOOL, "declared", "params")}
    assert "Find customer 58" not in (env.tmp_path / "lift_status.json").read_text()
    assert env.provider.only().body["arguments"] == NESTED


def test_text_form_args_are_coerced_then_lifted_and_modify_carries_them(env):
    env.provider.auto = "approved"
    hook = status_hook(env, coerce_fn=json_coerce)
    original = copy.deepcopy(TEXT_ARGS)
    result = call(hook, GW_TOOL, args=original)
    assert result == {"action": "modify", "args": LIFTED}
    assert original == TEXT_ARGS  # the caller's arguments are untouched
    assert env.provider.only().body["arguments"] == LIFTED
    assert env.provider.only().body["agent_reasoning"] == "Find customer 58"
    assert recorded(env) == set()
    (row,) = env.ledger.list_by_status("allowed")
    assert json.loads(row.args_json) == LIFTED


def test_text_form_args_park_with_the_coerced_lifted_args(env):
    hook = status_hook(env, coerce_fn=json_coerce)
    first = call(hook, GW_TOOL, args=copy.deepcopy(TEXT_ARGS))
    again = call(hook, GW_TOOL, args=copy.deepcopy(TEXT_ARGS))  # a replay is the same call
    assert first == again and first["message"].startswith("PARKED P-")
    (row,) = env.ledger.list_by_status("parked")
    assert json.loads(row.args_json) == LIFTED
    assert len(env.provider.requests) == 1


def test_no_coercion_runs_when_no_string_holds_a_note(env):
    env.provider.auto = "approved"
    seen = []
    hook = status_hook(env, coerce_fn=lambda tool, args: seen.append(tool) or args)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) == {"action": "modify", "args": LIFTED}
    assert call(hook, GW_TOOL, args={"params": '{"a": 1}', "note": "withhuman_context"}, tool_call_id="call-2") is None
    assert seen == []


def test_coercion_that_raises_is_recorded_and_the_original_args_are_used(env):
    def broken(tool, args):
        raise RuntimeError("hermes moved")

    hook = status_hook(env, coerce_fn=broken)
    result = call(hook, GW_TOOL, args=copy.deepcopy(TEXT_ARGS))
    (row,) = env.ledger.list_by_status("parked")
    assert result == {"action": "block", "message": park_message(row.park_id, GW_TOOL)}
    assert json.loads(row.args_json) == TEXT_ARGS
    assert recorded(env) == {(GW_TOOL, "args_unreadable", "params"),
                             (GW_TOOL, "note_left_inside", "params.withhuman_context")}  # the text still holds it


def test_coercion_that_returns_a_non_object_counts_as_unreadable(env):
    env.provider.auto = "approved"
    hook = status_hook(env, coerce_fn=lambda tool, args: "nonsense")
    assert call(hook, GW_TOOL, args=copy.deepcopy(TEXT_ARGS)) is None
    assert recorded(env) == {(GW_TOOL, "args_unreadable", "params"),
                             (GW_TOOL, "note_left_inside", "params.withhuman_context")}


def test_schema_lookup_failure_is_recorded(env):
    def broken(tool):
        raise RuntimeError("registry gone")

    hook = status_hook(env, schema_fn=broken)
    result = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    assert result["message"].startswith("PARKED P-")
    assert recorded(env) == {(GW_TOOL, "schema_unavailable", ""),
                             (GW_TOOL, "note_left_inside", "params.withhuman_context")}  # the walk still ran


def test_any_other_error_in_the_lift_step_is_recorded_and_the_original_args_are_used(env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr("durable_park.park_hook.lift_gateway_context", boom)
    env.provider.auto = "approved"
    assert call(status_hook(env), GW_TOOL, args=copy.deepcopy(NESTED)) is None
    assert recorded(env) == {(GW_TOOL, "lift_error", "")}
    assert env.provider.only().body["arguments"] == NESTED


def test_a_note_still_inside_after_the_lift_is_recorded(env):
    """``params`` declares its own field (left alone, recorded as declared); ``extra`` is a JSON string the type
    fixing did not turn into an object, so its note is still inside: the self-check reports it."""
    env.provider.auto = "approved"
    schema = {"properties": {"params": DECLARES["properties"]["params"], "extra": {"type": "string"},
                             "withhuman_context": {"type": "string"}}}
    hook = status_hook(env, schema_fn=lambda tool: schema, coerce_fn=lambda tool, args: args)
    args = {"params": {"withhuman_context": "owned by the server"}, "extra": '{"withhuman_context": "stuck"}'}
    assert call(hook, GW_TOOL, args=args) is None
    assert recorded(env) == {(GW_TOOL, "declared", "params"), (GW_TOOL, "args_unreadable", "extra"),
                             (GW_TOOL, "note_left_inside", "extra.withhuman_context")}
    assert "stuck" not in (env.tmp_path / "lift_status.json").read_text()


def test_a_declared_key_is_not_also_reported_as_left_inside(env):
    env.provider.auto = "approved"
    call(status_hook(env, schema_fn=lambda tool: DECLARES), GW_TOOL, args=copy.deepcopy(NESTED))
    assert recorded(env) == {(GW_TOOL, "declared", "params")}


def test_problems_never_log_values_and_repeat_calls_do_not_duplicate_entries(env, caplog, monkeypatch):
    env.provider.auto = "approved"
    caplog.set_level(logging.DEBUG, logger="durable_park")
    monkeypatch.setattr("durable_park.gatecall.LIFT_REWRITE_SECONDS", 0)
    hook = status_hook(env, schema_fn=lambda tool: DECLARES)
    args = {"params": {"customer_ref": "cust-ref-9911", "withhuman_context": "private note 4455"}}
    call(hook, GW_TOOL, args=copy.deepcopy(args), tool_call_id="call-1")
    call(hook, GW_TOOL, args=copy.deepcopy(args), tool_call_id="call-2")
    entries = json.loads((env.tmp_path / "lift_status.json").read_text())["entries"]
    assert len(entries) == 1 and entries[0]["count"] == 2
    everything = "\n".join(r.getMessage() for r in caplog.records) + (env.tmp_path / "lift_status.json").read_text()
    for secret in ("private note 4455", "cust-ref-9911"):
        assert secret not in everything


def test_a_failing_status_write_never_stops_the_call(env):
    env.provider.auto = "approved"
    hook = lifting_hook(env, schema_fn=lambda tool: DECLARES,
                        lift_status_path=str(env.tmp_path / "no-such-dir" / "lift_status.json"))
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None
    assert len(env.ledger.list_by_status("allowed")) == 1


def test_register_writes_the_lift_report_next_to_the_ledger(tmp_path, monkeypatch):
    captured = {}

    def spy(ledger, gate_factory, form_checked, **kw):
        captured.update(kw)
        return lambda **k: None

    monkeypatch.setattr("durable_park.make_pre_tool_call", spy)
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path / "data" / "ledger.db")})
    register(ctx)
    assert captured["lift_status"].path == str(tmp_path / "data" / "lift_status.json")


# ---- round 5: every silent path ------------------------------------------------------------------------

NOTHING_LISTED = {"type": "object", "properties": {"params": {"type": "object"}}}  # no top-level withhuman_context


@pytest.mark.parametrize("nested", [
    {"params": {"criteria": {"withhuman_context": "deep"}}},
    {"params": [{"a": 1, "withhuman_context": "in a list"}]},
    {"params": "{'a': 1, 'withhuman_context': 'not strict json'}"},
    {"params": {"WithHuman_Context": "other spelling"}},
    {"params": {"withHumanContext": "camel"}},
    {"params": {"with-human-context": "hyphens"}},
])
def test_a_note_that_stays_inside_a_deeper_shape_is_recorded_not_moved(env, nested):
    env.provider.auto = "approved"
    assert call(status_hook(env), GW_TOOL, args=copy.deepcopy(nested)) is None  # nothing moved
    kinds = {(tool, kind) for tool, kind, key in recorded(env)}
    assert kinds == {(GW_TOOL, "note_left_inside")}
    (_, _, key), = recorded(env)
    assert key.startswith("params") and "deep" not in key and "list" not in key and "strict" not in key


def test_a_gateway_tool_without_rules_is_reported_and_still_searched_for_a_note(env):
    env.provider.auto = "approved"
    hook = status_hook(env, schema_fn=lambda tool: None)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None
    assert recorded(env) == {(GW_TOOL, "schema_unavailable", ""), (GW_TOOL, "note_left_inside", "params.withhuman_context")}


def test_a_gateway_tool_whose_rules_do_not_show_the_field_is_searched_too(env):
    env.provider.auto = "approved"
    hook = status_hook(env, schema_fn=lambda tool: NOTHING_LISTED)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None
    assert recorded(env) == {(GW_TOOL, "note_left_inside", "params.withhuman_context")}  # not schema_unavailable
    assert call(hook, GW_TOOL, args={"params": {"a": 1}}, tool_call_id="call-2") is None
    assert recorded(env) == {(GW_TOOL, "note_left_inside", "params.withhuman_context")}  # a clean call adds nothing


def test_a_tool_that_is_not_a_gateway_tool_is_never_touched_or_reported(env):
    env.provider.auto = "approved"
    for schema_fn in (lambda tool: None, lambda tool: NOTHING_LISTED):  # no gateway slug: no extra search
        hook = status_hook(env, schema_fn=schema_fn)
        assert call(hook, "web_search", args=copy.deepcopy(NESTED), tool_call_id=f"c-{id(schema_fn)}") is None

    def broken(tool):
        raise RuntimeError("registry gone")

    assert call(status_hook(env, schema_fn=broken), "web_search", args=copy.deepcopy(NESTED),
                tool_call_id="c-broken") is None
    assert recorded(env) == set()


@pytest.mark.parametrize("args,detail", [
    ({"params": {"customer_ref": "58", "withhuman_context": {"why": "x"}}}, "not text"),
    ({"params": {"customer_ref": "58", "withhuman_context": ""}}, "empty"),
    ({"params": {"customer_ref": "58", "withhuman_context": "nested"}, "withhuman_context": "top"},
     "another note was already at the top"),
])
def test_a_note_that_is_removed_but_not_moved_is_recorded(env, args, detail):
    env.provider.auto = "approved"
    result = call(status_hook(env), GW_TOOL, args=copy.deepcopy(args))
    assert result["action"] == "modify" and "withhuman_context" not in result["args"]["params"]
    entries = json.loads((env.tmp_path / "lift_status.json").read_text())["entries"]
    assert [(e["kind"], e["key"], e["detail"]) for e in entries] == [("note_dropped", "params", detail)]
    assert "nested" not in (env.tmp_path / "lift_status.json").read_text().replace("note_dropped", "")


def test_two_nested_notes_record_the_second_as_dropped(env):
    env.provider.auto = "approved"
    args = {"a": {"withhuman_context": "first"}, "b": {"withhuman_context": "second"}}
    result = call(status_hook(env), GW_TOOL, args=args)
    assert result["args"]["withhuman_context"] == "first"
    entries = json.loads((env.tmp_path / "lift_status.json").read_text())["entries"]
    assert [(e["kind"], e["key"], e["detail"]) for e in entries] == [("note_dropped", "b", "more than one note")]


def rename_coerce(tool, args):
    """Hermes's ``unrename_tool_args`` maps a model-facing (sanitized) key back to its wire name."""
    return {("params_wire" if k == "params" else k): json.loads(v) if isinstance(v, str) and v.startswith("{") else v
            for k, v in args.items()}


def test_a_coercion_that_renames_a_key_is_never_adopted(env):
    env.provider.auto = "approved"
    args = {"params": '{"customer_ref": "58", "withhuman_context": "Find customer 58"}',
            "other": {"withhuman_context": "x"}}
    result = call(status_hook(env, coerce_fn=rename_coerce), GW_TOOL, args=copy.deepcopy(args))
    assert "params_wire" not in result["args"]  # the renamed key is not taken over
    assert result["args"]["params"] == args["params"]  # the text key keeps the model's own value
    assert result["args"]["other"] == {} and result["args"]["withhuman_context"] == "x"  # the plain key is still lifted
    assert {**args, **result["args"]} == result["args"]
    assert {(k, key) for _, k, key in recorded(env)} == {("args_unreadable", "params"),
                                                         ("note_left_inside", "params.withhuman_context")}
    assert env.provider.only().body["arguments"] == result["args"]


def test_only_the_text_keys_take_hermes_coerced_values(env):
    """Hermes's type fixing may also change unrelated values ("5" to 5): the model's own values are kept."""
    env.provider.auto = "approved"

    def coerce(tool, args):
        out = json_coerce(tool, args)
        out["amount"] = 5  # unrelated change by Hermes
        return out

    args = {"params": '{"customer_ref": "58", "withhuman_context": "n"}', "amount": "5"}
    result = call(status_hook(env, coerce_fn=coerce), GW_TOOL, args=args)
    assert result["args"] == {"params": {"customer_ref": "58"}, "amount": "5", "withhuman_context": "n"}


def test_an_unsafe_lift_is_refused_with_a_record_and_no_modify(env, monkeypatch):
    """The merge of a modify into the original must be exactly the lifted call: if the lift ever lost a key it
    could not be expressed, so the original arguments are used."""
    monkeypatch.setattr("durable_park.park_hook.lift_gateway_context",
                        lambda work, schema, on_problem=None: {"params": {"customer_ref": "58"}})  # dropped a key
    env.provider.auto = "approved"
    args = {"params": {"customer_ref": "58"}, "amount": 5}
    assert call(status_hook(env), GW_TOOL, args=copy.deepcopy(args)) is None
    assert recorded(env) == {(GW_TOOL, "lift_error", "")}
    assert env.provider.only().body["arguments"] == args


def test_a_failed_coercion_still_lifts_the_plain_nested_notes(env):
    env.provider.auto = "approved"

    def broken(tool, args):
        raise RuntimeError("hermes moved")

    args = {"params": '{"a": 1, "withhuman_context": "text"}', "other": {"x": 1, "withhuman_context": "plain"}}
    result = call(status_hook(env, coerce_fn=broken), GW_TOOL, args=args)
    assert result == {"action": "modify", "args": {"params": args["params"], "other": {"x": 1},
                                                   "withhuman_context": "plain"}}
    assert {(k, key) for _, k, key in recorded(env)} == {("args_unreadable", "params"),
                                                         ("note_left_inside", "params.withhuman_context")}


def test_too_big_an_argument_tree_is_reported_as_an_incomplete_check(env):
    env.provider.auto = "approved"
    args = {"params": [{"i": i} for i in range(11_000)]}
    assert call(status_hook(env), GW_TOOL, args=args) is None
    assert recorded(env) == {(GW_TOOL, "check_incomplete", "")}


def test_lift_report_that_cannot_be_written_is_kept_and_written_by_the_self_check_tick(env, tmp_path, caplog):
    from durable_park.gatecall import LiftStatus

    env.provider.auto = "approved"
    directory = tmp_path / "late"
    status = LiftStatus(str(directory / "lift_status.json"))
    hook = make_hook(env, schema_fn=lambda tool: DECLARES, lift_status=status)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None  # the call is not disturbed
    assert not directory.exists()
    directory.mkdir()
    status.flush()  # what the 30-minute self-check thread calls
    entries = json.loads((directory / "lift_status.json").read_text())["entries"]
    assert [(e["tool"], e["kind"], e["key"]) for e in entries] == [(GW_TOOL, "declared", "params")]


def test_register_gives_the_selfcheck_thread_the_lift_flush(tmp_path, monkeypatch):
    ticks = []
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck",
                        lambda checker, risky, path, on_tick=None: ticks.append(on_tick) or True)
    captured = {}

    def spy(ledger, gate_factory, form_checked, **kw):
        captured.update(kw)
        return lambda **k: None

    monkeypatch.setattr("durable_park.make_pre_tool_call", spy)
    register(FakeCtx(tmp_path / "data"))
    (tick,) = ticks
    assert tick == captured["lift_status"].flush


# ---- final fix wave -----------------------------------------------------------------------------------------

RULES_CHANGED = ('durable-park: the rules for "{tool}" changed since this call was first checked, so it no longer '
                 "matches the call that was parked; the call was not run.")


def flaky_schema(env_ok):
    """A schema lookup that works while ``env_ok[0]`` is true and fails otherwise."""
    def lookup(tool):
        if not env_ok[0]:
            raise RuntimeError("registry gone")
        return GW_SCHEMA

    return lookup


def test_a_replay_that_lifts_differently_is_blocked_with_an_accurate_message(env):
    ok = [False]
    hook = lifting_hook(env, schema_fn=flaky_schema(ok))
    first = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))  # no rules: parked with the original arguments
    assert first["message"].startswith("PARKED P-")
    ok[0] = True  # the rules are back: the same call now lifts to different arguments
    second = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    assert second == {"action": "block", "message": RULES_CHANGED.format(tool=GW_TOOL)}
    assert len(env.ledger.list_by_status("parked")) == 1 and len(env.provider.requests) == 1  # nothing new asked


def test_a_replay_whose_rules_went_missing_is_blocked_the_same_way(env):
    ok = [True]
    hook = lifting_hook(env, schema_fn=flaky_schema(ok))
    call(hook, GW_TOOL, args=copy.deepcopy(NESTED))  # parked with the lifted arguments
    ok[0] = False
    second = call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    assert second == {"action": "block", "message": RULES_CHANGED.format(tool=GW_TOOL)}
    assert len(env.provider.requests) == 1


def test_a_replay_that_lifts_differently_after_an_allow_never_runs_the_call(env):
    env.provider.auto = "approved"
    ok = [False]
    hook = lifting_hook(env, schema_fn=flaky_schema(ok))
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None  # allowed with the original arguments
    ok[0] = True
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED))["message"] == RULES_CHANGED.format(tool=GW_TOOL)
    assert len(env.provider.requests) == 1


def test_a_really_different_call_on_the_same_id_still_says_the_id_was_reused(env):
    ok = [True]
    hook = lifting_hook(env, schema_fn=flaky_schema(ok))
    call(hook, GW_TOOL, args=copy.deepcopy(NESTED))
    other = {"params": {"customer_ref": "99", "withhuman_context": "Find customer 58"}}
    assert call(hook, GW_TOOL, args=other)["message"] == REUSED
    ok[0] = False
    assert call(hook, GW_TOOL, args=other)["message"] == REUSED  # different content, whatever the rules say


def test_an_unsafe_lift_with_an_extra_key_is_refused_too(env, monkeypatch):
    """The merge guard also catches a key the lift invented: Hermes would merge it into the call."""
    monkeypatch.setattr("durable_park.park_hook.lift_gateway_context",
                        lambda work, schema, on_problem=None: {**work, "invented": 1})
    env.provider.auto = "approved"
    args = {"params": {"customer_ref": "58"}}
    assert call(status_hook(env), GW_TOOL, args=copy.deepcopy(args)) is None
    assert recorded(env) == {(GW_TOOL, "lift_error", "")}
    assert env.provider.only().body["arguments"] == args


PAYS = {"type": "object", "required": ["params"], "properties": {
    "params": {"type": "object", "required": ["total"],
               "properties": {"total": {"type": "number"}, "withhuman_context": {"type": "string"}}},
    "withhuman_context": {"type": "string"}}}


def test_problems_are_not_recorded_for_a_call_the_form_check_refused(env):
    """A refused call never reaches a server, so a note problem on it is nothing to alert about; the same
    call fixed and sent again is recorded."""
    hook = make_hook(env, form_checker=FormChecker(lambda tool: PAYS), schema_fn=lambda tool: PAYS,
                     lift_status_path=str(env.tmp_path / "lift_status.json"))
    bad = {"params": {"customer_ref": "58", "withhuman_context": "owned by the server"}}
    assert call(hook, GW_TOOL, args=bad)["message"].startswith(f'durable-park: "{GW_TOOL}" was NOT run')
    assert recorded(env) == set()
    good = {"params": {"total": 5, "withhuman_context": "owned by the server"}}
    assert call(hook, GW_TOOL, args=good, tool_call_id="call-2")["message"].startswith("PARKED P-")
    assert recorded(env) == {(GW_TOOL, "declared", "params")}


def test_a_note_of_emoji_reaches_the_approver_cut_to_2000_utf16_units(env):
    hook = lifting_hook(env)
    nested = {"params": {"customer_ref": "58", "withhuman_context": "\U0001F600" * 1500}}
    assert call(hook, GW_TOOL, args=nested)["message"].startswith("PARKED P-")
    sent = env.provider.only().body["agent_reasoning"]
    assert sent == "\U0001F600" * 1000 and len(sent.encode("utf-16-le")) // 2 == 2000


def test_a_report_nobody_can_write_leaves_the_marker_and_never_stops_the_call(env):
    env.provider.auto = "approved"
    (env.tmp_path / "lift_status.json.lock").mkdir()
    hook = status_hook(env, schema_fn=lambda tool: DECLARES)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) is None
    assert len(env.ledger.list_by_status("allowed")) == 1
    assert any(p.name.startswith("lift_status.unwritable.") for p in env.tmp_path.iterdir())


def test_one_calls_many_problems_wait_for_the_report_lock_once(env):
    """Four problems in one call and another process holding the report lock: the call waits one bound (0.2 s),
    not one per problem."""
    import fcntl
    import time
    hook = status_hook(env)
    args = {key: {"withhuman_context": 7} for key in ("a", "b", "c", "d")}  # a note that is not text: 4 problems
    holder = open(env.tmp_path / "lift_status.json.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        assert call(hook, GW_TOOL, args=args)["message"].startswith("PARKED P-")
        assert time.monotonic() - started < 0.6
    finally:
        holder.close()


def test_a_fifo_planted_at_the_marker_never_hangs_two_tool_calls(env):
    import threading
    env.provider.auto = "approved"
    os.mkfifo(env.tmp_path / f"lift_status.unwritable.{os.getpid()}")
    (env.tmp_path / "lift_status.json.lock").mkdir()  # the report cannot be written, so the marker is attempted
    hook = status_hook(env, schema_fn=lambda tool: DECLARES)
    results = []

    def one(call_id):
        results.append(call(hook, GW_TOOL, args=copy.deepcopy(NESTED), tool_call_id=call_id))

    for call_id in ("call-1", "call-2"):
        thread = threading.Thread(target=one, args=(call_id,), daemon=True)
        thread.start()
        thread.join(0.5)
        assert not thread.is_alive(), call_id
    assert results == [None, None] and len(env.ledger.list_by_status("allowed")) == 2


# ---- note_cut and note_not_text: the approver silently seeing less is reported too -------------------------

SECRET_NOTE = "pay-the-secret-vendor-4242 "


def test_a_note_cut_for_the_approver_is_recorded_and_never_stored(env, caplog):
    hook = status_hook(env)
    nested = {"params": {"customer_ref": "58", "withhuman_context": SECRET_NOTE + "\U0001F600" * 1500}}
    with caplog.at_level("DEBUG", logger="durable_park"):
        assert call(hook, GW_TOOL, args=nested)["message"].startswith("PARKED P-")
    assert recorded(env) == {(GW_TOOL, "note_cut", "")}
    assert "secret-vendor" not in (env.tmp_path / "lift_status.json").read_text()
    assert "secret-vendor" not in caplog.text


@pytest.mark.parametrize("note, cut", [
    ("x" * 2000, False), ("x" * 2001, True), ("\U0001F600" * 1000, False), ("\U0001F600" * 1000 + "x", True),
    ("x" * 1999 + "\U0001F600", True)],
    ids=["2000-chars-ok", "2001-chars-cut", "1000-emoji-ok", "1000-emoji-plus-1-cut", "pair-straddles-the-limit-cut"])
def test_the_cut_is_judged_in_utf16_units_like_build_call(env, note, cut):
    hook = status_hook(env)
    assert call(hook, GW_TOOL, args={"params": {"customer_ref": "58"}, "withhuman_context": note}
                )["message"].startswith("PARKED P-")
    assert (recorded(env) == {(GW_TOOL, "note_cut", "")}) is cut
    assert (len(env.provider.only().body["agent_reasoning"]) < len(note)) == cut


@pytest.mark.parametrize("top", ["", "   \n", 5, None, {"a": 1}, ["x"], True],
                         ids=["empty", "blank", "number", "null", "object", "list", "bool"])
def test_a_top_level_note_that_is_not_a_non_empty_text_is_recorded(env, top):
    hook = status_hook(env)
    args = {"params": {"customer_ref": "58"}, "withhuman_context": top}
    assert call(hook, GW_TOOL, args=args)["message"].startswith("PARKED P-")
    assert recorded(env) == {(GW_TOOL, "note_not_text", "")}


def test_a_proper_or_absent_note_records_neither(env):
    env.provider.auto = "approved"
    hook = status_hook(env)
    assert call(hook, GW_TOOL, args=copy.deepcopy(NESTED)) == {"action": "modify", "args": LIFTED}
    assert call(hook, GW_TOOL, args={"params": {"customer_ref": "59"}}, tool_call_id="call-2") is None
    assert recorded(env) == set()


def test_an_empty_top_level_note_over_a_nested_one_records_both_problems(env):
    hook = status_hook(env)
    args = {"params": {"customer_ref": "58", "withhuman_context": "the real reason"}, "withhuman_context": ""}
    assert call(hook, GW_TOOL, args=args)["message"].startswith("PARKED P-")
    assert recorded(env) == {(GW_TOOL, "note_not_text", ""), (GW_TOOL, "note_dropped", "params")}


def test_note_cut_and_note_not_text_are_not_recorded_for_a_refused_call(env):
    hook = make_hook(env, form_checker=FormChecker(lambda tool: PAYS), schema_fn=lambda tool: PAYS,
                     lift_status_path=str(env.tmp_path / "lift_status.json"))
    bad = {"params": {"customer_ref": "58"}, "withhuman_context": ""}
    assert call(hook, GW_TOOL, args=bad)["message"].startswith(f'durable-park: "{GW_TOOL}" was NOT run')
    assert recorded(env) == set()


def test_build_call_stays_pure_when_the_note_is_cut(env):
    """Recording belongs to the hook: build_call twice on the same row gives the same call and never writes."""
    from durable_park.gatecall import build_call
    row = env.ledger.park(session_key=SESSION_KEY, session_id="s", tool_call_id="c", tool=GW_TOOL,
                          args={"params": {}, "withhuman_context": "x" * 3000}, call_key="k" * 64, server="park:quickbooks")
    assert build_call(row).reasoning == build_call(row).reasoning == "x" * 2000
    assert not (env.tmp_path / "lift_status.json").exists()
