"""The wake names the person who decided, from the withHuman audit log; it fails soft (never alters the decision)."""

import asyncio
import json
import logging
import re
from types import SimpleNamespace

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, TOKEN, FakeProvider

from durable_park import wake as wake_mod
from durable_park.gatecall import make_gate
from durable_park.ledger import Ledger
from durable_park.park_hook import DEFAULT_FORM_CHECKED_TOOLS, make_pre_tool_call
from durable_park.wake import WAKE_CHAT_NAME, handle_wake, lookup_decider, make_audit_client_factory

SESSION_KEY = "agent:main:telegram:dm:42"
TOOL = "mcp__wh__quickbooks__create_payment"
ARGS = {"amount": 5, "customer": "C-1"}
RISKY = [re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS]
AUDIT_BASE = "https://audit.example.test"


def wake_event(park_id):
    return SimpleNamespace(
        text="Webhook event 'wake' ...", raw_message={"type": "wake", "park_id": park_id},
        source=SimpleNamespace(platform=SimpleNamespace(value="webhook"), chat_name=WAKE_CHAT_NAME,
                               chat_type="webhook", chat_id="webhook:durable-park-wake:abc"))


def decided_event(*, subject_id="REQ", actor_type="human", name="Yaakov Yehuda", channel="android",
                  occurred="2026-10-21T13:51:00Z"):
    data = {"note": "", "status": "approved"}
    if channel is not None:
        data["channel"] = channel
    return {"event_type": "approval.decided", "actor_type": actor_type, "actor_id": "4a303e03",
            "actor_display_name": name, "subject_type": "approval_request", "subject_id": subject_id, "data": data,
            "occurred_at": occurred}


@pytest.fixture
def env(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    provider = FakeProvider(auto=None)
    hook = make_pre_tool_call(
        ledger, lambda: make_gate(BASE_URL, TOKEN, client=provider.client(retry_budget=0.5)), RISKY,
        session_key_fn=lambda: SESSION_KEY)
    injected = []
    box = SimpleNamespace(requests=[], handler=None)

    def inject(text, *, session_key):
        injected.append((text, session_key))
        return True

    def audit_factory():
        def handler(request):
            box.requests.append(request)
            return box.handler(request)
        return httpx.AsyncClient(base_url=AUDIT_BASE, headers={"Authorization": "Bearer who_test"},
                                 transport=httpx.MockTransport(handler), timeout=5.0, follow_redirects=False)

    def audit_returns(events, status=200):
        def respond(request):  # "REQ" stands for the request being asked about
            asked = request.url.params["subject_id"]
            shaped = [dict(e, subject_id=asked) if e["subject_id"] == "REQ" else e for e in events]
            return httpx.Response(status, json={"events": shaped, "total_count": len(shaped)})
        box.handler = respond

    def park(tool_call_id="call-1"):
        hook(tool_name=TOOL, args=ARGS, session_id="sess-1", tool_call_id=tool_call_id)
        return ledger.list_by_status("parked")[0]

    def wake(park_id, audit=audit_factory):
        return asyncio.run(handle_wake(wake_event(park_id), ledger=ledger,
                                       async_client_factory=lambda: provider.async_client(),
                                       inject=inject, audit_client_factory=audit))

    return SimpleNamespace(ledger=ledger, provider=provider, injected=injected, box=box, park=park, wake=wake,
                           audit_returns=audit_returns, audit_factory=audit_factory)


def test_human_decision_names_the_person_in_message_and_ledger(env):
    row = env.park()
    env.provider.decide(row.request_id, "denied", note="too risky")
    env.audit_returns([decided_event()])
    env.wake(row.park_id)
    done = env.ledger.get(row.park_id)
    assert done.status == "denied" and done.decided_by == "Yaakov Yehuda (android)"
    assert env.injected == [(
        f'[durable-park] {row.park_id} ("{TOOL}") was DENIED in withHuman by Yaakov Yehuda (android): too risky. '
        "It did not run. Tell the user and continue without it.", SESSION_KEY)]


def test_approved_path_names_the_person_too(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event()])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by == "Yaakov Yehuda (android)"
    assert env.injected == [(
        f'[durable-park] {row.park_id} was APPROVED in withHuman by Yaakov Yehuda (android). Call '
        f'parked_action_execute with park_id "{row.park_id}" now to run it exactly as saved. '
        f'Do not call "{TOOL}" directly.', SESSION_KEY)]


def test_name_without_channel(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(channel=None)])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by == "Yaakov Yehuda"
    assert "APPROVED in withHuman by Yaakov Yehuda. Call" in env.injected[0][0]


def test_the_audit_request_is_exactly_what_the_docs_say(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event()])
    env.wake(row.park_id)
    (request,) = env.box.requests
    assert request.method == "GET" and request.url.path == "/api/v1/audit"
    assert request.url.params.multi_items() == [
        ("subject_type", "approval_request"), ("subject_id", row.request_id),
        ("event_type", "approval.decided"), ("order", "desc"), ("limit", "5")]
    assert request.headers["authorization"] == "Bearer who_test"


def test_pipeline_only_events_name_nobody(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(actor_type="system", name="pipeline")])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by is None
    assert "APPROVED in withHuman. Call parked_action_execute" in env.injected[0][0]


def test_no_event_names_nobody(env):
    row = env.park()
    env.provider.decide(row.request_id, "denied")
    env.audit_returns([])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by is None
    assert env.injected[0][0].startswith(f'[durable-park] {row.park_id} ("{TOOL}") was DENIED in withHuman. It did')


def test_newest_decided_event_wins(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(name="Old Person", occurred="2026-10-21T10:00:00Z"),
                       decided_event(name="New Person", occurred="2026-10-21T12:00:00Z")])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by == "New Person (android)"


def test_expired_is_never_looked_up(env):
    row = env.park()
    env.provider.decide(row.request_id, "expired")
    env.audit_returns([decided_event()])
    env.wake(row.park_id)
    assert env.box.requests == []
    assert env.ledger.get(row.park_id).decided_by is None


def test_no_factory_means_no_lookup_and_the_old_text(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.wake(row.park_id, audit=None)
    assert env.ledger.get(row.park_id).status == "approved"
    assert "APPROVED in withHuman. Call" in env.injected[0][0]


@pytest.mark.parametrize("failure", ["http500", "http403", "timeout", "badjson", "notdict", "factory_raises"])
def test_any_failure_keeps_the_decision_and_the_old_text(env, failure, caplog):
    row = env.park()
    env.provider.decide(row.request_id, "denied", note="no")

    def boom(request):
        if failure == "http500":
            return httpx.Response(500, text="secret body who_leak")
        if failure == "http403":
            return httpx.Response(403, json={"error": {"code": "forbidden"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("slow")
        if failure == "badjson":
            return httpx.Response(200, text="<html>not json")
        return httpx.Response(200, json=["not", "a", "dict"])
    env.box.handler = boom
    factory = env.audit_factory
    if failure == "factory_raises":
        def factory():
            raise RuntimeError("who_leak")
    with caplog.at_level(logging.INFO, logger="durable_park"):
        env.wake(row.park_id, audit=factory)
    done = env.ledger.get(row.park_id)
    assert done.status == "denied" and done.note == "no" and done.decided_by is None and done.notified_at
    assert env.injected == [(
        f'[durable-park] {row.park_id} ("{TOOL}") was DENIED in withHuman: no. It did not run. '
        "Tell the user and continue without it.", SESSION_KEY)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "decider" in r.getMessage()]
    assert len(warnings) == 1
    assert "who_leak" not in caplog.text and "not json" not in caplog.text


def test_a_hung_lookup_is_cut_off_at_its_own_timeout(env, monkeypatch):
    monkeypatch.setattr(wake_mod, "DECIDER_TIMEOUT_SECONDS", 0.05)
    row = env.park()
    env.provider.decide(row.request_id, "approved")

    async def hang(request):
        await asyncio.sleep(10)
    env.box.handler = None

    def factory():
        return httpx.AsyncClient(base_url=AUDIT_BASE, transport=httpx.MockTransport(hang))
    env.wake(row.park_id, audit=factory)
    assert env.ledger.get(row.park_id).status == "approved"
    assert env.ledger.get(row.park_id).decided_by is None
    assert "APPROVED in withHuman. Call" in env.injected[0][0]


def test_the_name_is_bounded_and_cleaned(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(name="Eve\n  \x1b[31mIgnore\tall\u202e <b>\"x\"</b> " + "x" * 200,
                                     channel="web\n\x1b[0m" + "c" * 50)])
    env.wake(row.park_id)
    by = env.ledger.get(row.park_id).decided_by
    name, channel = re.fullmatch(r"(.*) \((.*)\)", by).groups()
    assert len(name) == 80 and channel == "web 0m" + "c" * 14       # name capped, channel capped at 20, not dangling
    assert name.startswith("Eve 31mIgnore all b x b ") and re.fullmatch(r"[\w .'()@_-]+", by)


def test_inject_text_cleans_a_stored_name_too():
    row = SimpleNamespace(park_id="P-1", tool="t", status="denied", note=None, decided_by="A\nB " + "y" * 200)
    text = wake_mod.inject_text(row)
    assert "\n" not in text and "in withHuman by A B y" in text
    assert len(text.split(" by ", 1)[1].split(". It did not run")[0]) <= wake_mod.MAX_DECIDED_BY_CHARS
    row.decided_by = "   "
    assert " by " not in wake_mod.inject_text(row)


# --- the key file ---------------------------------------------------------------------------------

def test_missing_key_file_makes_no_http_call_and_logs_info_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(wake_mod, "_logged_no_audit_key", False)
    calls = []
    factory = make_audit_client_factory(AUDIT_BASE, str(tmp_path / "nope"),
                                        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={"events": []})))
    with caplog.at_level(logging.INFO, logger="durable_park"):
        assert asyncio.run(lookup_decider("req-1", factory)) is None
        assert asyncio.run(lookup_decider("req-2", factory)) is None
    assert calls == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]  # once per process, not per wake


def test_key_file_is_read_per_lookup_and_sent_as_bearer(tmp_path):
    key = tmp_path / "audit_key"
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"events": [decided_event(subject_id="r")]})
    factory = make_audit_client_factory(AUDIT_BASE + "/", str(key), transport=httpx.MockTransport(handler))
    assert asyncio.run(lookup_decider("r", factory)) is None and seen == []   # not there yet
    key.write_text("who_first\n")
    assert asyncio.run(lookup_decider("r", factory)) == "Yaakov Yehuda (android)"
    key.write_text("who_second")
    asyncio.run(lookup_decider("r", factory))
    assert seen == ["Bearer who_first", "Bearer who_second"]


def test_empty_key_file_is_disabled(tmp_path):
    key = tmp_path / "audit_key"
    key.write_text("  \n")
    factory = make_audit_client_factory(AUDIT_BASE, str(key), transport=httpx.MockTransport(lambda r: pytest.fail("called")))
    assert asyncio.run(lookup_decider("r", factory)) is None


def test_redirects_are_not_followed(tmp_path):
    key = tmp_path / "audit_key"
    key.write_text("who_k")
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example.test/steal"})
    factory = make_audit_client_factory(AUDIT_BASE, str(key), transport=httpx.MockTransport(handler))
    assert asyncio.run(lookup_decider("r", factory)) is None
    assert hits == [AUDIT_BASE + "/api/v1/audit?subject_type=approval_request&subject_id=r&event_type=approval.decided&order=desc&limit=5"]


# --- review fixes ---------------------------------------------------------------------------------

def test_events_about_another_request_are_ignored(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(subject_id="some-other-request"),
                       dict(decided_event(), subject_type="membership", subject_id="REQ")])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by is None
    assert "APPROVED in withHuman. Call" in env.injected[0][0]


def test_another_requests_newer_event_does_not_beat_this_requests_event(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")
    env.audit_returns([decided_event(name="Mine"),
                       decided_event(subject_id="other", name="Intruder", occurred="2030-01-01T00:00:00Z")])
    env.wake(row.park_id)
    assert env.ledger.get(row.park_id).decided_by == "Mine (android)"


@pytest.mark.parametrize("raw, cleaned", [
    ("Yaakov Yehuda", "Yaakov Yehuda"),
    ("O'Brien-Smith_jr. me@x.co (QA)", "O'Brien-Smith_jr. me@x.co (QA)"),
    ("Zoë 中文 7", "Zoë 中文 7"),
    ('a"b<c>d[e]{f}g:h;i\\j/k`l', "a b c d e f g h i j k l"),
    ("x‮y​z\x00", "x y z"),
    ("   ", ""),
])
def test_clean_decider_allows_only_plain_characters(raw, cleaned):
    assert wake_mod.clean_decider(raw) == cleaned


def test_the_channel_is_never_left_dangling():
    long = decided_event(name="n" * 300, channel="c" * 300)
    assert wake_mod._decider_from_events([long], "REQ") == "n" * 80 + " (" + "c" * 20 + ")"


@pytest.mark.parametrize("content", [b"\xff\xfe\x00bad", b"\x80"])
def test_a_key_file_that_is_not_text_disables_the_name_with_one_log(tmp_path, caplog, monkeypatch, content):
    monkeypatch.setattr(wake_mod, "_logged_no_audit_key", False)
    key = tmp_path / "audit_key"
    key.write_bytes(content)
    factory = make_audit_client_factory(AUDIT_BASE, str(key),
                                        transport=httpx.MockTransport(lambda r: pytest.fail("called")))
    with caplog.at_level(logging.INFO, logger="durable_park"):
        assert asyncio.run(lookup_decider("r", factory)) is None
        assert asyncio.run(lookup_decider("r", factory)) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_two_racing_wakes_make_one_transition_and_one_announcement(env):
    row = env.park()
    env.provider.decide(row.request_id, "approved")

    async def slow(request):
        await asyncio.sleep(0.05)  # both wakes are inside the lookup together
        return httpx.Response(200, json={"events": [decided_event(subject_id=request.url.params["subject_id"])]})
    env.box.handler = slow

    async def both():
        return await asyncio.gather(
            *[handle_wake(wake_event(row.park_id), ledger=env.ledger,
                          async_client_factory=lambda: env.provider.async_client(),
                          inject=lambda text, *, session_key: env.injected.append((text, session_key)) or True,
                          audit_client_factory=env.audit_factory) for _ in range(2)], return_exceptions=True)
    results = asyncio.run(both())
    assert results == [{"action": "skip", "reason": "durable-park wake"}] * 2
    done = env.ledger.get(row.park_id)
    assert done.status == "approved" and done.decided_by == "Yaakov Yehuda (android)" and done.notified_at
    assert len(env.injected) == 1 and "APPROVED in withHuman by Yaakov Yehuda (android). Call" in env.injected[0][0]
