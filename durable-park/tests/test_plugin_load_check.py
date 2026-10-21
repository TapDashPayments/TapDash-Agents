"""deploy/plugin_load_check.sh (ExecStartPost of the gateway drop-in) and the drop-in that wires it."""
import json
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


def run_check(tmp_path, *, mainpid="4242", proof=None, timeout="2", raw=None, extra_env=None):
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
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
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
