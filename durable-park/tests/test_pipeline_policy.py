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
    "pa-gateway-other",
    "pa-park-quickbooks-other",
    "pa-hermes-read",
    "pa-hermes-local-deny",
    "pa-mcp-human",
    "pa-default-human",
    "hermes",
    "approve-every-call",
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

REV8_APPROVE_EVERY_CALL = {
    "id": "approve-every-call",
    "name": "Approve every call",
    "type": "outcome",
    "enabled": True,
    "timeout": "50ms",
    "max_attempts": 1,
    "config": {"outcome": "approve", "reason": "No rule sent this call to a person."},
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


def _evaluate(node, request):
    if "all" in node:
        return all(_evaluate(c, request) for c in node["all"])
    if "any" in node:
        return any(_evaluate(c, request) for c in node["any"])
    value = request.get(node["field"])
    op, expected = node["operator"], node["value"]
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


def _decide(blocks, agent, server, tool):
    request = {"/agent/slug": agent, "/request/server": server, "/request/tool": tool}
    for block in blocks:
        if block["type"] == "outcome":
            return block["id"], block["config"]["outcome"]
        if _evaluate(block["config"]["when"], request):
            inner = block["config"]["blocks"][0]
            return block["id"], inner["config"]["outcome"] if inner["type"] == "outcome" else inner["type"]
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
    assert blocks[-2:] == [REV8_HERMES, REV8_APPROVE_EVERY_CALL]
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
    ("proto-agents", "park:quickbooks", QB + "search_customers", "pa-park-quickbooks-other", "approve"),
    ("proto-agents", "quickbooks", "create_payment", "block-06ebcdd6", "webhook"),
    ("proto-agents", "quickbooks", "create-bill", "block-06ebcdd6", "webhook"),
    ("proto-agents", "quickbooks", "search_customers", "pa-gateway-other", "approve"),
    ("proto-agents", "park:docusign", "mcp__wh__docusign__createEnvelope", "pa-default-human", "human"),
    ("proto-agents", "hermes", "web_search", "pa-hermes-read", "approve"),
    ("proto-agents", "hermes", "terminal", "pa-hermes-local-deny", "deny"),
    ("proto-agents", "hermes", "process_manage", "pa-hermes-local-deny", "deny"),
    ("proto-agents", "hermes", "browser_navigate", "pa-default-human", "human"),
    ("proto-agents", "mcp:github", "mcp__github__x", "pa-mcp-human", "human"),
    ("proto-agents", None, QB + "create_payment", "pa-default-human", "human"),
    ("hermes", None, "terminal", "approve-every-call", "approve"),
    ("hermes", None, "StripeRefund", "hermes", "human"),
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
