"""Live smoke test: Hermes on the proto VM talks to the withHuman gateway through MCP server ``wh``.

Manual only (Task 3 Step 3), after provisioning and ``sudo bash deploy/hermes_mcp.sh``:

    DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_gateway_smoke.py

Optional: ``DURABLE_PARK_QBO_COMPANY`` = a regex for the sandbox company name (default ``Sandbox Company``).
Run as the ``hermes`` user. Every surface (Teams, Telegram, the one-shot ``hermes chat -q`` CLI, the TUI,
desktop) can use tools, and every call goes through durable-park and withHuman. A CLI turn gets a session key
like ``agent:main:local:dm:local-...``, so its ``get_company_info`` read is asked about, auto-approved by
withHuman, run, and recorded in the ledger as an ``allowed`` row. Nothing here prints the instance token; the
tests also assert it never shows up in Hermes's output.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
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
LEDGER_DB = Path(os.environ.get("DURABLE_PARK_LEDGER", str(Path.home() / ".hermes" / "plugin-data" / "durable-park" / "ledger.db")))


def _hermes() -> str:
    found = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    assert Path(found).exists(), "hermes launcher not found"
    return found


def _hermes_env() -> dict[str, str]:
    """The environment for the real ``hermes`` subprocess.

    pytest sets ``PYTEST_CURRENT_TEST`` and the child inherits it. Hermes treats that as "this is a unit test"
    and its auth store refuses to open the real ``~/.hermes/auth.json`` ("Refusing to touch real user auth
    store during test run"). These live tests deliberately run the real agent as ``hermes``, so we drop that
    one marker for the child only. Everything else (HERMES_HOME, PATH, secrets) is passed through unchanged.
    """
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


def _run(*args: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([_hermes(), *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=_hermes_env())


def _ledger() -> sqlite3.Connection:
    """The live ledger, read-only (the same way the runbook reads it)."""
    return sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True)


def _last_rowid() -> int:
    with _ledger() as con:
        return con.execute("SELECT COALESCE(MAX(rowid), 0) FROM parks").fetchone()[0]


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


def test_one_shot_cli_turn_goes_through_withhuman_and_returns_the_sandbox_company_name():
    """A one-shot CLI turn is not refused: the read goes through durable-park and withHuman.

    withHuman auto-approves the read, so the tool runs and the model relays the sandbox company name. The
    ledger records the call as an ``allowed`` row whose session key is a ``local`` (CLI) one.
    """
    before = _last_rowid()
    prompt = (f"Call the tool {HERMES_TOOL} exactly once with no arguments, then reply with only the "
              "exact text the tool returned.")
    turn = _run("chat", "-Q", "-q", prompt, timeout=600)
    _assert_no_token(turn.stdout, turn.stderr)
    assert turn.returncode == 0, turn.stdout[-2000:] + turn.stderr[-2000:]
    assert "PARKED" not in turn.stdout  # an auto-approved read is never parked for a person
    assert "no chat session" not in turn.stdout, turn.stdout[-2000:]  # not refused for its surface
    assert re.search(COMPANY, turn.stdout), "the sandbox company name did not come back: " + turn.stdout[-2000:]
    with _ledger() as con:
        rows = con.execute(
            "SELECT park_id, session_key, status FROM parks WHERE rowid > ? AND tool = ? ORDER BY rowid",
            (before, HERMES_TOOL)).fetchall()
    assert rows, f"no ledger row for {HERMES_TOOL} after the CLI turn: the call did not go through durable-park"
    assert any(status == "allowed" and ":local:" in session_key for _, session_key, status in rows), rows
