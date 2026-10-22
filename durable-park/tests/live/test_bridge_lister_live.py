"""Live check: the bridge's withHuman lister works with the organization key on the proto VM.

Manual only, after the bridge change is deployed (docs/runbook.md "Check the bridge"):

    DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_bridge_lister_live.py

Run as the ``hermes`` user (it reads ``/etc/durable-park/audit_key``, 0640 root:hermes). It calls
``GET /api/v1/requests`` once for agent ``proto-agents`` and checks the answer is a list: that proves the key is
present, the network path works and the key has ``request.read`` (a 403 is a ``ListError``). It never prints or
asserts on the key or on any request's arguments.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DURABLE_PARK_LIVE") != "1",
                                reason="live bridge lister check: set DURABLE_PARK_LIVE=1 on the proto VM")

whapi = pytest.importorskip("bridge.whapi")


def test_the_org_key_lists_requests_for_the_agent():
    assert Path(whapi.DEFAULT_KEY_FILE).is_file(), f"{whapi.DEFAULT_KEY_FILE} missing: store the org key first"
    lister = whapi.RequestLister(whapi.DEFAULT_API_BASE, whapi.DEFAULT_KEY_FILE)
    try:
        found = lister.list(agent_slug="proto-agents", q="mcp__wh__", limit=5)
    except whapi.ListError as error:
        pytest.fail(f"withHuman list failed with code {error.code!r} (403 = the key lacks request.read)")
    assert isinstance(found, list)
    assert len(found) <= 5
