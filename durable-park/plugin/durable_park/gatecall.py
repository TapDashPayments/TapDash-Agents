"""Wiring between ledger rows and the Agent Approval Protocol SDK gate."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from typing import Any, Mapping

import httpx
from agentapprovalprotocol import Call, Client, Gate

from .ledger import ParkRow, canonical_json

AAP_TIMEOUT = "168h"

# The Hermes pre_tool_call hook gets ~60s in total (and a timeout blocks every tool for a while), so the
# SDK must give up well before that: 8s of retries plus at most 10s per HTTP attempt.
RETRY_BUDGET_SECONDS = 8.0
HTTP_TIMEOUT_SECONDS = 10.0

# What the approval request carries about the arguments (spec 4.2): secret-named keys are replaced, and a
# request whose canonical JSON is over 16 KiB gets every string cut to 1 KiB.
MAX_ARGS_BYTES = 16384
MAX_STRING_CHARS = 1024
REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"password|token|secret|authorization|api[_-]?key", re.IGNORECASE)
_GATEWAY_SLUG = re.compile(r"mcp__wh__(.+?)__")
_MCP_ALIAS = re.compile(r"mcp__(.+?)__")


def gateway_slug(tool_name: str) -> str:
    """``mcp__wh__quickbooks__create_payment`` -> ``quickbooks`` (empty if the name has no slug)."""
    match = _GATEWAY_SLUG.match(tool_name)
    return match.group(1) if match else ""


def server_label(tool: str) -> str:
    """``park:<slug>`` for withHuman gateway tools, ``mcp:<alias>`` for other MCP servers, else ``hermes``."""
    slug = gateway_slug(tool)
    if slug:
        return "park:" + slug
    match = _MCP_ALIAS.match(tool)
    return "mcp:" + match.group(1) if match else "hermes"


def platform_of(session_key: str) -> str:
    """Chat platform of a gateway session key (``agent:main:telegram:dm:42`` -> ``telegram``)."""
    fields = session_key.split(":")
    return fields[2] if session_key.startswith("agent:") and len(fields) >= 3 and fields[2] else "unknown"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _cut_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_STRING_CHARS]
    if isinstance(value, dict):
        return {k: _cut_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut_strings(v) for v in value]
    return value


def redact_arguments(args: Mapping) -> tuple[dict, bool]:
    """A redacted (and, when large, truncated) copy of ``args`` and whether it was truncated. Input untouched."""
    redacted = _redact(copy.deepcopy(dict(args)))
    if len(canonical_json(redacted).encode("utf-8")) > MAX_ARGS_BYTES:
        return _cut_strings(redacted), True
    return redacted, False


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
    """The AAP call for a ledger row: a pure function of the row (the executor replays it and must send the
    identical body). Raises ``ValueError`` for arguments that are not strict JSON (e.g. NaN)."""
    arguments, truncated = redact_arguments(json.loads(row.args_json))
    json.dumps(arguments, allow_nan=False)
    context: dict[str, Any] = {
        "args_sha256": row.args_digest, "platform": platform_of(row.session_key), "session_key": row.session_key}
    if truncated:
        context["args_truncated"] = True
    return Call(
        key=row.call_key,
        tool=row.tool,
        arguments=arguments,
        server=row.server,
        context=context,
        resume={"framework": "hermes", "park_id": row.park_id, "session_key": row.session_key},
    )


def make_gate(base_url: str, token: str, *, client: Client | None = None) -> Gate:
    """An async-mode gate with a 168h request timeout and bounded network waits (R13).

    ``client`` is for tests (e.g. FakeProvider.client()).
    """
    client = client or Client(base_url, token, retry_budget=RETRY_BUDGET_SECONDS, http=bounded_http())
    return Gate(client, mode="async", timeout=AAP_TIMEOUT, report_outcomes=False)


class GateHolder:
    """One shared ``Gate``, rebuilt when the token file changes (``(st_mtime_ns, st_size)``). Thread-safe.
    The token is read only when (re)building and is never logged; ``OSError`` propagates."""

    def __init__(self, base_url: str, token_file: str):
        self._base_url = base_url
        self._token_file = token_file
        self._lock = threading.Lock()
        self._gate: Gate | None = None
        self._stamp: tuple[int, int] | None = None

    def get(self) -> Gate:
        with self._lock:
            st = os.stat(self._token_file)
            stamp = (st.st_mtime_ns, st.st_size)
            if self._gate is None or stamp != self._stamp:
                with open(self._token_file, encoding="utf-8") as handle:
                    token = handle.read().strip()
                self._gate = make_gate(self._base_url, token)
                self._stamp = stamp
            return self._gate
