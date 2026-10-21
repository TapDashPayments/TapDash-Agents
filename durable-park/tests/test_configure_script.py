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
        if "bridge_secret" in line:  # Yaakov stores it; the script may only check for it
            assert line.startswith('[[ -e "$ETC/bridge_secret" ]] ||'), line
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
    "agentapprovalprotocol==0.3.3", "cron create \"every 5m\" --no-agent", "--deliver telegram", "--retention-days 30",
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


def test_bridge_unit_uses_the_deployed_venv_and_ledger():
    unit = (SCRIPT.parent / "durable-park-bridge.service").read_text()
    assert "ExecStart=/opt/durable-park/bin/python -B -m bridge.app --host 127.0.0.1 --port 8791" in unit
    assert "Environment=DURABLE_PARK_LEDGER=/home/hermes/.hermes/plugin-data/durable-park/ledger.db" in unit
    assert "User=hermes" in unit and "SupplementaryGroups=dpwake" in unit
    assert 'LEDGER="$HERMES_HOME/plugin-data/durable-park/ledger.db"' in SCRIPT.read_text()


MCP_SCRIPT = SCRIPT.parent / "hermes_mcp.sh"


def test_hermes_mcp_script_syntax_and_shellcheck():
    subprocess.run(["bash", "-n", str(MCP_SCRIPT)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(MCP_SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_hermes_mcp_script_values_and_guards():
    text = MCP_SCRIPT.read_text()
    for needle in ("GATEWAY_URL=https://app.withhuman.ai/gateway/mcp", "set_cfg \"mcp_servers.$SERVER.trust\" full",
                   "'Bearer ${env:DURABLE_PARK_WH_TOKEN}'", "TOKEN_FILE=/etc/durable-park/instance_token",
                   "does not exist: provision the instance first", "TOOL_INCLUDE='[\"quickbooks__*\"]'"):
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
    assert "%h/durable-park" not in out
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
