"""Wiring between ledger rows and the Agent Approval Protocol SDK gate."""

from __future__ import annotations

import json

import httpx
from agentapprovalprotocol import Call, Client, Gate

from .ledger import ParkRow

AAP_TIMEOUT = "168h"

# The Hermes pre_tool_call hook gets ~60s in total (and a timeout blocks every tool for a while), so the
# SDK must give up well before that: 8s of retries plus at most 10s per HTTP attempt.
RETRY_BUDGET_SECONDS = 8.0
HTTP_TIMEOUT_SECONDS = 10.0


class _BoundedTransport(httpx.BaseTransport):
    """Clamps every request's timeouts. The SDK passes its own per-request timeout (40s, or wait+10s for
    polls), which overrides the httpx client default, so only a transport can enforce an upper bound."""

    def __init__(self, inner: httpx.BaseTransport, limit: float = HTTP_TIMEOUT_SECONDS):
        self._inner = inner
        self._limit = limit

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        timeouts = request.extensions.get("timeout") or {}
        request.extensions["timeout"] = {
            key: (self._limit if value is None else min(value, self._limit)) for key, value in timeouts.items()
        } or {key: self._limit for key in ("connect", "read", "write", "pool")}
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def bounded_http(inner: httpx.BaseTransport | None = None) -> httpx.Client:
    """The httpx client for the AAP SDK: 10s timeouts (clamped per request), no redirects."""
    return httpx.Client(
        transport=_BoundedTransport(inner or httpx.HTTPTransport()),
        timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)


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
    """An async-mode gate with a 168h request timeout and bounded network waits (R13).

    ``client`` is for tests (e.g. FakeProvider.client()).
    """
    client = client or Client(base_url, token, retry_budget=RETRY_BUDGET_SECONDS, http=bounded_http())
    return Gate(client, mode="async", timeout=AAP_TIMEOUT, report_outcomes=False)
