"""Per-profile wake: each profile's copy of durable-park handles only its own wake route and its own rows."""
import asyncio
import json
import logging
import re
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from durable_park import keys, wake
from durable_park.gatecall import make_gate
from durable_park.ledger import Ledger
from durable_park.park_hook import DEFAULT_FORM_CHECKED_TOOLS, make_pre_tool_call
from durable_park.wake import WAKE_CHAT_NAME, drop_wake_hook, handle_wake, is_wake_event

TOOL = "mcp__wh__quickbooks__create_payment"
ARGS = {"amount": 5, "customer": "C-1"}
SKIP = {"action": "skip", "reason": "durable-park wake"}
RISKY = [re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS]
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def ev(chat_name, platform="webhook", park_id="P-1"):
    return SimpleNamespace(
        text="Webhook event 'wake' ...", raw_message={"type": "wake", "park_id": park_id},
        source=SimpleNamespace(platform=SimpleNamespace(value=platform), chat_name=chat_name,
                               chat_type="webhook", chat_id="webhook:x:abc"))


# --- item 3: which events a copy treats as a wake -------------------------------------------------

def test_is_wake_event_per_profile():
    assert is_wake_event(ev("webhook/durable-park-wake"), None)
    assert is_wake_event(ev("webhook/durable-park-wake"), "")
    assert is_wake_event(ev("webhook/durable-park-wake"), "default")
    assert is_wake_event(ev("webhook/durable-park-wake-sarah"), "sarah")
    assert not is_wake_event(ev("webhook/durable-park-wake-sarah"), "tom")
    assert not is_wake_event(ev("webhook/durable-park-wake-sarah"), None)
    assert not is_wake_event(ev("webhook/durable-park-wake"), "sarah")
    # a chat merely titled like the route is not the webhook platform
    assert not is_wake_event(ev("webhook/durable-park-wake", platform="telegram"), None)
    assert not is_wake_event(ev("webhook/durable-park-wake-sarah", platform="telegram"), "sarah")
    assert not is_wake_event(SimpleNamespace(), "sarah")
    assert WAKE_CHAT_NAME == "webhook/durable-park-wake"


def test_drop_wake_hook_drops_only_its_own_route():
    hook = drop_wake_hook("sarah")
    assert asyncio.run(hook(event=ev("webhook/durable-park-wake-sarah"))) == SKIP
    assert asyncio.run(hook(event=ev("webhook/durable-park-wake-tom"))) is None
    assert asyncio.run(hook(event=ev("webhook/durable-park-wake"))) is None
    default = drop_wake_hook()
    assert asyncio.run(default(event=ev("webhook/durable-park-wake"))) == SKIP
    assert asyncio.run(default(event=ev("webhook/durable-park-wake-sarah"))) is None


def test_drop_wake_hook_for_an_unknown_profile_drops_every_durable_park_route():
    hook = drop_wake_hook(None, any_profile=True)
    for name in ("webhook/durable-park-wake", "webhook/durable-park-wake-sarah"):
        assert asyncio.run(hook(event=ev(name))) == SKIP
    assert asyncio.run(hook(event=ev("webhook/github-pr"))) is None
    assert asyncio.run(hook(event=ev("webhook/durable-park-wake-sarah", platform="telegram"))) is None


# --- a world with one provider, a ledger, and key files ---------------------------------------------

@pytest.fixture
def env(tmp_path):
    instances = tmp_path / "instances"
    instances.mkdir()
    status = tmp_path / "status"
    status.mkdir()
    ledger = Ledger(str(tmp_path / "ledger.db"))
    provider = FakeProvider(auto=None)
    injected = []
    opened = []  # the key files the wake built a client from

    def inject(text, *, session_key):
        injected.append((text, session_key))
        return True

    def client_for(path):
        opened.append(path)
        return provider.async_client(retry_budget=0.2)

    def key(profile, text="tok\n"):
        path = instances / f"{profile}.token"
        path.write_text(text)
        return str(path)

    def park(profile, tool_call_id="call-1"):
        """A parked row of *profile* (None = the default profile)."""
        key_profile = profile or "default"
        hook = make_pre_tool_call(
            ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5)), RISKY,
            session_key_fn=lambda: f"agent:{profile or 'main'}:teams:dm:{tool_call_id}",  # one chat per row
            user_profile_fn=lambda: ("u-1", profile or ""))
        hook(tool_name=TOOL, args=ARGS, session_id=f"sess-{key_profile}-{tool_call_id}", tool_call_id=tool_call_id)
        return [r for r in ledger.list_by_status("parked") if r.tool_call_id == tool_call_id
                and r.session_id == f"sess-{key_profile}-{tool_call_id}"][0]

    def wake(row, own_profile, token_file, *, status_dir=str(status), with_client_for=True):
        kwargs = dict(own_profile=own_profile, token_file=token_file, status_dir=status_dir)
        if with_client_for:
            kwargs["client_for_key_file"] = client_for
        return asyncio.run(handle_wake(
            ev(f"webhook/durable-park-wake-{own_profile}" if own_profile not in (None, "", "default")
               else "webhook/durable-park-wake",
               park_id=row.park_id),
            ledger=ledger, async_client_factory=lambda: pytest.fail("the wake must use the key file's client"),
            inject=inject, **kwargs))

    def gets():
        return [c for c in provider.calls if c[0] == "GET"]

    return SimpleNamespace(ledger=ledger, provider=provider, injected=injected, opened=opened, key=key, park=park,
                           wake=wake, gets=gets, instances=instances, status=status, tmp=tmp_path)


def refusals(env):
    path = env.status / "wake_refusals.json"
    return json.loads(path.read_text()) if path.exists() else None


# --- item 4: a wake for another profile's row ------------------------------------------------------

@pytest.mark.parametrize("own, row_profile", [("tom", "sarah"), (None, "sarah"), ("sarah", None)])
def test_wake_for_another_profiles_row_is_not_posted(env, caplog, own, row_profile):
    caplog.set_level(logging.INFO, logger="durable_park")
    row = env.park(row_profile)
    env.provider.decide(row.request_id, "approved")
    own_file = env.key(own) if own else str(env.tmp / "default-token")
    if not own:
        (env.tmp / "default-token").write_text("tok\n")
    before = env.ledger.get(row.park_id)
    assert env.wake(row, own, own_file) == SKIP
    assert env.injected == [] and env.gets() == [] and env.opened == []
    assert env.ledger.get(row.park_id) == before  # untouched: the sweep deals with it
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    text = " ".join(r.getMessage() for r in warnings)
    assert row.park_id in text and (own or "default") in text and (row_profile or "default") in text
    assert TOKEN not in text and "tok" not in text.replace("token", "")


@pytest.mark.parametrize("own", [None, "", "default"])
@pytest.mark.parametrize("row_profile", [None, "", "default"])
def test_default_spelled_three_ways_is_one_profile(env, own, row_profile):
    default_file = env.tmp / "default-token"
    default_file.write_text("tok\n")
    row = env.ledger.park(session_key="agent:main:teams:dm:x", session_id="s", tool_call_id="c", tool=TOOL,
                          args=ARGS, call_key="k", server="wh", profile=row_profile)
    env.ledger.set_request_id(row.park_id, "req-1")
    env.ledger.transition(row.park_id, "parked", "approved")
    assert env.wake(row, own, str(default_file)) == SKIP
    assert len(env.injected) == 1 and "APPROVED" in env.injected[0][0]
    assert env.ledger.get(row.park_id).notified_at


# --- F4b: the row's profile must agree with the profile named in its session key ---------------------------

def _row_with_key(env, session_key, profile, status="parked"):
    row = env.ledger.park(session_key=session_key, session_id="s-" + session_key, tool_call_id="c", tool=TOOL,
                          args=ARGS, call_key="k-" + session_key, server="wh", profile=profile)
    env.ledger.set_request_id(row.park_id, "req-1")
    if status == "executed":
        env.ledger.transition(row.park_id, "parked", "approved")
        env.ledger.transition(row.park_id, "approved", "executing", executing_at="2026-01-01T00:00:00.000000Z")
        env.ledger.transition(row.park_id, "executing", "executed")
    elif status != "parked":
        env.ledger.transition(row.park_id, "parked", status)
    return env.ledger.get(row.park_id)


@pytest.mark.parametrize("status", ["parked", "approved", "denied", "executed"])
@pytest.mark.parametrize("own, row_profile, session_key", [
    ("sarah", "sarah", "agent:tom:teams:dm:x"),      # the row says sarah, the chat is tom's
    ("sarah", "sarah", "agent:main:teams:dm:x"),     # the row says sarah, the chat is the default profile's
    ("sarah", "sarah", "agent:default:teams:dm:x"),
    (None, None, "agent:sarah:teams:dm:x"),          # the row says default, the chat is sarah's
    ("default", "default", "agent:sarah:teams:dm:x"),
    ("sarah", "sarah", "agent:Bad Name:teams:dm:x"),  # not a profile name at all
])
def test_a_row_whose_session_key_names_another_profile_is_refused_untouched(env, caplog, status, own, row_profile,
                                                                           session_key):
    caplog.set_level(logging.INFO, logger="durable_park")
    own_file = env.key(own) if own and own != "default" else str(env.tmp / "default-token")
    if not own or own == "default":
        (env.tmp / "default-token").write_text("tok\n")
    row = _row_with_key(env, session_key, row_profile, status)
    before = env.ledger.get(row.park_id)
    assert env.wake(row, own, own_file) == SKIP
    assert env.injected == [] and env.gets() == [] and env.opened == []
    assert env.ledger.get(row.park_id) == before
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert row.park_id in text and (own or "default") in text
    assert "tok" not in text.replace("token", "")


@pytest.mark.parametrize("own, row_profile, session_key", [
    ("sarah", "sarah", "agent:sarah:teams:dm:x"),
    (None, None, "agent:main:teams:dm:x"),
    (None, "default", "agent:default:teams:dm:x"),
    ("default", None, "agent:main:teams:dm:x"),
    # a key that does not have the agent:<ns>:... shape is not checked (old rows, other platforms)
    ("sarah", "sarah", "teams-dm-x"),
    (None, None, "agent"),
    ("sarah", "sarah", "agent:"),
    ("sarah", "sarah", ""),
])
def test_a_row_whose_session_key_agrees_or_has_no_namespace_is_resumed(env, own, row_profile, session_key):
    own_file = env.key(own) if own and own != "default" else str(env.tmp / "default-token")
    if not own or own == "default":
        (env.tmp / "default-token").write_text("tok\n")
    row = _row_with_key(env, session_key, row_profile, "approved")
    assert env.wake(row, own, own_file) == SKIP
    assert len(env.injected) == 1 and env.injected[0][1] == session_key
    assert env.ledger.get(row.park_id).notified_at


# --- item 5: the decision is read with the row's key -------------------------------------------------

def test_wake_reads_decision_with_row_profile_key(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    env.provider.decide(row.request_id, "approved")
    assert env.wake(row, "sarah", sarah) == SKIP
    assert env.opened == [sarah]  # the client came from sarah's own key file
    assert env.ledger.get(row.park_id).status == "approved"
    assert len(env.injected) == 1 and env.injected[0][1] == row.session_key
    assert env.injected[0][1].startswith("agent:sarah:")


def test_the_default_profile_reads_with_its_configured_token_file(env):
    default_file = env.tmp / "elsewhere" / "any-name"
    default_file.parent.mkdir()
    default_file.write_text("tok\n")
    row = env.park(None)
    env.provider.decide(row.request_id, "approved")
    assert env.wake(row, None, str(default_file)) == SKIP
    assert env.opened == [str(default_file)]
    assert env.ledger.get(row.park_id).status == "approved"


def test_a_copy_whose_token_file_is_not_its_profiles_key_refuses(env, caplog):
    caplog.set_level(logging.INFO, logger="durable_park")
    other = env.key("someone-else")  # sarah's copy was wired to another person's key
    row = env.park("sarah")
    env.provider.decide(row.request_id, "approved")
    before = env.ledger.get(row.park_id)
    assert env.wake(row, "sarah", other) == SKIP
    assert env.opened == [] and env.gets() == [] and env.injected == []
    assert env.ledger.get(row.park_id) == before
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert row.park_id in text and "sarah" in text


# --- F5: the default copy must not be wired to a person's key file ---------------------------------------------

def test_a_default_copy_wired_into_the_instances_dir_refuses(env, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="durable_park")
    monkeypatch.setattr(keys, "INSTANCES_DIR", str(env.instances))
    wired = env.key("sarah")  # the default copy was given a person's key file
    row = env.park(None)
    env.provider.decide(row.request_id, "approved")
    before = env.ledger.get(row.park_id)
    assert env.wake(row, None, wired) == SKIP
    assert env.opened == [] and env.gets() == [] and env.injected == []
    assert env.ledger.get(row.park_id) == before
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert row.park_id in text and "instances" in text and "tok" not in text.replace("token", "")


def test_a_default_copy_wired_to_a_symlink_into_the_instances_dir_refuses(env, monkeypatch):
    monkeypatch.setattr(keys, "INSTANCES_DIR", str(env.instances))
    target = env.key("sarah")
    link = env.tmp / "innocent-name"
    link.symlink_to(target)
    row = env.park(None)
    env.provider.decide(row.request_id, "approved")
    assert env.wake(row, None, str(link)) == SKIP
    assert env.opened == [] and env.gets() == [] and env.injected == []
    assert env.ledger.get(row.park_id).status == "parked"


def test_the_instances_dir_is_a_parameter_and_only_the_default_copy_is_checked(env):
    sarah = env.key("sarah")
    other = env.tmp / "other-token"
    other.write_text("tok\n")
    sarah_row = env.park("sarah")
    default_row = env.park(None)
    inside = str(env.instances)
    assert "instances" in wake._own_key_problem(default_row, sarah, None, instances_dir=inside)
    assert wake._own_key_problem(default_row, str(other), None, instances_dir=inside) is None
    # a person's own key file lives in the instances dir, as it should
    assert wake._own_key_problem(sarah_row, sarah, "sarah", instances_dir=inside) is None
    # the real directory is the default
    assert wake._own_key_problem(default_row, str(other), None) is None
    assert wake._own_key_problem(default_row, keys.INSTANCES_DIR + "/x.token", None) is not None


def test_a_sibling_directory_with_the_same_prefix_is_not_the_instances_dir(env):
    sibling = env.tmp / "instances-old"
    sibling.mkdir()
    (sibling / "x.token").write_text("tok\n")
    row = env.park(None)
    assert wake._own_key_problem(row, str(sibling / "x.token"), None, instances_dir=str(env.instances)) is None


# --- item 6: no key file, no resume ------------------------------------------------------------------

@pytest.mark.parametrize("problem", ["missing", "empty", "blank", "not_text"])
def test_wake_missing_key_file_does_not_resume(env, caplog, problem):
    caplog.set_level(logging.INFO, logger="durable_park")
    path = env.instances / "sarah.token"
    if problem == "empty":
        path.write_text("")
    elif problem == "blank":
        path.write_text("  \n")
    elif problem == "not_text":
        path.write_bytes(b"\xff\xfe\x00")
    row = env.park("sarah")
    env.provider.decide(row.request_id, "approved")
    env.ledger.transition(row.park_id, "parked", "approved")  # the sweep or an earlier wake decided it
    before = env.ledger.get(row.park_id)
    assert env.wake(row, "sarah", str(path)) == SKIP
    assert env.injected == [] and env.opened == [] and env.gets() == []
    assert env.ledger.get(row.park_id) == before  # still approved, not notified
    assert before.status == "approved" and before.notified_at is None
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "sarah" in text and row.park_id in text


def test_missing_key_file_leaves_a_parked_row_unread(env, caplog):
    caplog.set_level(logging.INFO, logger="durable_park")
    row = env.park("sarah")
    env.provider.decide(row.request_id, "approved")
    assert env.wake(row, "sarah", str(env.instances / "sarah.token")) == SKIP
    assert env.injected == [] and env.gets() == []
    assert env.ledger.get(row.park_id).status == "parked"


def test_a_missing_key_does_not_stop_a_denial_or_an_outcome_notice(env):
    path = str(env.instances / "sarah.token")  # does not exist
    denied = env.park("sarah", tool_call_id="call-d")
    env.ledger.transition(denied.park_id, "parked", "denied", note="no")
    assert env.wake(denied, "sarah", path) == SKIP
    assert "was DENIED" in env.injected[-1][0]
    expired = env.park("sarah", tool_call_id="call-e")
    env.ledger.transition(expired.park_id, "parked", "expired")
    env.wake(expired, "sarah", path)
    assert "no decision before its deadline" in env.injected[-1][0]
    ended = env.park("sarah", tool_call_id="call-x")
    env.ledger.transition(ended.park_id, "parked", "approved")
    env.ledger.transition(ended.park_id, "approved", "executing", executing_at="2026-01-01T00:00:00.000000Z")
    env.ledger.transition(ended.park_id, "executing", "executed")
    env.wake(ended, "sarah", path)
    assert "ran: " in env.injected[-1][0]
    assert len(env.injected) == 3


# --- item 7: wake_refusals.json -----------------------------------------------------------------------

def refuse_reads(env):
    env.provider.intercept = lambda r: httpx.Response(401, json={"error": "no"}) if r.method == "GET" else None


def test_wake_records_credential_refused(env, caplog):
    caplog.set_level(logging.INFO, logger="durable_park")
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    assert env.wake(row, "sarah", sarah) == SKIP
    assert env.injected == [] and env.ledger.get(row.park_id).status == "parked"
    data = refusals(env)
    assert list(data) == [row.park_id]
    entry = data[row.park_id]
    assert set(entry) == {"profile", "count", "first_at"}
    assert entry["profile"] == "sarah" and entry["count"] == 1
    datetime.strptime(entry["first_at"], STAMP)  # the exact format the sweep reads
    assert entry["first_at"].endswith("Z") and len(entry["first_at"]) == len("2026-10-27T19:25:51.490000Z")
    assert [p.name for p in env.status.iterdir()] == ["wake_refusals.json"]  # no temp file left
    text = " ".join(r.getMessage() for r in caplog.records)
    assert row.park_id in text and "tok" not in text.replace("token", "")


def test_wake_records_credential_refused_for_the_default_profile_as_default(env):
    default_file = env.tmp / "default-token"
    default_file.write_text("tok\n")
    row = env.park(None)
    refuse_reads(env)
    env.wake(row, None, str(default_file))
    assert refusals(env)[row.park_id]["profile"] == "default"


def test_wake_records_credential_refused_counts_repeats(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    env.wake(row, "sarah", sarah)
    first = refusals(env)[row.park_id]["first_at"]
    env.wake(row, "sarah", sarah)
    env.wake(row, "sarah", sarah)
    entry = refusals(env)[row.park_id]
    assert entry["count"] == 3 and entry["first_at"] == first  # the first time is kept


def test_wake_records_credential_refused_keeps_other_rows_entries(env):
    sarah = env.key("sarah")
    one, two = env.park("sarah", "call-1"), env.park("sarah", "call-2")
    refuse_reads(env)
    env.wake(one, "sarah", sarah)
    env.wake(two, "sarah", sarah)
    assert set(refusals(env)) == {one.park_id, two.park_id}


def test_wake_records_credential_refused_clears_on_success(env):
    sarah = env.key("sarah")
    row, other = env.park("sarah", "call-1"), env.park("sarah", "call-2")
    refuse_reads(env)
    env.wake(row, "sarah", sarah)
    env.wake(other, "sarah", sarah)
    env.provider.intercept = None
    env.wake(row, "sarah", sarah)  # read fine: still pending
    assert set(refusals(env)) == {other.park_id}
    env.provider.decide(other.request_id, "denied")
    env.wake(other, "sarah", sarah)  # read fine: decided
    assert refusals(env) == {}


def test_wake_records_credential_refused_clears_when_the_row_is_no_longer_parked(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    env.wake(row, "sarah", sarah)
    assert row.park_id in refusals(env)
    env.ledger.transition(row.park_id, "parked", "cancelled", note="sweep")
    env.wake(row, "sarah", sarah)
    assert refusals(env) == {}


def test_other_provider_errors_are_not_recorded_as_refusals(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    env.provider.intercept = lambda r: httpx.Response(500) if r.method == "GET" else None
    env.wake(row, "sarah", sarah)
    assert refusals(env) is None


def test_no_status_dir_means_nothing_is_recorded(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    assert env.wake(row, "sarah", sarah, status_dir=None) == SKIP
    assert refusals(env) is None and env.ledger.get(row.park_id).status == "parked"


def test_a_broken_refusals_file_is_replaced(env):
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    (env.status / "wake_refusals.json").write_text("{not json")
    env.wake(row, "sarah", sarah)
    assert refusals(env)[row.park_id]["count"] == 1


def test_a_recording_failure_never_raises_out_of_the_wake(env, caplog):
    caplog.set_level(logging.INFO, logger="durable_park")
    sarah = env.key("sarah")
    row = env.park("sarah")
    refuse_reads(env)
    (env.status / "wake_refusals.json").mkdir()  # cannot be replaced by a file
    assert env.wake(row, "sarah", sarah) == SKIP
    assert env.ledger.get(row.park_id).status == "parked" and env.injected == []
    assert any(r.levelno == logging.WARNING and "wake_refusals.json" in r.getMessage() for r in caplog.records)
