"""Tests for bridge.verify: the pure decision rule that checks a gateway call against withHuman's own records.

Fixtures are shaped like the live ``GET /api/v1/requests`` items (keys: id, agent_slug, tool, server, arguments,
context, status, decision{status, decided_at, expires_at}, consumption{consumed_at, outcome}, created_at).
"""

import copy
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import bridge.verify as verify
from bridge.verify import expected_park, hermes_tool_name, parse_time

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
SERVER = "quickbooks"
TOOL = "create-payment"
AGENT = "proto-agents"
PARK_TOOL = "mcp__wh__quickbooks__create_payment"
PARK_SERVER = "park:quickbooks"
# What the gateway delivers carries withhuman_scope; what the model sent (and the park holds) does not.
GATEWAY_ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}, "withhuman_scope": "default"}
PARK_ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}}


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def call(**overrides):
    fields = dict(request_id="gw-1", server=SERVER, tool=TOOL, arguments=copy.deepcopy(GATEWAY_ARGS),
                  agent_slug=AGENT)
    fields.update(overrides)
    return verify.GatewayCall(**fields)


def park(**overrides):
    """An approved, consumed park request, consumed 10 s ago."""
    consumed = NOW - timedelta(seconds=10)
    item = {
        "id": "park-1", "agent_slug": AGENT, "tool": PARK_TOOL, "server": PARK_SERVER,
        "arguments": copy.deepcopy(PARK_ARGS), "context": {"reason": "pay invoice"},
        "status": "approved",
        "decision": {"status": "approved", "decided_at": iso(consumed - timedelta(minutes=5)),
                     "expires_at": iso(consumed + timedelta(minutes=30))},
        "consumption": {"consumed_at": iso(consumed), "outcome": "success"},
        "created_at": iso(consumed - timedelta(minutes=6)),
    }
    item.update(overrides)
    return item


def neighbour(**overrides):
    """Another request for the same gateway tool and args, created after the park was consumed."""
    item = {
        "id": "gw-2", "agent_slug": AGENT, "tool": TOOL, "server": SERVER,
        "arguments": copy.deepcopy(GATEWAY_ARGS), "context": {}, "status": "pending",
        "decision": None, "consumption": None,
        "created_at": iso(NOW - timedelta(seconds=5)),
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------- hermes_tool_name / expected_park

def test_hermes_tool_name_prefixes_the_wh_server_and_replaces_unsafe_characters():
    assert hermes_tool_name("quickbooks", "create-payment") == "mcp__wh__quickbooks__create_payment"


def test_hermes_tool_name_caps_long_names_at_64_chars_with_a_sha256_suffix():
    full = "mcp__wh__quickbooks__" + "x" * 80
    suffix = "_" + hashlib.sha256(full.encode()).hexdigest()[:8]
    name = hermes_tool_name("quickbooks", "x" * 80)
    assert len(name) == 64
    assert name == full[:64 - len(suffix)] + suffix


def test_app_still_exposes_hermes_tool_name_from_verify():
    from bridge import app
    assert app.hermes_tool_name is verify.hermes_tool_name


def test_expected_park_names_the_hermes_tool_and_the_park_server():
    assert expected_park("quickbooks", "create-payment") == (PARK_TOOL, PARK_SERVER)


# ---------------------------------------------------------------- parse_time

def test_parse_time_reads_z_suffix_with_fractional_seconds():
    assert parse_time("2026-10-22T12:00:00.123456Z") == datetime(2026, 10, 22, 12, 0, 0, 123456,
                                                                tzinfo=timezone.utc)


def test_parse_time_reads_an_offset():
    assert parse_time("2026-10-22T14:00:00+02:00") == datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", ["2026-10-22T12:00:00.1Z", "2026-10-22T12:00:00.123456789Z",
                                  "2026-10-22T12:00:00Z"])
def test_parse_time_accepts_any_fractional_precision(text):
    assert parse_time(text).tzinfo is not None


@pytest.mark.parametrize("value", [None, 5, 1.5, ["2026-10-22T12:00:00Z"], {}, b"2026-10-22T12:00:00Z"])
def test_parse_time_returns_none_for_non_strings(value):
    assert parse_time(value) is None


def test_parse_time_returns_none_for_a_naive_timestamp():
    assert parse_time("2026-10-22T12:00:00") is None


@pytest.mark.parametrize("text", ["", "not a time", "2026-13-45T99:00:00Z", "yesterday"])
def test_parse_time_returns_none_for_garbage(text):
    assert parse_time(text) is None


# ---------------------------------------------------------------- decide: the happy path

def test_decide_approves_a_call_backed_by_one_consumed_park_and_names_it():
    verdict = verify.decide(call(), [park()], [], NOW)
    assert verdict == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- decide: every condition of rule 2

def assert_no_approved_park(verdict):
    assert verdict == verify.Verdict(False, "no-approved-park", None)


def seconds_ago(n):
    return iso(NOW - timedelta(seconds=n))


def test_decide_sends_to_a_human_when_there_are_no_parks():
    assert_no_approved_park(verify.decide(call(), [], [], NOW))


def test_decide_rejects_a_park_of_another_agent():
    assert_no_approved_park(verify.decide(call(), [park(agent_slug="other-agent")], [], NOW))


def test_decide_rejects_a_park_when_the_call_is_from_another_agent():
    assert_no_approved_park(verify.decide(call(agent_slug="other-agent"), [park()], [], NOW))


def test_decide_rejects_a_park_for_another_tool():
    other = park(tool="mcp__wh__quickbooks__create_bill")
    assert_no_approved_park(verify.decide(call(), [other], [], NOW))


def test_decide_rejects_a_park_recorded_under_another_server():
    assert_no_approved_park(verify.decide(call(), [park(server="park:xero")], [], NOW))


@pytest.mark.parametrize("status", ["pending", "denied", "expired", "cancelled", None])
def test_decide_rejects_a_park_whose_status_is_not_approved(status):
    assert_no_approved_park(verify.decide(call(), [park(status=status)], [], NOW))


def test_decide_rejects_a_park_whose_decision_status_is_not_approved():
    item = park()
    item["decision"]["status"] = "denied"
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_rejects_a_park_without_a_decision():
    assert_no_approved_park(verify.decide(call(), [park(decision=None)], [], NOW))


def test_decide_rejects_a_park_that_was_never_consumed():
    assert_no_approved_park(verify.decide(call(), [park(consumption=None)], [], NOW))


def test_decide_rejects_a_park_with_no_consumed_at():
    assert_no_approved_park(verify.decide(call(), [park(consumption={"outcome": "success"})], [], NOW))


def test_decide_rejects_a_park_with_a_naive_consumed_at():
    item = park(consumption={"consumed_at": "2026-10-22T11:59:50", "outcome": "success"})
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_rejects_a_park_with_an_unparseable_consumed_at():
    item = park(consumption={"consumed_at": "a while ago", "outcome": "success"})
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_rejects_a_park_consumed_121_seconds_ago():
    item = park(consumption={"consumed_at": seconds_ago(121), "outcome": "success"})
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_accepts_a_park_consumed_exactly_120_seconds_ago():
    item = park(consumption={"consumed_at": seconds_ago(120), "outcome": "success"})
    assert verify.decide(call(), [item], [], NOW).approve is True


def test_decide_rejects_a_park_consumed_6_seconds_in_the_future():
    item = park(consumption={"consumed_at": seconds_ago(-6), "outcome": "success"})
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_accepts_a_park_consumed_5_seconds_in_the_future_clock_skew():
    item = park(consumption={"consumed_at": seconds_ago(-5), "outcome": "success"})
    assert verify.decide(call(), [item], [], NOW).approve is True


def test_decide_rejects_a_park_consumed_after_its_approval_expired():
    consumed = NOW - timedelta(seconds=10)
    item = park()
    item["decision"]["expires_at"] = iso(consumed - timedelta(seconds=1))
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_accepts_a_park_consumed_exactly_at_its_expiry():
    item = park()
    item["decision"]["expires_at"] = item["consumption"]["consumed_at"]
    assert verify.decide(call(), [item], [], NOW).approve is True


def test_decide_rejects_a_park_with_no_expires_at():
    item = park()
    del item["decision"]["expires_at"]
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_rejects_a_park_with_an_unparseable_expires_at():
    item = park()
    item["decision"]["expires_at"] = "soon"
    assert_no_approved_park(verify.decide(call(), [item], [], NOW))


def test_decide_rejects_a_park_whose_arguments_were_truncated():
    assert_no_approved_park(verify.decide(call(), [park(context={"args_truncated": True})], [], NOW))


def test_decide_accepts_a_park_whose_context_says_arguments_were_not_truncated():
    assert verify.decide(call(), [park(context={"args_truncated": False})], [], NOW).approve is True


def test_decide_rejects_a_park_whose_context_is_not_an_object():
    assert_no_approved_park(verify.decide(call(), [park(context="args_truncated")], [], NOW))


def test_decide_rejects_a_park_with_different_arguments():
    other = park(arguments={"Amount": 6, "CustomerRef": {"value": "58"}})
    assert_no_approved_park(verify.decide(call(), [other], [], NOW))


def test_decide_rejects_a_park_whose_arguments_are_not_an_object():
    assert_no_approved_park(verify.decide(call(), [park(arguments=["Amount", 5])], [], NOW))


def test_decide_rejects_a_park_with_no_arguments():
    assert_no_approved_park(verify.decide(call(), [park(arguments=None)], [], NOW))


# ---------------------------------------------------------------- decide: argument normalisation

def test_decide_matches_when_only_the_gateway_arguments_carry_the_default_scope():
    assert "withhuman_scope" in GATEWAY_ARGS and "withhuman_scope" not in PARK_ARGS
    assert verify.decide(call(), [park()], [], NOW).approve is True


def test_decide_matches_a_whole_number_float_against_the_same_int():
    float_park = park(arguments={"Amount": 5.0, "CustomerRef": {"value": "58"}})
    assert verify.decide(call(), [float_park], [], NOW).approve is True


def test_decide_keeps_an_explicit_non_default_scope_part_of_the_match():
    other_scope = park(arguments={**PARK_ARGS, "withhuman_scope": "other"})
    assert_no_approved_park(verify.decide(call(), [other_scope], [], NOW))


# ---------------------------------------------------------------- decide: ambiguity

def test_decide_sends_two_matching_parks_to_a_human_as_ambiguous():
    twins = [park(id="park-1"), park(id="park-2")]
    assert verify.decide(call(), twins, [], NOW) == verify.Verdict(False, "ambiguous-park", None)


def test_decide_is_not_ambiguous_when_only_one_of_two_parks_matches():
    other = park(id="park-2", arguments={"Amount": 99})
    assert verify.decide(call(), [other, park()], [], NOW) == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- decide: single use (rule 4)

def assert_already_used(verdict):
    assert verdict == verify.Verdict(False, "already-used", None)


@pytest.mark.parametrize("status", ["pending", "approved", "denied", "expired", "cancelled"])
def test_decide_rejects_when_another_gateway_request_with_the_same_args_came_after_the_park(status):
    assert_already_used(verify.decide(call(), [park()], [neighbour(status=status)], NOW))


def test_decide_ignores_a_neighbour_created_before_the_park_was_consumed():
    early = neighbour(created_at=seconds_ago(11))
    assert verify.decide(call(), [park()], [early], NOW).approve is True


def test_decide_counts_a_neighbour_created_exactly_when_the_park_was_consumed():
    at_consumption = neighbour(created_at=seconds_ago(10))
    assert_already_used(verify.decide(call(), [park()], [at_consumption], NOW))


def test_decide_ignores_the_gateway_request_being_decided_itself():
    itself = neighbour(id="gw-1")
    assert verify.decide(call(request_id="gw-1"), [park()], [itself], NOW).approve is True


def test_decide_ignores_a_neighbour_with_different_arguments():
    other = neighbour(arguments={"Amount": 6, "CustomerRef": {"value": "58"}})
    assert verify.decide(call(), [park()], [other], NOW).approve is True


def test_decide_ignores_a_neighbour_for_another_tool():
    assert verify.decide(call(), [park()], [neighbour(tool="create-bill")], NOW).approve is True


def test_decide_ignores_a_neighbour_on_another_server():
    assert verify.decide(call(), [park()], [neighbour(server="xero")], NOW).approve is True


def test_decide_ignores_a_neighbour_of_another_agent():
    assert verify.decide(call(), [park()], [neighbour(agent_slug="other-agent")], NOW).approve is True


def test_decide_counts_a_neighbour_whose_arguments_match_after_normalisation():
    same_after_normalising = neighbour(arguments={"Amount": 5.0, "CustomerRef": {"value": "58"}})
    assert_already_used(verify.decide(call(), [park()], [same_after_normalising], NOW))


def test_decide_counts_a_neighbour_with_no_created_at():
    item = neighbour()
    del item["created_at"]
    assert_already_used(verify.decide(call(), [park()], [item], NOW))


def test_decide_counts_a_neighbour_with_an_unparseable_created_at():
    assert_already_used(verify.decide(call(), [park()], [neighbour(created_at="whenever")], NOW))


def test_decide_counts_a_neighbour_with_a_naive_created_at():
    assert_already_used(verify.decide(call(), [park()], [neighbour(created_at="2026-10-22T11:59:55")], NOW))


@pytest.mark.parametrize("arguments", [None, "Amount=5", ["Amount", 5]])
def test_decide_counts_a_neighbour_whose_arguments_are_not_an_object(arguments):
    assert_already_used(verify.decide(call(), [park()], [neighbour(arguments=arguments)], NOW))


def test_decide_still_approves_with_an_unrelated_neighbour_among_conflict_free_ones():
    neighbours = [neighbour(tool="create-bill"), neighbour(created_at=seconds_ago(60))]
    assert verify.decide(call(), [park()], neighbours, NOW).approve is True


def test_decide_answers_a_retried_delivery_the_same_way():
    first = verify.decide(call(), [park()], [], NOW)
    retry = verify.decide(call(), [park()], [neighbour(id="gw-1")], NOW + timedelta(seconds=1))
    assert retry == first


# ---------------------------------------------------------------- decide: garbage in, never an exception

def test_decide_ignores_non_object_items_in_both_lists():
    verdict = verify.decide(call(), [None, 1, "x", park()], [None, 3, "y", ["gw"]], NOW)
    assert verdict == verify.Verdict(True, "park-request=park-1", "park-1")


def test_decide_with_only_non_object_parks_finds_no_approved_park():
    assert_no_approved_park(verify.decide(call(), [None, 1, "x"], [], NOW))


def test_decide_fails_closed_when_the_gateway_arguments_hold_a_value_json_cannot_encode():
    verdict = verify.decide(call(arguments={"Amount": {1, 2}}), [park()], [], NOW)
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_a_park_holds_a_value_json_cannot_encode():
    verdict = verify.decide(call(), [park(arguments={"Amount": {1, 2}})], [], NOW)
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_the_parks_are_not_a_list():
    assert verify.decide(call(), None, [], NOW) == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_now_is_naive():
    verdict = verify.decide(call(), [park()], [], NOW.replace(tzinfo=None))
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_names_the_exception_class_of_an_unexpected_failure(monkeypatch):
    def broken(_args):
        raise RuntimeError("secret detail that must not leak")
    monkeypatch.setattr(verify, "args_digest", broken)
    assert verify.decide(call(), [park()], [], NOW) == verify.Verdict(False, "error:RuntimeError", None)


def test_decide_sends_a_single_match_without_an_id_to_a_human():
    nameless = park()
    del nameless["id"]
    assert_no_approved_park(verify.decide(call(), [nameless], [], NOW))
