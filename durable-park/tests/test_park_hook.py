import hashlib
import re
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol.testing import FakeProvider

from durable_park import register
from durable_park.gatecall import build_call
from durable_park.ledger import Ledger, call_key
from durable_park.park_hook import DEFAULT_RISKY_TOOLS, make_pre_tool_call

SESSION_KEY = "agent:main:telegram:dm:42"
COULD_NOT_OPEN = "durable-park: could not open an approval request; the call was not run"
RISKY = [re.compile(p) for p in DEFAULT_RISKY_TOOLS]


def park_message(park_id, tool):
    return (f'PARKED {park_id}: "{tool}" needs human approval in withHuman and has NOT run. '
            "Do not retry it or try another way to do it. Tell the user it is waiting for approval; "
            "you will be told here when it is decided.")


@pytest.fixture
def env(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    provider = FakeProvider(auto=None)
    from durable_park.gatecall import make_gate
    from agentapprovalprotocol.testing import BASE_URL, TOKEN

    hook = make_pre_tool_call(
        ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client()), RISKY, lambda: SESSION_KEY)
    return SimpleNamespace(ledger=ledger, provider=provider, hook=hook, tmp_path=tmp_path)


def call(hook, tool, args=None, session_id="sess-1", tool_call_id="call-1"):
    return hook(tool_name=tool, args=args or {"amount": 5}, session_id=session_id,
                tool_call_id=tool_call_id, task_id="", turn_id="t1", api_request_id="", middleware_trace=[])


def test_non_risky_passes(env):
    assert call(env.hook, "mcp__wh__quickbooks__search_customers") is None
    assert env.ledger.list_by_status("parked") == []
    assert env.provider.requests == {}


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
    assert second["action"] == "block"
    assert row.park_id in second["message"]
    assert "already waiting" in second["message"]
    assert first["message"].startswith("PARKED ")
    assert len(env.ledger.list_by_status("parked")) == 1
    assert len(env.provider.requests) == 1


def test_no_session_key_blocks(env):
    from durable_park.gatecall import make_gate
    from agentapprovalprotocol.testing import BASE_URL, TOKEN

    hook = make_pre_tool_call(
        env.ledger, lambda: make_gate(BASE_URL, TOKEN, client=env.provider.client()), RISKY, lambda: "")
    result = call(hook, "mcp__wh__quickbooks__create_payment")
    assert result == {"action": "block", "message": "durable-park: no chat session; risky tool refused"}
    assert env.ledger.list_by_status("parked") == []
    assert env.provider.requests == {}


def test_provider_error_blocks(env):
    env.provider.intercept = lambda request: httpx.Response(401, json={"error": "bad token"})
    result = call(env.hook, "mcp__wh__quickbooks__create_payment")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.ledger.list_by_status("parked") == []
    (row,) = env.ledger.list_by_status("cancelled")
    assert row.request_id is None
    # the failed park does not wedge the chat: the next risky call can park
    env.provider.intercept = None
    again = call(env.hook, "mcp__wh__quickbooks__create_bill", tool_call_id="call-2")
    assert again["message"].startswith("PARKED ")


def test_gate_factory_failure_fails_closed(env):
    def broken_factory():
        raise OSError("token file missing")

    hook = make_pre_tool_call(env.ledger, broken_factory, RISKY, lambda: SESSION_KEY)
    assert call(hook, "mcp__wh__quickbooks__create_payment") == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.ledger.list_by_status("parked") == []


def test_allow_at_park_time_fails_closed(env):
    env.provider.auto = "approved"  # should not happen in production; the hook must still refuse
    result = call(env.hook, "mcp__wh__quickbooks__create_payment")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    assert env.ledger.list_by_status("parked") == []
    assert len(env.ledger.list_by_status("cancelled")) == 1


def test_missing_tool_call_id_blocks(env):
    result = call(env.hook, "mcp__wh__quickbooks__create_payment", tool_call_id="")
    assert result["action"] == "block"
    assert env.ledger.list_by_status("parked") == []
    assert env.provider.requests == {}


def test_concurrent_loser_is_cancelled(env):
    """A call that parks after an earlier active park appeared (race) backs out."""
    class Racy(Ledger):
        raced = False

        def active_for_session(self, session_key):
            if not self.raced:
                self.raced = True
                self.park(session_key=session_key, session_id="sess-1", tool_call_id="call-0",
                          tool="mcp__wh__quickbooks__create_bill", args={"a": 1},
                          call_key=call_key("sess-1", "call-0"), server="park:quickbooks")
                return []  # the pre-check ran just before the other call parked
            return super().active_for_session(session_key)

    from durable_park.gatecall import make_gate
    from agentapprovalprotocol.testing import BASE_URL, TOKEN

    ledger = Racy(str(env.tmp_path / "racy.db"))
    hook = make_pre_tool_call(
        ledger, lambda: make_gate(BASE_URL, TOKEN, client=env.provider.client()), RISKY, lambda: SESSION_KEY)
    result = call(hook, "mcp__wh__quickbooks__create_payment", tool_call_id="call-1")
    first = ledger.get(ledger.active_for_session(SESSION_KEY)[0].park_id)
    assert result["action"] == "block" and "already waiting" in result["message"]
    assert first.tool_call_id == "call-0" and first.status == "parked"
    assert [r.tool_call_id for r in ledger.list_by_status("cancelled")] == ["call-1"]
    assert env.provider.requests == {}


class FakeCtx:
    def __init__(self, data_dir, config=None):
        self.config = config or {}
        self.state = SimpleNamespace(data_dir=data_dir)
        self.hooks = {}
        self.asked = []

    def get_config(self, key, default=None):
        self.asked.append(key)
        return self.config.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_register_wires_pre_tool_call_with_default_settings(tmp_path):
    ctx = FakeCtx(tmp_path / "data")
    register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call"}
    assert {"risky_tools", "aap_base_url", "token_file", "ledger_path"} <= set(ctx.asked)
    assert (tmp_path / "data" / "ledger.db").exists()  # default ledger_path under state.data_dir
    # defaults classify risky tools; non-risky ones pass without touching the network or token file
    assert ctx.hooks["pre_tool_call"](tool_name="mcp__wh__quickbooks__search_customers", args={},
                                      session_id="s", tool_call_id="c") is None


def test_register_honours_settings_and_fails_closed_without_token(tmp_path):
    ctx = FakeCtx(tmp_path, {
        "ledger_path": str(tmp_path / "custom.db"),
        "token_file": str(tmp_path / "no-such-token"),
        "risky_tools": [r"mcp__wh__custom__do_it"],
    })
    register(ctx)
    assert (tmp_path / "custom.db").exists()
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="mcp__wh__quickbooks__create_payment", args={}, session_id="s", tool_call_id="c") is None
    # custom risky pattern is active; the missing token file makes it fail closed (and it needs a chat session)
    result = hook(tool_name="mcp__wh__custom__do_it", args={}, session_id="s", tool_call_id="c")
    assert result["action"] == "block"


def test_register_fails_closed_when_ledger_cannot_open(tmp_path):
    ctx = FakeCtx(tmp_path, {"ledger_path": str(tmp_path)})  # a directory, not a database file
    register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    result = hook(tool_name="mcp__wh__quickbooks__create_payment", args={}, session_id="s", tool_call_id="c")
    assert result == {"action": "block", "message": COULD_NOT_OPEN}
    assert hook(tool_name="mcp__wh__quickbooks__search_customers", args={}, session_id="s", tool_call_id="c") is None
