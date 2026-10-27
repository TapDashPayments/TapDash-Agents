"""Task 4: the sweep for per-person profiles (status per profile, wake per profile, key checks)."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

import durable_park_sweep as sweep
from test_sweep import (TOOL, World, _default_key, _hermes_pinned, _no_gateway_pid, stamp,  # noqa: F401
                        world)

SARAH_WAKE = "http://127.0.0.1:8644/p/sarah/webhooks/durable-park-wake-sarah"
DEFAULT_WAKE = "http://127.0.0.1:8644/webhooks/durable-park-wake"


def now_stamp(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pdir(tmp_path, name):
    path = tmp_path / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def healthy(tmp_path, name):
    """A profile directory with a passing form-check report and tool policy report."""
    path = pdir(tmp_path, name)
    (path / "formcheck_status.json").write_text(json.dumps(
        {"ok": True, "tools_checked": 1, "problems": [], "checked_at": now_stamp()}))
    (path / "tool_policy_status.json").write_text(json.dumps(
        {"ok": True, "checked_at": now_stamp(), "problems": [], "allowed": []}))
    return path


def sweeps(world, monkeypatch, capsys, n, *extra):
    outs = []
    for _ in range(n):
        assert world.main(monkeypatch, *extra) == 0
        outs.append(capsys.readouterr())
    return outs


def lines(captured):
    return captured.out.splitlines()


# ---- 1. status files per profile --------------------------------------------------------------------------

def test_sweep_alerts_per_profile_status(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "formcheck_status.json").write_text(json.dumps(
        {"ok": False, "tools_checked": 1, "problems": ["mcp__wh__x: no rules"], "checked_at": now_stamp()}))
    first, second, third = sweeps(world, monkeypatch, capsys, 3)
    assert first.out == ""
    assert lines(second) == [
        "durable-park ALERT (profile sarah): the form checker is not working — mcp__wh__x: no rules. "
        "Risky actions will be refused until it is fixed."]
    assert third.out == ""
    assert (sarah / "formcheck_alert_state.json").exists()  # its own state, in its own directory
    assert json.loads((tmp_path / "formcheck_alert_state.json").read_text()) == {"bad_runs": 0, "alerted": False}


def test_profile_form_check_recovery_names_the_profile(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    good = (sarah / "formcheck_status.json").read_text()
    (sarah / "formcheck_status.json").write_text(json.dumps(
        {"ok": False, "problems": ["p"], "checked_at": now_stamp()}))
    sweeps(world, monkeypatch, capsys, 2)
    (sarah / "formcheck_status.json").write_text(good)
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == ["durable-park (profile sarah): the form checker is working again."]


def test_profile_breaker_status_is_read(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "2026-10-27T10:00:00Z"}))
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == ["durable-park ALERT (profile sarah): withHuman could not be reached (since "
                          "2026-10-27T10:00:00Z); every tool call is being refused until it answers again."]


def test_profile_tool_policy_status_is_read(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "tool_policy_status.json").write_text(json.dumps(
        {"ok": False, "checked_at": now_stamp(), "problems": ["bad tool"], "allowed": []}))
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == ["durable-park ALERT (profile sarah): the tool policy check failed at gateway start "
                          "— bad tool. Every tool call is refused until it passes."]


def test_profile_tool_policy_load_verdict_is_read(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "tool_policy_load.json").write_text(json.dumps({"ok": False, "problem": "stale report"}))
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == ["durable-park ALERT (profile sarah): the tool policy check did not pass when the plugin "
                          "loaded — stale report. Every tool call is refused until an admin fixes it and "
                          "restarts the gateway."]


def test_profile_lift_status_is_read(world, monkeypatch, capsys, tmp_path):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "lift_status.json").write_text(json.dumps(
        {"entries": [{"tool": "mcp__wh__t", "kind": "declared", "key": "args.note", "count": 1}]}))
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == ["durable-park ALERT (profile sarah): mcp__wh__t has its own withhuman_context field "
                          "inside args.note; the agent's note is left there and goes to the server, not to the "
                          "approver."]
    (again,) = sweeps(world, monkeypatch, capsys, 1)
    assert again.out == ""
    assert (sarah / "lift_alert_state.json").exists()


def test_profile_lift_still_happening_note_keeps_the_count(world, monkeypatch, capsys, tmp_path):
    assert sweep._label_line("durable-park ALERT (still happening, 3 times): x", "sarah") == \
        "durable-park ALERT (profile sarah, still happening, 3 times): x"
    assert sweep._label_line("durable-park: ok", "sarah") == "durable-park (profile sarah): ok"
    assert sweep._label_line("odd line", "sarah") == "(profile sarah) odd line"
    assert sweep._label_line("", "sarah") == ""


def test_profile_load_proof_is_read(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    (tmp_path / "plugin_load.json").write_text(json.dumps({"pid": 4242, "mode": "asking"}))
    sarah = healthy(tmp_path, "sarah")
    tara = healthy(tmp_path, "tara")
    (sarah / "plugin_load.json").write_text(json.dumps({"pid": 4242, "mode": "asking"}))
    (tara / "plugin_load.json").write_text(json.dumps({"pid": 1, "mode": "asking"}))  # an earlier gateway's proof
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [
        "durable-park ALERT (profile tara): the running gateway (pid 4242) has no durable-park load proof; every "
        "tool call may be running unchecked. Restart the gateway with no active rows and check the journal."]
    (again,) = sweeps(world, monkeypatch, capsys, 1)
    assert again.out == ""
    (tara / "plugin_load.json").write_text(json.dumps({"pid": 4242, "mode": "asking"}))
    (fixed,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(fixed) == ["durable-park (profile tara): the durable-park plugin is loaded in the running gateway."]


def test_profile_dir_with_no_load_proof_at_all_alerts_like_the_root(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    (tmp_path / "plugin_load.json").write_text(json.dumps({"pid": 4242, "mode": "asking"}))
    healthy(tmp_path, "sarah")  # no plugin_load.json in it
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert len(lines(out)) == 1 and lines(out)[0].startswith("durable-park ALERT (profile sarah): the running gateway")


def test_root_load_proof_alert_is_unchanged_with_profiles_present(world, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sweep, "_gateway_mainpid", lambda: 4242)
    (pdir(tmp_path, "sarah") / "plugin_load.json").write_text(json.dumps({"pid": 4242, "mode": "asking"}))
    healthy(tmp_path, "sarah")
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [
        "durable-park ALERT: the running gateway (pid 4242) has no durable-park load proof; every tool call may be "
        "running unchecked. Restart the gateway with no active rows and check the journal."]


def test_profile_dirs_are_sorted_and_only_real_profile_directories(world, monkeypatch, capsys, tmp_path):
    for name in ("tara", "sarah"):
        path = healthy(tmp_path, name)
        (path / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "t"}))
    bad = pdir(tmp_path, "Bad.Name")  # not a name a profile may have
    (bad / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "t"}))
    (tmp_path / "profiles" / "afile").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "t"}))
    (tmp_path / "profiles" / "linked").symlink_to(outside, target_is_directory=True)
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert [ln.split(":")[0] for ln in lines(out)] == ["durable-park ALERT (profile sarah)",
                                                       "durable-park ALERT (profile tara)"]
    assert not (outside / "breaker_alert_state.json").exists()


def test_a_failing_step_for_one_profile_stops_nothing_else(world, monkeypatch, capsys, tmp_path):
    for name in ("sarah", "tara"):
        path = healthy(tmp_path, name)
        (path / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "t"}))
    real = sweep._formcheck_alert

    def boom(ledger_path, now):
        if "sarah" in ledger_path:
            raise RuntimeError("boom")
        return real(ledger_path, now)

    monkeypatch.setattr(sweep, "_formcheck_alert", boom)
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert "form-check alert failed for profile sarah (RuntimeError)" in out.err
    assert [ln.split(":")[0] for ln in lines(out)] == ["durable-park ALERT (profile sarah)",
                                                       "durable-park ALERT (profile tara)"]  # breakers still ran


def test_status_root_option_moves_every_status_read(world, monkeypatch, capsys, tmp_path):
    other = tmp_path / "other"
    sarah = other / "profiles" / "sarah"
    sarah.mkdir(parents=True)
    for name in ("formcheck_status.json", "tool_policy_status.json"):  # healthy reports in the new root and profile
        (other / name).write_text((tmp_path / name).read_text())
        (sarah / name).write_text((tmp_path / name).read_text())
    (sarah / "breaker_status.json").write_text(json.dumps({"open": True, "opened_at": "t"}))
    # the ledger's own directory now holds a failing report that must NOT be read any more
    (tmp_path / "formcheck_status.json").write_text(json.dumps({"ok": False, "problems": ["p"], "checked_at": now_stamp()}))
    first, second = sweeps(world, monkeypatch, capsys, 2, "--status-root", str(other))
    assert [ln.split(":")[0] for ln in lines(first)] == ["durable-park ALERT (profile sarah)"]
    assert second.out == ""
    assert (other / "formcheck_alert_state.json").exists()  # the root's state followed the option
    assert not (tmp_path / "formcheck_alert_state.json").exists()


# ---- 2. wake per row profile ------------------------------------------------------------------------------

def test_wake_goes_to_each_rows_own_profile_route(world, monkeypatch, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    default = world.row("parked")
    sarah = world.row("parked", profile="sarah")
    named_default = world.row("approved", profile="default")
    assert world.main(monkeypatch) == 0
    assert world.posted == [(default, "s3cr3t", DEFAULT_WAKE), (sarah, "s3cr3t", SARAH_WAKE),
                            (named_default, "s3cr3t", DEFAULT_WAKE)]


def test_wake_base_builds_the_profile_address_and_wake_url_stays_for_default(world, monkeypatch, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    default = world.row("parked")
    sarah = world.row("parked", profile="sarah")
    assert world.main(monkeypatch, "--wake-base", "http://127.0.0.1:9999/", "--wake-url",
                      "http://127.0.0.1:9998/x") == 0
    assert world.posted == [
        (default, "s3cr3t", "http://127.0.0.1:9998/x"),
        (sarah, "s3cr3t", "http://127.0.0.1:9999/p/sarah/webhooks/durable-park-wake-sarah")]


def test_wake_for_an_invalid_stored_profile_is_skipped_with_an_alert_naming_the_park(world, monkeypatch, capsys):
    bad = world.row("denied", profile="../etc/x")  # decided, not told: a wake target (no key check for it)
    good = world.row("denied")
    assert world.main(monkeypatch) == 0
    assert [p[0] for p in world.posted] == [good]
    out = capsys.readouterr()
    assert lines(out) == [f"durable-park ALERT: cannot wake {bad}: its stored profile name is not valid, so no "
                          "wake address can be built and the chat is not told. Check that row's profile in the "
                          "ledger."]
    assert "../etc/x" not in out.out and "../etc/x" not in out.err


# ---- 3. a failed wake post is visible in Teams --------------------------------------------------------------

def failing_post(world, codes):
    """A post_wake whose answer depends on the address: ``codes`` maps a substring of the url to a status or error."""
    def post(park_id, secret, url=DEFAULT_WAKE):
        world.posted.append((park_id, secret, url))
        for needle, outcome in codes.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return 202
    return post


def run(world, monkeypatch, post, *extra):
    monkeypatch.setattr(sweep, "post_wake", post)
    return sweep.main(["--ledger", world.ledger.path, "--wake-secret-file", str(world.secret_file), *extra])


def test_sweep_alerts_when_a_profile_wake_post_fails(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    park_id = world.row("parked", profile="sarah")
    post = failing_post(world, {"/p/sarah/": 404})
    outs = []
    for _ in range(3):
        assert run(world, monkeypatch, post) == 0
        outs.append(capsys.readouterr())
    assert "ALERT" not in outs[0].out
    assert lines(outs[1]) == [
        f"durable-park ALERT: waking {park_id} for profile sarah failed with HTTP 404 on 2 sweeps in a row; check "
        "that person's wake route (the route is missing from the Hermes config or the gateway did not load it): "
        "/p/sarah/webhooks/durable-park-wake-sarah."]
    assert outs[2].out == ""
    for captured in outs:  # the stderr line stays
        assert f"wake {park_id} for profile sarah returned HTTP 404" in captured.err
    assert (tmp_path / "wake_post_alert_state.json").exists()


def test_wake_post_exception_alerts_after_two_sweeps(world, monkeypatch, capsys):
    park_id = world.row("parked")
    post = failing_post(world, {"/webhooks/": ConnectionRefusedError("x")})
    assert run(world, monkeypatch, post) == 0
    first = capsys.readouterr()
    assert run(world, monkeypatch, post) == 0
    second = capsys.readouterr()
    assert first.out == "" and first.err == f"durable-park sweep: wake {park_id} failed (ConnectionRefusedError)\n"
    assert lines(second) == [
        f"durable-park ALERT: waking {park_id} for profile default failed (ConnectionRefusedError) on 2 sweeps in a "
        "row; check that the gateway is running and listening for the wake route /webhooks/durable-park-wake."]


def test_wake_post_success_resets_the_count(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    world.row("parked", profile="sarah")
    bad, good = failing_post(world, {"/p/sarah/": 500}), failing_post(world, {})
    for post in (bad, good, bad):  # fail, recover, fail: never two in a row
        assert run(world, monkeypatch, post) == 0
        assert "ALERT" not in capsys.readouterr().out
    assert json.loads((tmp_path / "wake_post_alert_state.json").read_text())["rows"]  # the last failure counts 1


def test_wake_post_entry_is_cleared_when_the_row_is_no_longer_a_target(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    park_id = world.row("parked", profile="sarah")
    assert run(world, monkeypatch, failing_post(world, {"/p/sarah/": 404})) == 0
    capsys.readouterr()
    assert park_id in json.loads((tmp_path / "wake_post_alert_state.json").read_text())["rows"]
    assert world.ledger.transition(park_id, "parked", "cancelled")
    world.ledger.mark_notified(park_id)
    assert run(world, monkeypatch, failing_post(world, {"/p/sarah/": 404})) == 0
    assert json.loads((tmp_path / "wake_post_alert_state.json").read_text()) == {}


def test_wake_post_alert_repeats_only_after_24_hours(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    park_id = world.row("parked", profile="sarah")
    post = failing_post(world, {"/p/sarah/": 404})
    for _ in range(2):
        assert run(world, monkeypatch, post) == 0
    capsys.readouterr()
    state_path = tmp_path / "wake_post_alert_state.json"
    state = json.loads(state_path.read_text())
    state["rows"][park_id]["alerted_at"] = (datetime.now(timezone.utc) - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state))
    assert run(world, monkeypatch, post) == 0
    assert "ALERT" not in capsys.readouterr().out
    state = json.loads(state_path.read_text())
    state["rows"][park_id]["alerted_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state))
    assert run(world, monkeypatch, post) == 0
    assert park_id in capsys.readouterr().out


def test_wake_post_failure_for_the_default_profile_keeps_its_stderr_text(world, monkeypatch, capsys):
    park_id = world.row("parked")
    assert run(world, monkeypatch, failing_post(world, {"/webhooks/": 503})) == 0
    assert capsys.readouterr().err == f"durable-park sweep: wake {park_id} returned HTTP 503\n"


# ---- 4. missing key file ----------------------------------------------------------------------------------

def key_line(profile, park_id):
    return (f"durable-park ALERT: no key file for profile {profile}; {park_id} cannot resume. Add the key or "
            "remove the person (people.sh).")


def test_sweep_alerts_missing_profile_key_once(world, monkeypatch, capsys, tmp_path):
    park_id = world.row("parked", profile="sarah")
    first, second = sweeps(world, monkeypatch, capsys, 2)
    assert lines(first) == [key_line("sarah", park_id)]
    assert second.out == ""
    state_path = tmp_path / "missing_key_alert_state.json"
    assert f"nokey:{park_id}" in json.loads(state_path.read_text())["said"]
    (tmp_path / "instances" / "sarah.token").write_text("k")  # the key appears: forgotten
    (third,) = sweeps(world, monkeypatch, capsys, 1)
    assert third.out == ""
    assert json.loads(state_path.read_text()).get("said", {}) == {}
    (tmp_path / "instances" / "sarah.token").unlink()  # and if it goes away again, it is said again
    (fourth,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(fourth) == [key_line("sarah", park_id)]


def test_missing_default_key_file_also_alerts(world, monkeypatch, capsys, tmp_path):
    park_id = world.row("approved")
    (tmp_path / "instance_token").unlink()
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [key_line("default", park_id)]


def test_key_check_ignores_other_statuses_and_forgets_rows_that_leave(world, monkeypatch, capsys, tmp_path):
    park_id = world.row("parked", profile="sarah")
    world.row("denied", profile="sarah", notified=True)
    world.row("executed", profile="sarah", notified=True)
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [key_line("sarah", park_id)]
    assert world.ledger.transition(park_id, "parked", "cancelled")
    (after,) = sweeps(world, monkeypatch, capsys, 1)
    assert after.out == ""
    assert json.loads((tmp_path / "missing_key_alert_state.json").read_text()).get("said", {}) == {}


def test_key_check_with_an_invalid_profile_name_names_the_park_not_the_name(world, monkeypatch, capsys):
    park_id = world.row("parked", profile="../../etc/passwd")
    first, second = sweeps(world, monkeypatch, capsys, 2)
    # the key alert first; the wake alert for the same row (no address can be built) follows
    assert lines(first)[0] == (f"durable-park ALERT: {park_id} has a profile name that is not valid, so its key file "
                               "cannot be found and it cannot resume. Fix the row's profile or remove the person "
                               "(people.sh).")
    assert len(lines(first)) == 2 and f"cannot wake {park_id}" in lines(first)[1]
    assert "passwd" not in first.out and "../" not in first.out and "passwd" not in first.err
    assert second.out == ""


def test_key_options_point_at_other_places(world, monkeypatch, capsys, tmp_path):
    other = tmp_path / "keys"
    other.mkdir()
    (other / "sarah.token").write_text("k")
    (other / "main-key").write_text("k")
    world.row("parked", profile="sarah")
    world.row("parked")
    (tmp_path / "instance_token").unlink()
    (out,) = sweeps(world, monkeypatch, capsys, 1, "--instances-dir", str(other),
                    "--default-token-file", str(other / "main-key"))
    assert out.out == ""


def unusable_line(profile, park_id, path):
    return (f"durable-park ALERT: the key file for profile {profile} is empty or unreadable; {park_id} cannot "
            f"resume. Check {path} (people.sh).")


@pytest.mark.parametrize("content", ["", "  \n", "\n\n\t\n"])
def test_an_empty_or_blank_key_file_alerts_once_and_clears_when_fixed(world, monkeypatch, capsys, tmp_path, content):
    key = tmp_path / "instances" / "sarah.token"
    key.write_text(content)
    park_id = world.row("parked", profile="sarah")
    first, second = sweeps(world, monkeypatch, capsys, 2)
    assert lines(first) == [unusable_line("sarah", park_id, key)]
    assert second.out == ""
    state_path = tmp_path / "missing_key_alert_state.json"
    said = json.loads(state_path.read_text())["said"]
    assert f"unusable:{park_id}" in said and f"nokey:{park_id}" not in said
    key.write_text("k\n")  # fixed: forgotten, and said again if it breaks again
    (third,) = sweeps(world, monkeypatch, capsys, 1)
    assert third.out == "" and json.loads(state_path.read_text()).get("said", {}) == {}
    key.write_text("")
    (fourth,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(fourth) == [unusable_line("sarah", park_id, key)]


def test_an_empty_default_key_file_alerts_with_the_default_token_file(world, monkeypatch, capsys, tmp_path):
    park_id = world.row("approved")
    (tmp_path / "instance_token").write_text("")
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [unusable_line("default", park_id, tmp_path / "instance_token")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a chmod 000 file")
def test_an_unreadable_key_file_alerts_and_never_prints_its_content(world, monkeypatch, capsys, tmp_path):
    key = tmp_path / "instances" / "sarah.token"
    key.write_text("SECRET-KEY-VALUE\n")
    key.chmod(0)
    park_id = world.row("parked", profile="sarah")
    try:
        (out,) = sweeps(world, monkeypatch, capsys, 1)
    finally:
        key.chmod(0o600)
    assert lines(out) == [unusable_line("sarah", park_id, key)]
    assert "SECRET" not in out.out and "SECRET" not in out.err


def test_a_key_file_with_text_gives_no_key_alert_and_never_prints_its_content(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("SECRET-KEY-VALUE\n")
    world.row("parked", profile="sarah")
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert out.out == "" and "SECRET" not in out.err


def test_a_directory_in_place_of_the_key_file_says_no_key_file(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").mkdir()
    park_id = world.row("parked", profile="sarah")
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [key_line("sarah", park_id)]


def test_the_sweep_and_the_plugin_agree_on_what_a_usable_key_file_is(tmp_path):
    """The sweep cannot import the plugin, so it carries its own copy of ``wake.key_file_usable``'s rule."""
    from durable_park import wake
    files = {"empty": b"", "spaces": b"  ", "newline": b"\n", "blank lines": b"\n \n\t\n", "text": b"tok\n",
             "text no newline": b"tok", "text after blank line": b"\n\ntok\n", "not text": b"\xff\xfe\x00"}
    paths = []
    for name, data in files.items():
        path = tmp_path / name.replace(" ", "_")
        path.write_bytes(data)
        paths.append((str(path), name in ("text", "text no newline", "text after blank line")))
    directory = tmp_path / "a-directory"
    directory.mkdir()
    paths += [(str(directory), False), (str(tmp_path / "missing"), False)]
    fifo = tmp_path / "a-fifo"
    os.mkfifo(fifo)  # opening it for reading would block forever: both must say "unusable" without opening it
    paths.append((str(fifo), False))
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.write_text("tok\n")
        locked.chmod(0)
        paths.append((str(locked), False))
    try:
        for path, usable in paths:
            for rule in (wake.key_file_usable, sweep._key_file_usable):
                finished = []
                worker = threading.Thread(target=lambda: finished.append(rule(path)), daemon=True)
                worker.start()
                worker.join(5)  # a rule that opens a FIFO hangs here: fail instead of hanging the suite
                assert finished == [usable], (rule.__module__, path)
    finally:
        if os.geteuid() != 0:
            locked.chmod(0o600)


# ---- 5. repeated CredentialRefused -------------------------------------------------------------------------

def refusals(directory, entries):
    (directory / "wake_refusals.json").write_text(json.dumps(entries))


def refused_line(profile, park_id, token):
    return (f"durable-park ALERT: withHuman refuses the key for profile {profile}, so {park_id} cannot resume and "
            f"blocks that chat and restarts. Check that {token} is that person's instance key (people.sh), then "
            "the sweep retries.")


def entry(profile, count):
    return {"profile": profile, "count": count, "first_at": "2026-10-27T10:00:00.000000Z"}


def test_sweep_alerts_repeated_credential_refused_once(world, monkeypatch, capsys, tmp_path):
    key = tmp_path / "instances" / "sarah.token"
    key.write_text("k")
    park_id = world.row("parked", profile="sarah")
    sarah = healthy(tmp_path, "sarah")
    refusals(sarah, {park_id: entry("sarah", 2)})
    (quiet,) = sweeps(world, monkeypatch, capsys, 1)
    assert quiet.out == ""
    refusals(sarah, {park_id: entry("sarah", 3)})
    first, second = sweeps(world, monkeypatch, capsys, 2)
    assert lines(first) == [refused_line("sarah", park_id, key)]
    assert second.out == ""
    assert f"refused:{park_id}" in json.loads((tmp_path / "missing_key_alert_state.json").read_text())["said"]
    refusals(sarah, {})  # the plugin deleted the entry (the row reads fine again): forgotten
    (third,) = sweeps(world, monkeypatch, capsys, 1)
    assert third.out == ""
    assert json.loads((tmp_path / "missing_key_alert_state.json").read_text()).get("said", {}) == {}


def test_refused_key_in_the_root_status_dir_is_checked_for_the_default_profile(world, monkeypatch, capsys, tmp_path):
    park_id = world.row("parked")
    refusals(tmp_path, {park_id: entry("default", 5)})
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert lines(out) == [refused_line("default", park_id, tmp_path / "instance_token")]


def test_refusals_for_rows_not_parked_or_unknown_or_malformed_entries_are_ignored(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    approved = world.row("approved", profile="sarah")
    sarah = healthy(tmp_path, "sarah")
    refusals(sarah, {approved: entry("sarah", 9), "P-NOPE0000": entry("sarah", 9), "bad": 7,
                     "b": {"count": True}, "c": {"count": "9"}})
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert out.out == ""


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", ""])
def test_unreadable_refusals_file_is_one_alert_never_a_crash(world, monkeypatch, capsys, tmp_path, raw):
    sarah = healthy(tmp_path, "sarah")
    (sarah / "wake_refusals.json").write_text(raw)
    first, second = sweeps(world, monkeypatch, capsys, 2)
    assert len(lines(first)) == 1
    assert lines(first)[0].startswith("durable-park ALERT: wake_refusals.json for profile sarah is unreadable (")
    assert second.out == ""
    (sarah / "wake_refusals.json").write_text("{}")  # reads again: forgotten, so a later break alerts again
    sweeps(world, monkeypatch, capsys, 1)
    (sarah / "wake_refusals.json").write_text(raw)
    (again,) = sweeps(world, monkeypatch, capsys, 1)
    assert len(lines(again)) == 1


def test_an_unreadable_refusals_file_keeps_earlier_refusal_alerts_quiet(world, monkeypatch, capsys, tmp_path):
    (tmp_path / "instances" / "sarah.token").write_text("k")
    park_id = world.row("parked", profile="sarah")
    sarah = healthy(tmp_path, "sarah")
    refusals(sarah, {park_id: entry("sarah", 3)})
    sweeps(world, monkeypatch, capsys, 1)
    (sarah / "wake_refusals.json").write_text("{broken")
    sweeps(world, monkeypatch, capsys, 1)  # the unreadable alert
    refusals(sarah, {park_id: entry("sarah", 3)})  # back, unchanged: not said again
    (out,) = sweeps(world, monkeypatch, capsys, 1)
    assert out.out == ""
