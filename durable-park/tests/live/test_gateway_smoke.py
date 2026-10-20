"""Live smoke test: Hermes on the proto VM talks to the withHuman gateway through MCP server ``wh``.

Manual only (Task 3 Step 3), after provisioning and ``sudo bash deploy/hermes_mcp.sh``:

    DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_gateway_smoke.py

Optional: ``DURABLE_PARK_QBO_COMPANY`` = a regex for the sandbox company name (default ``Sandbox Company``).
Run as the ``hermes`` user. The one-shot chat turn makes one real ``get_company_info`` call through the
gateway, so the approval pipeline must let that read-only call through (or someone approves it in time).
Nothing here prints the instance token; the tests also assert it never shows up in Hermes's output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DURABLE_PARK_LIVE") != "1",
                                reason="live gateway smoke test: set DURABLE_PARK_LIVE=1 on the proto VM")

SERVER = "wh"
RAW_TOOL = "quickbooks__get_company_info"
HERMES_TOOL = f"mcp__{SERVER}__{RAW_TOOL}"
TOKEN_FILE = Path("/etc/durable-park/instance_token")
AGENT_LOG = Path.home() / ".hermes" / "logs" / "agent.log"
COMPANY = os.environ.get("DURABLE_PARK_QBO_COMPANY", r"Sandbox Company")


def _hermes() -> str:
    found = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    assert Path(found).exists(), "hermes launcher not found"
    return found


def _run(*args: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([_hermes(), *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _assert_no_token(*texts: str) -> None:
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        pytest.fail(f"{TOKEN_FILE} unreadable: provision first and run as hermes")
    assert token and all(token not in text for text in texts), "the instance token leaked into Hermes output"


def test_hermes_lists_the_quickbooks_gateway_tool():
    probe = _run("mcp", "test", SERVER, timeout=120)
    _assert_no_token(probe.stdout, probe.stderr)
    assert probe.returncode == 0, probe.stdout[-2000:] + probe.stderr[-2000:]
    assert RAW_TOOL in probe.stdout
    # The running gateway registered it under the Hermes name (latest registration line for `wh`).
    lines = [line for line in AGENT_LOG.read_text(errors="replace").splitlines()
             if f"MCP server '{SERVER}'" in line and "registered" in line]
    assert lines, f"no registration line for MCP server '{SERVER}' in {AGENT_LOG}"
    assert HERMES_TOOL in lines[-1]


def test_one_shot_cli_turn_returns_the_sandbox_company_name():
    prompt = (f"Call the tool {HERMES_TOOL} exactly once with no arguments, then reply with only the "
              "company name it returned.")
    turn = _run("chat", "-Q", "-q", prompt, timeout=600)
    _assert_no_token(turn.stdout, turn.stderr)
    assert turn.returncode == 0, turn.stdout[-2000:] + turn.stderr[-2000:]
    assert "PARKED" not in turn.stdout  # a read-only call must never be parked
    assert re.search(COMPANY, turn.stdout), turn.stdout[-2000:]
