"""Wiring between ledger rows and the Agent Approval Protocol SDK gate."""

from __future__ import annotations

import json

from agentapprovalprotocol import Call, Client, Gate

from .ledger import ParkRow

AAP_TIMEOUT = "168h"


def build_call(row: ParkRow) -> Call:
    """The AAP call for a parked row; identical for the same row every time (key = call_key)."""
    return Call(
        key=row.call_key,
        tool=row.tool,
        arguments=json.loads(row.args_json),
        server=row.server,
        resume={"framework": "hermes", "park_id": row.park_id, "session_key": row.session_key},
    )


def make_gate(base_url: str, token: str, *, client: Client | None = None) -> Gate:
    """An async-mode gate with a 168h request timeout. ``client`` is for tests (e.g. FakeProvider.client())."""
    return Gate(client or Client(base_url, token), mode="async", timeout=AAP_TIMEOUT, report_outcomes=False)
