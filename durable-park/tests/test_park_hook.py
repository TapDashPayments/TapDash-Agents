import hashlib
import re
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from durable_park import register
from durable_park.executor import EXECUTE_SCHEMA
from durable_park.gatecall import build_call, bounded_http, make_gate
from durable_park.ledger import Ledger, call_key
from agentapprovalprotocol import Gate

from durable_park.breaker import Breaker
from durable_park.park_hook import DEFAULT_FORM_CHECKED_TOOLS, make_pre_tool_call

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
    def __init__(self, data_dir, config=None):
        self.config = config or {}
        self.state = SimpleNamespace(data_dir=data_dir)
        self.hooks = {}
        self.tools = {}
        self.asked = []

    def get_config(self, key, default=None):
        self.asked.append(key)
        return self.config.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = SimpleNamespace(toolset=toolset, schema=schema, handler=handler, **kwargs)

    def dispatch_tool(self, tool_name, args, **kwargs):
        raise AssertionError("not expected in registration tests")


def test_register_wires_pre_tool_call_with_default_settings(tmp_path):
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "pre_gateway_dispatch"}
    assert set(ctx.tools) == {"parked_action_execute"}
    tool = ctx.tools["parked_action_execute"]
    assert tool.toolset == "durable_park" and tool.schema == EXECUTE_SCHEMA and callable(tool.handler)
    assert {"risky_tools", "aap_base_url", "token_file", "ledger_path"} <= set(ctx.asked)
    assert (tmp_path / "data" / "ledger.db").exists()  # default ledger_path under state.data_dir
    # every non-exempt tool is asked (here: refused, there is no chat session); exempt tools pass
    result = ctx.hooks["pre_tool_call"](tool_name="mcp__wh__quickbooks__search_customers", args={},
                                        session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": NO_SESSION}
    assert ctx.hooks["pre_tool_call"](tool_name="clarify", args={}, session_id="s", tool_call_id="c2") is None


def test_register_honours_settings_and_fails_closed_without_token(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path, {
        "ledger_path": str(tmp_path / "custom.db"),
        "token_file": str(tmp_path / "no-such-token"),
        "risky_tools": [r"mcp__wh__custom__do_it"],
    })
    monkeypatch.setattr("durable_park.park_hook.default_session_key", lambda: SESSION_KEY)
    register(ctx)
    assert (tmp_path / "custom.db").exists()
    hook = ctx.hooks["pre_tool_call"]
    # it has a chat session; only the unreadable token file can stop it (every non-exempt tool is asked)
    result = hook(tool_name="mcp__wh__custom__do_it", args={}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    (row,) = Ledger(str(tmp_path / "custom.db")).list_by_status("cancelled")
    assert row.tool == "mcp__wh__custom__do_it"


def test_register_fails_closed_when_ledger_cannot_open(tmp_path):
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path)})  # a directory, not a database file
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    result = hook(tool_name="mcp__wh__quickbooks__create_payment", args={}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    assert hook(tool_name="mcp__wh__quickbooks__search_customers", args={}, session_id="s",
                tool_call_id="c") == {"action": "block", "message": COULD_NOT_OPEN}
    assert hook(tool_name="clarify", args={}, session_id="s", tool_call_id="c") is None
    assert ctx.tools == {}  # without a ledger there is nothing to execute


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

from durable_park.formcheck import FormChecker  # noqa: E402

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
                        lambda checker, risky, path: started.append((checker, len(risky), path)) or True)
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    ((checker, n_risky, path),) = started
    assert isinstance(checker, FormChecker) and n_risky == len(DEFAULT_FORM_CHECKED_TOOLS)
    assert path == str(tmp_path / "data" / "formcheck_status.json")


def test_register_without_ledger_does_not_start_selfcheck(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck", lambda *a: started.append(a) or True)
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
    assert row.note.startswith("withHuman denied: ") and row.note.endswith("Their note: too big")


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
    assert breaker._failures == 0


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
