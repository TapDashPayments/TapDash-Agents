import os
import sqlite3
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import durable_park_sweep as sweep
from durable_park.ledger import Ledger

SESSION_KEY = "agent:main:telegram:dm:42"
TOOL = "mcp__wh__quickbooks__create_payment"
SCRIPT = Path(sweep.__file__).resolve()
REAL_MAINPID = sweep._gateway_mainpid if hasattr(sweep, "_gateway_mainpid") else None
DEFAULT_URL = "http://127.0.0.1:8644/webhooks/durable-park-wake"


def stamp(minutes_ago):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_policy(tmp_path, *, ok, problems=()):
    (tmp_path / "tool_policy_status.json").write_text(json.dumps(
        {"ok": ok, "checked_at": "2026-10-21T10:00:00Z", "problems": list(problems), "allowed": []}))


class World:
    def __init__(self, tmp_path):
        self.ledger = Ledger(str(tmp_path / "ledger.db"))
        # a healthy gateway form-check report (tests of that alert replace it)
        (tmp_path / "formcheck_status.json").write_text(
            '{"ok": true, "tools_checked": 1, "problems": [], "checked_at": "%s"}'
            % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        write_policy(tmp_path, ok=True)  # a passing tool policy report from the last gateway start
        self.secret_file = tmp_path / "wake_secret"
        self.secret_file.write_text("s3cr3t\n")
        self.posted = []
        self.fail = {}
        self.n = 0

    def row(self, status="parked", *, request_id="req-x", notified=False, executing_minutes_ago=None, tool=TOOL):
        self.n += 1
        row = self.ledger.park(session_key=SESSION_KEY, session_id="s", tool_call_id=f"c{self.n}", tool=tool,
                               args={"n": self.n}, call_key=f"k{self.n}", server="wh")
        if request_id:
            self.ledger.set_request_id(row.park_id, f"{request_id}-{self.n}")
        path = {"parked": [], "approved": ["approved"], "denied": ["denied"], "expired": ["expired"],
                "cancelled": ["cancelled"], "allowed": ["allowed"], "executing": ["approved", "executing"],
                "executed": ["approved", "executing", "executed"]}[status]
        previous = "parked"
        for nxt in path:
            extra = {}
            if nxt == "executing":
                extra["executing_at"] = stamp(executing_minutes_ago if executing_minutes_ago is not None else 1)
            assert self.ledger.transition(row.park_id, previous, nxt, **extra)
            previous = nxt
        if notified:
            self.ledger.mark_notified(row.park_id)
        return row.park_id

    def backdate(self, park_id, minutes):
        con = sqlite3.connect(self.ledger.path, isolation_level=None)
        try:
            con.execute("UPDATE parks SET created_at=? WHERE park_id=?", (stamp(minutes), park_id))
        finally:
            con.close()

    def age(self, park_id, days):
        con = sqlite3.connect(self.ledger.path, isolation_level=None)
        try:
            con.execute("UPDATE parks SET updated_at=? WHERE park_id=?", (stamp(days * 24 * 60), park_id))
        finally:
            con.close()

    def post(self, park_id, secret, url=DEFAULT_URL):
        self.posted.append((park_id, secret, url))
        outcome = self.fail.get(park_id, 202)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def main(self, monkeypatch, *extra):
        monkeypatch.setattr(sweep, "post_wake", self.post)
        return sweep.main(["--ledger", self.ledger.path, "--wake-secret-file", str(self.secret_file), *extra])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture(autouse=True)
def _no_gateway_pid(monkeypatch):
    """No real systemctl in tests: by default the gateway's MainPID is unknown, so the load-proof alert stays
    silent (the tests of that alert set a pid)."""
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: None)


def test_orphan_parked_row_is_cancelled_after_ten_minutes(world, monkeypatch, capsys):
    park_id = world.row("parked", request_id=None)
    world.backdate(park_id, 11)
    assert world.main(monkeypatch) == 0
    row = world.ledger.get(park_id)
    assert row.status == "cancelled"
    assert row.note == "no approval request after 10 minutes (cancelled by the sweep)"
    assert (f"durable-park: {park_id} ({TOOL}) never got an approval request; cancelled after 10 minutes"
            in capsys.readouterr().out)


def test_young_or_requested_parked_rows_are_left_alone(world, monkeypatch, capsys):
    young = world.row("parked", request_id=None)
    world.backdate(young, 9)
    requested = world.row("parked")
    world.backdate(requested, 60)
    world.main(monkeypatch)
    assert world.ledger.get(young).status == "parked"
    assert world.ledger.get(requested).status == "parked"
    assert "never got" not in capsys.readouterr().out


def test_sweep_rewakes_unnotified_approved(world, monkeypatch, capsys):
    parked = world.row("parked")
    parked_no_request = world.row("parked", request_id=None)
    approved = world.row("approved")
    approved_notified = world.row("approved", notified=True)
    world.row("denied", notified=True)   # decided and already announced: nothing to do
    world.row("expired", notified=True)
    world.row("executed")
    world.row("executing", executing_minutes_ago=1)
    assert world.main(monkeypatch) == 0
    assert world.posted == [(parked, "s3cr3t", DEFAULT_URL), (approved, "s3cr3t", DEFAULT_URL)]
    assert parked_no_request not in [p[0] for p in world.posted]
    assert approved_notified not in [p[0] for p in world.posted]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_sweep_rewakes_unnotified_decisions_that_have_a_request(world, monkeypatch, capsys):
    denied = world.row("denied")
    expired = world.row("expired")
    cancelled = world.row("cancelled")
    world.row("cancelled", request_id=None)               # park-hook cancel: never a withHuman decision
    world.row("denied", notified=True)
    assert world.main(monkeypatch) == 0
    assert [p[0] for p in world.posted] == [denied, expired, cancelled]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_sweep_uses_given_wake_url(world, monkeypatch):
    parked = world.row("parked")
    assert world.main(monkeypatch, "--wake-url", "http://127.0.0.1:9999/webhooks/x") == 0
    assert world.posted == [(parked, "s3cr3t", "http://127.0.0.1:9999/webhooks/x")]


def test_sweep_marks_stale_executing_unknown(world, monkeypatch, capsys):
    stale = world.row("executing", executing_minutes_ago=11)
    fresh = world.row("executing", executing_minutes_ago=9)
    assert world.main(monkeypatch) == 0
    assert capsys.readouterr().out == f"durable-park: {stale} outcome UNKNOWN — check {TOOL} in the sandbox before retrying\n"
    assert world.ledger.get(stale).status == "unknown"
    assert world.ledger.get(stale).note
    assert world.ledger.get(fresh).status == "executing"
    assert world.posted == []
    # alert once: the next run is silent
    assert world.main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_sweep_prints_one_line_per_stale_row_in_order(world, monkeypatch, capsys):
    a = world.row("executing", executing_minutes_ago=30, tool="mcp__wh__quickbooks__create_bill")
    b = world.row("executing", executing_minutes_ago=20)
    world.main(monkeypatch)
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"durable-park: {a} outcome UNKNOWN — check mcp__wh__quickbooks__create_bill in the sandbox before retrying",
        f"durable-park: {b} outcome UNKNOWN — check {TOOL} in the sandbox before retrying"]


def test_sweep_stale_check_falls_back_to_updated_at_when_executing_at_missing(world, monkeypatch, capsys):
    pid = world.row("executing", executing_minutes_ago=1)
    con = sqlite3.connect(world.ledger.path, isolation_level=None)
    try:
        con.execute("UPDATE parks SET executing_at=NULL, updated_at=? WHERE park_id=?", (stamp(15), pid))
    finally:
        con.close()
    world.main(monkeypatch)
    assert f"durable-park: {pid} outcome UNKNOWN" in capsys.readouterr().out


def test_unknown_alert_is_printed_even_if_the_wake_phase_fails(world, monkeypatch, capsys):
    stale = world.row("executing", executing_minutes_ago=15)
    world.row("parked")

    def boom(ledger):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sweep, "_wake_targets", boom)
    with pytest.raises(RuntimeError):
        world.main(monkeypatch)
    assert capsys.readouterr().out == (
        f"durable-park: {stale} outcome UNKNOWN \u2014 check {TOOL} in the sandbox before retrying\n")
    assert world.ledger.get(stale).status == "unknown"


def test_unknown_alerts_are_printed_before_any_wake_is_posted(world, monkeypatch, capsys):
    stale = world.row("executing", executing_minutes_ago=15)
    world.row("parked")
    order = []
    real_print = print

    def spy_print(*args, **kwargs):
        order.append("print")
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", spy_print)
    monkeypatch.setattr(sweep, "post_wake", lambda *a, **k: order.append("post") or 202)
    sweep.main(["--ledger", world.ledger.path, "--wake-secret-file", str(world.secret_file)])
    assert order == ["print", "post"] and stale


def test_sweep_silent_when_nothing_to_do(world, monkeypatch, capsys):
    world.secret_file.unlink()   # nothing to wake, so the secret is never needed
    assert world.main(monkeypatch) == 0
    world.row("denied", notified=True)
    world.row("cancelled", request_id=None)
    world.row("executed")
    world.row("executing", executing_minutes_ago=2)
    world.row("approved", notified=True)
    assert world.main(monkeypatch) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and world.posted == []


def test_sweep_post_failures_go_to_stderr_only_and_do_not_stop_the_sweep(world, monkeypatch, capsys):
    first = world.row("parked")
    second = world.row("approved")
    third = world.row("parked")
    world.fail = {first: urllib.error.URLError("connection refused"), second: 401}
    assert world.main(monkeypatch) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert [p[0] for p in world.posted] == [first, third, second]  # parked rows, then approved ones
    assert f"{first}" in out.err and "URLError" in out.err
    assert f"{second}" in out.err and "401" in out.err
    assert third not in out.err
    assert "s3cr3t" not in out.err


def test_sweep_missing_ledger_is_an_error_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "nope" / "ledger.db"
    assert sweep.main(["--ledger", str(missing)]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "ledger" in out.err
    assert not missing.exists()


def test_sweep_unreadable_secret_still_reports_unknown_outcomes(world, monkeypatch, capsys):
    stale = world.row("executing", executing_minutes_ago=12)
    world.row("parked")
    world.secret_file.unlink()
    assert world.main(monkeypatch) == 1
    out = capsys.readouterr()
    assert f"durable-park: {stale} outcome UNKNOWN" in out.out
    assert "wake secret" in out.err and world.posted == []


def test_sweep_blank_secret_is_an_error(world, monkeypatch, capsys):
    world.row("parked")
    world.secret_file.write_text("  \n")
    assert world.main(monkeypatch) == 1
    assert world.posted == [] and "wake secret" in capsys.readouterr().err


def test_retention_days_flag(world, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        sweep.parse_args(["--ledger", "/x/ledger.db", "--retention-days", "0"])
    assert exc.value.code == 2
    assert sweep.parse_args(["--ledger", "/x/ledger.db"]).retention_days == 30
    old = world.row("allowed", request_id="r")
    world.age(old, 31)
    fresh = world.row("allowed", request_id="r")
    assert world.main(monkeypatch) == 0
    assert world.ledger.get(old) is None
    assert world.ledger.get(fresh) is not None
    assert capsys.readouterr().out == ""


def test_retention_error_does_not_stop_the_sweep(world, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(sweep, "_apply_retention", boom)
    assert world.main(monkeypatch) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "retention failed (RuntimeError)" in captured.err


def test_orphan_cancel_error_does_not_stop_the_sweep(world, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(sweep, "_cancel_orphan_parks", boom)
    parked = world.row("parked", request_id="req-1")
    assert world.main(monkeypatch) == 0
    assert parked in [p for p, _, _ in world.posted]  # the wakes still went out
    assert "orphan cancel failed (RuntimeError)" in capsys.readouterr().err


def test_sweep_defaults_match_the_deployment():
    args = sweep.parse_args(["--ledger", "/x/ledger.db"])
    assert args.wake_secret_file == "/etc/durable-park/wake_secret"
    assert args.wake_url == DEFAULT_URL
    with pytest.raises(SystemExit):
        sweep.parse_args([])  # --ledger is required


def test_sweep_runs_without_the_sdk_from_any_cwd_through_a_symlink(world, tmp_path):
    stale = world.row("executing", executing_minutes_ago=20)
    link = tmp_path / "linked_sweep.py"
    link.symlink_to(SCRIPT)
    # The subprocess runs the real sweep, so the autouse ``_no_gateway_pid`` patch does not reach it: a fake
    # ``systemctl`` first on its PATH stands in for a failing one (MainPID unknown -> no load-proof alert). Without
    # it, a VM with a live gateway would add a "no durable-park load proof" alert for the test's temp directory.
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\nexit 1\n")
    fake_systemctl.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
    # Hermes's cron runs the script with its own interpreter: the SDK and httpx must not be needed
    code = ("import runpy, sys; sys.modules['agentapprovalprotocol'] = None; sys.modules['httpx'] = None; "
            "sys.argv = [sys.argv[1], *sys.argv[2:]]; runpy.run_path(sys.argv[0], run_name='__main__')")
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(link), "--ledger", world.ledger.path,
         "--wake-secret-file", str(world.secret_file)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"durable-park: {stale} outcome UNKNOWN — check {TOOL} in the sandbox before retrying\n"
    assert done.stderr == ""
    assert Ledger(world.ledger.path).get(stale).status == "unknown"


# ---- form-check self-check alert -------------------------------------------------------------

import json  # noqa: E402

ALERT = "durable-park ALERT: the form checker is not working"
RECOVERED = "durable-park: the form checker is working again."


def write_report(tmp_path, *, ok=True, problems=(), minutes_ago=0):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "formcheck_status.json").write_text(json.dumps(
        {"ok": ok, "tools_checked": 1, "problems": list(problems), "checked_at": at}))


def runs(world, monkeypatch, capsys, n):
    outs = []
    for _ in range(n):
        assert world.main(monkeypatch) == 0
        outs.append(capsys.readouterr().out)
    return outs


def test_formcheck_healthy_is_silent(world, monkeypatch, capsys, tmp_path):
    assert runs(world, monkeypatch, capsys, 3) == ["", "", ""]


def test_formcheck_failure_alerts_once_after_two_sweeps(world, monkeypatch, capsys, tmp_path):
    write_report(tmp_path, ok=False, problems=["mcp__wh__quickbooks__create_bill: no rules found for this tool"])
    first, second, third = runs(world, monkeypatch, capsys, 3)
    assert first == ""  # one bad sweep may be a restart in progress
    assert second.startswith(ALERT) and "create_bill: no rules found" in second
    assert third == ""  # not repeated every 5 minutes


def test_formcheck_missing_report_alerts(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "formcheck_status.json").unlink()
    _, second = runs(world, monkeypatch, capsys, 2)
    assert second.startswith(ALERT) and "no form-check report" in second


def test_formcheck_stale_report_alerts(world, monkeypatch, capsys, tmp_path):
    write_report(tmp_path, minutes_ago=120)
    _, second = runs(world, monkeypatch, capsys, 2)
    assert second.startswith(ALERT) and "120 minutes ago" in second


def test_formcheck_unreadable_report_alerts(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "formcheck_status.json").write_text("{not json")
    _, second = runs(world, monkeypatch, capsys, 2)
    assert second.startswith(ALERT) and "unreadable" in second


def test_formcheck_recovery_is_announced_once(world, monkeypatch, capsys, tmp_path):
    write_report(tmp_path, ok=False, problems=["x"])
    runs(world, monkeypatch, capsys, 2)
    write_report(tmp_path)
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == RECOVERED and second == ""


def test_formcheck_blip_then_healthy_never_alerts(world, monkeypatch, capsys, tmp_path):
    write_report(tmp_path, ok=False, problems=["x"])
    first = runs(world, monkeypatch, capsys, 1)
    write_report(tmp_path)
    assert first + runs(world, monkeypatch, capsys, 2) == ["", "", ""]


def test_formcheck_problem_text_is_bounded(world, monkeypatch, capsys, tmp_path):
    write_report(tmp_path, ok=False, problems=["p" * 5000, "\x1b[31mred"])
    _, second = runs(world, monkeypatch, capsys, 2)
    assert len(second) < 700 and "\x1b" not in second


def test_formcheck_broken_state_file_does_not_stop_the_sweep(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "formcheck_alert_state.json").write_text("garbage")
    world.row("approved")
    assert world.main(monkeypatch) == 0
    assert len(world.posted) == 1


OPEN_ALERT = ("durable-park ALERT: withHuman could not be reached (since {at}); every tool call is being "
              "refused until it answers again.")
CLOSE_NOTE = "durable-park: withHuman answers again (since {at}); tool calls are decided normally."


def write_breaker(tmp_path, *, open_, opened_at="2026-10-21T10:00:00Z", closed_at=None):
    (tmp_path / "breaker_status.json").write_text(json.dumps(
        {"open": open_, "opened_at": opened_at, "closed_at": closed_at}))


def test_breaker_alert_once_on_open_once_on_close(world, monkeypatch, capsys, tmp_path):
    write_breaker(tmp_path, open_=True)
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == OPEN_ALERT.format(at="2026-10-21T10:00:00Z") and second == ""
    write_breaker(tmp_path, open_=False, closed_at="2026-10-21T10:07:00Z")
    third, fourth = runs(world, monkeypatch, capsys, 2)
    assert third.strip() == CLOSE_NOTE.format(at="2026-10-21T10:07:00Z") and fourth == ""


def test_breaker_opened_and_closed_between_sweeps_prints_both(world, monkeypatch, capsys, tmp_path):
    write_breaker(tmp_path, open_=False, closed_at="2026-10-21T10:03:00Z")
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.splitlines() == [OPEN_ALERT.format(at="2026-10-21T10:00:00Z"),
                                  CLOSE_NOTE.format(at="2026-10-21T10:03:00Z")]
    assert second == ""


def test_breaker_reopened_later_alerts_again(world, monkeypatch, capsys, tmp_path):
    write_breaker(tmp_path, open_=True)
    runs(world, monkeypatch, capsys, 1)
    write_breaker(tmp_path, open_=False, closed_at="2026-10-21T10:07:00Z")
    runs(world, monkeypatch, capsys, 1)
    write_breaker(tmp_path, open_=True, opened_at="2026-10-21T11:00:00Z", closed_at="2026-10-21T10:07:00Z")
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.strip() == OPEN_ALERT.format(at="2026-10-21T11:00:00Z")


def test_no_breaker_file_prints_nothing(world, monkeypatch, capsys, tmp_path):
    assert runs(world, monkeypatch, capsys, 2) == ["", ""]


def test_breaker_broken_status_or_state_does_not_stop_the_sweep(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "breaker_status.json").write_text("{not json")
    (tmp_path / "breaker_alert_state.json").write_text("[1")
    assert runs(world, monkeypatch, capsys, 1) == [""]


POLICY_ALERT = ("durable-park ALERT: the tool policy check failed at gateway start \u2014 {problems}. "
                "Every tool call is refused until it passes.")
POLICY_OK = "durable-park: the tool policy check passes again."


def test_policy_alert_once_and_recovery(world, monkeypatch, capsys, tmp_path):
    assert runs(world, monkeypatch, capsys, 1) == [""]  # healthy: silent
    write_policy(tmp_path, ok=False, problems=["terminal is open", "wh missing"])
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == POLICY_ALERT.format(problems="terminal is open; wh missing") and second == ""
    write_policy(tmp_path, ok=True)
    third, fourth = runs(world, monkeypatch, capsys, 2)
    assert third.strip() == POLICY_OK and fourth == ""


def test_policy_failed_report_gives_one_alert_and_one_recovery(world, monkeypatch, capsys, tmp_path):
    # the plugin refuses at load because the report failed, so its load verdict repeats the same failure
    write_policy(tmp_path, ok=False, problems=["terminal is open"])
    write_load(tmp_path, ok=False, problem="the tool policy check failed")
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == POLICY_ALERT.format(problems="terminal is open") and second == ""
    write_policy(tmp_path, ok=True)
    write_load(tmp_path, ok=True)
    third, fourth = runs(world, monkeypatch, capsys, 2)
    assert third.strip() == POLICY_OK and fourth == ""


def test_policy_alert_when_the_report_is_missing_or_unreadable(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "tool_policy_status.json").unlink()
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == POLICY_ALERT.format(problems="no tool policy report") and second == ""
    write_policy(tmp_path, ok=True)
    runs(world, monkeypatch, capsys, 1)
    (tmp_path / "tool_policy_status.json").write_text("{not json")
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.strip() == POLICY_ALERT.format(problems="the tool policy report is unreadable")


def test_policy_old_passing_report_is_not_an_alert(world, monkeypatch, capsys, tmp_path):
    # written only at gateway start: an old pass on a long-running gateway is normal
    assert runs(world, monkeypatch, capsys, 2) == ["", ""]


def test_policy_broken_state_file_does_not_stop_the_sweep(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "tool_policy_alert_state.json").write_text("[1")
    world.row("approved")
    assert world.main(monkeypatch) == 0
    assert len(world.posted) == 1


LOAD_ALERT = ("durable-park ALERT: the tool policy check did not pass when the plugin loaded \u2014 {problem}. "
              "Every tool call is refused until an admin fixes it and restarts the gateway.")
LOAD_OK = "durable-park: the tool policy check passed when the plugin loaded."


def write_load(tmp_path, *, ok, problem=None):
    (tmp_path / "tool_policy_load.json").write_text(json.dumps(
        {"ok": ok, "problem": problem, "checked_at": "2026-10-21T10:00:00Z"}))


def test_load_verdict_alert_once_and_recovery(world, monkeypatch, capsys, tmp_path):
    assert runs(world, monkeypatch, capsys, 1) == [""]  # no verdict file (old plugin / not loaded yet): silent
    write_load(tmp_path, ok=True)
    assert runs(world, monkeypatch, capsys, 1) == [""]
    write_load(tmp_path, ok=False, problem="stale")  # the report itself still says ok: the blind spot
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == LOAD_ALERT.format(problem="stale") and second == ""
    write_load(tmp_path, ok=True)
    third, fourth = runs(world, monkeypatch, capsys, 2)
    assert third.strip() == LOAD_OK and fourth == ""


def test_load_verdict_unreadable_is_silent_and_keeps_the_sweep_going(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "tool_policy_load.json").write_text("{not json")
    world.row("approved")
    assert world.main(monkeypatch) == 0
    assert capsys.readouterr().out == "" and len(world.posted) == 1


LOADPROOF_ALERT = ("durable-park ALERT: the running gateway (pid {pid}) has no durable-park load proof; every tool "
                   "call may be running unchecked. Restart the gateway with no active rows and check the journal.")
LOADPROOF_OK = "durable-park: the durable-park plugin is loaded in the running gateway."


def write_proof(tmp_path, pid, mode="asking"):
    (tmp_path / "plugin_load.json").write_text(json.dumps(
        {"pid": pid, "written_at": "2026-10-21T10:00:00Z", "mode": mode, "hooks": ["pre_tool_call"]}))


def test_load_proof_missing_alerts_once_then_recovers(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.strip() == LOADPROOF_ALERT.format(pid=4242) and second == ""
    write_proof(tmp_path, 4242)
    third, fourth = runs(world, monkeypatch, capsys, 2)
    assert third.strip() == LOADPROOF_OK and fourth == ""


def test_load_proof_matching_pid_is_silent(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    write_proof(tmp_path, 4242)
    assert runs(world, monkeypatch, capsys, 3) == ["", "", ""]
    write_proof(tmp_path, 4242, mode="refusing-policy")  # live and refusing, as designed
    assert runs(world, monkeypatch, capsys, 1) == [""]


@pytest.mark.parametrize("proof", ["other-pid", "unreadable", "not-a-mapping", "pid-not-int"])
def test_load_proof_that_does_not_match_the_running_gateway_alerts(world, monkeypatch, capsys, tmp_path, proof):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    path = tmp_path / "plugin_load.json"
    if proof == "other-pid":
        write_proof(tmp_path, 1)
    elif proof == "unreadable":
        path.write_text("{not json")
    elif proof == "not-a-mapping":
        path.write_text("[4242]")
    else:
        path.write_text(json.dumps({"pid": "4242", "mode": "asking"}))
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.strip() == LOADPROOF_ALERT.format(pid=4242)


def test_load_proof_alert_is_silent_when_the_gateway_pid_is_unknown(world, monkeypatch, capsys, tmp_path):
    # _gateway_mainpid() is None (systemctl failed, timed out, or MainPID 0): never alert, never fail
    assert runs(world, monkeypatch, capsys, 2) == ["", ""]
    assert not (tmp_path / "plugin_load_alert_state.json").exists()


@pytest.mark.parametrize("outcome,expected", [
    (subprocess.CompletedProcess([], 0, stdout="4242\n"), 4242),
    (subprocess.CompletedProcess([], 0, stdout="0\n"), None),
    (subprocess.CompletedProcess([], 0, stdout=""), None),
    (subprocess.CompletedProcess([], 0, stdout="junk"), None),
    (subprocess.CompletedProcess([], 1, stdout="4242"), None),
    (FileNotFoundError("systemctl"), None),
    (subprocess.TimeoutExpired("systemctl", 5), None),
    (OSError("boom"), None),
])
def test_gateway_mainpid_reads_systemctl_and_fails_to_none(monkeypatch, outcome, expected):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert REAL_MAINPID() == expected
    assert seen["cmd"] == ["systemctl", "--user", "show", "hermes-gateway", "-p", "MainPID", "--value"]
    assert seen["kw"]["timeout"] == 5


def test_a_failing_load_proof_alert_does_not_stop_the_sweep(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    (tmp_path / "plugin_load_alert_state.json").mkdir()  # the state file cannot be written
    world.row("approved")
    assert world.main(monkeypatch) == 0
    assert len(world.posted) == 1


def _run_mainpid(monkeypatch, env):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="4242\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    for key in ("XDG_RUNTIME_DIR",):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert REAL_MAINPID() == 4242
    return seen


def test_gateway_mainpid_sets_xdg_runtime_dir_when_unset(monkeypatch):
    import os
    seen = _run_mainpid(monkeypatch, {})
    assert seen["env"]["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"


def test_gateway_mainpid_keeps_an_existing_xdg_runtime_dir(monkeypatch):
    seen = _run_mainpid(monkeypatch, {"XDG_RUNTIME_DIR": "/run/user/77"})
    assert seen.get("env") is None  # inherit as is


# ---- lift problems (lift_status.json) -----------------------------------------------------------------

LIFT_TOOL = "mcp__wh__quickbooks__get_customer"
LIFT_TEXTS = {
    "declared": ("durable-park ALERT: {tool} has its own withhuman_context field inside {key}; the agent's note is "
                 "left there and goes to the server, not to the approver."),
    "schema_unavailable": ("durable-park ALERT: could not read {tool}'s tool rules, so the agent's note was not moved; "
                           "it may reach the server."),
    "args_unreadable": ("durable-park ALERT: could not read {tool}'s arguments, so the agent's note was not moved; "
                        "it may reach the server."),
    "lift_error": "durable-park ALERT: moving the agent's note failed for {tool}; it may reach the server.",
    "note_left_inside": ("durable-park ALERT: the agent's note is still inside {key} for {tool} after the move; it may "
                         "reach the server."),
}


def write_lift(tmp_path, *entries):
    rows = [{"tool": t, "kind": k, "key": key, "first_seen": "2026-10-22T10:00:00Z",
             "last_seen": "2026-10-22T10:00:00Z", "count": 1} for t, k, key in entries]
    (tmp_path / "lift_status.json").write_text(json.dumps({"entries": rows, "updated_at": "2026-10-22T10:00:00Z"}))


def test_lift_status_missing_is_silent(world, monkeypatch, capsys, tmp_path):
    assert runs(world, monkeypatch, capsys, 2) == ["", ""]


@pytest.mark.parametrize("kind", sorted(LIFT_TEXTS))
def test_each_lift_problem_has_its_exact_alert_text_and_alerts_once(world, monkeypatch, capsys, tmp_path, kind):
    write_lift(tmp_path, (LIFT_TOOL, kind, "params"))
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.splitlines() == [LIFT_TEXTS[kind].format(tool=LIFT_TOOL, key="params")]
    assert second == ""


def test_a_new_problem_alerts_once_and_the_old_ones_stay_quiet(world, monkeypatch, capsys, tmp_path):
    write_lift(tmp_path, (LIFT_TOOL, "declared", "params"))
    runs(world, monkeypatch, capsys, 1)
    write_lift(tmp_path, (LIFT_TOOL, "declared", "params"), (LIFT_TOOL, "lift_error", ""),
               ("mcp__wh__quickbooks__get_bill", "declared", "params"))
    out, again = runs(world, monkeypatch, capsys, 2)
    assert out.splitlines() == [LIFT_TEXTS["lift_error"].format(tool=LIFT_TOOL, key=""),
                                LIFT_TEXTS["declared"].format(tool="mcp__wh__quickbooks__get_bill", key="params")]
    assert again == ""


def test_a_new_kind_for_the_same_tool_alerts(world, monkeypatch, capsys, tmp_path):
    write_lift(tmp_path, (LIFT_TOOL, "declared", "params"))
    runs(world, monkeypatch, capsys, 1)
    write_lift(tmp_path, (LIFT_TOOL, "declared", "params"), (LIFT_TOOL, "schema_unavailable", ""))
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.splitlines() == [LIFT_TEXTS["schema_unavailable"].format(tool=LIFT_TOOL, key="")]


def test_a_problem_that_goes_away_and_comes_back_alerts_again(world, monkeypatch, capsys, tmp_path):
    write_lift(tmp_path, (LIFT_TOOL, "lift_error", ""))
    runs(world, monkeypatch, capsys, 1)
    write_lift(tmp_path)  # the gateway restarted and wrote an empty list
    assert runs(world, monkeypatch, capsys, 1) == [""]
    write_lift(tmp_path, (LIFT_TOOL, "lift_error", ""))
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.splitlines() == [LIFT_TEXTS["lift_error"].format(tool=LIFT_TOOL, key="")]


def test_a_corrupt_lift_status_alerts_once_then_recovers_quietly(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "lift_status.json").write_text("{not json")
    first, second = runs(world, monkeypatch, capsys, 2)
    assert first.startswith("durable-park ALERT: could not read lift_status.json (") and first.rstrip().endswith(").")
    assert len(first.splitlines()) == 1 and second == ""
    write_lift(tmp_path, (LIFT_TOOL, "lift_error", ""))
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.splitlines() == [LIFT_TEXTS["lift_error"].format(tool=LIFT_TOOL, key="")]
    (tmp_path / "lift_status.json").write_text("[1, 2]")  # readable JSON of the wrong shape: a problem again
    again, quiet = runs(world, monkeypatch, capsys, 2)
    assert again.startswith("durable-park ALERT: could not read lift_status.json (") and quiet == ""


def test_lift_alert_state_and_garbage_entries_do_not_stop_the_sweep(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "lift_alert_state.json").write_text("[1")
    (tmp_path / "lift_status.json").write_text(json.dumps({"entries": [5, {"tool": 1}, {"tool": LIFT_TOOL,
                                                                                         "kind": "lift_error"}]}))
    out, = runs(world, monkeypatch, capsys, 1)
    assert out.count("durable-park ALERT:") >= 1
