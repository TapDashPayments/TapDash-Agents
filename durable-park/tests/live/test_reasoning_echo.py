"""Live check: withHuman echoes ``agent_reasoning`` exactly (deploy gate for the withhuman_context lift).

Manual only, on the proto VM as the ``hermes`` user, before restarting the gateway with the lift change:

    DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_reasoning_echo.py

``build_call`` sends the agent's note as the request's reasoning, cut to 2000 UTF-16 code units (an emoji counts
as two), and the AAP SDK refuses
any answer whose ``agent_reasoning`` is not byte-for-byte what was sent (on create and on consume). This submits ONE
request for a read-only QuickBooks tool whose entry-pipeline rule ("Park: QuickBooks other") approves it instantly,
with a 2000-character reasoning that starts with spaces and holds a newline, non-ASCII text and a trailing
newline, and then a second request whose reasoning is 1000 emoji (2000 UTF-16 units, 4000 UTF-8 bytes).
``Allow`` proves the exact-match check passed. A ``Pending`` verdict means the pipeline did not approve it
at once: the request is cancelled at once and the test fails. The instance token is read only by the plugin's own
``GateHolder`` and is never printed or put in a message.
"""

from __future__ import annotations

import os
import uuid

import pytest
from agentapprovalprotocol import Allow, Call, Deny, Pending

from durable_park import DEFAULT_AAP_BASE_URL, DEFAULT_TOKEN_FILE
from durable_park.gatecall import MAX_REASONING_UNITS, GateHolder

pytestmark = pytest.mark.skipif(os.environ.get("DURABLE_PARK_LIVE") != "1",
                                reason="live withHuman check: set DURABLE_PARK_LIVE=1 on the proto VM")

BASE_URL = "https://app.withhuman.ai/api/aap"
TOKEN_FILE = "/etc/durable-park/instance_token"
SERVER = "park:quickbooks"
TOOL = "mcp__wh__quickbooks__get_company_info"


def _reasoning() -> str:
    head = "  Check the sandbox company: café — ✓ 日本\nsecond line, then padding "
    tail = "\n"
    return head + "x" * (MAX_REASONING_UNITS - len(head) - len(tail)) + tail


def _emoji_reasoning() -> str:
    return "\U0001F600" * (MAX_REASONING_UNITS // 2)


def test_the_plugin_defaults_are_the_ones_this_check_uses():
    assert (DEFAULT_AAP_BASE_URL, DEFAULT_TOKEN_FILE) == (BASE_URL, TOKEN_FILE)


def _check_echo(reasoning: str) -> None:
    gate = GateHolder(BASE_URL, TOKEN_FILE).get()
    try:
        unique = uuid.uuid4().hex
        call = Call(
            key=uuid.uuid4().hex + uuid.uuid4().hex,
            tool=TOOL,
            arguments={"params": {}},
            server=SERVER,
            reasoning=reasoning,
            context={"platform": "live-check", "session_key": f"agent:main:live-check:{unique}"},
            resume={"framework": "hermes", "park_id": f"P-LIVE-{unique[:8]}",
                    "session_key": f"agent:main:live-check:{unique}"},
        )
        verdict = gate.check(call, can_pause=True)
        if isinstance(verdict, Pending):
            gate.client.cancel(verdict.request_id)
            pytest.fail("pipeline did not approve instantly; a person may have been paged - request cancelled")
        if isinstance(verdict, Deny):
            pytest.fail(f"withHuman did not allow the call: status={verdict.status} reason={verdict.reason}")
        assert isinstance(verdict, Allow), type(verdict).__name__
    finally:
        gate.client.close()


def test_withhuman_echoes_a_2000_character_reasoning_exactly():
    reasoning = _reasoning()
    assert len(reasoning) == MAX_REASONING_UNITS == 2000
    assert reasoning.startswith("  ") and reasoning.endswith("\n") and "\n" in reasoning[:-1]
    assert not reasoning.isascii()
    _check_echo(reasoning)


def test_withhuman_echoes_a_reasoning_of_1000_emoji_exactly():
    """2000 UTF-16 code units but 1000 characters and 4000 UTF-8 bytes: the case the character cut got wrong."""
    reasoning = _emoji_reasoning()
    assert len(reasoning) == 1000 and len(reasoning.encode("utf-16-le")) // 2 == MAX_REASONING_UNITS == 2000
    assert len(reasoning.encode("utf-8")) == 4000
    _check_echo(reasoning)
