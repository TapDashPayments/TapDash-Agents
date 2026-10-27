"""Tests for bridge.verify: the pure decision rule that checks a gateway call against withHuman's own records.

Fixtures are shaped like the live ``GET /api/v1/requests`` items (keys: id, agent_slug, tool, server, arguments,
context, status, decision{id, request_id, organization_id, membership_id, status, channel, reviewer_display_name,
reviewer_email, authentication, idempotency_key, decided_at}, consumption{consumed_at}, created_at). The
listing's ``decision`` carries no ``expires_at`` and the rule never reads one: withHuman refuses to consume an
expired approval, so ``consumed_at`` is the proof. ``consumption`` has no ``outcome`` until the executor reports
one. A request still pending has neither a ``decision`` nor a ``consumption`` key.
"""

import copy
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import bridge.verify as verify
from bridge.verify import expected_park, hermes_tool_name, parse_time
from durable_park.gatecall import REDACTED

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
SERVER = "quickbooks"
TOOL = "create-payment"
AGENT = "proto-agents"
PERSON = "inst-A"  # the withHuman instance (one per person in the per-person setup)
OTHER_PERSON = "inst-B"
PARK_TOOL = "mcp__wh__quickbooks__create_payment"
PARK_SERVER = "park:quickbooks"
# What the gateway delivers carries withhuman_scope; what the model sent (and the park holds) does not.
GATEWAY_ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}, "withhuman_scope": "default"}
PARK_ARGS = {"Amount": 5, "CustomerRef": {"value": "58"}}


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def call(**overrides):
    fields = dict(request_id="gw-1", server=SERVER, tool=TOOL, arguments=copy.deepcopy(GATEWAY_ARGS),
                  agent_slug=AGENT, person=PERSON)
    fields.update(overrides)
    return verify.GatewayCall(**fields)


def park(**overrides):
    """An approved, consumed park request, consumed 10 s ago."""
    consumed = NOW - timedelta(seconds=10)
    item = {
        "id": "park-1", "agent_slug": AGENT, "agent_instance_id": PERSON, "tool": PARK_TOOL,
        "server": PARK_SERVER,
        "arguments": copy.deepcopy(PARK_ARGS), "context": {"reason": "pay invoice"},
        "status": "approved",
        "decision": {"id": "dec-1", "request_id": "park-1", "organization_id": "org-1", "membership_id": "mem-1",
                     "status": "approved", "channel": "web", "reviewer_display_name": "Reviewer",
                     "reviewer_email": "reviewer@example.com", "authentication": {"method": "session"},
                     "idempotency_key": "idem-dec-1", "decided_at": iso(consumed - timedelta(minutes=5))},
        "consumption": {"consumed_at": iso(consumed)},
        "created_at": iso(consumed - timedelta(minutes=6)),
    }
    item.update(overrides)
    return item


def neighbour(**overrides):
    """Another request for the same gateway tool and args, created after the park was consumed."""
    item = {
        "id": "gw-2", "agent_slug": AGENT, "agent_instance_id": PERSON, "tool": TOOL, "server": SERVER,
        "arguments": copy.deepcopy(GATEWAY_ARGS), "context": {}, "status": "pending",
        "created_at": iso(NOW - timedelta(seconds=5)),
    }
    item.update(overrides)
    return item


def own(**overrides):
    """The gateway request being decided (``call()``), as withHuman lists it: created 9 s ago, 1 s after the
    park was consumed."""
    item = {
        "id": "gw-1", "agent_slug": AGENT, "agent_instance_id": PERSON, "tool": TOOL, "server": SERVER,
        "arguments": copy.deepcopy(GATEWAY_ARGS), "context": {}, "status": "pending",
        "created_at": iso(NOW - timedelta(seconds=9)),
    }
    item.update(overrides)
    return item


def old_park():
    """The park request itself as withHuman lists it: the same agent's request, created long before the release."""
    return park(id="park-1", created_at=iso(NOW - timedelta(days=1)))


def decide(gateway_call, parks, neighbours, now):
    """``verify.decide`` on a listing shaped the way withHuman returns one: newest first, ending with the park.

    The tests below list only the requests that matter to them; this adds the old park request and sorts newest
    first. Tests of the listing rule itself call ``verify.decide`` on a hand-built list instead.
    """
    if not isinstance(neighbours, list):
        return verify.decide(gateway_call, parks, neighbours, now)
    latest = datetime.max.replace(tzinfo=timezone.utc)
    items = [*neighbours, old_park()]
    items.sort(key=lambda o: (parse_time(o.get("created_at")) if isinstance(o, dict) else None) or latest,
               reverse=True)
    return verify.decide(gateway_call, parks, items, now)


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
    verdict = decide(call(), [park()], [own()], NOW)
    assert verdict == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- decide: every condition of rule 2

def assert_no_approved_park(verdict):
    assert verdict == verify.Verdict(False, "no-approved-park", None)


def seconds_ago(n):
    return iso(NOW - timedelta(seconds=n))


def test_decide_sends_to_a_human_when_there_are_no_parks():
    assert_no_approved_park(decide(call(), [], [own()], NOW))


def test_decide_rejects_a_park_of_another_agent():
    assert_no_approved_park(decide(call(), [park(agent_slug="other-agent")], [own()], NOW))


def test_decide_rejects_a_park_when_the_call_is_from_another_agent():
    assert_no_approved_park(decide(call(agent_slug="other-agent"), [park()], [own()], NOW))


def test_decide_rejects_a_park_for_another_tool():
    other = park(tool="mcp__wh__quickbooks__create_bill")
    assert_no_approved_park(decide(call(), [other], [own()], NOW))


def test_decide_rejects_a_park_recorded_under_another_server():
    assert_no_approved_park(decide(call(), [park(server="park:xero")], [own()], NOW))


@pytest.mark.parametrize("status", ["pending", "denied", "expired", "cancelled", None])
def test_decide_rejects_a_park_whose_status_is_not_approved(status):
    assert_no_approved_park(decide(call(), [park(status=status)], [own()], NOW))


def test_decide_rejects_a_park_whose_decision_status_is_not_approved():
    item = park()
    item["decision"]["status"] = "denied"
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_rejects_a_park_without_a_decision():
    assert_no_approved_park(decide(call(), [park(decision=None)], [own()], NOW))


def test_decide_rejects_a_park_that_was_never_consumed():
    assert_no_approved_park(decide(call(), [park(consumption=None)], [own()], NOW))


def test_decide_rejects_a_park_with_no_consumed_at():
    assert_no_approved_park(decide(call(), [park(consumption={"outcome": None})], [own()], NOW))


def test_decide_rejects_a_park_with_a_naive_consumed_at():
    item = park(consumption={"consumed_at": "2026-10-22T11:59:50", "outcome": None})
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_rejects_a_park_with_an_unparseable_consumed_at():
    item = park(consumption={"consumed_at": "a while ago", "outcome": None})
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_rejects_a_park_consumed_121_seconds_ago():
    item = park(consumption={"consumed_at": seconds_ago(121), "outcome": None})
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_accepts_a_park_consumed_exactly_120_seconds_ago():
    item = park(consumption={"consumed_at": seconds_ago(120), "outcome": None})
    assert decide(call(), [item], [own()], NOW).approve is True


def test_decide_rejects_a_park_consumed_6_seconds_in_the_future():
    item = park(consumption={"consumed_at": seconds_ago(-6), "outcome": None})
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_accepts_a_park_consumed_5_seconds_in_the_future_clock_skew():
    item = park(consumption={"consumed_at": seconds_ago(-5), "outcome": None})
    assert decide(call(), [item], [own(created_at=seconds_ago(-6))], NOW).approve is True


def test_decide_accepts_a_park_in_the_real_live_listing_shape():
    item = park()
    assert "expires_at" not in item["decision"] and set(item["consumption"]) == {"consumed_at"}
    assert decide(call(), [item], [own()], NOW).approve is True


@pytest.mark.parametrize("stray", [None, "soon", "2026-10-22T11:59:00Z", iso(NOW - timedelta(days=1))])
def test_decide_ignores_a_stray_expires_at_on_the_decision(stray):
    """The product listing never sends one; if it ever did, the rule would still not read it."""
    item = park()
    item["decision"]["expires_at"] = stray
    assert decide(call(), [item], [own()], NOW).approve is True


def test_decide_rejects_a_park_whose_arguments_were_truncated():
    assert_no_approved_park(decide(call(), [park(context={"args_truncated": True})], [own()], NOW))


def test_decide_accepts_a_park_whose_context_says_arguments_were_not_truncated():
    assert decide(call(), [park(context={"args_truncated": False})], [own()], NOW).approve is True


def test_decide_rejects_a_park_whose_context_is_not_an_object():
    assert_no_approved_park(decide(call(), [park(context="args_truncated")], [own()], NOW))


def test_decide_rejects_a_park_with_different_arguments():
    other = park(arguments={"Amount": 6, "CustomerRef": {"value": "58"}})
    assert_no_approved_park(decide(call(), [other], [own()], NOW))


def test_decide_rejects_a_park_whose_arguments_are_not_an_object():
    assert_no_approved_park(decide(call(), [park(arguments=["Amount", 5])], [own()], NOW))


def test_decide_rejects_a_park_with_no_arguments():
    assert_no_approved_park(decide(call(), [park(arguments=None)], [own()], NOW))


# ---------------------------------------------------------------- decide: argument normalisation

def test_decide_matches_when_only_the_gateway_arguments_carry_the_default_scope():
    assert "withhuman_scope" in GATEWAY_ARGS and "withhuman_scope" not in PARK_ARGS
    assert decide(call(), [park()], [own()], NOW).approve is True


def test_decide_matches_a_whole_number_float_against_the_same_int():
    float_park = park(arguments={"Amount": 5.0, "CustomerRef": {"value": "58"}})
    assert decide(call(), [float_park], [own()], NOW).approve is True


def test_decide_keeps_an_explicit_non_default_scope_part_of_the_match():
    other_scope = park(arguments={**PARK_ARGS, "withhuman_scope": "other"})
    assert_no_approved_park(decide(call(), [other_scope], [own()], NOW))


# ---------------------------------------------------------------- decide: ambiguity

def test_decide_sends_two_matching_parks_to_a_human_as_ambiguous():
    twins = [park(id="park-1"), park(id="park-2")]
    assert decide(call(), twins, [own()], NOW) == verify.Verdict(False, "ambiguous-park", None)


def test_decide_is_not_ambiguous_when_only_one_of_two_parks_matches():
    other = park(id="park-2", arguments={"Amount": 99})
    assert decide(call(), [other, park()], [own()], NOW) == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- decide: single use (rule 4)

def assert_already_used(verdict):
    assert verdict == verify.Verdict(False, "already-used", None)


@pytest.mark.parametrize("status", ["pending", "approved", "denied", "expired", "cancelled"])
def test_decide_rejects_when_another_gateway_request_with_the_same_args_came_after_the_park(status):
    assert_already_used(decide(call(), [park()], [own(), neighbour(status=status)], NOW))


def test_decide_ignores_a_neighbour_created_more_than_5_seconds_before_the_park_was_consumed():
    early = neighbour(created_at=seconds_ago(16))
    assert decide(call(), [park()], [own(), early], NOW).approve is True


def test_decide_ignores_a_neighbour_created_just_over_5_seconds_before_the_consumption():
    assert decide(call(), [park()], [own(), neighbour(created_at=seconds_ago(15.001))], NOW).approve is True


def test_decide_counts_a_neighbour_created_exactly_5_seconds_before_the_consumption():
    assert_already_used(decide(call(), [park()], [own(), neighbour(created_at=seconds_ago(15))], NOW))


def test_decide_counts_a_neighbour_created_3_seconds_before_the_consumption():
    # both stamps come from withHuman's clock, so a request this close before the release is a second use
    assert_already_used(decide(call(), [park()], [own(), neighbour(created_at=seconds_ago(13))], NOW))


def test_decide_counts_a_neighbour_created_exactly_when_the_park_was_consumed():
    at_consumption = neighbour(created_at=seconds_ago(10))
    assert_already_used(decide(call(), [park()], [own(), at_consumption], NOW))


def test_decide_ignores_the_gateway_request_being_decided_itself():
    itself = neighbour(id="gw-1")
    assert decide(call(request_id="gw-1"), [park()], [itself], NOW).approve is True


def test_decide_ignores_a_neighbour_with_different_arguments():
    other = neighbour(arguments={"Amount": 6, "CustomerRef": {"value": "58"}})
    assert decide(call(), [park()], [own(), other], NOW).approve is True


def test_decide_ignores_a_neighbour_for_another_tool():
    assert decide(call(), [park()], [own(), neighbour(tool="create-bill")], NOW).approve is True


def test_decide_ignores_a_neighbour_on_another_server():
    assert decide(call(), [park()], [own(), neighbour(server="xero")], NOW).approve is True


def test_decide_ignores_a_neighbour_of_another_agent():
    assert decide(call(), [park()], [own(), neighbour(agent_slug="other-agent")], NOW).approve is True


def test_decide_counts_a_neighbour_whose_arguments_match_after_normalisation():
    same_after_normalising = neighbour(arguments={"Amount": 5.0, "CustomerRef": {"value": "58"}})
    assert_already_used(decide(call(), [park()], [own(), same_after_normalising], NOW))


@pytest.mark.parametrize("created", [None, "whenever", "2026-10-22T11:59:55", 5])
def test_is_conflict_counts_a_neighbour_without_a_readable_created_at(created):
    consumed = NOW - timedelta(seconds=10)
    digest = verify.args_digest(call().arguments)
    assert verify._is_conflict(neighbour(created_at=created), call(), digest, consumed) is True


@pytest.mark.parametrize("arguments", [None, "Amount=5", ["Amount", 5]])
def test_decide_counts_a_neighbour_whose_arguments_are_not_an_object(arguments):
    assert_already_used(decide(call(), [park()], [own(), neighbour(arguments=arguments)], NOW))


def test_decide_still_approves_with_an_unrelated_neighbour_among_conflict_free_ones():
    neighbours = [own(), neighbour(tool="create-bill"), neighbour(created_at=seconds_ago(60))]
    assert decide(call(), [park()], neighbours, NOW).approve is True


def test_decide_refuses_the_attackers_earlier_request_and_the_executors_later_one_alike():
    # the review scenario: O is the model's own call, made 3 s before the executor's G, same arguments
    consumed = NOW - timedelta(seconds=10)
    attacker = own(id="gw-O", created_at=iso(consumed - timedelta(seconds=3)))
    executor = own(id="gw-1", created_at=iso(consumed + timedelta(seconds=2)))
    listing = [executor, attacker]
    assert_why(decide(call(request_id="gw-O"), [park()], listing, NOW), "gateway-request-before-release")
    assert_already_used(decide(call(request_id="gw-1"), [park()], listing, NOW))


def test_decide_answers_a_retried_delivery_the_same_way():
    first = decide(call(), [park()], [own()], NOW)
    retry = decide(call(), [park()], [neighbour(id="gw-1")], NOW + timedelta(seconds=1))
    assert retry == first


# ---------------------------------------------------------------- decide: garbage in, never an exception

def test_decide_ignores_non_object_items_in_the_parks():
    verdict = decide(call(), [None, 1, "x", park()], [own()], NOW)
    assert verdict == verify.Verdict(True, "park-request=park-1", "park-1")


def test_decide_with_only_non_object_parks_finds_no_approved_park():
    assert_no_approved_park(decide(call(), [None, 1, "x"], [own()], NOW))


def test_decide_fails_closed_when_the_gateway_arguments_hold_a_value_json_cannot_encode():
    verdict = decide(call(arguments={"Amount": {1, 2}}), [park()], [own()], NOW)
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_a_park_holds_a_value_json_cannot_encode():
    verdict = decide(call(), [park(arguments={"Amount": {1, 2}})], [own()], NOW)
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_the_parks_are_not_a_list():
    assert decide(call(), None, [own()], NOW) == verify.Verdict(False, "error:TypeError", None)


def test_decide_fails_closed_when_now_is_naive():
    verdict = decide(call(), [park()], [own()], NOW.replace(tzinfo=None))
    assert verdict == verify.Verdict(False, "error:TypeError", None)


def test_decide_names_the_exception_class_of_an_unexpected_failure(monkeypatch):
    def broken(_args):
        raise RuntimeError("secret detail that must not leak")
    monkeypatch.setattr(verify, "args_digest", broken)
    assert decide(call(), [park()], [own()], NOW) == verify.Verdict(False, "error:RuntimeError", None)


def test_decide_sends_a_single_match_without_an_id_to_a_human():
    nameless = park()
    del nameless["id"]
    assert_no_approved_park(decide(call(), [nameless], [own()], NOW))


# ---------------------------------------------------------------- decide: unsafe names (rule 1)

def park_for(server, tool, **overrides):
    """A consumed park that matches a call to ``server``/``tool`` (the park tool and server derived the same way)."""
    park_tool, park_server = expected_park(server, tool)
    return park(tool=park_tool, server=park_server, **overrides)


def own_for(server, tool, **overrides):
    return own(server=server, tool=tool, **overrides)


def assert_unsafe_name(verdict):
    assert verdict == verify.Verdict(False, "unsafe-name", None)


@pytest.mark.parametrize("server", [
    "quick__books", "_quickbooks", "quickbooks_", "-quickbooks", "quickbooks-", "quick--books", "quick-_books",
    "quick_-books", "", "quick books", "quick.books", "quick:books", "quickbooks\n", "quickböoks",
])
def test_decide_sends_an_unsafe_server_name_to_a_human(server):
    assert_unsafe_name(decide(call(server=server), [], [], NOW))


@pytest.mark.parametrize("tool", [
    "", "create bill", "create.bill", "create:bill", "create-bill\n", "créate",
    "quickbooks__create-bill", "a__b", "_x", "x_", "x-", "-x", "x--y", "x-_y", "x_-y", "create__bill",
])
def test_decide_sends_an_unsafe_tool_name_to_a_human(tool):
    assert_unsafe_name(decide(call(tool=tool), [], [], NOW))


@pytest.mark.parametrize("server, tool", [(5, TOOL), (SERVER, None), (None, None), (b"quickbooks", TOOL)])
def test_decide_sends_non_string_names_to_a_human(server, tool):
    assert_unsafe_name(decide(call(server=server, tool=tool), [], [], NOW))


def test_decide_sends_a_name_whose_hermes_tool_name_would_need_a_hash_suffix_to_a_human():
    long_tool = "x" * 44  # mcp__wh__quickbooks__ is 21 chars: 65 in total
    assert len(hermes_tool_name(SERVER, long_tool)) == 64 and "x" * 44 not in hermes_tool_name(SERVER, long_tool)
    assert_unsafe_name(decide(call(tool=long_tool), [], [], NOW))


def test_decide_accepts_a_name_whose_hermes_tool_name_is_exactly_64_chars():
    tool = "x" * 43
    assert len(f"mcp__wh__{SERVER}__{tool}") == 64
    verdict = decide(call(tool=tool), [park_for(SERVER, tool)], [own_for(SERVER, tool)], NOW)
    assert verdict.approve is True


def test_decide_counts_the_hermes_length_after_hyphens_become_underscores_not_before():
    tool = "-".join(["x"] * 22)  # 43 chars: hyphens and underscores are one char each, so no change in length
    assert len(f"mcp__wh__{SERVER}__{tool}") == 64
    verdict = decide(call(tool=tool), [park_for(SERVER, tool)], [own_for(SERVER, tool)], NOW)
    assert verdict.approve is True


def test_decide_never_approves_an_unsafe_name_even_with_a_park_that_would_match():
    server = "quick__books"
    verdict = decide(call(server=server), [park_for(server, TOOL)], [own_for(server, TOOL)], NOW)
    assert_unsafe_name(verdict)


def test_decide_does_not_pair_a_prefixed_tool_with_a_park_for_the_unprefixed_one():
    # the gateway pipeline's money rule accepts an optional "quickbooks__" prefix that the park rule lacks
    prefixed = "quickbooks__create-bill"
    verdict = decide(call(tool=prefixed), [park_for(SERVER, "create-bill")], [own_for(SERVER, prefixed)], NOW)
    assert_unsafe_name(verdict)


def test_decide_checks_names_before_anything_else_so_even_garbage_lists_give_unsafe_name():
    assert_unsafe_name(decide(call(server="a__b"), None, None, NOW))


@pytest.mark.parametrize("server, tool", [("quick-books", "create-payment"), ("quick_books", "create_payment"),
                                          ("Quick1", "Create-9_x"), ("quickbooks", "create-bill"),
                                          ("quickbooks", "create_bill_payment"),
                                          ("quickbooks", "get_vendor_balance"), ("quickbooks", "x")])
def test_decide_still_approves_ordinary_names_with_single_hyphens_and_underscores(server, tool):
    verdict = decide(call(server=server, tool=tool), [park_for(server, tool)], [own_for(server, tool)], NOW)
    assert verdict.approve is True


def test_decide_still_says_no_approved_park_for_safe_names_without_a_park():
    assert_no_approved_park(decide(call(), [], [], NOW))


# ---------------------------------------------------------------- decide: the gateway request itself (rule 2)

def assert_why(verdict, why):
    assert verdict == verify.Verdict(False, why, None)


def test_decide_sends_a_call_whose_own_request_is_not_listed_to_a_human():
    assert_why(decide(call(), [park()], [], NOW), "gateway-request-not-listed")


def test_decide_does_not_take_another_request_for_the_gateway_request_itself():
    assert_why(decide(call(), [park()], [own(id="gw-9")], NOW), "gateway-request-not-listed")


def test_decide_looks_for_the_call_request_id_not_a_fixed_one():
    verdict = decide(call(request_id="req-77"), [park()], [own(id="req-77")], NOW)
    assert verdict.approve is True


def test_decide_accepts_a_gateway_request_created_exactly_when_the_park_was_consumed():
    assert decide(call(), [park()], [own(created_at=seconds_ago(10))], NOW).approve is True


def test_decide_rejects_a_gateway_request_created_a_millisecond_before_the_park_was_consumed():
    assert_why(decide(call(), [park()], [own(created_at=seconds_ago(10.001))], NOW),
               "gateway-request-before-release")


@pytest.mark.parametrize("seconds", [3, 5, 16])
def test_decide_rejects_a_gateway_request_created_before_the_park_was_consumed(seconds):
    # no allowance for skew: both stamps come from withHuman's own clock
    assert_why(decide(call(), [park()], [own(created_at=seconds_ago(10 + seconds))], NOW),
               "gateway-request-before-release")


def test_decide_sends_a_gateway_request_with_no_created_at_to_a_human_as_unsorted():
    item = own()
    del item["created_at"]
    assert_why(decide(call(), [park()], [item], NOW), "neighbours-unsorted")


@pytest.mark.parametrize("created", ["whenever", "2026-10-22T11:59:55", 5, None])
def test_decide_sends_a_gateway_request_with_an_unparseable_or_naive_created_at_to_a_human_as_unsorted(created):
    assert_why(decide(call(), [park()], [own(created_at=created)], NOW), "neighbours-unsorted")


@pytest.mark.parametrize("field, value", [
    ("agent_slug", "other-agent"), ("server", "xero"), ("tool", "create-bill"),
    ("arguments", {"Amount": 6, "CustomerRef": {"value": "58"}}),
    ("arguments", None), ("arguments", ["Amount", 5]), ("agent_slug", None), ("server", None), ("tool", None),
])
def test_decide_rejects_a_listed_gateway_request_that_differs_from_the_call(field, value):
    assert_why(decide(call(), [park()], [own(**{field: value})], NOW), "gateway-request-mismatch")


def test_decide_accepts_a_listed_gateway_request_whose_arguments_match_after_normalisation():
    listed = own(arguments={"Amount": 5.0, "CustomerRef": {"value": "58"}})
    assert decide(call(), [park()], [listed], NOW).approve is True


def test_decide_checks_the_request_time_before_comparing_its_fields():
    both = own(created_at=seconds_ago(60), tool="create-bill")
    assert_why(decide(call(), [park()], [both], NOW), "gateway-request-before-release")


def test_decide_looks_at_the_park_before_the_gateway_request():
    assert_no_approved_park(decide(call(), [], [], NOW))
    assert_no_approved_park(decide(call(), [park(arguments={"Amount": 9})], [], NOW))


# ---------------------------------------------------------------- decide: the listing must be complete (rule 3)
# withHuman's page size and sort order are not trusted. A listing is complete only when every item carries a
# readable created_at, the items run newest first, and the list reaches back to a request created strictly
# before consumed_at - SKEW_SECONDS (normally the park itself). These tests call verify.decide directly on
# hand-built lists.

def older(n, **overrides):
    """A gateway request for another tool, created ``60 + n`` seconds ago (well before the park was consumed)."""
    return neighbour(**{"id": f"old-{n}", "tool": "create-bill", "created_at": seconds_ago(60 + n), **overrides})


def newer(n, **overrides):
    """A request for another tool created after the park was consumed (not a conflict, but recent)."""
    return neighbour(**{"id": f"new-{n}", "tool": "create-bill", "created_at": seconds_ago(5 - n), **overrides})


def test_decide_accepts_a_listing_that_ends_with_the_park_itself():
    assert verify.decide(call(), [park()], [own(), park()], NOW).approve is True


def test_decide_accepts_a_listing_that_ends_with_another_old_request():
    assert verify.decide(call(), [park()], [own(), older(1), older(2)], NOW).approve is True


def test_decide_accepts_a_long_listing_that_reaches_back_whatever_its_length():
    listing = [newer(0, id=f"r-{i}") for i in range(198)] + [own(), older(1)]
    assert len(listing) == 200 and verify.decide(call(), [park()], listing, NOW).approve is True


def test_decide_sends_a_listing_with_nothing_older_than_the_release_to_a_human_as_truncated():
    assert_why(verify.decide(call(), [park()], [newer(1), own()], NOW), "neighbours-truncated")


def test_decide_sends_a_listing_of_only_the_gateway_request_to_a_human_as_truncated():
    assert_why(verify.decide(call(), [park()], [own()], NOW), "neighbours-truncated")


def test_decide_does_not_let_the_page_size_decide_that_a_long_listing_is_truncated_or_complete():
    recent = [newer(0, id=f"r-{i}") for i in range(250)]
    assert_why(verify.decide(call(), [park()], recent + [own()], NOW), "neighbours-truncated")


def test_decide_treats_an_oldest_item_created_exactly_5_seconds_before_the_consumption_as_not_old_enough():
    edge = neighbour(id="edge", tool="create-bill", created_at=seconds_ago(15))
    assert_why(verify.decide(call(), [park()], [own(), edge], NOW), "neighbours-truncated")


def test_decide_accepts_an_oldest_item_just_over_5_seconds_before_the_consumption():
    edge = neighbour(id="edge", tool="create-bill", created_at=seconds_ago(15.001))
    assert verify.decide(call(), [park()], [own(), edge], NOW).approve is True


def test_decide_does_not_accept_a_listing_just_because_one_item_is_old_if_it_is_out_of_order():
    assert_why(verify.decide(call(), [park()], [older(1), own(), park()], NOW), "neighbours-unsorted")


def test_decide_sends_a_listing_that_runs_oldest_first_to_a_human_as_unsorted():
    assert_why(verify.decide(call(), [park()], [park(), own()], NOW), "neighbours-unsorted")


def test_decide_sends_a_listing_with_one_misplaced_recent_item_to_a_human_as_unsorted():
    assert_why(verify.decide(call(), [park()], [own(), newer(1), older(1)], NOW), "neighbours-unsorted")


def test_decide_accepts_items_created_at_the_same_instant():
    twin = own(id="gw-twin", tool="create-bill")
    assert verify.decide(call(), [park()], [own(), twin, park()], NOW).approve is True


def test_decide_sends_a_listing_with_an_item_that_has_no_created_at_to_a_human_as_unsorted():
    nameless = older(1)
    del nameless["created_at"]
    assert_why(verify.decide(call(), [park()], [own(), nameless, older(2)], NOW), "neighbours-unsorted")


@pytest.mark.parametrize("created", ["whenever", "2026-10-22T11:00:00", 5, None])
def test_decide_sends_a_listing_with_an_unreadable_created_at_to_a_human_as_unsorted(created):
    assert_why(verify.decide(call(), [park()], [own(), older(1, created_at=created), older(2)], NOW),
               "neighbours-unsorted")


def test_decide_sends_a_listing_with_a_non_object_item_to_a_human_as_unsorted():
    assert_why(verify.decide(call(), [park()], [own(), "junk", older(2)], NOW), "neighbours-unsorted")


def test_decide_checks_the_order_before_whether_the_listing_reaches_back():
    assert_why(verify.decide(call(), [park()], [own(), newer(1)], NOW), "neighbours-unsorted")


def test_decide_checks_that_the_listing_reaches_back_before_looking_for_the_gateway_request():
    assert_why(verify.decide(call(), [park()], [newer(2), newer(1)], NOW), "neighbours-truncated")


def test_decide_checks_that_the_listing_reaches_back_before_looking_for_a_second_use():
    listing = [neighbour(), own()]  # the neighbour would be a conflict
    assert_why(verify.decide(call(), [park()], listing, NOW), "neighbours-truncated")


def test_decide_checks_the_gateway_request_before_looking_for_a_second_use():
    assert_why(verify.decide(call(), [park()], [neighbour(), old_park()], NOW), "gateway-request-not-listed")


def test_decide_checks_the_park_and_its_outcome_before_the_listing():
    redacted = {"Amount": 5, "Memo": REDACTED}
    assert_why(verify.decide(call(arguments=redacted), [park(arguments=redacted)], [own(), newer(1)], NOW),
               "redacted-args")
    reported = park(consumption={"consumed_at": seconds_ago(10), "outcome": "success"})
    assert_why(verify.decide(call(), [reported], [own(), newer(1)], NOW), "already-reported")


def test_decide_still_finds_a_second_use_in_a_listing_that_reaches_back_far_enough():
    listing = [neighbour(), own(), older(1)]
    assert_already_used(verify.decide(call(), [park()], listing, NOW))


def test_decide_no_longer_takes_a_page_size():
    with pytest.raises(TypeError):
        verify.decide(call(), [park()], [own(), park()], NOW, neighbours_limit=200)


# ---------------------------------------------------------------- decide: collision-aware single use (rule 4)
# Hermes folds "-" into "_", so two gateway tools that differ only there share one park request: a use of
# either one uses the approval.

def test_decide_counts_a_later_request_for_the_underscore_spelling_of_the_tool_as_a_second_use():
    assert_already_used(decide(call(), [park()], [own(), neighbour(tool="create_payment")], NOW))


def test_decide_counts_a_later_request_for_the_hyphen_spelling_of_the_tool_as_a_second_use():
    mine = call(tool="create_payment")
    listed = [own(tool="create_payment"), neighbour(tool="create-payment")]
    assert_already_used(decide(mine, [park_for(SERVER, "create_payment")], listed, NOW))


def test_decide_counts_a_later_request_on_the_other_spelling_of_the_server_as_a_second_use():
    mine = call(server="quick-books")
    listed = [own_for("quick-books", TOOL), neighbour(server="quick_books")]
    assert_already_used(decide(mine, [park_for("quick-books", TOOL)], listed, NOW))


def test_decide_does_not_count_a_request_whose_names_hold_a_double_underscore_as_a_use():
    # a tool name with "__" is never approved, so no approved call can share a park with such a request
    mine = call(server="a", tool="b_create_payment")  # mcp__wh__a__b_create_payment
    listed = [own_for("a", "b_create_payment"), neighbour(server="a__b", tool="create_payment")]
    assert decide(mine, [park_for("a", "b_create_payment")], listed, NOW).approve is True


def test_decide_still_ignores_a_collision_that_is_older_than_the_consumption():
    early = neighbour(tool="create_payment", created_at=seconds_ago(16))
    assert decide(call(), [park()], [own(), early], NOW).approve is True


def test_decide_still_ignores_a_collision_with_different_arguments():
    other = neighbour(tool="create_payment", arguments={"Amount": 6, "CustomerRef": {"value": "58"}})
    assert decide(call(), [park()], [own(), other], NOW).approve is True


def test_decide_still_ignores_a_collision_of_another_agent():
    other = neighbour(tool="create_payment", agent_slug="other-agent")
    assert decide(call(), [park()], [own(), other], NOW).approve is True


def test_decide_does_not_count_a_park_request_as_a_gateway_use():
    mine = call(server="park_quickbooks")  # its park tool name: mcp__wh__park_quickbooks__create_payment
    a_park = neighbour(server="park:quickbooks")  # sanitises to the same name, but it is a park, not a gateway call
    listed = [own_for("park_quickbooks", TOOL), a_park]
    assert decide(mine, [park_for("park_quickbooks", TOOL)], listed, NOW).approve is True


@pytest.mark.parametrize("server, tool", [(5, TOOL), (SERVER, None), (None, None), (["quickbooks"], TOOL),
                                          ({"a": 1}, {"b": 2})])
def test_decide_skips_a_neighbour_whose_server_or_tool_is_not_a_string(server, tool):
    odd = neighbour(server=server, tool=tool)
    assert decide(call(), [park()], [own(), odd], NOW).approve is True


def test_decide_skips_a_neighbour_whose_names_expected_park_cannot_be_computed(monkeypatch):
    real = verify.expected_park

    def picky(server, tool):
        if server == "unmappable":
            raise ValueError("cannot map")
        return real(server, tool)
    monkeypatch.setattr(verify, "expected_park", picky)
    odd = neighbour(server="unmappable")
    assert decide(call(), [park()], [own(), odd], NOW).approve is True


# ---------------------------------------------------------------- decide: redacted arguments (rule 5)
# withHuman stores park arguments after the plugin redacted secret-looking keys; a human who saw "[redacted]"
# approved something they could not read, so the call is theirs to see again.

def redacted_pair(park_arguments, call_arguments=None):
    """A park whose stored arguments are ``park_arguments`` and the call that matches it digest-wise."""
    return park(arguments=park_arguments), call(arguments=park_arguments if call_arguments is None else call_arguments)


@pytest.mark.parametrize("arguments", [
    {"Amount": 5, "Memo": REDACTED},
    {"Amount": 5, "Customer": {"Name": "x", "Note": REDACTED}},
    {"Amount": 5, "Lines": [1, REDACTED]},
    {"Amount": 5, "Lines": [{"Detail": {"Tags": ["a", [REDACTED]]}}]},
    {"Amount": 5, "Deep": {"a": {"b": {"c": [{"d": REDACTED}]}}}},
])
def test_decide_sends_a_park_holding_a_redacted_value_anywhere_to_a_human(arguments):
    matched_park, mine = redacted_pair(arguments)
    assert_why(decide(mine, [matched_park], [own(arguments=arguments)], NOW), "redacted-args")


def test_decide_names_redacted_args_rather_than_a_missing_gateway_request():
    matched_park, mine = redacted_pair({"Amount": 5, "Memo": REDACTED})
    assert_why(decide(mine, [matched_park], [], NOW), "redacted-args")


def test_decide_sends_a_call_whose_arguments_would_be_redacted_to_a_human():
    secret_args = {"Amount": 5, "api_key": "sk-live-123"}
    matched_park, mine = redacted_pair(secret_args)
    assert_why(decide(mine, [matched_park], [own(arguments=secret_args)], NOW), "redacted-args")


@pytest.mark.parametrize("key", ["password", "Token", "client_secret", "Authorization", "x-api-key", "apiKey"])
def test_decide_sends_a_call_with_a_secret_looking_key_anywhere_to_a_human(key):
    nested = {"Amount": 5, "Auth": {key: "value"}}
    matched_park, mine = redacted_pair(nested)
    assert_why(decide(mine, [matched_park], [own(arguments=nested)], NOW), "redacted-args")


def test_decide_sends_a_call_with_arguments_too_large_to_have_been_shown_whole_to_a_human():
    big = {"Memo": "x" * 20000}
    matched_park, mine = redacted_pair(big)
    assert_why(decide(mine, [matched_park], [own(arguments=big)], NOW), "redacted-args")


def test_decide_does_not_mistake_text_that_merely_mentions_redaction_for_a_redacted_value():
    text = {"Amount": 5, "Memo": "customer asked for [redacted] in the memo", "Tags": ["[redacted] soon"]}
    matched_park, mine = redacted_pair(text)
    assert decide(mine, [matched_park], [own(arguments=text)], NOW).approve is True


def test_decide_does_not_look_at_redaction_in_a_park_that_did_not_match():
    other = park(id="park-2", arguments={"Amount": 9, "Memo": REDACTED})
    assert decide(call(), [other, park()], [own()], NOW) == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- decide: already reported (rule 6)

@pytest.mark.parametrize("outcome", ["success", "failed", "unknown", "error", "", 0, False, {"result": "ok"}])
def test_decide_sends_a_park_with_a_reported_outcome_to_a_human(outcome):
    reported = park(consumption={"consumed_at": seconds_ago(10), "outcome": outcome})
    assert_why(decide(call(), [reported], [own()], NOW), "already-reported")


def test_decide_accepts_a_park_whose_consumption_has_no_outcome_key():
    fresh = park(consumption={"consumed_at": seconds_ago(10)})
    assert decide(call(), [fresh], [own()], NOW).approve is True


def test_decide_accepts_a_park_whose_outcome_is_null():
    fresh = park(consumption={"consumed_at": seconds_ago(10), "outcome": None})
    assert decide(call(), [fresh], [own()], NOW).approve is True


def test_decide_names_already_reported_rather_than_a_missing_gateway_request():
    reported = park(consumption={"consumed_at": seconds_ago(10), "outcome": "success"})
    assert_why(decide(call(), [reported], [], NOW), "already-reported")


def test_decide_names_redacted_args_before_already_reported():
    both = park(arguments={"Amount": 5, "Memo": REDACTED}, consumption={"consumed_at": seconds_ago(10),
                                                                         "outcome": "success"})
    assert_why(decide(call(arguments={"Amount": 5, "Memo": REDACTED}), [both], [own()], NOW), "redacted-args")


def test_decide_does_not_look_at_the_outcome_of_a_park_that_did_not_match():
    other = park(id="park-2", arguments={"Amount": 9},
                 consumption={"consumed_at": seconds_ago(10), "outcome": "success"})
    assert decide(call(), [other, park()], [own()], NOW) == verify.Verdict(True, "park-request=park-1", "park-1")


# ---------------------------------------------------------------- per-person (role agents): the same person

def test_decide_approves_a_role_agent_call_backed_by_the_same_persons_park():
    role = "ops-ops"
    verdict = decide(call(agent_slug=role), [park(agent_slug=role)], [own(agent_slug=role)], NOW)
    assert verdict == verify.Verdict(True, "park-request=park-1", "park-1")


def test_decide_does_not_use_the_approval_of_another_person():
    # Sarah's park (instance A) is approved and consumed; Tom (instance B, same role agent) sends the same call.
    assert_no_approved_park(decide(call(person=OTHER_PERSON), [park()],
                                   [own(agent_instance_id=OTHER_PERSON)], NOW))


@pytest.mark.parametrize("shape", ["missing", "empty", "none"])
def test_decide_rejects_a_park_that_names_no_person(shape):
    item = park()
    if shape == "missing":
        del item["agent_instance_id"]
    else:
        item["agent_instance_id"] = "" if shape == "empty" else None
    assert_no_approved_park(decide(call(), [item], [own()], NOW))


def test_decide_never_matches_two_empty_persons():
    # "" == "" must not count as "the same person".
    assert_no_approved_park(decide(call(person=""), [park(agent_instance_id="")],
                                   [own(agent_instance_id="")], NOW))


def test_decide_refuses_when_the_listed_gateway_request_belongs_to_another_person():
    assert_why(decide(call(), [park()], [own(agent_instance_id=OTHER_PERSON)], NOW), "gateway-request-mismatch")


def test_decide_counts_another_persons_identical_call_as_a_second_use():
    # On purpose agent-wide: this can only send more calls to a person, never fewer.
    assert_already_used(decide(call(), [park()], [own(), neighbour(agent_instance_id=OTHER_PERSON)], NOW))
