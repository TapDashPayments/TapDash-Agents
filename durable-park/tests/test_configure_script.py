"""deploy/configure_hermes.sh: syntax, shellcheck (when available) and a few invariants of the plan."""

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
        assert "bridge_secret" not in line, line  # Task 7 owns it
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
    "agentapprovalprotocol==0.3.3", "cron create \"every 5m\" --no-agent", "--deliver telegram",
    "hermes_cli plugins enable", "systemctl enable --quiet \"$UNIT\"",
])
def test_plan_values(needle):
    assert needle in SCRIPT.read_text()
