"""deploy/configure_hermes.sh: syntax, shellcheck (when available) and a few invariants of the plan."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "configure_hermes.sh"


def _shellcheck():
    local = Path(sys.executable).with_name("shellcheck")  # shellcheck-py in the dev venv, if installed
    return str(local) if local.exists() else shutil.which("shellcheck")


def test_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(_shellcheck() is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    result = subprocess.run([_shellcheck(), str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _code_lines():
    return [line.strip() for line in SCRIPT.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def test_never_creates_the_bridge_secret_and_never_echoes_secret_files():
    for line in _code_lines():
        if "bridge_secret" in line:  # Yaakov stores it; the script may only check for it, or fix an existing file
            assert line.startswith(('[[ -e "$ETC/bridge_secret" ]] &&', '[[ -s "$ETC/bridge_secret" ]] || die')), line
            assert not re.search(r"\b(touch|tee|install|mktemp|cp|mv|cat|echo|printf)\b|>", line), line  # never created
        # the only reader of the wake secret is the secret-source helper, whose stdout goes to Hermes
        if "wake_secret" in line and ("cat " in line or "echo " in line):
            raise AssertionError(line)


@pytest.mark.parametrize("needle", [
    "ETC=/etc/durable-park", "chown root:dpwake", "chmod 0640", "groupadd --system dpwake", "usermod -aG dpwake",
    "set_cfg plugins.hook_callback_timeout 60", "allow_gateway_injection\" true",
    "settings.ledger_path\" \"$LEDGER\"", "settings.token_file\" \"$ETC/instance_token\"",
    "platforms.webhook.extra.host 127.0.0.1", "platforms.webhook.extra.port 8644",
    "platforms.webhook.extra.rate_limit 120", "ROUTE=durable-park-wake", "routes.$ROUTE.deliver\" log",
    "FUNNEL_PATH=/aap/events", "RECEIVER_LOCAL=http://127.0.0.1:8790/aap/events",
    "tailscale funnel --bg --set-path=\"$FUNNEL_PATH\" \"$RECEIVER_LOCAL\"",
    "agentapprovalprotocol==0.3.3", "cron create \"$CRON_SCHEDULE\" --no-agent", "CRON_SCHEDULE='*/5 * * * *'", "--deliver telegram", "--retention-days 30",
    "hermes_cli plugins enable", "systemctl enable --quiet \"$UNIT\"",
    "BRIDGE_FUNNEL_PATH=/wh/bridge", "BRIDGE_LOCAL=http://127.0.0.1:8791/wh/bridge",
    "tailscale funnel --bg --set-path=\"$BRIDGE_FUNNEL_PATH\" \"$BRIDGE_LOCAL\"",
    "systemctl enable --quiet \"$BRIDGE_UNIT\"", "set_cfg secrets.command.command \"$SECRETS_HELPER\"",
    "install -m 640 -o root -g hermes /dev/null \"$ETC/allowed_toolsets\"",
])
def test_plan_values(needle):
    assert needle in SCRIPT.read_text()


def test_no_pipe_into_grep_q_under_pipefail():
    """M2: `cmd | grep -q` under pipefail can turn the writer's SIGPIPE into "not found" (duplicate cron job)."""
    import re
    assert "set -euo pipefail" in SCRIPT.read_text()
    for line in _code_lines():
        assert not re.search(r"\|\s*grep\b[^|]*-[a-zA-Z]*q", line), line


def _unit_lines():
    unit = (SCRIPT.parent / "durable-park-bridge.service").read_text()
    return [line.strip() for line in unit.splitlines() if line.strip() and not line.strip().startswith("#")]


BRIDGE_SERVICE = SCRIPT.parent / "durable-park-bridge.service"
BRIDGE_SOCKET = SCRIPT.parent / "durable-park-bridge.socket"


def _unit_sections(path):
    """{section: [non-comment lines]} of a unit file."""
    sections, current = {}, None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
        else:
            assert current is not None, line
            current.append(line)
    return sections


def test_bridge_socket_unit_lets_systemd_hold_the_port():
    """A socket unit owns 127.0.0.1:8791 at all times, so nothing the bot runs can bind it while the bridge restarts."""
    sections = _unit_sections(BRIDGE_SOCKET)
    assert set(sections) == {"Unit", "Socket", "Install"}
    assert {"Accept=no", "ListenStream=127.0.0.1:8791", "NoDelay=true"} <= set(sections["Socket"])
    assert [line for line in sections["Socket"] if line.startswith("ListenStream=")] == ["ListenStream=127.0.0.1:8791"]
    assert sections["Install"] == ["WantedBy=sockets.target"]


def test_bridge_unit_runs_as_its_own_user_in_no_other_group():
    service = _unit_sections(BRIDGE_SERVICE)["Service"]
    assert "User=dpbridge" in service and "Group=dpbridge" in service
    for line in service:
        assert not line.startswith("SupplementaryGroups"), line  # not dpwake, not hermes
        assert line not in ("User=hermes", "Group=hermes"), line
    assert not any("dpwake" in line for lines in _unit_sections(BRIDGE_SERVICE).values() for line in lines)


def test_bridge_unit_is_bound_to_its_socket_and_always_restarts():
    sections = _unit_sections(BRIDGE_SERVICE)
    assert "Requires=durable-park-bridge.socket" in sections["Unit"]
    assert "After=durable-park-bridge.socket network-online.target" in sections["Unit"]
    assert "Wants=network-online.target" in sections["Unit"]  # without it the After= ordering has no effect
    assert "Restart=always" in sections["Service"] and "RestartSec=1" in sections["Service"]
    assert not any(line.startswith("Restart=on-failure") for line in sections["Service"])


def test_bridge_service_never_trips_systemds_start_limit():
    """With Accept=no systemd fails the socket unit (service-start-limit-hit) and closes the listening fd when the
    service trips its start limit, which frees 127.0.0.1:8791 for anyone. The default limit (5 starts in 10 s) trips
    in about 6 s of a persistent start failure with RestartSec=1, so the service must have no start limit at all
    (systemd.unit: StartLimitIntervalSec=0 disables the rate limiting)."""
    assert "StartLimitIntervalSec=0" in _unit_sections(BRIDGE_SERVICE)["Unit"]
    assert not any(line.startswith("StartLimitBurst") for lines in _unit_sections(BRIDGE_SERVICE).values() for line in lines)


def test_bridge_socket_has_no_trigger_limit_that_could_fail_it():
    """systemd.socket: when TriggerLimitIntervalSec/TriggerLimitBurst (default 20 activations per 2 s for Accept=no)
    is hit, the socket unit is put into a failure mode and closes its listening fd, so a burst of connections while
    the bridge restarts would free the port. 0 disables the limit; PollLimit* still slows a flood down temporarily."""
    assert "TriggerLimitIntervalSec=0" in _unit_sections(BRIDGE_SOCKET)["Socket"]


def test_bridge_unit_takes_the_socket_from_systemd_and_reads_secrets_as_credentials():
    service = _unit_sections(BRIDGE_SERVICE)["Service"]
    exec_start = [line for line in service if line.startswith("ExecStart=")]
    assert exec_start == ["ExecStart=/opt/durable-park/bin/python -B -m bridge.app --systemd-socket "
                          "--secret-file %d/bridge_secret --key-file %d/audit_key --expected-agent proto-agents"]
    assert "--host" not in exec_start[0] and "--port" not in exec_start[0]  # a usage error with --systemd-socket
    assert "/etc/durable-park" not in exec_start[0]  # the files are read as root by systemd, not by the bridge
    assert "LoadCredential=bridge_secret:/etc/durable-park/bridge_secret" in service
    assert "LoadCredential=audit_key:/etc/durable-park/audit_key" in service
    assert "WorkingDirectory=/opt/durable-park" in service
    assert "Environment=PYTHONPATH=/opt/durable-park/plugin:/opt/durable-park" in service


@pytest.mark.parametrize("line", [
    "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
    "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "RestrictSUIDSGID=yes", "LockPersonality=yes",
    "CapabilityBoundingSet=", "UMask=0077",
    "ProtectProc=invisible", "ProcSubset=pid",  # the bot's user cannot even see the bridge's process
])
def test_bridge_unit_hardening(line):
    assert line in _unit_sections(BRIDGE_SERVICE)["Service"]


def test_bridge_unit_is_stateless_and_cannot_touch_the_ledger():
    """The bridge asks withHuman with the org key; it keeps no state, so no ledger path and no writable path."""
    code = "\n".join(_unit_lines())
    for gone in ("DURABLE_PARK_LEDGER", "ReadWritePaths", "--ledger", "ledger.db", "plugin-data"):
        assert gone not in code, gone
    assert "ProtectHome=yes" in code and "ProtectSystem=strict" in code
    assert "ProtectHome=read-only" not in code


def test_missing_or_empty_key_files_stop_the_script_before_anything_changes():
    """LoadCredential makes the unit fail to start when a key file is missing, so the script says so up front
    (and changes nothing), instead of stopping the old bridge and dying later at the 401 gate."""
    code = "\n".join(_code_lines())
    secret = next(l for l in _code_lines() if l.startswith('[[ -s "$ETC/bridge_secret" ]] || die'))
    key = next(l for l in _code_lines() if l.startswith('[[ -s "$ETC/audit_key" ]] || die'))
    for line in (secret, key):
        assert "read -rs" in line and "docs/runbook.md" in line and "nothing was changed" in line, line
    first_change = min(code.index(x) for x in (
        "useradd --system --no-create-home", 'sync_tree "$REPO/bridge"', 'install_root "$REPO/deploy/$BRIDGE_UNIT.socket"',
        'systemctl stop "$BRIDGE_UNIT"', 'chown root:root "$ETC/bridge_secret"', 'systemctl enable --now "$BRIDGE_UNIT.socket"'))
    assert code.index(secret) < first_change and code.index(key) < first_change
    # the old behaviour is gone: a missing key is no longer a warning
    assert "the bridge answers 401 (Yaakov" not in code and "answers human to every gateway call (Yaakov" not in code
    assert "the bridge answers human until the plugin creates it" not in code
    assert "ReadWritePaths" not in SCRIPT.read_text()  # the ledger directory is no longer a bridge path


def test_webhook_route_secret_is_a_bare_ref():
    """Hermes expands `${env:VAR}` at import-time config load, before its secrets.command helper has put
    the value in the environment, and warns on every process. A bare `${VAR}` resolves identically (same
    primitive, hermes_cli/config.py `_env_expand_match`) but warns only for the `env:` form."""
    text = SCRIPT.read_text()
    assert ".secret\" '${DURABLE_PARK_WAKE_SECRET}'" in text
    assert "${env:" not in text


def test_no_env_prefixed_durable_park_refs_anywhere():
    root = Path(__file__).resolve().parents[1]
    offenders = [str(path.relative_to(root)) for path in
                 [*(root / "deploy").glob("*.sh"), *(root / "docs").glob("*.md"), root / "README.md"]
                 if "${env:DURABLE_PARK" in path.read_text()]
    assert offenders == []


LIST_OUTPUT = """
\u250c\u2500\u2500\u2510
\u2502 Scheduled Jobs (profile: default) \u2502
\u2514\u2500\u2500\u2518

  0aaaaaaaaaaa [active]
    Name:      some-other-job
    Schedule:  every 5m
    Repeat:    \u221e

  5982945c6071 [active]
    Name:      durable-park-sweep
    Schedule:  {schedule}
    Repeat:    \u221e
    Next run:  2026-10-21T19:31:39.697852+00:00
    Deliver:   telegram
"""


def _job_awk():
    match = re.search(r"^CRON_JOB_AWK='(.*?)'$", SCRIPT.read_text(), re.S | re.M)
    assert match, "CRON_JOB_AWK is not defined"
    return match.group(1)


def _parse_job(schedule, name="durable-park-sweep"):
    out = subprocess.run(["awk", "-v", f"name={name}", _job_awk()], input=LIST_OUTPUT.format(schedule=schedule),
                         capture_output=True, text=True, check=True).stdout
    return out.rstrip("\n")


def test_sweep_runs_on_the_wall_clock_every_five_minutes():
    """An `every 5m` interval is re-armed from the previous run's time, so the 60 s cron ticker phase made it
    run every ~6 minutes; a cron expression is anchored to the wall clock (cron/jobs.py compute_next_run)."""
    text = SCRIPT.read_text()
    assert "CRON_SCHEDULE='*/5 * * * *'" in text
    assert "every 5m" not in "\n".join(_code_lines())


def test_cron_job_lookup_reads_id_and_schedule_of_the_named_job_only():
    assert _parse_job("every 5m") == "5982945c6071\tevery 5m"
    assert _parse_job("*/5 * * * *") == "5982945c6071\t*/5 * * * *"
    assert _parse_job("every 5m", name="no-such-job") == ""


def test_existing_job_schedule_is_edited_only_when_it_differs():
    """Re-running configure must not re-arm a job that is already on the wanted schedule (an edit resets the next
    run) and must move an old `every 5m` job over."""
    text = SCRIPT.read_text()
    assert 'cron edit "$job_id" --schedule "$CRON_SCHEDULE"' in text
    edit = text.index('cron edit "$job_id"')
    guard = text.rindex('!= "$CRON_SCHEDULE"', 0, edit)
    assert 0 < edit - guard < 200  # the edit sits directly under the comparison
    assert text.index("cron create") < text.index("--- 6. Funnel")


MCP_SCRIPT = SCRIPT.parent / "hermes_mcp.sh"


def test_hermes_mcp_script_syntax_and_shellcheck():
    subprocess.run(["bash", "-n", str(MCP_SCRIPT)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(MCP_SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_hermes_mcp_script_values_and_guards():
    text = MCP_SCRIPT.read_text()
    for needle in ("GATEWAY_URL=https://app.withhuman.ai/gateway/mcp", "set_cfg \"mcp_servers.$SERVER.trust\" full",
                   "'Bearer ${DURABLE_PARK_WH_TOKEN}'", "TOKEN_FILE=/etc/durable-park/instance_token",
                   "does not exist: provision the instance first",
                   "TOOL_INCLUDE='[\"quickbooks__*\", \"maverick__*\"]'",
                   "EXPECTED_TOOL_MAVERICK=mcp__wh__maverick__maverick_read",
                   "has \"$registered\" -F -- \"$EXPECTED_TOOL_MAVERICK\"",
                   "MAVERICK_SKILLS_DIR=/opt/maverick-mcp-src/skills",
                   'set_cfg skills.external_dirs "[\\"$MAVERICK_SKILLS_DIR\\"]"'):
        assert needle in text, needle
    code = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    for line in code:
        assert not ("instance_token" in line and ("cat " in line or "echo " in line)), line
        assert "untrusted" not in line, line


POLICY_WRAPPER = SCRIPT.parent / "tool_policy_check.sh"
DROP_IN = SCRIPT.parent / "hermes-gateway-policy.conf"


def test_drop_in_installed_and_reloaded():
    text = SCRIPT.read_text()
    assert "50-durable-park-policy.conf" in text
    assert "hermes-gateway.service.d" in text
    assert "daemon-reload" in text
    assert text.index("50-durable-park-policy.conf") < text.index('user_systemctl daemon-reload') \
        < text.index("# --- 7. restart Hermes")  # in place before the gateway restarts
    conf = DROP_IN.read_text()
    assert "[Service]" in conf
    assert "Environment=DURABLE_PARK_POLICY_STATUS=%h/.hermes/plugin-data/durable-park/tool_policy_status.json" in conf
    assert "ExecStartPre=-/bin/bash %h/durable-park/deploy/tool_policy_check.sh" in conf


def test_policy_wrapper_syntax_and_shellcheck():
    subprocess.run(["bash", "-n", str(POLICY_WRAPPER)], check=True)
    if _shellcheck() is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run([_shellcheck(), str(POLICY_WRAPPER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_policy_wrapper_finds_python_the_way_configure_does():
    def snippet(path):
        text = path.read_text()
        start = text.index("import glob, json, sys")
        return text[start:text.index("\"$HERMES_HOME\"", start)]
    assert snippet(POLICY_WRAPPER) == snippet(SCRIPT)


def _run_wrapper(tmp_path, *, python_ok):
    """Run the wrapper against a fake HERMES_HOME; returns (exit code, status path)."""
    home = tmp_path / "hermes"
    status = tmp_path / "state" / "tool_policy_status.json"
    if python_ok:
        install = home / "installs" / "x"
        install.mkdir(parents=True)
        env_dir = tmp_path / "venv"
        (env_dir / "bin").mkdir(parents=True)
        (env_dir / "bin" / "python").symlink_to(sys.executable)
        (install / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(env_dir)}}}))
    else:
        home.mkdir()
    result = subprocess.run(["bash", str(POLICY_WRAPPER)], env={
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(home),
        "DURABLE_PARK_POLICY_STATUS": str(status)}, capture_output=True, text=True)
    return result.returncode, status


def test_policy_wrapper_without_python_writes_a_failing_report_and_exits_0(tmp_path):
    code, status = _run_wrapper(tmp_path, python_ok=False)
    assert code == 0
    report = json.loads(status.read_text())
    assert report["ok"] is False and report["problems"] == ["hermes python not found"] and report["allowed"] == []
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", report["checked_at"])
    assert [p.name for p in status.parent.iterdir()] == [status.name]  # no temp file left


def test_policy_wrapper_failed_check_leaves_a_failing_report_and_exits_0(tmp_path):
    # the fake Hermes python cannot import hermes_bootstrap, so the check itself fails: still exit 0, ok false
    code, status = _run_wrapper(tmp_path, python_ok=True)
    assert code == 0
    assert json.loads(status.read_text())["ok"] is False


def _run_wrapper_with_stub(tmp_path, stub_body, *, existing=None):
    """The wrapper against a fake HERMES_HOME whose "python" is a shell stub; returns (code, status path)."""
    home = tmp_path / "hermes"
    install = home / "installs" / "x"
    install.mkdir(parents=True)
    env_dir = tmp_path / "venv"
    (env_dir / "bin").mkdir(parents=True)
    stub = env_dir / "bin" / "python"
    stub.write_text("#!/bin/sh\n" + stub_body)
    stub.chmod(0o755)
    (install / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(env_dir)}}}))
    status = tmp_path / "state" / "tool_policy_status.json"
    if existing is not None:
        status.parent.mkdir()
        status.write_text(existing)
    result = subprocess.run(["bash", str(POLICY_WRAPPER)], env={
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(home),
        "DURABLE_PARK_POLICY_STATUS": str(status)}, capture_output=True, text=True)
    return result.returncode, status


STUB_FAILS = "exit 1\n"
STUB_PASSES = ('while [ $# -gt 0 ]; do [ "$1" = --status-file ] && f=$2; shift; done\n'
               'printf \'{"ok": true, "checked_at": "2099-01-01T00:00:00Z", "problems": [], "allowed": []}\\n\' > "$f"\n')
OLD_PASS = '{"ok": true, "checked_at": "2020-01-01T00:00:00Z", "problems": [], "allowed": []}'


def test_policy_wrapper_stub_that_fails_without_writing(tmp_path):
    code, status = _run_wrapper_with_stub(tmp_path, STUB_FAILS)
    assert code == 0
    report = json.loads(status.read_text())
    assert report["ok"] is False and report["problems"] and report["allowed"] == []


def _old_report_run(tmp_path, *, mv_fails):
    """Stub exits 1 and leaves an OLD passing report (mtime in the past); optionally ``mv`` fails, which is what
    an unwritable directory does to the wrapper's own write (cannot be made unwritable as root)."""
    extra = {}
    if mv_fails:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "mv").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "mv").chmod(0o755)
        extra = {"PATH": f"{bindir}:/usr/bin:/bin"}
    home = tmp_path / "hermes"
    (home / "installs" / "x").mkdir(parents=True)
    env_dir = tmp_path / "venv"
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / "bin" / "python").write_text("#!/bin/sh\n" + STUB_FAILS)
    (env_dir / "bin" / "python").chmod(0o755)
    (home / "installs" / "x" / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(env_dir)}}}))
    status = tmp_path / "state" / "tool_policy_status.json"
    status.parent.mkdir()
    status.write_text(OLD_PASS)
    subprocess.run(["touch", "-d", "2020-01-01", str(status)], check=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(home),
           "DURABLE_PARK_POLICY_STATUS": str(status), **extra}
    result = subprocess.run(["bash", str(POLICY_WRAPPER)], env=env, capture_output=True, text=True)
    return result, status


def test_policy_wrapper_replaces_an_old_passing_report_the_check_did_not_refresh(tmp_path):
    result, status = _old_report_run(tmp_path, mv_fails=False)
    assert result.returncode == 0
    assert json.loads(status.read_text())["ok"] is False
    assert "could not be refreshed" not in result.stderr


def test_policy_wrapper_says_so_when_it_cannot_refresh_the_report(tmp_path):
    result, status = _old_report_run(tmp_path, mv_fails=True)
    assert result.returncode == 0
    assert "durable-park: tool policy report could not be refreshed (directory not writable?)" in result.stderr
    assert status.read_text() == OLD_PASS  # the old file stays; the plugin's 15-minute check is the backstop
    assert [p.name for p in status.parent.iterdir()] == [status.name]  # no temp file left


def test_policy_wrapper_keeps_the_report_a_passing_check_wrote(tmp_path):
    code, status = _run_wrapper_with_stub(tmp_path, STUB_PASSES, existing=OLD_PASS)
    assert code == 0
    report = json.loads(status.read_text())
    assert report["ok"] is True and report["checked_at"] == "2099-01-01T00:00:00Z"


def test_drop_in_path_rewrite_uses_the_checkout():
    line = next(l for l in SCRIPT.read_text().splitlines() if l.startswith("sed "))
    expr = re.search(r'^sed "([^"]+)"', line).group(1).replace("$REPO", "/srv/checkout")
    out = subprocess.run(["sed", expr], input=DROP_IN.read_text(), capture_output=True, text=True, check=True).stdout
    assert "ExecStartPre=-/bin/bash /srv/checkout/deploy/tool_policy_check.sh" in out
    assert "ExecStartPost=/bin/bash /srv/checkout/deploy/plugin_load_check.sh" in out
    assert "%h/durable-park" not in out
    assert "ExecStartPre=-/bin/rm -f -- %h/.hermes/plugin-data/durable-park/plugin_load.json" in out  # untouched
    assert "Environment=DURABLE_PARK_POLICY_STATUS=%h/.hermes/" in out  # %h stays for systemd
    assert 'trap \'rm -f "$dropin_tmp"\' EXIT' in SCRIPT.read_text()


def test_a_checkout_path_with_whitespace_is_refused():
    text = SCRIPT.read_text()
    guard = text.index('[[ "$REPO" != *[[:space:]]* ]]')
    assert "die" in text[guard:guard + 200] and "whitespace" in text[guard:guard + 200]
    assert guard < text.index("tool_policy apply")


def test_an_invalid_allow_file_makes_the_script_exit_2_with_nothing_applied():
    text = SCRIPT.read_text()
    at = text.index("tool_policy apply")
    block = text[at - 20:at + 260]
    assert "rc=$?" in block and 'exit "$rc"' in block and "could not apply the agent tool policy" in block
    assert "tool_policy apply || die" not in text


def test_bridge_copy_is_the_whole_package_including_vendor():
    text = SCRIPT.read_text()
    code = "\n".join(_code_lines())
    assert 'sync_tree "$REPO/plugin/durable_park" "$VENV/plugin/durable_park"' in code
    assert 'sync_tree "$REPO/bridge" "$VENV/bridge"' in code
    assert "find . -type f -not -path '*/__pycache__/*'" in code
    assert "LICENSE.fastjsonschema" in code and "README.md" in code
    assert "_vendor" in text  # explained next to the copy
    assert 'rm -f -- "${dst:?}/$rel"' in code  # stale files go, and only below $dst


def test_bridge_health_gate_demands_401_before_going_on():
    code = "\n".join(_code_lines())
    assert "-w '%{http_code}' -X POST \"$BRIDGE_LOCAL\"" in code
    assert "== 401" in code and "seq 1 10" in code
    assert 'journalctl -u "$BRIDGE_UNIT" -n 20' in code and "grep -v -i secret" in code
    gate = code.index("bridge_ok=0")
    assert "die " in code[gate:] and code.index("die \"bridge unit is not answering 401") > gate


def _bridge_section():
    """The code of the `4b. gateway bridge` section after the sync_tree helper, up to the sweep wrapper."""
    code = "\n".join(_code_lines())
    return code[code.index('install -d -m 0755 -o root -g root "$VENV/plugin"'):code.index('as_hermes install -d -m 0700 "$HERMES_HOME/scripts"')]


def test_dpbridge_is_created_once_with_no_login_no_home_and_no_other_group():
    code = "\n".join(_code_lines())
    useradd = ("useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "
               "--user-group dpbridge")
    assert code.count(useradd) == 1
    lines = _code_lines()
    assert lines[lines.index(useradd) - 1] == "if ! getent passwd dpbridge >/dev/null; then"  # only when missing
    for line in _code_lines():
        if "dpbridge" in line:
            assert "usermod" not in line and "dpwake" not in line and "-aG" not in line, line
    assert 'for member in "$HERMES_USER" aaprecv; do' in code  # the dpwake members are still only these two
    assert code.index(useradd) < code.index("sync_tree \"$REPO/bridge\"")  # exists before its unit is installed


def test_bridge_secret_becomes_root_only_before_the_bridge_restarts():
    """systemd reads it as root through LoadCredential, so the bot (group dpwake) no longer can."""
    section = _bridge_section()
    line = next(l for l in section.splitlines() if 'chown root:root "$ETC/bridge_secret"' in l)
    assert line.startswith('[[ -e "$ETC/bridge_secret" ]] &&')  # only an existing file; the script never creates it
    assert 'chmod 0600 "$ETC/bridge_secret"' in line and "CHANGED=1" in line  # LoadCredential copies at start
    assert section.index(line) < section.index('restart_if_needed "$BRIDGE_UNIT"')
    assert 'chown "root:$HERMES_USER" "$ETC/audit_key"' in "\n".join(_code_lines())  # audit_key stays root:hermes 0640


def test_both_bridge_units_are_installed_and_the_socket_is_enabled_before_the_service_restarts():
    section = _bridge_section()
    socket_install = 'install_root "$REPO/deploy/$BRIDGE_UNIT.socket" "/etc/systemd/system/$BRIDGE_UNIT.socket" 0644'
    service_install = 'install_root "$REPO/deploy/$BRIDGE_UNIT.service" "/etc/systemd/system/$BRIDGE_UNIT.service" 0644'
    reload_ = "systemctl daemon-reload"
    enable_socket = 'systemctl enable --now "$BRIDGE_UNIT.socket"'
    order = [section.index(x) for x in (socket_install, service_install, reload_, enable_socket,
                                         'restart_if_needed "$BRIDGE_UNIT"')]
    assert order == sorted(order), order
    assert 'systemctl enable --quiet "$BRIDGE_UNIT"' in section  # the service stays enabled, as today


def test_a_service_that_still_holds_the_port_is_stopped_before_the_socket_takes_it():
    """First migration: the old unit (User=hermes) binds 127.0.0.1:8791 itself, so `enable --now` on the socket
    would fail with 'address already in use'. The stop happens only when the socket is not active yet."""
    section = _bridge_section()
    guard = 'if ! systemctl is-active --quiet "$BRIDGE_UNIT.socket"; then'
    stop = 'systemctl stop "$BRIDGE_UNIT"'
    assert section.index(guard) < section.index(stop) < section.index('systemctl enable --now "$BRIDGE_UNIT.socket"')
    assert 0 < section.index(stop) - section.index(guard) < 120  # the stop sits directly under the guard


def test_bridge_check_is_installed_root_owned_run_isolated_and_a_failure_or_absence_stops_the_script():
    code = "\n".join(_code_lines())
    section = _bridge_section()
    absent = '[[ -f "$REPO/deploy/bridge_check.py" ]] || die'
    assert absent in code and "nothing was changed" in next(l for l in _code_lines() if l.startswith(absent))
    first_change = min(code.index(x) for x in ("useradd --system --no-create-home", 'sync_tree "$REPO/bridge"',
                                               'systemctl stop "$BRIDGE_UNIT"'))
    assert code.index(absent) < first_change  # a checkout without it is a broken deploy: stop before changing anything
    assert 'if [[ -f "$REPO/deploy/bridge_check.py" ]]' not in code and 'if [[ -f "$VENV/deploy/bridge_check.py" ]]' not in code
    assert 'install -d -m 0755 -o root -g root "$VENV/deploy"' in section
    install = 'install_root "$REPO/deploy/bridge_check.py" "$VENV/deploy/bridge_check.py" 0755'
    run = 'python3 -I "$VENV/deploy/bridge_check.py" || die '
    assert install in section and run in section
    gate = section.index("bridge unit: $(systemctl is-active")
    assert section.index(install) < gate < section.index(run)  # always runs, after the 401 health gate passed
    assert "--- 5." not in section


def test_bridge_health_gate_curl_has_a_time_limit():
    code = "\n".join(_code_lines())
    assert "curl -s -m 3 -o /dev/null -w '%{http_code}' -X POST \"$BRIDGE_LOCAL\"" in code


def _socket_failure_handler():
    """The bridge_socket_failed() function of the script, as text."""
    match = re.search(r"^bridge_socket_failed\(\) \{\n.*?^\}\n", SCRIPT.read_text(), re.S | re.M)
    assert match, "configure_hermes.sh has no bridge_socket_failed()"
    return match.group(0)


def test_a_socket_that_cannot_start_stops_the_script_and_may_be_a_security_incident(tmp_path):
    """First migration: the old unit is stopped, then the socket must take 127.0.0.1:8791. If that fails, something
    else holds the port, and with the bot's user that is an impostor answering withHuman. The script must say so, show
    who holds the port (ss output names processes, never secrets) and tell the admin not to go on."""
    code = "\n".join(_code_lines())
    assert 'systemctl enable --now "$BRIDGE_UNIT.socket" || bridge_socket_failed' in code
    argv_file = tmp_path / "ss-args"
    harness = f"""
die() {{ printf 'configure_hermes: ERROR: %s\\n' "$*" >&2; exit 1; }}
ss() {{ printf '%s\\n' "$*" > {argv_file}; printf 'LISTEN 0 5 127.0.0.1:8791 0.0.0.0:* users:(("python3",pid=999,fd=3))\\n'; }}
BRIDGE_UNIT=durable-park-bridge
{_socket_failure_handler()}
bridge_socket_failed
echo REACHED-AFTER-DIE
"""
    done = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert done.returncode == 1 and "REACHED-AFTER-DIE" not in done.stdout
    assert "security incident" in done.stderr.lower()
    assert 'users:(("python3",pid=999,fd=3))' in done.stderr  # the listener's line is shown
    assert "do not continue" in done.stderr.lower()
    assert argv_file.read_text().strip() == "-ltnp sport = :8791"


def _receiver_package_pins():
    match = re.search(r"^RECEIVER_PACKAGES=\((.*)\)$", SCRIPT.read_text(), re.M)
    assert match, "RECEIVER_PACKAGES not found"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_venv_pins_httpx_which_the_bridge_imports_directly():
    """bridge/whapi.py imports httpx. It must not depend on agentapprovalprotocol happening to pull it in, so the
    venv pins it, to the version the VM's /opt/durable-park venv has (0.28.1) and the dev requirements test against."""
    assert "httpx==0.28.1" in _receiver_package_pins()


def test_every_venv_pin_is_the_version_the_tests_run_against():
    dev = (SCRIPT.parents[1] / "requirements-dev.txt").read_text().split()
    pins = _receiver_package_pins()
    assert pins and all(pin in dev for pin in pins), [pin for pin in pins if pin not in dev]
