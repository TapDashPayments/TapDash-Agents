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
DEFAULT_URL = "http://127.0.0.1:8644/webhooks/durable-park-wake"


def stamp(minutes_ago):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class World:
    def __init__(self, tmp_path):
        self.ledger = Ledger(str(tmp_path / "ledger.db"))
        # a healthy gateway form-check report (tests of that alert replace it)
        (tmp_path / "formcheck_status.json").write_text(
            '{"ok": true, "tools_checked": 1, "problems": [], "checked_at": "%s"}'
            % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
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
                "cancelled": ["cancelled"], "executing": ["approved", "executing"],
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
    # Hermes's cron runs the script with its own interpreter: the SDK and httpx must not be needed
    code = ("import runpy, sys; sys.modules['agentapprovalprotocol'] = None; sys.modules['httpx'] = None; "
            "sys.argv = [sys.argv[1], *sys.argv[2:]]; runpy.run_path(sys.argv[0], run_name='__main__')")
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(link), "--ledger", world.ledger.path,
         "--wake-secret-file", str(world.secret_file)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
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
