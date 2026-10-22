import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import warnings

import pytest

with warnings.catch_warnings():
    # starlette 1.9 warns at import that its TestClient prefers httpx2 (a third-party notice, raised while the
    # module is collected, so a filterwarnings mark cannot reach it). Ignore exactly that warning here so
    # `pytest -W error` still fails on every other warning.
    from starlette.exceptions import StarletteDeprecationWarning

    warnings.filterwarnings("ignore", category=StarletteDeprecationWarning,
                            message=r"Using `httpx` with `starlette\.testclient` is deprecated")
    from starlette.testclient import TestClient

import bridge.app as bridge_app
from bridge.app import (BLOCK_TYPE, DEFAULT_SECRET_FILE, NO_PARK_REASON, create_app, load_secret, parse_args,
                        verify_signature)
from bridge.verify import expected_park
from bridge.whapi import DEFAULT_API_BASE, DEFAULT_KEY_FILE, ListError
from durable_park.gatecall import gateway_slug
from durable_park.ledger import args_digest

SECRET = "whsec_0123456789abcdefTESTSECRETvalue=="
FAKE_KEY = "whk_FAKE-ORG-KEY-do-not-leak-0123456789"
NOW = 1_800_000_000.0
TOOL = "create_payment"
SERVER = "quickbooks"
ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}, "withhuman_scope": "default"}
PRIVATE_VALUE = "ACME-PRIVATE-VALUE-123"
PARK_TOOL, PARK_SERVER = expected_park(SERVER, TOOL)


def sign(body: bytes, *, secret=SECRET, t=NOW):
    t = int(t)
    mac = hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def payload(*, server=SERVER, tool=TOOL, arguments=None, slug="proto-agents", preview=False,
            type_=BLOCK_TYPE, delivery_id="del-1", idempotency_key="idem-1", request_id="req_1"):
    request = {"tool": tool, "server": server,
               "arguments": ARGS if arguments is None else arguments,
               "created_at": "2026-10-19T12:00:00Z", "deadline_at": "2026-10-19T12:10:00Z"}
    if request_id is not None:
        request["id"] = request_id
    return {
        "version": "2026-09-01", "type": type_, "preview": preview,
        "delivery_id": delivery_id, "idempotency_key": idempotency_key, "organization_id": "org_1",
        "block": {"id": "blk_1", "name": "durable-park-bridge", "attempt": 1, "max_attempts": 2},
        "pipeline": {"evaluation_id": "ev_1"},
        "request": request,
        "agent": {"slug": slug, "name": "Proto Agents"},
    }


# ---- withHuman-shaped request records and a fake lister --------------------------------------

def iso(offset: float) -> str:
    return datetime.fromtimestamp(NOW + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def park_item(*, id="park_req_1", args=None, consumed=-10, tool=PARK_TOOL, server=PARK_SERVER,
              slug="proto-agents", outcome=None):
    """The park request withHuman holds after the model asked, a person approved and the executor consumed it."""
    return {
        "id": id, "agent_slug": slug, "tool": tool, "server": server,
        "arguments": ARGS if args is None else args, "context": {},
        "status": "approved",
        "decision": {"status": "approved", "decided_at": iso(consumed - 60), "expires_at": iso(86400),
                     "note": None},
        "consumption": {"consumed_at": iso(consumed), "outcome": outcome},
        "created_at": iso(consumed - 120), "deadline_at": iso(86400),
    }


def gateway_item(*, id, created, args=None, server=SERVER, tool=TOOL, status="pending", slug="proto-agents"):
    """A request that came through the gateway (what the webhook block is asked about)."""
    return {
        "id": id, "agent_slug": slug, "tool": tool, "server": server,
        "arguments": ARGS if args is None else args, "context": {},
        "status": status, "decision": None, "consumption": None,
        "created_at": iso(created), "deadline_at": iso(created + 600),
    }


class FakeLister:
    """Records every ``list`` call and answers from canned lists keyed by ``q`` (or raises)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.by_q: dict[str, list] = {}
        self.raises: dict[str, Exception] = {}

    def list(self, *, agent_slug, q, limit=50, deadline=None):
        self.calls.append({"agent_slug": agent_slug, "q": q, "limit": limit, "deadline": deadline})
        if q in self.raises:
            raise self.raises[q]
        return list(self.by_q.get(q, []))


@pytest.fixture
def lister():
    return FakeLister()


@pytest.fixture
def client(lister):
    return TestClient(create_app(secret=SECRET, lister=lister, expected_agent="proto-agents",
                                 clock=lambda: NOW))


def arm(lister, *, park=None, neighbours=None, park_q=PARK_TOOL):
    """Make withHuman hold one consumed park for the call, and the gateway request itself (req_1).

    The neighbour listing is asked for with ``q=None`` (every request of the agent, newest first)."""
    lister.by_q[park_q] = [park_item() if park is None else park]
    lister.by_q[None] = [gateway_item(id="req_1", created=-5)] if neighbours is None else neighbours


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


def approves(client, key="k-ok", **kw):
    """A well-formed delivery that must be approved: proves a preceding human was for the intended reason."""
    resp = deliver(client, key=key, **kw)
    assert resp.status_code == 200 and resp.json()["outcome"] == "approve", resp.text


def human(resp):
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "human", "reason": NO_PARK_REASON}


def bridge_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "durable_park.bridge"]


# ---- the core decision ------------------------------------------------------------------------

def test_bridge_approves_when_an_approved_consumed_park_matches(client, lister):
    arm(lister)
    resp = deliver(client)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"outcome": "approve", "reason": "Matches approved park request park_req_1"}


def test_bridge_asks_withhuman_for_parks_by_tool_name_then_for_all_recent_requests_of_the_agent(client, lister):
    arm(lister)
    deliver(client)
    assert [(c["agent_slug"], c["q"], c["limit"]) for c in lister.calls] == [
        ("proto-agents", PARK_TOOL, 50), ("proto-agents", None, 200)]


def test_bridge_unmatched_goes_to_human(client, lister):
    human(deliver(client))  # withHuman holds nothing
    arm(lister, park=park_item(tool="mcp__wh__quickbooks__create_invoice"))  # a park for another tool
    human(deliver(client, key="k-other"))


def test_bridge_args_mismatch_goes_to_human(client, lister):
    arm(lister)
    human(deliver(client, payload(arguments=dict(ARGS, Amount=6))))
    approves(client)  # the exact arguments are what gets approved


def test_bridge_unapproved_park_goes_to_human(client, lister):
    parked = park_item()
    parked["status"] = "pending"
    arm(lister, park=parked)
    human(deliver(client))


def test_bridge_second_gateway_request_after_consumption_goes_to_human(client, lister):
    arm(lister, neighbours=[gateway_item(id="req_0", created=-8), gateway_item(id="req_1", created=-5)])
    human(deliver(client))


def test_bridge_gateway_request_before_consumption_does_not_count_as_a_use(client, lister):
    arm(lister, neighbours=[gateway_item(id="req_0", created=-3600), gateway_item(id="req_1", created=-5)])
    approves(client)


def test_retry_of_the_same_gateway_request_approves_again(client, lister):
    arm(lister)
    first = deliver(client, payload(delivery_id="a1", idempotency_key="k1"),
                    headers={"WithHuman-Delivery-Id": "a1"})
    retry = deliver(client, payload(delivery_id="a2", idempotency_key="k2"),
                    headers={"WithHuman-Delivery-Id": "a2"})
    assert first.json() == retry.json() == {"outcome": "approve",
                                            "reason": "Matches approved park request park_req_1"}


def test_park_for_another_agent_goes_to_human(client, lister):
    arm(lister, park=park_item(slug="someone-else"))
    human(deliver(client))


# ---- signature, size, routing -----------------------------------------------------------------

def test_bridge_bad_signature_401(client, lister):
    arm(lister)
    raw = json.dumps(payload()).encode()
    wrong = deliver(client, raw=raw, secret="whsec_wrong")
    assert wrong.status_code == 401 and wrong.content == b""
    assert deliver(client, raw=raw, signature=False).status_code == 401
    assert deliver(client, raw=raw, headers={"WithHuman-Signature": "garbage"}).status_code == 401
    assert deliver(client, raw=raw, headers={"WithHuman-Signature": f"t={int(NOW)},v1=zz"}).status_code == 401
    tampered = sign(raw)
    assert deliver(client, raw=raw + b" ", headers={"WithHuman-Signature": tampered}).status_code == 401
    assert lister.calls == []


def test_bridge_stale_timestamp_401(client, lister):
    arm(lister)
    assert deliver(client, t=NOW - 301).status_code == 401
    assert deliver(client, t=NOW + 301).status_code == 401
    assert lister.calls == []
    # exactly at the tolerance edge is still accepted
    assert deliver(client, t=NOW - 300).status_code == 200


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


def test_oversized_body_rejected(client, lister):
    big = b"x" * (1024 * 1024 + 1)
    assert deliver(client, raw=big).status_code == 413
    assert lister.calls == []


def test_only_post_allowed(client):
    assert client.get("/wh/bridge").status_code == 405
    assert client.post("/elsewhere", content=b"{}").status_code == 404


# ---- payload checks short-circuit before any withHuman call ----------------------------------

def test_wrong_agent_slug_goes_to_human_without_asking_withhuman(client, lister):
    arm(lister)
    human(deliver(client, payload(slug="someone-else")))
    assert lister.calls == []
    approves(client)


def test_preview_delivery_goes_to_human_without_asking_withhuman(client, lister):
    arm(lister)
    human(deliver(client, payload(preview=True)))
    assert lister.calls == []
    approves(client, "k-real")  # the real delivery afterwards is judged on its own


def test_other_delivery_type_goes_to_human_without_asking_withhuman(client, lister):
    arm(lister)
    human(deliver(client, payload(type_="webhook_endpoint.test")))
    assert lister.calls == []
    approves(client)


@pytest.mark.parametrize("preview", ["false", 0, None, "true", 1, True, []])
def test_only_explicit_false_preview_proceeds(client, lister, preview):
    arm(lister)
    body = payload()
    body["preview"] = preview
    human(deliver(client, body))
    assert lister.calls == []
    approves(client)


def test_absent_preview_proceeds(client, lister):
    arm(lister)
    body = payload()
    del body["preview"]
    assert deliver(client, body).json()["outcome"] == "approve"


def test_missing_or_mismatched_idempotency_key_goes_to_human_without_asking_withhuman(client, lister):
    arm(lister)
    raw = json.dumps(payload(idempotency_key="body-key")).encode()
    resp = client.post("/wh/bridge", content=raw, headers={"WithHuman-Signature": sign(raw)})  # no header
    human(resp)
    human(deliver(client, payload(idempotency_key="body-key"), headers={"WithHuman-Idempotency-Key": "other"}))
    assert lister.calls == []
    approves(client)


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"type":"request_approval_block.webhook"}',
                                 b'{"type":"request_approval_block.webhook","preview":false,'
                                 b'"agent":{"slug":"proto-agents"},"request":{"tool":"t","server":"s","arguments":[]}}'])
def test_malformed_but_signed_body_goes_to_human_without_asking_withhuman(client, lister, raw):
    human(deliver(client, raw=raw, headers={"WithHuman-Idempotency-Key": "k1"}))
    assert lister.calls == []


def test_missing_secret_answers_401_to_everything(tmp_path, lister):
    assert load_secret(str(tmp_path / "absent")) is None
    app = create_app(secret=None, lister=lister, expected_agent="proto-agents", clock=lambda: NOW)
    resp = deliver(TestClient(app))
    assert resp.status_code == 401 and resp.content == b""
    assert lister.calls == []


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

def test_one_log_line_per_delivery_without_values(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    args = {"Amount": 5, "Memo": PRIVATE_VALUE, "withhuman_scope": "default"}
    arm(lister, park=park_item(args=args), neighbours=[gateway_item(id="req_1", created=-5, args=args)])
    deliver(client, payload(arguments=args, delivery_id="del-77"))
    lines = bridge_lines(caplog)
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
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and "why=agent" in line


def test_endpoint_test_delivery_without_request_logs_why_type(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    body = {"version": "2026-09-01", "type": "webhook_endpoint.test", "delivery_id": "t-1"}
    human(deliver(client, body, key="kt"))
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and "why=type" in line


# ---- the tool name is built the way Hermes registers it ---------------------------------------
# withHuman sends the downstream server's own tool name (e.g. "create-bill"); Hermes registers it as
# mcp__<server>__<tool> with every char outside [A-Za-z0-9_] turned into "_" and names over 64 chars
# clamped with an 8-hex sha256 suffix (hermes-agent tools/mcp_tool_schema.py, v0.22.2). The bridge only
# approves names where that mapping is plain: ASCII words, single "-" or "_" separators, at most 64 chars.

@pytest.mark.parametrize("wire_server, wire_tool, hermes_tool", [
    ("quickbooks", "create-bill", "mcp__wh__quickbooks__create_bill"),
    ("quickbooks", "update-bill", "mcp__wh__quickbooks__update_bill"),
    ("quickbooks", "delete-bill", "mcp__wh__quickbooks__delete_bill"),
    ("quick-books", "create-pay-ment_v2", "mcp__wh__quick_books__create_pay_ment_v2"),
    ("quickbooks", "c-" + "x" * 41, "mcp__wh__quickbooks__c_" + "x" * 41),  # exactly 64 chars: no clamp
])
def test_bridge_asks_for_parks_under_the_hermes_tool_name_and_matches_them(client, lister, wire_server,
                                                                          wire_tool, hermes_tool):
    assert len(hermes_tool) <= 64
    lister.by_q[hermes_tool] = [park_item(tool=hermes_tool, server="park:" + gateway_slug(hermes_tool))]
    lister.by_q[None] = [gateway_item(id="req_1", created=-5, server=wire_server, tool=wire_tool)]
    resp = deliver(client, payload(server=wire_server, tool=wire_tool))
    assert [c["q"] for c in lister.calls] == [hermes_tool, None]
    assert resp.json() == {"outcome": "approve", "reason": "Matches approved park request park_req_1"}


@pytest.mark.parametrize("wire_tool", ["create-bill", "update-bill", "delete-bill"])
def test_bridge_hyphenated_bill_tool_without_park_goes_to_human(client, wire_tool):
    human(deliver(client, payload(tool=wire_tool)))


def test_model_args_park_matches_gateway_delivery_with_default_scope(client, lister):
    params = {"params": {"CustomerRef": {"value": "58"}, "TotalAmt": 12.5}}
    park = park_item(args={**params, "withhuman_context": "paying invoice 42"})  # what the model sent
    arm(lister, park=park, neighbours=[gateway_item(id="req_1", created=-5,
                                                    args={**params, "withhuman_scope": "default"})])
    human(deliver(client, payload(arguments={**params, "withhuman_scope": "other"})))  # another scope: another call
    resp = deliver(client, payload(arguments={**params, "withhuman_scope": "default"}, idempotency_key="k-gw"))
    assert resp.json() == {"outcome": "approve", "reason": "Matches approved park request park_req_1"}


def test_bridge_matches_when_gateway_rewrites_2_point_0_as_2(client, lister):
    model_args = {"params": {"bill": {"Line": [{"Amount": 2.0, "DetailType": "AccountBasedExpenseLineDetail"}],
                                      "TotalAmt": 2.0, "VendorRef": {"value": "56"}}},
                  "withhuman_context": "test bill"}
    gateway_args = {"params": {"bill": {"Line": [{"Amount": 2, "DetailType": "AccountBasedExpenseLineDetail"}],
                                        "TotalAmt": 2, "VendorRef": {"value": "56"}}},
                    "withhuman_scope": "default"}
    bill_tool = "mcp__wh__quickbooks__create_bill"
    lister.by_q[bill_tool] = [park_item(tool=bill_tool, server="park:" + gateway_slug(bill_tool), args=model_args)]
    lister.by_q[None] = [gateway_item(id="req_1", created=-5, tool="create-bill", args=gateway_args)]
    resp = deliver(client, payload(tool="create-bill", arguments=gateway_args))
    assert resp.json() == {"outcome": "approve", "reason": "Matches approved park request park_req_1"}


# ---- the hardened rule, end to end ---------------------------------------------------------------

def last_why(caplog):
    (line,) = bridge_lines(caplog)
    return line.split("why=")[1].split()[0]


@pytest.mark.parametrize("wire_server, wire_tool", [
    ("quickbooks", "create-" + "x" * 70),  # Hermes would hash this name: the bridge cannot name the park for it
    ("quick__books", "create-bill"), ("quickbooks", "create.bill"), ("quickbooks", "create bill"),
    ("_quickbooks", "create-bill"),
])
def test_unsafe_names_go_to_human_with_why_unsafe_name(client, lister, caplog, wire_server, wire_tool):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    hermes_tool = bridge_app.hermes_tool_name(wire_server, wire_tool)
    lister.by_q[hermes_tool] = [park_item(tool=hermes_tool, server="park:" + gateway_slug(hermes_tool))]
    lister.by_q[None] = [gateway_item(id="req_1", created=-5, server=wire_server, tool=wire_tool)]
    human(deliver(client, payload(server=wire_server, tool=wire_tool)))
    assert last_why(caplog) == "unsafe-name"


def test_a_gateway_request_missing_from_the_listing_goes_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, neighbours=[gateway_item(id="req_0", created=-8)])
    human(deliver(client))
    assert last_why(caplog) == "gateway-request-not-listed"


def test_a_gateway_request_created_long_before_the_release_goes_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, neighbours=[gateway_item(id="req_1", created=-3600)])
    human(deliver(client))
    assert last_why(caplog) == "gateway-request-before-release"


def test_a_listed_gateway_request_with_other_arguments_goes_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, neighbours=[gateway_item(id="req_1", created=-5, args=dict(ARGS, Amount=6))])
    human(deliver(client))
    assert last_why(caplog) == "gateway-request-mismatch"


def recent_others(count):
    """``count`` gateway requests for another tool, all created after the park was consumed."""
    return [gateway_item(id=f"other_{i}", created=-4, tool="create-bill") for i in range(count)]


def test_a_full_neighbour_page_that_does_not_reach_back_to_the_consumption_goes_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, neighbours=[gateway_item(id="req_1", created=-5)] + recent_others(199))
    human(deliver(client))
    assert last_why(caplog) == "neighbours-truncated"


def test_a_full_neighbour_page_that_reaches_back_before_the_consumption_still_approves(client, lister):
    old = gateway_item(id="old_0", created=-3600, tool="create-bill")
    arm(lister, neighbours=[gateway_item(id="req_1", created=-5)] + recent_others(198) + [old])
    assert len(lister.by_q[None]) == 200
    approves(client)


def test_a_page_one_short_of_the_limit_is_not_treated_as_cut_off(client, lister):
    arm(lister, neighbours=[gateway_item(id="req_1", created=-5)] + recent_others(198))
    assert len(lister.by_q[None]) == 199
    approves(client)


def test_a_later_request_for_the_other_spelling_of_the_tool_name_counts_as_a_second_use(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    # the park is for mcp__wh__quickbooks__create_payment: it covers create-payment and create_payment alike
    arm(lister, neighbours=[gateway_item(id="req_1", created=-5, tool="create-payment"),
                            gateway_item(id="req_2", created=-3, tool="create_payment")])
    human(deliver(client, payload(tool="create-payment")))
    assert last_why(caplog) == "already-used"


def test_arguments_the_approver_saw_redacted_go_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    args = {"Amount": 5, "Memo": "[redacted]", "withhuman_scope": "default"}
    arm(lister, park=park_item(args=args), neighbours=[gateway_item(id="req_1", created=-5, args=args)])
    human(deliver(client, payload(arguments=args)))
    assert last_why(caplog) == "redacted-args"


def test_a_secret_looking_argument_goes_to_human_even_when_everything_else_matches(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    args = {"Amount": 5, "api_key": PRIVATE_VALUE, "withhuman_scope": "default"}
    arm(lister, park=park_item(args=args), neighbours=[gateway_item(id="req_1", created=-5, args=args)])
    human(deliver(client, payload(arguments=args)))
    assert last_why(caplog) == "redacted-args"
    assert PRIVATE_VALUE not in caplog.text


def test_a_park_whose_outcome_was_already_reported_goes_to_human(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, park=park_item(outcome="success"))
    human(deliver(client))
    assert last_why(caplog) == "already-reported"


# ---- request.id -------------------------------------------------------------------------------

def body_with_request_id(value):
    body = payload()
    if value == "absent":
        del body["request"]["id"]
    else:
        body["request"]["id"] = value
    return body


@pytest.mark.parametrize("bad_id", ["absent", "", None, 5, ["req_1"], {"id": "req_1"}])
def test_missing_empty_or_non_string_request_id_goes_to_human_without_asking_withhuman(client, lister, bad_id):
    arm(lister)
    human(deliver(client, body_with_request_id(bad_id)))
    assert lister.calls == []


def test_missing_request_id_logs_why_no_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    deliver(client, body_with_request_id("absent"))
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and "why=no-request-id" in line


# ---- one overall deadline, the clock ----------------------------------------------------------

def test_both_list_calls_get_one_deadline_of_monotonic_now_plus_three_and_a_half(lister):
    arm(lister)
    ticks = iter([1000.0, 1001.0, 1002.0, 1003.0])
    app = create_app(secret=SECRET, lister=lister, expected_agent="proto-agents", clock=lambda: NOW,
                     monotonic=lambda: next(ticks))
    deliver(TestClient(app))
    assert [c["deadline"] for c in lister.calls] == [1003.5, 1003.5]


def test_park_age_is_judged_against_the_injected_clock(lister):
    arm(lister)  # consumed 10 s before NOW
    late = create_app(secret=SECRET, lister=lister, expected_agent="proto-agents", clock=lambda: NOW + 200)
    human(deliver(TestClient(late)))  # 210 s old at the gateway call: outside the 120 s window
    on_time = create_app(secret=SECRET, lister=lister, expected_agent="proto-agents", clock=lambda: NOW)
    approves(TestClient(on_time))


# ---- withHuman failures go to a person --------------------------------------------------------

@pytest.mark.parametrize("code", ["no-key", "http-403", "deadline", "transport", "shape"])
def test_list_error_on_the_first_call_goes_to_human_and_logs_its_code(client, lister, caplog, code):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister)
    lister.raises[PARK_TOOL] = ListError(code)
    human(deliver(client))
    assert len(lister.calls) == 1
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and f"why={code}" in line


@pytest.mark.parametrize("code", ["no-key", "http-403", "deadline", "transport", "shape"])
def test_list_error_on_the_second_call_goes_to_human_and_logs_its_code(client, lister, caplog, code):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister)
    lister.raises[None] = ListError(code)
    human(deliver(client))
    assert len(lister.calls) == 2
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and f"why={code}" in line


def test_unexpected_lister_exception_goes_to_human_and_logs_only_its_class(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister)
    lister.raises[PARK_TOOL] = RuntimeError(f"boom {PRIVATE_VALUE} {FAKE_KEY}")
    human(deliver(client))
    (line,) = bridge_lines(caplog)
    assert "why=error:RuntimeError" in line
    assert PRIVATE_VALUE not in caplog.text and FAKE_KEY not in caplog.text


# ---- reasons in the log, nothing sensitive in the answer or the log ---------------------------

def test_approve_line_names_the_park_request(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister)
    deliver(client)
    (line,) = bridge_lines(caplog)
    assert "outcome=approve" in line and "why=park-request=park_req_1" in line


def test_human_line_names_already_used(client, lister, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    arm(lister, neighbours=[gateway_item(id="req_0", created=-8), gateway_item(id="req_1", created=-5)])
    deliver(client)
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and "why=already-used" in line


def test_human_line_names_no_approved_park(client, caplog):
    caplog.set_level(logging.INFO, logger="durable_park.bridge")
    deliver(client)  # withHuman holds nothing
    (line,) = bridge_lines(caplog)
    assert "outcome=human" in line and "why=no-approved-park" in line


def test_responses_never_contain_argument_values(client, lister):
    args = {"Amount": 5, "Memo": PRIVATE_VALUE, "withhuman_scope": "default"}
    arm(lister, park=park_item(args=args), neighbours=[gateway_item(id="req_1", created=-5, args=args)])
    approved = deliver(client, payload(arguments=args))
    refused = deliver(client, payload(arguments=dict(args, Memo=PRIVATE_VALUE + "-x"), idempotency_key="k2"))
    assert approved.json()["outcome"] == "approve" and refused.json()["outcome"] == "human"
    assert PRIVATE_VALUE not in approved.text and PRIVATE_VALUE not in refused.text


def test_logs_never_contain_argument_values_or_keys_even_when_withhuman_fails(client, lister, caplog):
    caplog.set_level(logging.DEBUG)
    args = {"Amount": 5, "Memo": PRIVATE_VALUE, "withhuman_scope": "default"}
    arm(lister, park=park_item(args=args), neighbours=[gateway_item(id="req_1", created=-5, args=args)])
    deliver(client, payload(arguments=args))
    lister.raises[PARK_TOOL] = Exception(f"{PRIVATE_VALUE} {FAKE_KEY}")
    deliver(client, payload(arguments=args, idempotency_key="k2"))
    assert PRIVATE_VALUE not in caplog.text and FAKE_KEY not in caplog.text and SECRET not in caplog.text


def test_ledger_and_delivery_store_are_gone_from_the_bridge():
    for name in ("_DeliveryStore", "LedgerMissing", "DELIVERIES_FILENAME", "Ledger"):
        assert not hasattr(bridge_app, name), name


# ---- config ----------------------------------------------------------------------------------

def test_parse_args_defaults_and_env(monkeypatch):
    for name in ("DURABLE_PARK_BRIDGE_SECRET_FILE", "DURABLE_PARK_EXPECTED_AGENT", "DURABLE_PARK_WH_API_BASE",
                 "DURABLE_PARK_WH_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    ns = parse_args([])
    assert (ns.secret_file, ns.host, ns.port, ns.expected_agent, ns.api_base, ns.key_file) == (
        DEFAULT_SECRET_FILE, "127.0.0.1", 8791, "proto-agents", DEFAULT_API_BASE, DEFAULT_KEY_FILE)
    assert DEFAULT_SECRET_FILE == "/etc/durable-park/bridge_secret"


def test_parse_args_env_overrides(monkeypatch):
    monkeypatch.setenv("DURABLE_PARK_EXPECTED_AGENT", "agent-z")
    monkeypatch.setenv("DURABLE_PARK_WH_API_BASE", "https://wh.example/api/v1")
    monkeypatch.setenv("DURABLE_PARK_WH_KEY_FILE", "/env/key")
    ns = parse_args([])
    assert (ns.expected_agent, ns.api_base, ns.key_file) == ("agent-z", "https://wh.example/api/v1", "/env/key")


def test_parse_args_flags_beat_env(monkeypatch):
    monkeypatch.setenv("DURABLE_PARK_WH_API_BASE", "https://env.example/api/v1")
    ns = parse_args(["--api-base", "https://flag.example/api/v1", "--key-file", "/flag/key"])
    assert (ns.api_base, ns.key_file) == ("https://flag.example/api/v1", "/flag/key")


def test_ledger_flag_is_no_longer_accepted(monkeypatch, capsys):
    monkeypatch.delenv("DURABLE_PARK_LEDGER", raising=False)
    with pytest.raises(SystemExit):
        parse_args(["--ledger", "/x/ledger.db"])
    assert "--ledger" in capsys.readouterr().err  # argparse names the unknown flag


def test_ledger_env_is_ignored(monkeypatch):
    monkeypatch.setenv("DURABLE_PARK_LEDGER", "/env/ledger.db")
    assert not hasattr(parse_args([]), "ledger")


def test_main_builds_the_request_lister_from_the_cli_options(monkeypatch, tmp_path):
    import uvicorn
    built, served = {}, {}

    class Recorder:
        def __init__(self, base_url, key_file):
            built["args"] = (base_url, key_file)

    monkeypatch.setattr(bridge_app, "RequestLister", Recorder)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    secret = tmp_path / "secret"
    secret.write_text(SECRET)
    bridge_app.main(["--secret-file", str(secret), "--api-base", "https://wh.example/api/v1",
                     "--key-file", "/k/key", "--port", "9"])
    assert built["args"] == ("https://wh.example/api/v1", "/k/key")
    assert served["port"] == 9


def test_main_refuses_to_start_on_a_bad_api_base(monkeypatch, tmp_path):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("the server must not start"))
    secret, key = tmp_path / "secret", tmp_path / "key"
    secret.write_text(SECRET)
    key.write_text(FAKE_KEY)
    with pytest.raises(SystemExit) as exc:
        bridge_app.main(["--secret-file", str(secret), "--key-file", str(key),
                         "--api-base", "http://insecure.example/api/v1"])
    message = str(exc.value.code)
    assert "--api-base" in message and "https" in message
    assert FAKE_KEY not in message and "insecure.example" not in message
