"""The form check: a risky call's arguments must follow the tool's own input rules before it is parked."""

import json

import pytest

from durable_park.formcheck import (FormChecker, FormCheckUnavailable, run_selfcheck, strip_gateway_args,
                                    write_status)

# The create-bill rules as the withHuman gateway lists them (trimmed), with the gateway's own field.
BILL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object", "required": ["params"], "additionalProperties": False,
    "properties": {
        "params": {"type": "object", "required": ["bill"], "additionalProperties": True, "properties": {
            "bill": {"type": "object", "required": ["Line", "VendorRef"], "additionalProperties": True, "properties": {
                "Line": {"type": "array", "items": {
                    "type": "object", "required": ["Amount", "DetailType"], "additionalProperties": True,
                    "properties": {
                        "Amount": {"type": "number"},
                        "AccountRef": {"type": "object", "required": ["value"], "additionalProperties": False,
                                       "properties": {"name": {"type": "string"}, "value": {"type": "string"}}},
                        "DetailType": {"type": "string"},
                        "AccountBasedExpenseLineDetail": {
                            "type": "object", "required": ["AccountRef"], "additionalProperties": False,
                            "properties": {"AccountRef": {
                                "$ref": "#/properties/params/properties/bill/properties/Line/items/properties/AccountRef"}}}}}},
                "VendorRef": {"$ref": "#/properties/params/properties/bill/properties/Line/items/properties/AccountRef"},
                "TotalAmt": {"type": "number"}}}}},
        "withhuman_scope": {"type": "string", "enum": ["default"]},
    },
}
GOOD_BILL = {"params": {"bill": {"Line": [{"Amount": 3, "DetailType": "AccountBasedExpenseLineDetail",
                                           "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}}}],
                                 "VendorRef": {"value": "56"}, "TotalAmt": 3}}}
BAD_BILL = {"params": {"bill": {"Line": [{"Amount": 3,  # DetailType missing (the real failed bill)
                                          "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}}}],
                                "VendorRef": {"value": "56"}}}}


def checker(schema=BILL_SCHEMA):
    return FormChecker(lambda tool: schema)


def test_valid_call_passes():
    assert checker().check("mcp__wh__quickbooks__create_bill", GOOD_BILL) is None


def test_missing_required_field_is_named():
    problem = checker().check("mcp__wh__quickbooks__create_bill", BAD_BILL)
    assert problem is not None
    assert "params.bill.Line[0]" in problem and "DetailType" in problem
    assert not problem.startswith("data")


def test_wrong_type_is_reported():
    bad = json.loads(json.dumps(GOOD_BILL))
    bad["params"]["bill"]["TotalAmt"] = "three"
    assert "TotalAmt" in checker().check("t", bad)


def test_local_ref_is_followed():
    bad = json.loads(json.dumps(GOOD_BILL))
    bad["params"]["bill"]["VendorRef"] = {"name": "Bob"}  # $ref target requires "value"
    assert "VendorRef" in checker().check("t", bad)


@pytest.mark.parametrize("extra", [{"withhuman_context": "why"}, {"withhuman_scope": "default"},
                                   {"withhuman_context": "why", "withhuman_scope": "default"}])
def test_gateway_fields_never_block(extra):
    # The gateway strips these before the server sees the call; the rules forbid unknown top-level fields.
    assert checker().check("t", {**GOOD_BILL, **extra}) is None


def test_gateway_fields_ignored_even_when_rules_require_them():
    schema = {**BILL_SCHEMA, "required": ["params", "withhuman_scope"]}
    assert checker(schema).check("t", GOOD_BILL) is None


def test_other_unknown_top_level_field_is_still_blocked():
    assert checker().check("t", {**GOOD_BILL, "surprise": 1}) is not None


def test_args_are_not_modified():
    schema = {"type": "object", "properties": {"x": {"type": "number", "default": 7}}}
    args = {"withhuman_context": "c"}
    assert checker(schema).check("t", args) is None
    assert args == {"withhuman_context": "c"}


def test_strip_gateway_args_top_level_only():
    nested = {"params": {"withhuman_context": "kept"}, "withhuman_context": "dropped"}
    assert strip_gateway_args(nested) == {"params": {"withhuman_context": "kept"}}


@pytest.mark.parametrize("schema", [None, "nope", [], 5])
def test_missing_rules_are_unavailable(schema):
    with pytest.raises(FormCheckUnavailable):
        checker(schema).check("t", GOOD_BILL)


def test_rules_lookup_error_is_unavailable():
    def boom(tool):
        raise RuntimeError("registry moved")
    with pytest.raises(FormCheckUnavailable):
        FormChecker(boom).check("t", GOOD_BILL)


@pytest.mark.parametrize("ref", ["https://example.invalid/schema.json", "http://x/y#/a", "other.json",
                                 "file:///etc/passwd", "ftp://x/y"])
def test_remote_refs_are_refused_without_fetching(ref, monkeypatch):
    import urllib.request

    def no_network(*a, **k):
        raise AssertionError("network fetch attempted")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    schema = {"type": "object", "properties": {"a": {"$ref": ref}}}
    with pytest.raises(FormCheckUnavailable):
        checker(schema).check("t", {"a": 1})


def test_unusable_rules_are_unavailable():
    with pytest.raises(FormCheckUnavailable):
        checker({"type": "object", "properties": {"a": {"$ref": "#/does/not/exist"}}}).check("t", {"a": 1})


def test_formats_are_not_enforced():
    # The server decides formats; the check must never block what the server would accept.
    schema = {"type": "object", "properties": {"d": {"type": "string", "format": "date"}}}
    assert checker(schema).check("t", {"d": "20 Oct 2026"}) is None


def test_compiled_rules_are_cached():
    calls = []

    def schema_fn(tool):
        calls.append(tool)
        return BILL_SCHEMA
    fc = FormChecker(schema_fn)
    for _ in range(3):
        assert fc.check("t", GOOD_BILL) is None
    assert len(calls) == 3  # rules re-read every call (they can change) ...
    assert fc.cache_size() == 1  # ... but compiled once


def test_problem_message_is_bounded_and_clean():
    schema = {"type": "object", "required": ["k" * 500 + "\n\x00"], "properties": {}}
    problem = checker(schema).check("t", {})
    assert len(problem) <= 300 and "\n" not in problem and "\x00" not in problem


# ---- self-check ------------------------------------------------------------------------------

RISKY = ["mcp__wh__quickbooks__create_bill", "mcp__wh__quickbooks__create_payment"]


def selfcheck(names, schema_fn, risky=RISKY):
    import re
    patterns = [re.compile(p) for p in risky]
    return run_selfcheck(FormChecker(schema_fn), lambda: names, patterns)


def test_selfcheck_ok_when_all_risky_rules_compile():
    status = selfcheck(["mcp__wh__quickbooks__create_bill", "mcp__wh__quickbooks__search_bills"],
                       lambda t: BILL_SCHEMA)
    assert status["ok"] is True and status["tools_checked"] == 1 and status["problems"] == []


def test_selfcheck_fails_when_no_risky_tools_registered():
    status = selfcheck(["mcp__wh__quickbooks__search_bills"], lambda t: BILL_SCHEMA)
    assert status["ok"] is False and "no risky tools" in status["problems"][0]


def test_selfcheck_fails_when_rules_missing():
    status = selfcheck(["mcp__wh__quickbooks__create_bill"], lambda t: None)
    assert status["ok"] is False and "mcp__wh__quickbooks__create_bill" in status["problems"][0]


def test_selfcheck_fails_when_registry_unreadable():
    import re

    def names():
        raise RuntimeError("registry API changed")
    status = run_selfcheck(FormChecker(lambda t: BILL_SCHEMA), names, [re.compile(RISKY[0])])
    assert status["ok"] is False and "registry" in status["problems"][0]


def test_write_status_is_atomic_json(tmp_path):
    path = tmp_path / "formcheck_status.json"
    write_status(str(path), {"ok": True, "problems": [], "tools_checked": 2})
    data = json.loads(path.read_text())
    assert data["ok"] is True and data["tools_checked"] == 2 and data["checked_at"].endswith("Z")
    assert [p.name for p in tmp_path.iterdir()] == ["formcheck_status.json"]


def test_selfcheck_loop_waits_for_tools_then_writes(tmp_path):
    import re
    from durable_park.formcheck import selfcheck_loop
    path = tmp_path / "formcheck_status.json"
    seen = iter([[], [], ["mcp__wh__quickbooks__create_bill"]])
    current = {"names": []}

    def names():
        return current["names"]

    def sleep(_s):
        current["names"] = next(seen, current["names"])
    t = {"now": 0.0}

    def clock():
        t["now"] += 1
        return t["now"]
    selfcheck_loop(FormChecker(lambda tool: BILL_SCHEMA), names, [re.compile(RISKY[0])], str(path),
                   sleep=sleep, clock=clock, iterations=1)
    data = json.loads(path.read_text())
    assert data["ok"] is True and data["tools_checked"] == 1


def test_selfcheck_loop_writes_failure_after_wait_timeout(tmp_path):
    import re
    from durable_park.formcheck import selfcheck_loop
    path = tmp_path / "formcheck_status.json"
    t = {"now": 0.0}

    def clock():
        t["now"] += 100
        return t["now"]
    selfcheck_loop(FormChecker(lambda tool: BILL_SCHEMA), lambda: [], [re.compile(RISKY[0])], str(path),
                   sleep=lambda s: None, clock=clock, wait_timeout=300, iterations=1)
    assert json.loads(path.read_text())["ok"] is False


def test_selfcheck_loop_never_raises(tmp_path):
    import re
    from durable_park.formcheck import selfcheck_loop
    selfcheck_loop(FormChecker(lambda tool: BILL_SCHEMA), lambda: ["x"], [re.compile("x")],
                   str(tmp_path / "missing-dir" / "s.json"), sleep=lambda s: None, wait_timeout=0, iterations=1)


def test_start_selfcheck_is_noop_outside_hermes(tmp_path):
    import re
    from durable_park.formcheck import start_selfcheck
    assert start_selfcheck(FormChecker(), [re.compile("x")], str(tmp_path / "s.json")) is False
