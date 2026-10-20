import hashlib
import json
import re
import sqlite3

import pytest

from durable_park.ledger import Ledger, ParkRow, args_digest, normalize_args


def canonical(args):
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def make_ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def park(ledger, *, session_key="tg:1", session_id="s1", tool_call_id="c1",
         tool="mcp__wh__quickbooks__create_payment", args=None, server="park:quickbooks"):
    args = {"amount": 5, "to": "x"} if args is None else args
    return ledger.park(
        session_key=session_key,
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool=tool,
        args=args,
        call_key=hashlib.sha256(f"{session_id}:{tool_call_id}".encode()).hexdigest(),
        server=server,
    )


def test_park_is_idempotent(tmp_path):
    ledger = make_ledger(tmp_path)
    first = park(ledger)
    again = park(ledger, args={"amount": 999})  # same (session_id, tool_call_id)
    assert isinstance(first, ParkRow)
    assert re.fullmatch(r"P-[0-9A-F]{8}", first.park_id)
    assert again.park_id == first.park_id
    assert again.args_json == first.args_json  # original frozen, not overwritten
    assert first.status == "parked"
    assert first.server == "park:quickbooks"
    # R22: the digest is over the normalized arguments (gateway default scope added); args_json stays as given
    assert first.args_digest == hashlib.sha256(
        canonical({"amount": 5, "to": "x", "withhuman_scope": "default"}).encode()).hexdigest()
    assert first.args_json == canonical({"amount": 5, "to": "x"})
    assert first.call_key == hashlib.sha256(b"s1:c1").hexdigest()
    assert len(ledger.list_by_status("parked")) == 1
    other = park(ledger, tool_call_id="c2")
    assert other.park_id != first.park_id
    # durable: a second Ledger on the same file sees the row
    assert Ledger(str(tmp_path / "ledger.db")).get(first.park_id).tool == first.tool


def test_transition_cas(tmp_path):
    ledger = make_ledger(tmp_path)
    row = park(ledger)
    assert ledger.transition(row.park_id, "parked", "approved", note="ok") is True
    # CAS: same from_status no longer matches
    assert ledger.transition(row.park_id, "parked", "denied") is False
    got = ledger.get(row.park_id)
    assert got.status == "approved"
    assert got.note == "ok"
    assert got.updated_at >= row.updated_at
    assert ledger.transition(row.park_id, "approved", "executing", executing_at="2026-10-19T00:00:00Z") is True
    assert ledger.get(row.park_id).executing_at == "2026-10-19T00:00:00Z"
    assert ledger.transition(row.park_id, "executing", "executed", result_json="{}") is True
    assert ledger.get(row.park_id).result_json == "{}"
    assert ledger.get("P-NOPE0000") is None
    assert ledger.transition("P-NOPE0000", "parked", "approved") is False


def test_transition_rejects_illegal_edges_and_unknown_fields(tmp_path):
    ledger = make_ledger(tmp_path)
    row = park(ledger)
    with pytest.raises(ValueError):
        ledger.transition(row.park_id, "parked", "executing")
    with pytest.raises(ValueError):
        ledger.transition(row.park_id, "executed", "parked")
    with pytest.raises(ValueError):
        ledger.transition(row.park_id, "parked", "approved", bogus="x")
    with pytest.raises(ValueError):
        ledger.transition(row.park_id, "parked", "approved", created_at="x")
    assert ledger.get(row.park_id).status == "parked"
    for edge in [("parked", "approved"), ("parked", "denied"), ("parked", "expired"), ("parked", "cancelled"),
                 ("approved", "executing"), ("executing", "executed"), ("executing", "failed"),
                 ("executing", "unknown"), ("executing", "expired")]:
        fresh = park(ledger, tool_call_id="edge-" + "-".join(edge))
        path = {"parked": [], "approved": [("parked", "approved")],
                "executing": [("parked", "approved"), ("approved", "executing")]}[edge[0]]
        for a, b in path:
            assert ledger.transition(fresh.park_id, a, b)
        assert ledger.transition(fresh.park_id, *edge) is True
    # nothing leaves a terminal state
    done = park(ledger, tool_call_id="done")
    ledger.transition(done.park_id, "parked", "denied")
    with pytest.raises(ValueError):
        ledger.transition(done.park_id, "denied", "approved")


def test_active_for_session(tmp_path):
    ledger = make_ledger(tmp_path)
    a = park(ledger, session_key="tg:1", tool_call_id="a")
    b = park(ledger, session_key="tg:1", tool_call_id="b")
    c = park(ledger, session_key="tg:1", tool_call_id="c")
    d = park(ledger, session_key="tg:1", tool_call_id="d")
    other = park(ledger, session_key="tg:2", tool_call_id="e")
    ledger.transition(b.park_id, "parked", "approved")
    ledger.transition(c.park_id, "parked", "approved")
    ledger.transition(c.park_id, "approved", "executing")
    ledger.transition(d.park_id, "parked", "denied")
    active = {r.park_id for r in ledger.active_for_session("tg:1")}
    assert active == {a.park_id, b.park_id, c.park_id}
    assert [r.park_id for r in ledger.active_for_session("tg:2")] == [other.park_id]
    assert ledger.active_for_session("tg:none") == []
    assert {r.park_id for r in ledger.list_by_status("approved", "executing")} == {b.park_id, c.park_id}
    assert ledger.list_by_status() == []


def test_set_request_id_and_mark_notified(tmp_path):
    ledger = make_ledger(tmp_path)
    row = park(ledger)
    assert row.request_id is None and row.notified_at is None
    ledger.set_request_id(row.park_id, "req-1")
    ledger.mark_notified(row.park_id)
    got = ledger.get(row.park_id)
    assert got.request_id == "req-1"
    assert got.notified_at is not None


def test_find_executing_match_and_gateway_pass_once(tmp_path):
    ledger = make_ledger(tmp_path)
    args = {"amount": 5, "to": "x"}
    row = park(ledger, args=args)
    digest = args_digest(args)  # R22: the same normalized digest the bridge computes
    # not executing yet -> no match
    assert ledger.find_executing_match(row.tool, digest) is None
    ledger.transition(row.park_id, "parked", "approved")
    ledger.transition(row.park_id, "approved", "executing")
    match = ledger.find_executing_match(row.tool, digest)
    assert match is not None and match.park_id == row.park_id
    assert ledger.find_executing_match(row.tool, "0" * 64) is None
    assert ledger.find_executing_match("other_tool", digest) is None
    assert ledger.mark_gateway_passed(row.park_id) is True
    assert ledger.mark_gateway_passed(row.park_id) is False
    assert ledger.get(row.park_id).gateway_passed_at is not None


def test_ledger_uses_wal_and_unique_constraint(tmp_path):
    ledger = make_ledger(tmp_path)
    park(ledger)
    con = sqlite3.connect(str(tmp_path / "ledger.db"))
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO parks (park_id, session_key, session_id, tool_call_id, tool, server, args_json,"
                " args_digest, call_key, status, created_at, updated_at)"
                " VALUES ('P-DUPLICAT','k','s1','c1','t','s','{}','d','c','parked','t','t')"
            )
    finally:
        con.close()


# ---- R22: gateway argument normalization ------------------------------------------------------

PARAMS = {"params": {"CustomerRef": {"value": "58"}, "TotalAmt": 12.5}}


def test_missing_scope_digests_like_the_gateway_default_scope():
    assert args_digest(PARAMS) == args_digest({**PARAMS, "withhuman_scope": "default"})


def test_an_explicit_other_scope_is_part_of_the_match():
    assert args_digest({**PARAMS, "withhuman_scope": "other"}) != args_digest(PARAMS)
    assert args_digest({**PARAMS, "withhuman_scope": "other"}) == args_digest({**PARAMS, "withhuman_scope": "other"})


def test_withhuman_context_is_ignored():
    assert args_digest({**PARAMS, "withhuman_context": "paying invoice 42"}) == args_digest(PARAMS)
    assert args_digest({**PARAMS, "withhuman_context": "x", "withhuman_scope": "default"}) == args_digest(PARAMS)


def test_only_top_level_keys_are_normalized():
    nested = {"params": {"withhuman_context": "kept", "withhuman_scope": "kept"}}
    assert normalize_args(nested) == {**nested, "withhuman_scope": "default"}
    assert args_digest(nested) != args_digest({"params": {}})


def test_normalize_args_never_mutates_its_input():
    original = {"params": {"a": [1, 2]}, "withhuman_context": "c"}
    snapshot = json.loads(json.dumps(original))
    normalized = normalize_args(original)
    normalized["params"]["a"].append(3)
    assert original == snapshot
    assert normalize_args({"x": 1}) == {"x": 1, "withhuman_scope": "default"}


def test_non_dict_arguments_digest_as_before():
    assert args_digest([1, 2]) == hashlib.sha256(canonical([1, 2]).encode()).hexdigest()


def test_park_stores_the_models_arguments_unchanged_and_matches_gateway_style_arguments(tmp_path):
    ledger = make_ledger(tmp_path)
    model_args = {**PARAMS, "withhuman_context": "why"}
    row = park(ledger, args=model_args)
    assert json.loads(row.args_json) == model_args  # the executor still dispatches exactly these
    assert ledger.transition(row.park_id, "parked", "approved")
    assert ledger.transition(row.park_id, "approved", "executing", executing_at="2026-10-19T12:00:00.000000Z")
    gateway_args = {**PARAMS, "withhuman_scope": "default"}
    assert ledger.find_executing_match(row.tool, args_digest(gateway_args)).park_id == row.park_id
    assert ledger.find_executing_match(row.tool, args_digest({**PARAMS, "withhuman_scope": "other"})) is None
