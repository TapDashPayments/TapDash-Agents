import hashlib

from agentapprovalprotocol import Allow, Deny, Pending
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from durable_park.gatecall import build_call, make_gate
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


def test_build_call_is_stable(tmp_path):
    row = make_row(tmp_path)
    one, two = build_call(row), build_call(row)
    assert one == two
    assert one.key == row.call_key
    assert one.tool == "mcp__wh__quickbooks__create_payment"
    assert dict(one.arguments) == {"amount": 12.5, "customer": {"id": "7"}}
    assert one.server == "park:quickbooks"
    assert dict(one.resume) == {"framework": "hermes", "park_id": row.park_id, "session_key": row.session_key}


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
