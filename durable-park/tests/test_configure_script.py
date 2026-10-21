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
