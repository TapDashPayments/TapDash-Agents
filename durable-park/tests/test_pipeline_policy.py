"""Policy tests for the withHuman entry pipeline revision kept as a reviewed file.

The evaluator below supports only the condition grammar the file uses:
all / any / equals / in / starts_with / regex (re.search on the pattern as written).
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import uuid
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parent.parent / "deploy" / "withhuman" / "entry-pipeline.json"

EXPECTED_ORDER = [
    "pa-park-money",
    "block-06ebcdd6",
    "pa-gateway-read",
    "qb-gateway-write-bridge",
    "pa-park-quickbooks-read",
    "pa-hermes-read",
    "pa-hermes-local-deny",
    "pa-park-maverick-auto",
    "pa-park-maverick-note",
    "pa-park-maverick-ticket",
    "pa-gateway-maverick-auto",
    "pa-gateway-maverick-note",
    "pa-gateway-maverick-ticket",
    "mav-gateway-write-bridge",
    "money-server-human",
    "pa-park-supabase-read",
    "pa-park-monday-read",
    "pa-park-docusign-read",
    "gw-supabase",
    "gw-monday",
    "gw-docusign",
    "pa-mcp-human",
    "pa-default-human",
    "hermes",
    "ask-unruled-call",
]

MONEY_TYPES = [
    "payment", "bill_payment", "transfer", "deposit", "journal_entry", "refund_receipt",
    "sales_receipt", "credit_memo", "vendor_credit", "purchase", "invoice", "bill",
]

# Rev 8 blocks kept unchanged (snapshot_id, config_version, content_hash stripped).
REV8_HERMES = {
    "id": "hermes",
    "name": "Hermes Agent",
    "type": "branch",
    "enabled": True,
    "timeout": "50ms",
    "max_attempts": 1,
    "config": {
        "when": {
            "id": "55ca73c4-ad89-4a29-8966-825e48ba57b4",
            "any": [
                {
                    "id": "d3603274-548a-5cf5-8884-cd89358c9fb8",
                    "field": "/request/tool",
                    "operator": "contains",
                    "value": "Stripe",
                }
            ],
        },
        "blocks": [
            {
                "id": "hermes-approve-the-rest",
                "name": "Ask a human",
                "type": "outcome",
                "enabled": True,
                "timeout": "50ms",
                "max_attempts": 1,
                "config": {
                    "outcome": "human",
                    "reason": "This tool requires a human to approve prior to running.",
                    "escalation_path": "notify-yaakov-yehuda",
                },
            }
        ],
        "reason": "No rule for Hermes Agent decided this request, so it waits for a person.",
        "escalation_path": "notify-yaakov-yehuda",
        "ask_agent_owner": True,
    },
}

# The catch-all asks a person (safety/default-deny). It replaced rev 8's ``approve-every-call``, which approved every call
# (money included) for every agent other than proto-agents, including a new tool whose Deck "Ask me" setting
# was still a pending change.
ASK_UNRULED_CALL = {
    "id": "ask-unruled-call",
    "name": "Ask about every unruled call",
    "type": "outcome",
    "enabled": True,
    "timeout": "50ms",
    "max_attempts": 1,
    "config": {
        "outcome": "human",
        "reason": "No rule covers this tool yet, so a person decides. Nothing unruled is auto-approved.",
        "escalation_path": "notify-yaakov-yehuda",
    },
}

REV8_QUICKBOOKS_WEBHOOK = {
    "id": "block-06ebcdd6",
    "name": "proto-agents risky QuickBooks",
    "type": "branch",
    "enabled": True,
    "timeout": "50ms",
    "max_attempts": 1,
    "config": {
        "when": {
            "id": "e5a5422a-13b0-43db-b141-80a828bd50d4",
            "all": [
                {"id": "e9497130-c119-4887-b1fa-e414db40255a", "field": "/agent/slug",
                 "operator": "equals", "value": "proto-agents"},
                {"id": "99db7f5b-2c27-4d17-839f-bb2bc0fdec98", "field": "/request/server",
                 "operator": "equals", "value": "quickbooks"},
                {"id": "18ca8de9-75fc-46b7-9645-39927857ece4", "field": "/request/tool",
                 "operator": "regex",
                 "value": "^(quickbooks__)?(create|update|delete)[_-](payment|bill_payment|transfer|deposit|journal_entry|refund_receipt|sales_receipt|credit_memo|vendor_credit|purchase|invoice|bill)$"},
            ],
        },
        "blocks": [
            {
                "id": "block-ad4daeb4",
                "name": "durable-park-bridge",
                "type": "webhook",
                "enabled": True,
                "timeout": "5s",
                "max_attempts": 3,
                "config": {
                    "endpoint_key": "durable-park-bridge",
                    "allowed_outcomes": ["human", "approve"],
                    "include": {"agent_reasoning": False, "context": False, "previous_metadata": False},
                    "reason": "durable-park bridge: approves only a call matching an approved park; anything else goes to Yaakov.",
                    "escalation_path": "notify-yaakov-yehuda",
                },
            }
        ],
        "reason": "",
    },
}


@pytest.fixture(scope="module")
def doc():
    return json.loads(PIPELINE.read_text())


@pytest.fixture(scope="module")
def blocks(doc):
    return doc["blocks"]


def _walk_conditions(node):
    yield node
    for child in node.get("all", []) + node.get("any", []):
        yield from _walk_conditions(child)


def _all_condition_nodes(blocks):
    for block in blocks:
        when = block.get("config", {}).get("when")
        if when:
            yield from _walk_conditions(when)


class _EvalError(Exception):
    """withHuman stops a block with an evaluation error (seen with preview_pipeline 2026-10-25: is_false on null, 0 or
    the text "false"); the block then ends as a person (reason_code block_error)."""


def _evaluate(node, request):
    if "all" in node:
        return all(_evaluate(c, request) for c in node["all"])
    if "any" in node:
        return any(_evaluate(c, request) for c in node["any"])
    value = request.get(node["field"])
    op, expected = node["operator"], node.get("value")
    if op == "is_false":  # withHuman: JSON false matches; missing does not match; null/0/"false" is an error
        if node["field"] not in request:
            return False
        if isinstance(value, bool):
            return value is False
        raise _EvalError(node["field"])
    if op == "equals":
        return value == expected
    if value is None:
        return False
    if op == "in":
        return value in expected
    if op == "starts_with":
        return any(value.startswith(p) for p in expected)
    if op == "regex":
        return re.search(expected, value) is not None
    if op == "contains":
        return expected in value
    raise AssertionError(f"unsupported operator {op}")


def _inside(blocks, request):
    """The outcome inside a branch the request entered: nested branches are walked in order; a branch that runs out
    of blocks ends with a person (withHuman: decided inside it or it goes to a human)."""
    for inner in blocks:
        if inner["type"] == "branch":
            try:
                if not _evaluate(inner["config"]["when"], request):
                    continue
            except _EvalError:
                return "human"
            return _inside(inner["config"]["blocks"], request)
        return inner["config"]["outcome"] if inner["type"] == "outcome" else inner["type"]
    return "human"


def _decide(blocks, agent, server, tool, arguments=None):
    request = {"/agent/slug": agent, "/request/server": server, "/request/tool": tool}
    request.update({f"/request/arguments/{k}": v for k, v in (arguments or {}).items()})
    for block in blocks:
        if block["type"] == "outcome":
            return block["id"], block["config"]["outcome"]
        try:
            matched = _evaluate(block["config"]["when"], request)
        except _EvalError:
            return block["id"], "human"
        if matched:
            return block["id"], _inside(block["config"]["blocks"], request)
    raise AssertionError("no block decided")


def test_default_escalation_path(doc):
    assert doc["default_escalation_path"] == "notify-yaakov-yehuda"


def test_block_ids_are_unique_and_key_shaped(blocks):
    ids = [b["id"] for b in blocks]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", i) for i in ids)


def test_condition_ids_are_unique_uuids(blocks):
    ids = [n["id"] for n in _all_condition_nodes(blocks)]
    assert ids
    assert len(ids) == len(set(ids))
    for i in ids:
        assert str(uuid.UUID(i)) == i


def test_order_and_tail(blocks):
    assert [b["id"] for b in blocks] == EXPECTED_ORDER
    assert blocks[-2:] == [REV8_HERMES, ASK_UNRULED_CALL]
    assert blocks[1] == REV8_QUICKBOOKS_WEBHOOK


def test_rule_blocks_shape(blocks):
    for block in blocks:
        if not block["id"].startswith("pa-"):
            continue
        assert block["type"] == "branch" and block["enabled"] is True
        assert block["timeout"] == "50ms" and block["max_attempts"] == 1
        assert set(block["config"]) == {"when", "blocks"}
        assert len(block["config"]["blocks"]) == 1
        inner = block["config"]["blocks"][0]
        assert inner["type"] == "outcome" and inner["timeout"] == "50ms" and inner["max_attempts"] == 1
        first = block["config"]["when"]["all"][0]
        assert (first["field"], first["operator"], first["value"]) == ("/agent/slug", "equals", "proto-agents")


def _money_pattern(blocks):
    leaves = block_leaves(blocks[0])
    return next(l["value"] for l in leaves if l["field"] == "/request/tool")


def block_leaves(block):
    return [n for n in _walk_conditions(block["config"]["when"]) if "field" in n]


def test_park_money_regex_matches_exactly_the_36_money_tools(blocks):
    pattern = _money_pattern(blocks)
    names = [f"mcp__wh__quickbooks__{v}_{t}" for v, t in itertools.product(("create", "update", "delete"), MONEY_TYPES)]
    assert len(names) == 36
    for name in names + ["mcp__wh__quickbooks__create-bill"]:
        assert re.search(pattern, name), name
    for name in (
        "mcp__wh__quickbooks__create_payment_method",
        "mcp__wh__quickbooks__create_purchase_order",
        "mcp__wh__quickbooks__create_estimate",
        "mcp__wh__quickbooks__update_company_info",
    ):
        assert not re.search(pattern, name), name


def test_money_regex_matches_the_park_hook_form_checked_regex(blocks):
    from durable_park.park_hook import DEFAULT_FORM_CHECKED_TOOLS

    assert _money_pattern(blocks) == "^" + DEFAULT_FORM_CHECKED_TOOLS[0] + "$"


def test_park_money_outcome(blocks):
    inner = blocks[0]["config"]["blocks"][0]["config"]
    assert inner == {
        "outcome": "human",
        "reason": "Parked durable-park action: approve to let the agent run it exactly once.",
        "escalation_path": "notify-yaakov-yehuda",
    }


QB = "mcp__wh__quickbooks__"
DECISIONS = [
    ("proto-agents", "park:quickbooks", QB + "create_payment", "pa-park-money", "human"),
    ("proto-agents", "park:quickbooks", QB + "search_customers", "pa-park-quickbooks-read", "approve"),
    ("proto-agents", "park:quickbooks", QB + "get_company_info", "pa-park-quickbooks-read", "approve"),
    # a non-money-looking QuickBooks write is no longer approved by name: the server makes it money (TAP2-115)
    ("proto-agents", "park:quickbooks", QB + "create_customer", "money-server-human", "human"),
    ("proto-agents", "park:quickbooks", QB + "update_company_info", "money-server-human", "human"),
    ("proto-agents", "park:quickbooks", QB + "pay_vendor", "money-server-human", "human"),
    ("proto-agents", "park:quickbooks", QB + "wire_funds", "money-server-human", "human"),
    ("proto-agents", "quickbooks", "create_payment", "block-06ebcdd6", "webhook"),
    ("proto-agents", "quickbooks", "create-bill", "block-06ebcdd6", "webhook"),
    ("proto-agents", "quickbooks", "search_customers", "pa-gateway-read", "approve"),
    ("proto-agents", "quickbooks", "quickbooks__get_bill", "pa-gateway-read", "approve"),
    # the gateway replay of any other QuickBooks write goes to the bridge: approved only with a matching approved park
    ("proto-agents", "quickbooks", "create_customer", "qb-gateway-write-bridge", "webhook"),
    ("proto-agents", "quickbooks", "pay_vendor", "qb-gateway-write-bridge", "webhook"),
    ("proto-agents", "park:docusign", "mcp__wh__docusign__createEnvelope", "pa-default-human", "human"),
    ("proto-agents", "hermes", "web_search", "pa-hermes-read", "approve"),
    ("proto-agents", "hermes", "terminal", "pa-hermes-local-deny", "deny"),
    ("proto-agents", "hermes", "process_manage", "pa-hermes-local-deny", "deny"),
    ("proto-agents", "hermes", "browser_navigate", "pa-default-human", "human"),
    ("proto-agents", "mcp:github", "mcp__github__x", "pa-mcp-human", "human"),
    ("proto-agents", None, QB + "create_payment", "money-server-human", "human"),
    ("proto-agents", None, "some_tool", "pa-default-human", "human"),
    ("hermes", None, "terminal", "ask-unruled-call", "human"),
    ("new-agent", "brand-new-server", "brand_new_tool", "ask-unruled-call", "human"),
    ("hermes", None, "StripeRefund", "hermes", "human"),
    # money by SERVER for every other agent: payments and accounting servers always ask
    ("tapdash-ops", "quickbooks", "get_company_info", "money-server-human", "human"),
    ("tapdash-ops", "quickbooks", "quickbooks__create_customer", "money-server-human", "human"),
    ("tapdash-ops", "park:quickbooks", QB + "search_customers", "money-server-human", "human"),
    ("tapdash-ops", "stripe", "list_customers", "money-server-human", "human"),
    ("tapdash-ops", "maverick", "lookup", "money-server-human", "human"),
    ("tapdash-ops", "accept-blue", "lookup", "money-server-human", "human"),
    ("tapdash-ops", "authorize_net", "lookup", "money-server-human", "human"),
    ("tapdash-ops", "mcp:payments", "mcp__payments__anything", "money-server-human", "human"),
    ("tapdash-ops", "tapdash-pay", "anything", "money-server-human", "human"),
    ("tapdash-ops", "mcp:accept_blue", "mcp__accept_blue__lookup", "money-server-human", "human"),
    ("tapdash-ops", None, "mcp__wh__stripe__list_charges", "money-server-human", "human"),
    # name heuristics are an extra: a money verb on any server asks too
    ("tapdash-ops", "github", "pay_vendor", "money-server-human", "human"),
    ("tapdash-ops", "github", "wire_funds", "money-server-human", "human"),
    ("tapdash-ops", "acme", "send_money", "money-server-human", "human"),
    ("tapdash-ops", "acme", "issue_refund", "money-server-human", "human"),
    ("tapdash-content", "outlook", "send_mail", "ask-unruled-call", "human"),
    ("tapdash-content", "mcp:reach", "mcp__reach__search", "ask-unruled-call", "human"),
]


@pytest.mark.parametrize("agent,server,tool,block_id,outcome", DECISIONS)
def test_decisions(blocks, agent, server, tool, block_id, outcome):
    assert _decide(copy.deepcopy(blocks), agent, server, tool) == (block_id, outcome)


def test_every_listed_hermes_tool_decides_as_its_block_says(blocks):
    read = next(b for b in blocks if b["id"] == "pa-hermes-read")
    deny = next(b for b in blocks if b["id"] == "pa-hermes-local-deny")
    for block, outcome in ((read, "approve"), (deny, "deny")):
        leaf = next(l for l in block_leaves(block) if l["operator"] == "in")
        assert len(leaf["value"]) == 12
        for tool in leaf["value"]:
            assert _decide(blocks, "proto-agents", "hermes", tool) == (block["id"], outcome)


def test_no_unconditional_approve(blocks):
    """Nothing unruled is ever auto-approved: the only top-level outcome block is the last one and it asks a
    person, and no branch that approves has an empty or agent-only condition."""
    outcomes = [b for b in blocks if b["type"] == "outcome"]
    assert outcomes == [blocks[-1]]
    assert blocks[-1]["config"]["outcome"] == "human"
    assert blocks[-1]["config"]["escalation_path"] == "notify-yaakov-yehuda"
    assert all(b["id"] != "approve-every-call" for b in blocks)
    for block in blocks:
        if block["type"] != "branch":
            continue
        inner = block["config"]["blocks"][0]
        if inner["type"] == "outcome" and inner["config"]["outcome"] == "approve":
            fields = {leaf["field"] for leaf in block_leaves(block)}
            assert "/request/tool" in fields or "/request/server" in fields, block["id"]


@pytest.mark.parametrize("agent", ["hermes", "tapdash-ops", "tapdash-content", "some-new-agent"])
@pytest.mark.parametrize("server,tool", [
    (None, "a_tool_nobody_has_seen"), ("hermes", "web_search"), ("outlook", "send_mail"), ("mcp:new", "mcp__new__x"),
])
def test_a_tool_with_no_rule_asks_for_any_agent(blocks, agent, server, tool):
    assert _decide(blocks, agent, server, tool) == ("ask-unruled-call", "human")


MONEY_SERVERS = ["quickbooks", "park:quickbooks", "park:maverick", "stripe", "park:stripe", "mcp:stripe", "paypal", "square",
                 "plaid", "maverick", "accept-blue", "accept_blue", "authorize-net", "authnet", "mcp:payments",
                 "payments", "tapdash-pay", "mercury", "brex", "xero", "bill.com", "mcp:bank-feed"]


@pytest.mark.parametrize("agent", ["proto-agents", "hermes", "tapdash-ops", "some-new-agent"])
@pytest.mark.parametrize("server", MONEY_SERVERS)
@pytest.mark.parametrize("tool", ["pay_vendor", "wire_funds", "create_customer", "do_the_thing", "batch_update"])
def test_no_tool_on_a_money_server_is_approved(blocks, agent, server, tool):
    """Money by server: any tool name on a payments or accounting server is never approved by a rule (a
    QuickBooks gateway replay may only be approved by the bridge, which needs an approved park)."""
    for name in (tool, f"quickbooks__{tool}", f"mcp__wh__{server.split(':')[-1].replace('-', '_')}__{tool}"):
        _, outcome = _decide(blocks, agent, server, name)
        assert outcome in ("human", "webhook", "deny"), (agent, server, name, outcome)


def test_the_only_quickbooks_approvals_are_reads(blocks):
    """The two QuickBooks approve rules name read verbs only (get/search/list/read/query/find)."""
    for block_id in ("pa-gateway-read", "pa-park-quickbooks-read"):
        block = next(b for b in blocks if b["id"] == block_id)
        pattern = next(l["value"] for l in block_leaves(block) if l["field"] == "/request/tool")
        assert "(get|search|list|read|query|find)[_-]" in pattern
        for write in ("create_customer", "update_vendor", "delete_item", "pay_vendor", "void_invoice", "wire_funds"):
            for name in (write, "quickbooks__" + write, QB + write):
                assert not re.search(pattern, name), (block_id, name)


def test_write_bridge_block_matches_the_money_bridge_block(blocks):
    money_bridge = blocks[1]["config"]["blocks"][0]
    write_bridge = next(b for b in blocks if b["id"] == "qb-gateway-write-bridge")["config"]["blocks"][0]
    assert write_bridge["id"] != money_bridge["id"]
    assert {k: v for k, v in write_bridge.items() if k != "id"} == {k: v for k, v in money_bridge.items() if k != "id"}


def test_money_rule_is_before_every_non_proto_agents_rule(blocks):
    ids = [b["id"] for b in blocks]
    assert ids.index("money-server-human") < ids.index("hermes") < ids.index("ask-unruled-call")
    outcome = next(b for b in blocks if b["id"] == "money-server-human")["config"]["blocks"][0]["config"]
    assert outcome["outcome"] == "human" and outcome["escalation_path"] == "notify-yaakov-yehuda"


def test_regexes_avoid_inline_flags_and_lookaround(blocks):
    """withHuman's regex engine is not known to support (?i) or lookaround, so the file uses neither."""
    for node in _all_condition_nodes(blocks):
        if node.get("operator") == "regex":
            assert "(?" not in node["value"], node["value"]


# --- Maverick (tunnel plan Task 6) -----------------------------------------------------------------------------
MV = "mcp__wh__maverick__"
MAVERICK_AUTO = ["maverick_search_lookups", "maverick_describe_lookup", "maverick_read",
                 "maverick_create_boarding_application", "maverick_update_boarding_application",
                 "maverick_save_boarding_principal", "maverick_update_boarding_fees",
                 "maverick_update_boarding_equipment", "maverick_save_vault_customer", "maverick_save_ach_customer",
                 "maverick_upload_dispute_evidence", "maverick_create_dispute_draft", "maverick_update_ticket"]
OFF = {"notify_merchant": False, "attention_merchant": False}

MAVERICK_DECISIONS = [
    ("proto-agents", "park:maverick", MV + "maverick_read", None, "pa-park-maverick-auto", "approve"),
    ("proto-agents", "park:maverick", MV + "maverick_refund_transaction", None, "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", OFF, "pa-park-maverick-ticket", "approve"),
    ("proto-agents", "park:maverick", MV + "maverick_reply_to_ticket", OFF, "pa-park-maverick-ticket", "approve"),
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", {**OFF, "notify_merchant": True},
     "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", {**OFF, "attention_merchant": True},
     "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", None, "money-server-human", "human"),
    # odd values are an evaluation error in withHuman: the block itself ends as a person (not a fall-through)
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", {"notify_merchant": "false",
                                                                      "attention_merchant": "false"},
     "pa-park-maverick-ticket", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_create_ticket", {"notify_merchant": None,
                                                                      "attention_merchant": False},
     "pa-park-maverick-ticket", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_add_boarding_note", {"notify_staff": 0},
     "pa-park-maverick-note", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_add_boarding_note", {"notify_staff": False},
     "pa-park-maverick-note", "approve"),
    ("proto-agents", "park:maverick", MV + "maverick_add_boarding_note", {"notify_staff": True},
     "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_add_boarding_note", None, "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_read_all", None, "money-server-human", "human"),
    ("proto-agents", "park:maverick", MV + "maverick_readx", None, "money-server-human", "human"),
    ("proto-agents", "park:maverick", "mcp__wh__maverickx__maverick_read", None, "money-server-human", "human"),
    ("proto-agents", "maverick", "maverick_read", None, "pa-gateway-maverick-auto", "approve"),
    ("proto-agents", "maverick", "maverick__maverick_read", None, "pa-gateway-maverick-auto", "approve"),
    ("proto-agents", "maverick", "maverick_create_ticket", OFF, "pa-gateway-maverick-ticket", "approve"),
    ("proto-agents", "maverick", "maverick_add_boarding_note", {"notify_staff": False},
     "pa-gateway-maverick-note", "approve"),
    ("proto-agents", "maverick", "maverick_refund_transaction", None, "mav-gateway-write-bridge", "webhook"),
    ("proto-agents", "maverick", "maverick_create_ticket", None, "mav-gateway-write-bridge", "webhook"),
    ("proto-agents", "maverick", "maverick_read_all", None, "mav-gateway-write-bridge", "webhook"),
    ("tapdash-ops", "maverick", "maverick_read", None, "money-server-human", "human"),
    ("tapdash-ops", "park:maverick", MV + "maverick_read", None, "money-server-human", "human"),
]


@pytest.mark.parametrize("agent,server,tool,arguments,block_id,outcome", MAVERICK_DECISIONS)
def test_maverick_decisions(blocks, agent, server, tool, arguments, block_id, outcome):
    assert _decide(copy.deepcopy(blocks), agent, server, tool, arguments) == (block_id, outcome)


@pytest.mark.parametrize("name", MAVERICK_AUTO)
def test_every_maverick_auto_tool_is_approved_on_both_paths(blocks, name):
    assert _decide(blocks, "proto-agents", "park:maverick", MV + name) == ("pa-park-maverick-auto", "approve")
    assert _decide(blocks, "proto-agents", "maverick", name) == ("pa-gateway-maverick-auto", "approve")


def test_maverick_auto_list_matches_the_server_list(blocks):
    """The regex names exactly the 13 auto tools (python -m maverick_mcp --list-approvals at the pinned commit)."""
    block = next(b for b in blocks if b["id"] == "pa-park-maverick-auto")
    pattern = next(l["value"] for l in block_leaves(block) if l["field"] == "/request/tool")
    inner = pattern.split("(", 1)[1].rsplit(")", 1)[0].split("|")
    assert sorted("maverick_" + n for n in inner) == sorted(MAVERICK_AUTO)


def test_maverick_bridge_block_matches_the_quickbooks_bridge(blocks):
    qb = next(b for b in blocks if b["id"] == "qb-gateway-write-bridge")["config"]["blocks"][0]
    mav = next(b for b in blocks if b["id"] == "mav-gateway-write-bridge")["config"]["blocks"][0]
    assert mav["id"] == "mav-gateway-write-bridge-webhook"
    assert {k: v for k, v in mav.items() if k != "id"} == {k: v for k, v in qb.items() if k != "id"}


# --- remote gateway servers (Supabase CRM dev, monday, DocuSign demo) ---------------------------------------------
REMOTE = {
    # slug: (park server, a read tool, a tool that writes / sends / runs code)
    "supabase-crm-dev": ("park:supabase_crm_dev", "list_tables", "execute_sql"),
    "monday": ("park:monday", "get_board_items_page", "create_item"),
    "docusign-demo": ("park:docusign_demo", "getEnvelope", "createEnvelope"),
}
SHORT = {"supabase-crm-dev": "supabase", "monday": "monday", "docusign-demo": "docusign"}


def _hermes(slug, tool):
    return f"mcp__wh__{re.sub('[^A-Za-z0-9_]', '_', slug)}__{re.sub('[^A-Za-z0-9_]', '_', tool)}"


@pytest.mark.parametrize("slug", sorted(REMOTE))
def test_remote_server_reads_and_writes(blocks, slug):
    park, read, write = REMOTE[slug]
    s = SHORT[slug]
    assert _decide(blocks, "proto-agents", park, _hermes(slug, read)) == (f"pa-park-{s}-read", "approve")
    assert _decide(blocks, "proto-agents", park, _hermes(slug, write)) == ("pa-default-human", "human")
    assert _decide(blocks, "proto-agents", slug, read) == (f"gw-{s}", "approve")
    assert _decide(blocks, "proto-agents", slug, f"{slug}__{read}") == (f"gw-{s}", "approve")
    # every other gateway call goes to the bridge: approved only as the replay of an approved park
    assert _decide(blocks, "proto-agents", slug, write) == (f"gw-{s}", "webhook")
    assert _decide(blocks, "proto-agents", slug, read + "_all") == (f"gw-{s}", "webhook")
    # another agent: a person
    assert _decide(blocks, "tapdash-ops", slug, read) == ("ask-unruled-call", "human")
    assert _decide(blocks, "tapdash-ops", park, _hermes(slug, read)) == ("ask-unruled-call", "human")


@pytest.mark.parametrize("slug", sorted(REMOTE))
def test_remote_park_and_gateway_read_lists_agree(blocks, slug):
    s = SHORT[slug]
    park = next(b for b in blocks if b["id"] == f"pa-park-{s}-read")
    gw = next(b for b in blocks if b["id"] == f"gw-{s}")
    park_names = next(l["value"] for l in block_leaves(park) if l["field"] == "/request/tool")
    gw_read = gw["config"]["blocks"][0]
    gw_names = gw_read["config"]["when"]["value"]
    plain = [n for n in gw_names if "__" not in n]
    assert sorted(gw_names) == sorted(plain + [f"{slug}__{n}" for n in plain])
    assert sorted(park_names) == sorted(_hermes(slug, n) for n in plain)
    assert all(len(n) <= 64 for n in park_names)  # Hermes would hash longer names and the bridge could not pair them


def test_remote_bridges_match_the_quickbooks_bridge(blocks):
    qb = next(b for b in blocks if b["id"] == "qb-gateway-write-bridge")["config"]["blocks"][0]
    for s in SHORT.values():
        hook = next(b for b in blocks if b["id"] == f"gw-{s}")["config"]["blocks"][1]
        assert hook["id"] == f"gw-{s}-bridge-webhook"
        assert {k: v for k, v in hook.items() if k != "id"} == {k: v for k, v in qb.items() if k != "id"}


def test_execute_sql_is_never_approved_by_a_rule(blocks):
    assert _decide(blocks, "proto-agents", "park:supabase_crm_dev", _hermes("supabase-crm-dev", "execute_sql"))[1] == "human"
    assert _decide(blocks, "proto-agents", "supabase-crm-dev", "execute_sql")[1] == "webhook"
