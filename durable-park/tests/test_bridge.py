import hashlib
import hmac
import json
import logging
import sqlite3
import threading

import pytest
from starlette.testclient import TestClient

from bridge.app import (BLOCK_TYPE, DEFAULT_SECRET_FILE, NO_PARK_REASON, _DeliveryStore, create_app,
                        load_secret, parse_args, verify_signature)
from durable_park.ledger import Ledger, args_digest

SECRET = "whsec_0123456789abcdefTESTSECRETvalue=="
NOW = 1_800_000_000.0
TOOL = "create_payment"
SERVER = "quickbooks"
ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}, "withhuman_scope": "default"}
PRIVATE_VALUE = "ACME-PRIVATE-VALUE-123"


def sign(body: bytes, *, secret=SECRET, t=NOW):
    t = int(t)
    mac = hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def payload(*, server=SERVER, tool=TOOL, arguments=None, slug="proto-agents", preview=False,
            type_=BLOCK_TYPE, delivery_id="del-1", idempotency_key="idem-1"):
    return {
        "version": "2026-09-01", "type": type_, "preview": preview,
        "delivery_id": delivery_id, "idempotency_key": idempotency_key, "organization_id": "org_1",
        "block": {"id": "blk_1", "name": "durable-park-bridge", "attempt": 1, "max_attempts": 2},
        "pipeline": {"evaluation_id": "ev_1"},
        "request": {"id": "req_1", "tool": tool, "server": server,
                    "arguments": ARGS if arguments is None else arguments,
                    "created_at": "2026-10-19T12:00:00Z", "deadline_at": "2026-10-19T12:10:00Z"},
        "agent": {"slug": slug, "name": "Proto Agents"},
    }


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


@pytest.fixture
def client(tmp_path, ledger):
    app = create_app(ledger_path=ledger.path, secret=SECRET, expected_agent="proto-agents",
                     clock=lambda: NOW)
    return TestClient(app)


def executing_park(ledger, *, tool=f"mcp__wh__{SERVER}__{TOOL}", args=None, call="c1"):
    args = ARGS if args is None else args
    row = ledger.park(session_key="tg:1", session_id="s1", tool_call_id=call, tool=tool, args=args,
                      call_key=hashlib.sha256(call.encode()).hexdigest(), server=f"park:{SERVER}")
    assert ledger.transition(row.park_id, "parked", "approved")
    assert ledger.transition(row.park_id, "approved", "executing", executing_at="2026-10-19T12:00:00.000000Z")
    return row


def deliver(client, body_obj=None, *, key="idem-1", headers=None, raw=None, signature=True, t=NOW,
            secret=SECRET):
    """POST a signed delivery. The idempotency header follows the body's idempotency_key (as withHuman does)."""
    if raw is None:
        body_obj = payload(idempotency_key=key) if body_obj is None else body_obj
        raw = json.dumps(body_obj).encode()
        key = body_obj.get("idempotency_key", key)
    hdrs = {"Content-Type": "application/json", "User-Agent": "withHuman-webhooks/1",
            "WithHuman-Delivery-Id": "hdr-del-1", "WithHuman-Idempotency-Key": key}
    if signature:
        hdrs["WithHuman-Signature"] = sign(raw, secret=secret, t=t)
    hdrs.update(headers or {})
    return client.post("/wh/bridge", content=raw, headers=hdrs)


def approves(client, key, **kw):
    """A well-formed delivery that must be approved: proves a preceding human was for the intended reason."""
    resp = deliver(client, key=key, **kw)
    assert resp.status_code == 200 and resp.json()["outcome"] == "approve", resp.text


def human(resp):
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "human", "reason": NO_PARK_REASON}


# ---- the five brief-named tests -------------------------------------------------------------

def test_bridge_approves_matching_executing_once(client, ledger):
    row = executing_park(ledger)
    first = deliver(client, payload(delivery_id="d1", idempotency_key="k1"),
                    headers={"WithHuman-Idempotency-Key": "k1"})
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("application/json")
    assert first.json() == {"outcome": "approve", "reason": f"Matches approved park {row.park_id}"}
    assert ledger.get(row.park_id).gateway_passed_at is not None
    # a second identical call (a different delivery) is not auto-approved again
    second = deliver(client, payload(delivery_id="d2", idempotency_key="k2"),
                     headers={"WithHuman-Idempotency-Key": "k2"})
    human(second)


def test_bridge_unmatched_goes_to_human(client, ledger):
    human(deliver(client))  # nothing parked at all
    executing_park(ledger, tool="mcp__wh__quickbooks__create_invoice")  # other tool is executing
    human(deliver(client, key="k-other"))


def test_bridge_args_mismatch_goes_to_human(client, ledger):
    row = executing_park(ledger)
    changed = dict(ARGS, Amount=6)
    human(deliver(client, payload(arguments=changed)))
    assert ledger.get(row.park_id).gateway_passed_at is None
    approves(client, "k-ok")  # the exact arguments are what gets approved


def test_bridge_bad_signature_401(client, ledger):
    row = executing_park(ledger)
    raw = json.dumps(payload()).encode()
    wrong = deliver(client, raw=raw, secret="whsec_wrong")
    assert wrong.status_code == 401 and wrong.content == b""
    assert deliver(client, raw=raw, signature=False).status_code == 401
    assert deliver(client, raw=raw, headers={"WithHuman-Signature": "garbage"}).status_code == 401
    assert deliver(client, raw=raw, headers={"WithHuman-Signature": f"t={int(NOW)},v1=zz"}).status_code == 401
    tampered = sign(raw)
    assert deliver(client, raw=raw + b" ", headers={"WithHuman-Signature": tampered}).status_code == 401
    assert ledger.get(row.park_id).gateway_passed_at is None


def test_bridge_stale_timestamp_401(client, ledger):
    row = executing_park(ledger)
    assert deliver(client, t=NOW - 301).status_code == 401
    assert deliver(client, t=NOW + 301).status_code == 401
    assert ledger.get(row.park_id).gateway_passed_at is None
    # exactly at the tolerance edge is still accepted
    assert deliver(client, t=NOW - 300).status_code == 200


# ---- signature details ----------------------------------------------------------------------

def test_signature_key_is_the_whole_whsec_string_as_text():
    raw = b'{"a":1}'
    mac = hmac.new(SECRET.encode(), b"1800000000." + raw, hashlib.sha256).hexdigest()
    assert verify_signature(f"t=1800000000,v1={mac}", raw, SECRET, NOW)
    import base64
    decoded_key = base64.b64decode(SECRET[len("whsec_"):] + "=" * 8, validate=False)
    bad = hmac.new(decoded_key, b"1800000000." + raw, hashlib.sha256).hexdigest()
    assert not verify_signature(f"t=1800000000,v1={bad}", raw, SECRET, NOW)


def test_signature_header_edge_cases():
    raw = b"{}"
    good = sign(raw)
    assert verify_signature(good, raw, SECRET, NOW)
    assert not verify_signature(None, raw, SECRET, NOW)
    assert not verify_signature("", raw, SECRET, NOW)
    assert not verify_signature(good, raw, None, NOW)
    t, v1 = good.split(",")
    assert verify_signature(f"{v1}, {t}", raw, SECRET, NOW)  # order/space tolerant
    assert not verify_signature(f"{good},{v1}", raw, SECRET, NOW)  # duplicate v1 refused
    assert not verify_signature(f"{t},v1={'0' * 64}", raw, SECRET, NOW)
    assert not verify_signature(f"t=-1,{v1}", raw, SECRET, NOW)
    assert not verify_signature(f"t=1e9,{v1}", raw, SECRET, NOW)


def test_oversized_body_rejected(client):
    big = b"x" * (1024 * 1024 + 1)
    assert deliver(client, raw=big).status_code == 413


def test_only_post_allowed(client):
    assert client.get("/wh/bridge").status_code == 405
    assert client.post("/elsewhere", content=b"{}").status_code == 404


# ---- R20 -------------------------------------------------------------------------------------

def test_wrong_agent_slug_goes_to_human(client, ledger):
    row = executing_park(ledger)
    human(deliver(client, payload(slug="someone-else")))
    assert ledger.get(row.park_id).gateway_passed_at is None
    approves(client, "k-ok")


def test_preview_delivery_goes_to_human_and_never_marks(client, ledger):
    row = executing_park(ledger)
    human(deliver(client, payload(preview=True)))
    assert ledger.get(row.park_id).gateway_passed_at is None
    # the real delivery afterwards still gets its one approval
    approves(client, "k-real")


def test_other_delivery_type_goes_to_human(client, ledger):
    row = executing_park(ledger)
    human(deliver(client, payload(type_="webhook_endpoint.test")))
    assert ledger.get(row.park_id).gateway_passed_at is None
    approves(client, "k-ok")


# ---- idempotent retries ----------------------------------------------------------------------

def test_same_idempotency_key_retry_gets_approve_again(client, ledger):
    row = executing_park(ledger)
    hdr = {"WithHuman-Idempotency-Key": "k1"}
    first = deliver(client, payload(delivery_id="a1", idempotency_key="k1"),
                    headers={**hdr, "WithHuman-Delivery-Id": "a1"})
    retry = deliver(client, payload(delivery_id="a2", idempotency_key="k1"),
                    headers={**hdr, "WithHuman-Delivery-Id": "a2"})
    assert first.json() == retry.json() == {"outcome": "approve", "reason": f"Matches approved park {row.park_id}"}
    # a different delivery for the same call is still refused
    other = deliver(client, payload(idempotency_key="k2"), headers={"WithHuman-Idempotency-Key": "k2"})
    human(other)
    # and a second park with the same call can only be approved by its own delivery
    second = executing_park(ledger, call="c2")
    again = deliver(client, payload(idempotency_key="k3"), headers={"WithHuman-Idempotency-Key": "k3"})
    assert again.json() == {"outcome": "approve", "reason": f"Matches approved park {second.park_id}"}


def test_human_answer_is_replayed_for_same_key(client, ledger):
    human(deliver(client, key="k9"))
    executing_park(ledger)  # a matching park appears after the first answer
    human(deliver(client, key="k9"))  # same evaluation: same answer
    approves(client, "k10")


def test_missing_or_mismatched_idempotency_key_goes_to_human(client, ledger):
    row = executing_park(ledger)
    raw = json.dumps(payload(idempotency_key="body-key")).encode()
    hdrs = {"WithHuman-Signature": sign(raw)}
    resp = client.post("/wh/bridge", content=raw, headers=hdrs)  # no idempotency header
    human(resp)
    human(deliver(client, payload(idempotency_key="body-key"), headers={"WithHuman-Idempotency-Key": "other"}))
    assert ledger.get(row.park_id).gateway_passed_at is None
    approves(client, "k-ok")


def test_deliveries_live_in_a_separate_file_and_ledger_schema_untouched(client, ledger, tmp_path):
    executing_park(ledger)
    deliver(client, key="k1")
    con = sqlite3.connect(ledger.path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert tables == {"parks"}
    others = [p for p in tmp_path.iterdir() if p.name.startswith("bridge_deliveries")]
    assert others, "expected a bridge_deliveries*.db next to the ledger"


# ---- failure behaviour -----------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"type":"request_approval_block.webhook"}',
                                 b'{"type":"request_approval_block.webhook","preview":false,'
                                 b'"agent":{"slug":"proto-agents"},"request":{"tool":"t","server":"s","arguments":[]}}'])
def test_malformed_but_signed_body_goes_to_human(client, raw):
    human(deliver(client, raw=raw, headers={"WithHuman-Idempotency-Key": "k1"}))


def test_ledger_error_goes_to_human(tmp_path):
    app = create_app(ledger_path=str(tmp_path / "no" / "such" / "dir" / "ledger.db"), secret=SECRET,
                     expected_agent="proto-agents", clock=lambda: NOW)
    human(deliver(TestClient(app)))


def test_missing_secret_answers_401_to_everything(tmp_path, ledger, caplog):
    assert load_secret(str(tmp_path / "absent")) is None
    app = create_app(ledger_path=ledger.path, secret=None, expected_agent="proto-agents", clock=lambda: NOW)
    resp = deliver(TestClient(app))
    assert resp.status_code == 401 and resp.content == b""


def test_load_secret(tmp_path, caplog):
    path = tmp_path / "s"
    path.write_text(SECRET + "\n")
    assert load_secret(str(path)) == SECRET
    path.write_text("   \n")
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    assert load_secret(str(path)) is None
    assert load_secret(str(tmp_path / "absent")) is None
    assert SECRET not in caplog.text
    assert "secret" in caplog.text.lower()


# ---- logging ---------------------------------------------------------------------------------

def test_one_log_line_per_delivery_without_values(client, ledger, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    args = {"Amount": 5, "Memo": PRIVATE_VALUE, "withhuman_scope": "default"}
    row = executing_park(ledger, args=args)
    deliver(client, payload(arguments=args, delivery_id="del-77"))
    lines = [r.getMessage() for r in caplog.records if r.name == "durable_park.bridge"]
    assert len(lines) == 1
    line = lines[0]
    for needed in ("del-77", SERVER, TOOL, "Amount,Memo,withhuman_scope", args_digest(args), "approve"):
        assert needed in line, needed
    assert PRIVATE_VALUE not in caplog.text
    assert SECRET not in caplog.text
    assert "default" not in line  # even the scope VALUE is not logged


def test_rejected_signature_logs_without_secret(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    deliver(client, secret="whsec_wrong")
    assert SECRET not in caplog.text and "whsec_wrong" not in caplog.text
    assert any("401" in r.getMessage() or "signature" in r.getMessage() for r in caplog.records)


def test_human_line_names_the_reason_tag(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    deliver(client, payload(slug="other"))
    (line,) = [r.getMessage() for r in caplog.records if r.name == "durable_park.bridge"]
    assert "outcome=human" in line and "why=agent" in line


def test_endpoint_test_delivery_without_request_logs_why_type(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    body = {"version": "2026-09-01", "type": "webhook_endpoint.test", "delivery_id": "t-1"}
    human(deliver(client, body, key="kt"))
    (line,) = [r.getMessage() for r in caplog.records if r.name == "durable_park.bridge"]
    assert "outcome=human" in line and "why=type" in line


@pytest.mark.parametrize("preview", ["false", 0, None, "true", 1, True, []])
def test_only_explicit_false_preview_proceeds(client, ledger, preview):
    row = executing_park(ledger)
    body = payload()
    body["preview"] = preview
    human(deliver(client, body))
    assert ledger.get(row.park_id).gateway_passed_at is None
    approves(client, "k-ok")


def test_absent_preview_proceeds(client, ledger):
    executing_park(ledger)
    body = payload()
    del body["preview"]
    body["idempotency_key"] = "k-absent"
    assert deliver(client, body).json()["outcome"] == "approve"


# ---- replay is bound to the call -------------------------------------------------------------

def test_replay_with_changed_arguments_goes_to_human(client, ledger):
    first = executing_park(ledger)
    other_args = dict(ARGS, Amount=6)
    other = executing_park(ledger, args=other_args, call="c2")  # a legitimate, unused park for the NEW args
    approves(client, "k1")
    human(deliver(client, payload(arguments=other_args, idempotency_key="k1")))
    assert ledger.get(other.park_id).gateway_passed_at is None
    assert ledger.get(first.park_id).gateway_passed_at is not None
    # the genuine retry (same body) still replays the approve
    assert deliver(client, payload(idempotency_key="k1")).json() == {
        "outcome": "approve", "reason": f"Matches approved park {first.park_id}"}
    # and the other park is approvable under its own key
    approves(client, "k2", body_obj=payload(arguments=other_args, idempotency_key="k2"))


def test_replay_with_different_tool_goes_to_human(client, ledger):
    first = executing_park(ledger)
    invoice = executing_park(ledger, tool="mcp__wh__quickbooks__create_invoice", call="c3")
    approves(client, "k1")
    human(deliver(client, payload(tool="create_invoice", idempotency_key="k1")))
    assert ledger.get(invoice.park_id).gateway_passed_at is None
    human(deliver(client, payload(server="docusign", idempotency_key="k1")))


def test_replay_mismatch_is_logged(client, ledger, caplog):
    executing_park(ledger)
    approves(client, "k1")
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    deliver(client, payload(arguments={"Amount": 7}, idempotency_key="k1"))
    assert "why=replay-mismatch" in caplog.text and "outcome=human" in caplog.text


def test_store_binds_agent_slug_too(tmp_path):
    store = _DeliveryStore(str(tmp_path / "d.db"))
    calls = []

    def decide():
        calls.append(1)
        return "approve", "P-1"

    assert store.decide_once("k", "d1", "n", "dg", "agent-a", decide) == ("approve", "P-1", "new")
    assert store.decide_once("k", "d2", "n", "dg", "agent-a", decide) == ("approve", "P-1", "replay")
    for changed in (("n2", "dg", "agent-a"), ("n", "dg2", "agent-a"), ("n", "dg", "agent-b")):
        assert store.decide_once("k", "d3", *changed, decide) == ("human", None, "replay-mismatch")
    assert calls == [1]


def test_old_deliveries_file_without_binding_columns_is_migrated(tmp_path):
    path = tmp_path / "d.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE deliveries (idempotency_key TEXT PRIMARY KEY, delivery_id TEXT,"
                " outcome TEXT NOT NULL, park_id TEXT, created_at TEXT NOT NULL)")
    con.execute("INSERT INTO deliveries VALUES ('k','d','approve','P-1','t')")
    con.commit()
    con.close()
    store = _DeliveryStore(str(path))
    assert store.decide_once("k", "d2", "n", "dg", "a", lambda: ("approve", "P-2")) == ("human", None, "replay-mismatch")


# ---- ledger path safety ----------------------------------------------------------------------

def test_missing_ledger_file_is_never_created(tmp_path, caplog):
    path = tmp_path / "ledger.db"  # directory exists, file does not
    app = create_app(ledger_path=str(path), secret=SECRET, expected_agent="proto-agents", clock=lambda: NOW)
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    c = TestClient(app)
    human(deliver(c, key="k1"))
    human(deliver(c, key="k2"))
    assert not path.exists()
    assert not (tmp_path / "bridge_deliveries.db").exists()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "does not exist" in errors[0].getMessage()
    assert "why=ledger-missing" in caplog.text
    # once the ledger appears (created by the plugin), the bridge works without a restart
    ledger = Ledger(str(path))
    executing_park(ledger)
    approves(c, "k3")


# ---- concurrency -----------------------------------------------------------------------------

def test_two_threads_same_park_different_keys_exactly_one_approve(tmp_path, ledger):
    app = create_app(ledger_path=ledger.path, secret=SECRET, expected_agent="proto-agents", clock=lambda: NOW)
    for n in range(6):
        row = executing_park(ledger, args=dict(ARGS, Amount=100 + n), call=f"cc{n}")
        barrier = threading.Barrier(2)
        results = []

        def worker(key):
            c = TestClient(app)
            barrier.wait()
            results.append(deliver(c, payload(arguments=dict(ARGS, Amount=100 + n), idempotency_key=key)).json())

        threads = [threading.Thread(target=worker, args=(f"race-{n}-{i}",)) for i in range(2)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(r["outcome"] for r in results) == ["approve", "human"], results
        assert ledger.get(row.park_id).gateway_passed_at is not None


# ---- config ----------------------------------------------------------------------------------

def test_parse_args_defaults_and_env(monkeypatch):
    monkeypatch.delenv("DURABLE_PARK_LEDGER", raising=False)
    monkeypatch.delenv("DURABLE_PARK_BRIDGE_SECRET_FILE", raising=False)
    monkeypatch.delenv("DURABLE_PARK_EXPECTED_AGENT", raising=False)
    ns = parse_args(["--ledger", "/x/ledger.db"])
    assert (ns.ledger, ns.secret_file, ns.host, ns.port, ns.expected_agent) == (
        "/x/ledger.db", DEFAULT_SECRET_FILE, "127.0.0.1", 8791, "proto-agents")
    assert DEFAULT_SECRET_FILE == "/etc/durable-park/bridge_secret"
    monkeypatch.setenv("DURABLE_PARK_LEDGER", "/env/ledger.db")
    monkeypatch.setenv("DURABLE_PARK_EXPECTED_AGENT", "agent-z")
    ns = parse_args([])
    assert ns.ledger == "/env/ledger.db" and ns.expected_agent == "agent-z"
    monkeypatch.delenv("DURABLE_PARK_LEDGER")
    with pytest.raises(SystemExit):
        parse_args([])
