"""deploy/plugin_load_check.sh (ExecStartPost of the gateway drop-in) and the drop-in that wires it."""
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPT = DEPLOY / "plugin_load_check.sh"
DROP_IN = DEPLOY / "hermes-gateway-policy.conf"
ALERT = ("durable-park ALERT: the plugin did not load in this gateway (pid {pid}); stopping the gateway so no "
         "tool call runs unchecked")


def run_check(tmp_path, *, mainpid="4242", proof=None, timeout="2", raw=None, extra_env=None, cwd=None):
    path = tmp_path / "plugin_load.json"
    if raw is not None:
        path.write_text(raw)
    elif proof is not None:
        path.write_text(json.dumps(proof))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DURABLE_PARK_PLUGIN_LOAD": str(path),
           "DURABLE_PARK_LOAD_TIMEOUT": timeout, **(extra_env or {})}
    if mainpid is not None:
        env["MAINPID"] = mainpid
    started = time.monotonic()
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30, cwd=cwd)
    return result, time.monotonic() - started


def good(pid=4242, mode="asking"):
    return {"pid": pid, "written_at": "2026-10-21T10:00:00Z", "mode": mode, "hooks": ["pre_tool_call"]}


@pytest.mark.parametrize("mode", ["asking", "refusing-policy", "refusing-error"])
def test_loaded_gateway_passes_with_one_line(tmp_path, mode):
    result, took = run_check(tmp_path, proof=good(mode=mode))
    assert result.returncode == 0
    assert result.stdout.strip() == f"durable-park plugin loaded (mode={mode}, pid=4242)"
    assert took < 1.5  # no needless wait


def test_waits_for_the_proof_to_appear(tmp_path):
    path = tmp_path / "plugin_load.json"
    writer = subprocess.Popen(["bash", "-c", f"sleep 1.2; printf '%s' '{json.dumps(good())}' > '{path}'"])
    try:
        result, _ = run_check(tmp_path, timeout="6")
    finally:
        writer.wait()
    assert result.returncode == 0 and "mode=asking, pid=4242" in result.stdout


def test_pid_mismatch_fails_after_the_timeout(tmp_path):
    result, took = run_check(tmp_path, proof=good(pid=1))  # a proof from an earlier gateway process
    assert result.returncode == 1
    assert result.stdout.strip() == ALERT.format(pid=4242)
    assert 1.5 <= took < 10


def test_missing_proof_fails_after_the_timeout(tmp_path):
    result, took = run_check(tmp_path)
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)
    assert 1.5 <= took < 10


@pytest.mark.parametrize("raw", ["{not json", "[4242]", "", '{"pid": "4242", "mode": "asking"}',
                                 '{"pid": true, "mode": "asking"}', '{"pid": 4242, "mode": "weird"}',
                                 '{"pid": 4242}'])
def test_unreadable_or_malformed_proof_fails(tmp_path, raw):
    result, _ = run_check(tmp_path, raw=raw, timeout="1")
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)


def test_unreadable_file_permissions_fail(tmp_path):
    path = tmp_path / "plugin_load.json"
    path.mkdir()  # a directory where the file should be: cannot be read
    result, _ = run_check(tmp_path, timeout="1")
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)


@pytest.mark.parametrize("mainpid", [None, "", "abc", "0"])
def test_no_usable_mainpid_fails_closed(tmp_path, mainpid):
    result, _ = run_check(tmp_path, mainpid=mainpid, proof=good(pid=4242), timeout="1")
    assert result.returncode == 1
    assert result.stdout.startswith("durable-park ALERT: the plugin did not load in this gateway (pid ")


def test_a_bad_timeout_setting_falls_back_to_the_default(tmp_path):
    result, took = run_check(tmp_path, proof=good(), timeout="not-a-number")
    assert result.returncode == 0 and took < 1.5


def test_the_default_timeout_is_30_seconds():
    text = SCRIPT.read_text()
    assert "TIMEOUT=${DURABLE_PARK_LOAD_TIMEOUT:-30}" in text
    assert "TIMEOUT=30" in text  # the fallback for a non-numeric value
    assert "120" not in text


def test_missing_python_fails_fast_with_the_alert(tmp_path):
    result, took = run_check(tmp_path, proof=good(), timeout="20",
                             extra_env={"DURABLE_PARK_PYTHON": str(tmp_path / "no-such-python")})
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)
    assert took < 3  # no polling when it cannot work


def test_the_timeout_counts_wall_time_not_loop_turns(tmp_path):
    # a slow parser must not stretch the wait: a python that sleeps 2 s per call, timeout 3 s
    slow = tmp_path / "slow-python"
    slow.write_text("#!/bin/sh\nsleep 2\nexit 1\n")
    slow.chmod(0o755)
    result, took = run_check(tmp_path, proof=good(), timeout="3", extra_env={"DURABLE_PARK_PYTHON": str(slow)})
    assert result.returncode == 1 and took < 8


def _wait_for_second_fraction(fraction):
    """Sleep until the wall clock is `fraction` of the way through a second (bash's $SECONDS counts whole wall seconds)."""
    now = time.time()
    target = math.floor(now) + fraction
    if target <= now:
        target += 1
    time.sleep(target - now)


@pytest.mark.parametrize("fraction", [0.80, 0.90])
def test_the_wait_is_at_least_the_timeout_whatever_the_second_phase(tmp_path, fraction):
    # Regression: counting whole $SECONDS gave up up to ~1 s early when the wait began late in a second (CI saw 1.05 s
    # for TIMEOUT=2). A python that takes 0.3 s per call and fails makes the boundary crossing deterministic.
    slow_fail = tmp_path / "slow-fail-python"
    # (the profile lister gets the real python: only the proof reader is slow and failing)
    slow_fail.write_text('#!/bin/sh\ncase "$*" in *profile_keys_check*) exec /usr/bin/python3 "$@" ;; esac\nsleep 0.3\nexit 1\n')
    slow_fail.chmod(0o755)
    _wait_for_second_fraction(fraction)
    result, took = run_check(tmp_path, proof=good(), timeout="2", extra_env={"DURABLE_PARK_PYTHON": str(slow_fail)})
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)
    assert 2 <= took < 6


def test_every_missing_proof_run_waits_the_full_timeout(tmp_path):
    # Natural (no stub) version: late-in-the-second starts, real python. Always >= TIMEOUT, never a second short.
    for fraction in (0.93, 0.96, 0.98):
        _wait_for_second_fraction(fraction)
        result, took = run_check(tmp_path, timeout="2")
        assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)
        assert 2 <= took < 6, (fraction, took)


def test_a_zero_padded_timeout_is_decimal_not_octal(tmp_path):
    result, took = run_check(tmp_path, timeout="02")  # "08"/"09" would be invalid octal without 10#
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)
    assert 2 <= took < 6


def test_unset_home_does_not_break_the_script(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "MAINPID": "4242", "DURABLE_PARK_LOAD_TIMEOUT": "1"}  # no HOME, no proof path
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "unbound variable" not in result.stderr
    assert result.stdout.strip() == ALERT.format(pid=4242)


def test_script_syntax_and_shellcheck():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash\n")
    assert "set -u" in SCRIPT.read_text()
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed (CI runs it)")
    result = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_script_never_prints_secrets_or_reads_them():
    text = SCRIPT.read_text()
    assert "/etc/durable-park" not in text and "token" not in text.lower()


def test_drop_in_runs_the_check_and_limits_restarts():
    conf = DROP_IN.read_text()
    lines = [ln.strip() for ln in conf.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert "ExecStartPost=/bin/bash %h/durable-park/deploy/plugin_load_check.sh" in lines
    assert not any(ln.startswith("ExecStartPost=-") for ln in lines)  # a failure must fail the unit
    assert "StartLimitIntervalSec=600" in lines and "StartLimitBurst=3" in lines
    assert "Environment=DURABLE_PARK_PLUGIN_LOAD=%h/.hermes/plugin-data/durable-park/plugin_load.json" in lines
    # [Unit] holds the start limit, [Service] the commands
    unit_at, service_at = lines.index("[Unit]"), lines.index("[Service]")
    assert unit_at < service_at
    assert lines.index("StartLimitBurst=3") < service_at
    assert lines.index("ExecStartPost=/bin/bash %h/durable-park/deploy/plugin_load_check.sh") > service_at
    rm = "ExecStartPre=-/bin/rm -f -- %h/.hermes/plugin-data/durable-park/plugin_load.json"
    check = "ExecStartPre=-/bin/bash %h/durable-park/deploy/tool_policy_check.sh"
    assert lines.index(rm) < lines.index(check)  # the stale proof is gone before anything else runs
    assert re.search(r"^ExecStartPre=-/bin/bash %h/durable-park/deploy/tool_policy_check\.sh$", conf, re.M)


# ---- per-person profiles (Task 4) ----------------------------------------------------------------------------

PROFILE_ALERT = ("durable-park ALERT: the plugin did not load for profile {profile} in this gateway (pid {pid}); "
                 "stopping the gateway so no tool call runs unchecked")
ROUTES_ALERT = ("durable-park ALERT: could not read gateway.profile_routes from the Hermes config; stopping the "
                "gateway so no profile runs unchecked")
BAD_NAME_ALERT = ("durable-park ALERT: a gateway.profile_routes entry has a profile name that is not valid; "
                  "stopping the gateway so no profile runs unchecked")
SYSTEM_PYTHON = "/usr/bin/python3"


def has_yaml():
    return subprocess.run([SYSTEM_PYTHON, "-I", "-c", "import yaml"], capture_output=True).returncode == 0


needs_yaml = pytest.mark.skipif(not has_yaml(), reason="the system python has no yaml")


def write_profile_proof(tmp_path, name, **kw):
    folder = tmp_path / "profiles" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin_load.json").write_text(json.dumps(good(**kw)))


def write_root_proof(tmp_path, **kw):
    (tmp_path / "plugin_load.json").write_text(json.dumps(good(**kw)))


def routes(*names):
    return {"DURABLE_PARK_PROFILES": " ".join(names)}


def test_load_check_names_each_profile_without_proof(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")  # tara has no proof
    result, took = run_check(tmp_path, timeout="2", extra_env=routes("sarah", "tara"))
    assert result.returncode == 1
    assert result.stdout.strip() == PROFILE_ALERT.format(profile="tara", pid=4242)
    assert "sarah" not in result.stdout
    assert took >= 1.5  # it waited for the proof, like for the root


def test_load_check_passes_when_the_root_and_every_profile_have_a_proof(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah", mode="refusing-policy")
    write_profile_proof(tmp_path, "tara")
    result, took = run_check(tmp_path, extra_env=routes("sarah", "tara"))
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "durable-park plugin loaded (mode=asking, pid=4242)",
        "durable-park plugin loaded for profile sarah (mode=refusing-policy, pid=4242)",
        "durable-park plugin loaded for profile tara (mode=asking, pid=4242)"]
    assert took < 2.5  # no needless wait


def test_load_check_fails_naming_everything_that_has_no_proof(tmp_path):
    result, _ = run_check(tmp_path, timeout="1", extra_env=routes("sarah", "tara"))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [ALERT.format(pid=4242), PROFILE_ALERT.format(profile="sarah", pid=4242),
                                          PROFILE_ALERT.format(profile="tara", pid=4242)]


def test_load_check_root_missing_alone_keeps_todays_line(tmp_path):
    write_profile_proof(tmp_path, "sarah")
    result, _ = run_check(tmp_path, timeout="1", extra_env=routes("sarah"))
    assert result.returncode == 1 and result.stdout.strip() == ALERT.format(pid=4242)


def test_load_check_profile_proof_from_another_pid_fails(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah", pid=1)  # an earlier gateway process
    result, _ = run_check(tmp_path, timeout="1", extra_env=routes("sarah"))
    assert result.returncode == 1 and result.stdout.strip() == PROFILE_ALERT.format(profile="sarah", pid=4242)


def test_load_check_waits_for_a_profile_proof_to_appear(tmp_path):
    write_root_proof(tmp_path)
    folder = tmp_path / "profiles" / "sarah"
    folder.mkdir(parents=True)
    writer = subprocess.Popen(["bash", "-c", f"sleep 1.2; printf '%s' '{json.dumps(good())}' > '{folder}/plugin_load.json'"])
    try:
        result, _ = run_check(tmp_path, timeout="6", extra_env=routes("sarah"))
    finally:
        writer.wait()
    assert result.returncode == 0 and "loaded for profile sarah (mode=asking, pid=4242)" in result.stdout


def test_load_check_skips_default_names_and_repeats(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")
    result, _ = run_check(tmp_path, extra_env=routes("default", "main", "sarah", "sarah"))
    assert result.returncode == 0
    assert result.stdout.count("loaded for profile") == 1


@pytest.mark.parametrize("name", ["../x", "Sarah", "a b/c", ".hidden", "x;y"])
def test_load_check_invalid_profile_name_fails_closed(tmp_path, name):
    write_root_proof(tmp_path)
    result, took = run_check(tmp_path, timeout="20", extra_env={"DURABLE_PARK_PROFILES": f"sarah,{name}"})
    assert result.returncode == 1
    assert result.stdout.strip() == BAD_NAME_ALERT
    assert took < 5  # a bad name cannot be fixed by waiting


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return {"DURABLE_PARK_HERMES_CONFIG": str(path)}


@needs_yaml
def test_load_check_reads_the_routed_profiles_from_the_config(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")
    env = write_config(tmp_path, (
        "model:\n  default: x\n"
        "gateway:\n  multiplex_profiles: true\n  profile_routes:\n"
        "    - {name: sarah, platform: teams, user_id: u1, guild_id: g, profile: sarah}\n"
        "    - {name: tara, platform: teams, user_id: u2, guild_id: g, profile: tara}\n"))
    result, _ = run_check(tmp_path, timeout="1", extra_env=env)
    assert result.returncode == 1
    assert result.stdout.strip() == PROFILE_ALERT.format(profile="tara", pid=4242)  # sarah has a proof, tara none


@needs_yaml
@pytest.mark.parametrize("text", ["model:\n  default: x\n", "", "gateway:\n  profile_routes: []\n",
                                  "gateway:\n  profile_routes:\n"])
def test_load_check_with_no_routes_in_the_config_is_exactly_todays(tmp_path, text):
    write_root_proof(tmp_path)
    result, took = run_check(tmp_path, extra_env=write_config(tmp_path, text))
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"
    assert took < 1.5


def test_load_check_with_no_config_file_is_exactly_todays(tmp_path):
    write_root_proof(tmp_path)
    result, _ = run_check(tmp_path, extra_env={"DURABLE_PARK_HERMES_CONFIG": str(tmp_path / "nope.yaml")})
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"


@needs_yaml
def test_load_check_unparseable_config_with_routes_fails_closed(tmp_path):
    write_root_proof(tmp_path)
    env = write_config(tmp_path, "gateway:\n  profile_routes: [unclosed\n")
    result, took = run_check(tmp_path, timeout="20", extra_env=env)
    assert result.returncode == 1 and result.stdout.strip() == ROUTES_ALERT
    assert took < 5


def no_yaml_python(tmp_path):
    """A python that has no yaml (exit 3 for the routes reader) and otherwise is the system python."""
    stub = tmp_path / "no-yaml-python"
    stub.write_text(f'#!/bin/sh\ncase "$*" in *yaml*) exit 3 ;; esac\nexec {SYSTEM_PYTHON} "$@"\n')
    stub.chmod(0o755)
    return stub


@needs_yaml
def test_load_check_falls_back_to_hermes_own_python_for_the_config(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")
    home = tmp_path / "hh"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    hermes_python = venv / "bin" / "python"
    hermes_python.write_text(f'#!/bin/sh\nexec {SYSTEM_PYTHON} "$@"\n')
    hermes_python.chmod(0o755)
    (home / "installs" / "one").mkdir(parents=True)
    (home / "installs" / "one" / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
    env = write_config(tmp_path, "gateway:\n  profile_routes:\n    - {profile: sarah}\n")
    result, _ = run_check(tmp_path, extra_env={**env, "HERMES_HOME": str(home),
                                               "DURABLE_PARK_PYTHON": str(no_yaml_python(tmp_path))})
    assert result.returncode == 0
    assert "loaded for profile sarah (mode=asking, pid=4242)" in result.stdout


def test_load_check_no_yaml_anywhere_with_routes_fails_closed(tmp_path):
    write_root_proof(tmp_path)
    env = write_config(tmp_path, "gateway:\n  profile_routes:\n    - {profile: sarah}\n")
    result, took = run_check(tmp_path, timeout="20", extra_env={**env, "HERMES_HOME": str(tmp_path / "none"),
                                                               "DURABLE_PARK_PYTHON": str(no_yaml_python(tmp_path))})
    assert result.returncode == 1 and result.stdout.strip() == ROUTES_ALERT
    assert took < 5


def test_load_check_env_override_wins_over_the_config(tmp_path):
    write_root_proof(tmp_path)
    env = write_config(tmp_path, "gateway:\n  profile_routes:\n    - {profile: tara}\n")
    result, _ = run_check(tmp_path, extra_env={**env, "DURABLE_PARK_PROFILES": ""})  # set but empty: no profiles
    assert result.returncode == 0 and "profile" not in result.stdout


# ---- F7: a route name is never a glob, and a config that cannot be read is never "no routes" -----------------

def glob_bait(tmp_path):
    """A working directory holding files named like valid profiles, each with a proof: if a route name such as ``*``
    were expanded against it, the check would see two healthy profiles and pass."""
    write_root_proof(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for name in ("sarah", "tom"):
        (cwd / name).write_text("x")
        write_profile_proof(tmp_path, name)
    return cwd


@pytest.mark.parametrize("name", ["*", "s*", "?arah", "[a-z]arah"])
def test_load_check_a_glob_in_a_route_name_is_invalid_and_never_expanded(tmp_path, name):
    cwd = glob_bait(tmp_path)
    result, took = run_check(tmp_path, timeout="20", cwd=cwd, extra_env={"DURABLE_PARK_PROFILES": name})
    assert result.returncode == 1 and result.stdout.strip() == BAD_NAME_ALERT
    assert took < 5


def test_load_check_the_glob_check_catches_the_expansion(tmp_path):
    """The bait works: the same two names, written out, pass."""
    cwd = glob_bait(tmp_path)
    result, _ = run_check(tmp_path, cwd=cwd, extra_env=routes("sarah", "tom"))
    assert result.returncode == 0 and result.stdout.count("loaded for profile") == 2


@needs_yaml
@pytest.mark.parametrize("name", ["*", "s*"])
def test_load_check_a_glob_in_the_config_is_invalid_and_never_expanded(tmp_path, name):
    cwd = glob_bait(tmp_path)
    env = write_config(tmp_path, f'gateway:\n  profile_routes:\n    - {{profile: "{name}"}}\n')
    result, took = run_check(tmp_path, timeout="20", cwd=cwd, extra_env=env)
    assert result.returncode == 1 and result.stdout.strip() == BAD_NAME_ALERT
    assert took < 5


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a chmod 000 file")
def test_load_check_an_unreadable_config_fails_closed(tmp_path):
    write_root_proof(tmp_path)
    env = write_config(tmp_path, "gateway:\n  profile_routes:\n    - {profile: sarah}\n")
    config = Path(env["DURABLE_PARK_HERMES_CONFIG"])
    config.chmod(0)
    try:
        result, took = run_check(tmp_path, timeout="20", extra_env=env)
    finally:
        config.chmod(0o600)
    assert result.returncode == 1 and result.stdout.strip() == ROUTES_ALERT
    assert took < 5


def test_load_check_grep_finding_nothing_is_still_no_routes(tmp_path):
    write_root_proof(tmp_path)
    env = write_config(tmp_path, "model:\n  default: x\n")  # readable, never mentions profile_routes
    result, took = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"
    assert took < 1.5


# ---- every installed profile folder is waited for, routed or not (per-person roles, Task 5) ----------------

LIST_ALERT = ("durable-park ALERT: could not list the Hermes profiles; stopping the gateway so no profile runs "
              "unchecked")


def make_home(tmp_path, *names, marker="config.yaml"):
    home = tmp_path / "hh"
    for name in names:
        folder = home / "profiles" / name
        folder.mkdir(parents=True, exist_ok=True)
        if marker:
            (folder / marker).write_text("{}")
    home.mkdir(exist_ok=True)
    return {"HERMES_HOME": str(home)}


def test_load_check_waits_for_a_profile_folder_that_is_not_routed(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")  # tara has a folder and no proof
    env = make_home(tmp_path, "sarah", "tara")
    result, took = run_check(tmp_path, timeout="2", extra_env=env)
    assert result.returncode == 1
    assert result.stdout.strip() == PROFILE_ALERT.format(profile="tara", pid=4242)
    assert took >= 1.5


def test_load_check_passes_when_every_profile_folder_has_a_proof(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")
    write_profile_proof(tmp_path, "tara", mode="refusing-policy")
    result, took = run_check(tmp_path, extra_env=make_home(tmp_path, "sarah", "tara"))
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "durable-park plugin loaded (mode=asking, pid=4242)",
        "durable-park plugin loaded for profile sarah (mode=asking, pid=4242)",
        "durable-park plugin loaded for profile tara (mode=refusing-policy, pid=4242)"]
    assert took < 2.5


def test_load_check_names_the_not_set_up_profile_too(tmp_path):
    write_root_proof(tmp_path)
    result, _ = run_check(tmp_path, timeout="1", extra_env=make_home(tmp_path, "not-set-up"))
    assert result.returncode == 1
    assert result.stdout.strip() == PROFILE_ALERT.format(profile="not-set-up", pid=4242)


def test_load_check_does_not_wait_for_a_parked_profile(tmp_path):
    write_root_proof(tmp_path)
    env = make_home(tmp_path, "asleep")
    (tmp_path / "hh" / "profiles" / "asleep" / "gateway.parked").write_text("")
    result, took = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"
    assert took < 1.5


def test_load_check_does_not_wait_for_a_standalone_profile(tmp_path):
    """Hermes does not serve a gateway.standalone profile from this gateway: no copy of the plugin loads for it."""
    write_root_proof(tmp_path)
    env = make_home(tmp_path, "alone")
    (tmp_path / "hh" / "profiles" / "alone" / "config.yaml").write_text('{"gateway": {"standalone": true}}')
    result, took = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"
    assert took < 1.5


def test_load_check_ignores_deleted_and_marker_less_folders_and_bad_names(tmp_path):
    write_root_proof(tmp_path)
    env = make_home(tmp_path, "gone", "Shouting", ".hidden")
    make_home(tmp_path, "ghost", marker=None)
    (tmp_path / "hh" / "profiles" / ".deleted").mkdir(exist_ok=True)
    (tmp_path / "hh" / "profiles" / ".deleted" / "gone").write_text("x")
    result, took = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"
    assert took < 1.5


def test_load_check_override_replaces_the_folder_scan(tmp_path):
    write_root_proof(tmp_path)
    env = {**make_home(tmp_path, "sarah"), "DURABLE_PARK_PROFILES": ""}  # set but empty: no profiles at all
    result, _ = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and "profile" not in result.stdout


def test_load_check_a_missing_profiles_folder_is_no_profiles(tmp_path):
    write_root_proof(tmp_path)
    result, _ = run_check(tmp_path, extra_env={"HERMES_HOME": str(tmp_path / "hh")})
    assert result.returncode == 0 and result.stdout.strip() == "durable-park plugin loaded (mode=asking, pid=4242)"


def failing_lister(tmp_path):
    """A python that runs everything normally except the profile lister, which fails."""
    stub = tmp_path / "python-without-lister"
    stub.write_text(f'#!/bin/sh\ncase "$*" in *profile_keys_check*) exit 1 ;; esac\nexec {SYSTEM_PYTHON} "$@"\n')
    stub.chmod(0o755)
    return stub


def test_load_check_that_cannot_list_the_profiles_fails_closed_at_once(tmp_path):
    write_root_proof(tmp_path)
    env = {**make_home(tmp_path, "sarah"), "DURABLE_PARK_PYTHON": str(failing_lister(tmp_path))}
    result, took = run_check(tmp_path, timeout="20", extra_env=env)
    assert result.returncode == 1 and result.stdout.strip() == LIST_ALERT
    assert took < 5  # waiting cannot fix it


@needs_yaml
def test_load_check_needs_both_the_routed_and_the_installed_profiles(tmp_path):
    write_root_proof(tmp_path)
    write_profile_proof(tmp_path, "sarah")  # installed, has a proof; tara is routed only, no proof
    env = {**make_home(tmp_path, "sarah"), **write_config(tmp_path, "gateway:\n  profile_routes:\n    - {profile: tara}\n")}
    result, _ = run_check(tmp_path, timeout="1", extra_env=env)
    assert result.returncode == 1
    assert result.stdout.strip() == PROFILE_ALERT.format(profile="tara", pid=4242)
    write_profile_proof(tmp_path, "tara")
    result, _ = run_check(tmp_path, extra_env=env)
    assert result.returncode == 0 and "for profile sarah" in result.stdout and "for profile tara" in result.stdout
