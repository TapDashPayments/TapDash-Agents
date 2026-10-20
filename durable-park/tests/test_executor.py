import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol import Allow, Pending
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider, _error

from durable_park import executor as executor_mod
from durable_park.executor import EXECUTE_SCHEMA, crash_after_consume_enabled, make_executor
from durable_park.gatecall import build_call, make_gate
from durable_park.ledger import Ledger
from durable_park.park_hook import DEFAULT_RISKY_TOOLS, make_pre_tool_call

SESSION_KEY = "agent:main:telegram:dm:42"
TOOL = "mcp__wh__quickbooks__create_payment"
ARGS = {"amount": 5, "customer": "C-1"}
EXPIRED = "The approval expired before it could run. Ask the user whether to request approval again."
RISKY = [re.compile(p) for p in DEFAULT_RISKY_TOOLS]


class Crash(BaseException):
    """Raised by the injected exit function so the test process survives."""


@pytest.fixture
def env(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    provider = FakeProvider(auto=None)

    def gate_factory():
        return make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5))

    hook = make_pre_tool_call(ledger, gate_factory, RISKY, lambda: SESSION_KEY)
    dispatched = []
    box = SimpleNamespace(result='{"ok": true, "payment_id": "pay_1"}', raises=None)

    def dispatch(tool, args, **kw):
        dispatched.append((tool, args))
        if box.raises:
            raise box.raises
        return box.result

    def build(**kw):
        kw.setdefault("session_key_fn", lambda: SESSION_KEY)
        return make_executor(ledger, gate_factory, dispatch, **kw)

    def approve(tool=TOOL, args=ARGS, tool_call_id="call-1"):
        """Park through the real hook, let the 'human' approve at the provider, mark the row approved."""
        hook(tool_name=tool, args=args, session_id="sess-1", tool_call_id=tool_call_id)
        row = [r for r in ledger.list_by_status("parked") if r.tool_call_id == tool_call_id][0]
        provider.decide(row.request_id, "approved")
        assert ledger.transition(row.park_id, "parked", "approved")
        return ledger.get(row.park_id)

    return SimpleNamespace(ledger=ledger, provider=provider, gate_factory=gate_factory, hook=hook,
                           dispatched=dispatched, box=box, build=build, approve=approve, tmp_path=tmp_path)


def run(handler, park_id):
    return json.loads(handler({"park_id": park_id}))


def test_executes_exactly_once(env):
    row = env.approve()
    handler = env.build()
    out = run(handler, row.park_id)
    assert out == {"status": "executed", "park_id": row.park_id, "result": {"ok": True, "payment_id": "pay_1"}}
    assert env.dispatched == [(TOOL, ARGS)]
    done = env.ledger.get(row.park_id)
    assert done.status == "executed"
    assert json.loads(done.result_json) == {"ok": True, "payment_id": "pay_1"}
    assert done.executing_at
    assert env.provider.only().consumed_at is not None
    # a second call (model retry, replay) never runs it again
    again = run(handler, row.park_id)
    assert again == {"status": "refused", "park_id": row.park_id,
                     "message": f"durable-park: {row.park_id} is executed, not approved; it cannot run"}
    assert len(env.dispatched) == 1


def test_executor_rejects_pending(env):
    env.hook(tool_name=TOOL, args=ARGS, session_id="sess-1", tool_call_id="call-1")
    (row,) = env.ledger.list_by_status("parked")
    out = run(env.build(), row.park_id)
    assert out == {"status": "refused", "park_id": row.park_id,
                   "message": f"durable-park: {row.park_id} is parked, not approved; it cannot run"}
    assert env.dispatched == []
    assert env.provider.only().consumed_at is None
    assert env.ledger.get(row.park_id).status == "parked"


def test_executor_rejects_other_session(env):
    row = env.approve()
    handler = env.build(session_key_fn=lambda: "agent:main:telegram:dm:999")
    out = run(handler, row.park_id)
    assert out == {"status": "refused", "park_id": row.park_id, "message": "durable-park: not your session"}
    assert env.dispatched == []
    assert env.ledger.get(row.park_id).status == "approved"  # untouched, still runnable by its own chat
    assert env.provider.only().consumed_at is None
    # no chat session at all is also "not your session"
    assert run(env.build(session_key_fn=lambda: ""), row.park_id)["message"] == "durable-park: not your session"


def test_executor_refuses_expired_approval(env):
    row = env.approve()
    # the real SDK path: the approval's validity window ran out (requested 20 min ago, decided 10 min ago,
    # valid for 5), so the gate answers Deny(status="expired") without consuming anything
    stored = env.provider.only()
    stored.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    stored.decided_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    stored.expires_at = stored.decided_at + env.provider.validity
    out = run(env.build(), row.park_id)
    assert out == {"status": "expired", "park_id": row.park_id, "message": EXPIRED}
    assert env.dispatched == []
    assert env.ledger.get(row.park_id).status == "expired"
    assert env.provider.only().consumed_at is None


def test_provider_says_expired_at_consume(env):
    row = env.approve()

    def intercept(request):  # the local clock still thinks the approval is valid; the provider disagrees
        return _error(409, "conflict", "approval_expired") if request.url.path.endswith("/consume") else None

    env.provider.intercept = intercept
    out = run(env.build(), row.park_id)
    assert out == {"status": "expired", "park_id": row.park_id, "message": EXPIRED}
    assert env.ledger.get(row.park_id).status == "expired" and env.dispatched == []


def test_consumed_elsewhere_marks_failed(env):
    row = env.approve()
    assert isinstance(env.gate_factory().check(build_call(row)), Allow)  # someone else consumed the approval
    out = run(env.build(), row.park_id)
    assert out["status"] == "failed" and out["park_id"] == row.park_id
    assert out["message"].startswith(f"durable-park: {row.park_id} failed: ") and "consumed" in out["message"]
    failed = env.ledger.get(row.park_id)
    assert failed.status == "failed" and "consumed" in failed.note
    assert env.dispatched == []


def test_tool_error_marks_failed(env):
    row = env.approve()
    env.box.result = json.dumps({"error": "QuickBooks rejected the payment"})
    out = run(env.build(), row.park_id)
    assert out["status"] == "failed"
    assert out["message"] == f"durable-park: {row.park_id} failed: tool error: QuickBooks rejected the payment"
    failed = env.ledger.get(row.park_id)
    assert failed.status == "failed" and failed.note == "tool error: QuickBooks rejected the payment"
    assert json.loads(failed.result_json) == {"error": "QuickBooks rejected the payment"}
    assert len(env.dispatched) == 1


def unknown_message(row):
    return f"durable-park: {row.park_id} outcome UNKNOWN \u2014 check {row.tool} in the sandbox before retrying"


def test_dispatch_exception_marks_unknown_without_leaking_text(env):
    row = env.approve()
    env.box.raises = RuntimeError("secret-token-123 exploded")
    out = run(env.build(), row.park_id)
    assert out == {"status": "unknown", "park_id": row.park_id, "message": unknown_message(row)}
    after = env.ledger.get(row.park_id)
    assert after.status == "unknown" and after.note == "dispatch raised RuntimeError"
    assert "secret-token" not in json.dumps(out) and "secret-token" not in after.note
    assert len(env.dispatched) == 1
    # never retried automatically: the row is no longer approved, so a second call is refused
    assert run(env.build(), row.park_id)["status"] == "refused" and len(env.dispatched) == 1


# Hermes' own error texts (tools/mcp_tool_handlers.py), as returned through registry.dispatch.
UNKNOWN_ERRORS = {
    "timeout": {"error": "MCP call failed: TimeoutError: tools/call create_payment timed out after 120s"},
    "interrupt": {"error": "MCP call interrupted: user sent a new message"},
    "teardown": {"error": "MCP call failed: RuntimeError: MCP tools/call create_payment on 'wh' was aborted by a "
                          "reconnect teardown; the request may already have been dispatched, so verify its effect "
                          "before re-invoking"},
    "generic transport": {"error": "MCP call failed: ClosedResourceError: ClosedResourceError()"},
    "retry after respawn": {"error": "MCP call failed after respawning the stdio subprocess for 'wh': OSError: x"},
    "handler raised": {"error": "[TOOL_ERROR] Tool execution failed: KeyError: 'x'"},
    "session expired mid-write": {"error": "The MCP transport session to 'wh' expired while this write-capable call "
                                           "was in flight, so the outcome is UNKNOWN", "outcome_uncertain": True,
                                  "server": "wh"},
    "stdio died mid-call": {"error": "MCP server 'wh' lost its stdio subprocess after the tool call began.",
                            "outcome_uncertain": True},
}

FAILED_ERRORS = {
    "remote isError": {"error": "Customer C-1 not found"},
    "circuit breaker (unreachable)": {"error": "MCP server 'wh' is unreachable after 5 consecutive failures. "
                                                "Auto-retry available in ~30s. Do NOT retry this tool yet"},
    "circuit breaker (rejected)": {"error": "MCP server 'wh' rejected the last 5 calls (it is reachable; see the "
                                             "error text those calls returned). Paused for ~30s."},
    "trust gate": {"error": "The user did not approve running write-capable MCP tool 'create_payment' on untrusted "
                            "server 'wh'. The command was NOT run."},
    "not connected": {"error": "MCP server 'wh' is not connected"},
    "transport down": {"error": "MCP server 'wh' transport is down; reconnect requested. Do NOT retry this tool "
                                "immediately"},
    "unknown tool": {"error": "Unknown tool: mcp__wh__quickbooks__create_payment"},
    "needs reauth": {"error": "MCP server 'wh' requires re-authentication.", "needs_reauth": True},
    "unrecognised text": {"error": "something Hermes has not said before"},
}


@pytest.mark.parametrize("payload", list(UNKNOWN_ERRORS.values()), ids=list(UNKNOWN_ERRORS))
def test_ambiguous_tool_errors_mark_unknown(env, payload):
    row = env.approve()
    env.box.result = json.dumps(payload)
    out = run(env.build(), row.park_id)
    assert out == {"status": "unknown", "park_id": row.park_id, "message": unknown_message(row)}
    after = env.ledger.get(row.park_id)
    assert after.status == "unknown" and after.note.startswith("tool error: ")
    assert json.loads(after.result_json) == payload  # the raw result is kept for the human who verifies
    assert len(env.dispatched) == 1
    assert run(env.build(), row.park_id)["status"] == "refused" and len(env.dispatched) == 1


@pytest.mark.parametrize("payload", list(FAILED_ERRORS.values()), ids=list(FAILED_ERRORS))
def test_definite_non_runs_and_remote_errors_mark_failed(env, payload):
    row = env.approve()
    env.box.result = json.dumps(payload)
    out = run(env.build(), row.park_id)
    assert out["status"] == "failed" and out["park_id"] == row.park_id
    assert out["message"] == f"durable-park: {row.park_id} failed: tool error: {payload['error'][:200]}"
    after = env.ledger.get(row.park_id)
    assert after.status == "failed" and json.loads(after.result_json) == payload


def test_ledger_write_failure_after_dispatch_is_unknown_not_failed(env, monkeypatch):
    row = env.approve()
    real = env.ledger.transition

    def flaky(park_id, from_status, to_status, **fields):
        if to_status == "executed":
            raise sqlite3.OperationalError("database is locked")
        return real(park_id, from_status, to_status, **fields)

    monkeypatch.setattr(env.ledger, "transition", flaky)
    out = run(env.build(), row.park_id)
    assert out == {"status": "unknown", "park_id": row.park_id, "message": unknown_message(row)}
    assert "could not run" not in json.dumps(out)
    after = env.ledger.get(row.park_id)
    assert after.status == "unknown" and after.note == "executor error after dispatch: OperationalError"
    assert len(env.dispatched) == 1


def test_every_ledger_write_failing_after_dispatch_still_reports_unknown(env, monkeypatch):
    row = env.approve()
    real = env.ledger.transition

    def broken(park_id, from_status, to_status, **fields):
        if from_status == "executing":
            raise sqlite3.OperationalError("disk full")
        return real(park_id, from_status, to_status, **fields)

    monkeypatch.setattr(env.ledger, "transition", broken)
    out = run(env.build(), row.park_id)
    assert out == {"status": "unknown", "park_id": row.park_id, "message": unknown_message(row)}
    assert env.ledger.get(row.park_id).status == "executing"  # could not be moved; the reply is still truthful
    assert len(env.dispatched) == 1


def test_ledger_failure_before_dispatch_is_not_unknown(env, monkeypatch):
    row = env.approve()
    monkeypatch.setattr(env.ledger, "get", lambda park_id: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    out = run(env.build(), row.park_id)
    assert out["status"] == "refused" and "could not run" in out["message"] and env.dispatched == []


def test_empty_session_key_is_never_your_session(env):
    row = env.approve()
    # the park hook always stores a key; force an empty-key row to prove the defence in depth
    with env.ledger._connect() as con:
        con.execute("UPDATE parks SET session_key='' WHERE park_id=?", (row.park_id,))
    out = run(env.build(session_key_fn=lambda: ""), row.park_id)
    assert out == {"status": "refused", "park_id": row.park_id, "message": "durable-park: not your session"}
    assert env.dispatched == [] and env.ledger.get(row.park_id).status == "approved"


def test_null_error_field_is_not_a_failure(env):
    row = env.approve()
    env.box.result = json.dumps({"error": None, "id": "x"})
    assert run(env.build(), row.park_id)["status"] == "executed"


def test_non_json_result_is_returned_as_text(env):
    row = env.approve()
    env.box.result = "plain text ok"
    out = run(env.build(), row.park_id)
    assert out["status"] == "executed" and out["result"] == "plain text ok"
    assert env.ledger.get(row.park_id).result_json == "plain text ok"


def test_missing_row_and_missing_park_id(env):
    handler = env.build()
    assert run(handler, "P-NOPE0000") == {"status": "refused", "park_id": "P-NOPE0000",
                                           "message": "durable-park: no such parked action"}
    out = json.loads(handler({}))
    assert out["status"] == "refused" and out["message"] == "durable-park: no such parked action"
    assert env.dispatched == []


def test_lost_cas_is_refused(env, monkeypatch):
    row = env.approve()
    assert env.ledger.transition(row.park_id, "approved", "executing", executing_at="x")  # another worker won
    monkeypatch.setattr(env.ledger, "get", lambda park_id: row)  # this worker still sees the stale approved row
    out = run(env.build(), row.park_id)
    assert out == {"status": "refused", "park_id": row.park_id,
                   "message": f"durable-park: {row.park_id} is already running or done"}
    assert env.dispatched == []
    assert env.provider.only().consumed_at is None


def test_provider_error_fails_closed_without_running(env):
    row = env.approve()
    env.provider.intercept = lambda request: httpx.Response(500, json={})
    out = run(env.build(), row.park_id)
    assert out["status"] == "failed" and out["message"].endswith("approval check failed; nothing ran")
    failed = env.ledger.get(row.park_id)
    assert failed.status == "failed" and failed.note == "approval check failed; nothing ran"
    assert env.dispatched == []


def test_gate_factory_exception_fails_closed(env):
    row = env.approve()

    def boom():
        raise OSError("token file missing")

    handler = make_executor(env.ledger, boom, lambda tool, args, **kw: env.dispatched.append(1),
                            session_key_fn=lambda: SESSION_KEY)
    out = run(handler, row.park_id)
    assert out["status"] == "failed" and "approval check failed; nothing ran" in out["message"]
    assert env.ledger.get(row.park_id).status == "failed" and env.dispatched == []


def test_pending_verdict_fails_closed(env):
    row = env.approve()

    class StillPending:
        def check(self, call):
            return Pending("req-1")

    handler = make_executor(env.ledger, StillPending, lambda *a, **k: env.dispatched.append(1),
                            session_key_fn=lambda: SESSION_KEY)
    out = run(handler, row.park_id)
    assert out["status"] == "failed" and env.ledger.get(row.park_id).status == "failed" and env.dispatched == []


def test_crash_after_consume_exits_before_dispatch(env):
    row = env.approve()
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise Crash

    handler = env.build(crash_after_consume=True, exit_fn=fake_exit)
    with pytest.raises(Crash):
        handler({"park_id": row.park_id})
    assert codes == [70]
    assert env.dispatched == []
    assert env.provider.only().consumed_at is not None  # approval is burned...
    assert env.ledger.get(row.park_id).status == "executing"  # ...and the row is left for recovery


def test_crash_flag_default_exit_is_os_exit(env, monkeypatch):
    row = env.approve()
    seen = []
    monkeypatch.setattr(executor_mod, "_hard_exit", lambda code: (seen.append(code), (_ for _ in ()).throw(Crash)))
    with pytest.raises(Crash):
        env.build(crash_after_consume=True)({"park_id": row.park_id})
    assert seen == [70]


def test_crash_env_flag(monkeypatch):
    monkeypatch.delenv("DURABLE_PARK_CRASH_AFTER_CONSUME", raising=False)
    assert crash_after_consume_enabled() is False
    monkeypatch.setenv("DURABLE_PARK_CRASH_AFTER_CONSUME", "1")
    assert crash_after_consume_enabled() is True
    monkeypatch.setenv("DURABLE_PARK_CRASH_AFTER_CONSUME", "0")
    assert crash_after_consume_enabled() is False


def test_logs_never_contain_args(env, caplog):
    row = env.approve(args={"amount": 5, "memo": "SENSITIVE-MEMO"})
    caplog.set_level("DEBUG", logger="durable_park")
    run(env.build(), row.park_id)
    assert "SENSITIVE-MEMO" not in caplog.text and "pay_1" not in caplog.text


def test_schema():
    assert EXECUTE_SCHEMA["name"] == "parked_action_execute"
    assert EXECUTE_SCHEMA["description"] == "Run a previously parked action after you were told it was approved."
    params = EXECUTE_SCHEMA["parameters"]
    assert params["type"] == "object" and params["required"] == ["park_id"]
    assert params["properties"]["park_id"]["type"] == "string"
