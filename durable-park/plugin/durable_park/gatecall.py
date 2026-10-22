"""Wiring between ledger rows and the Agent Approval Protocol SDK gate."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from typing import Any, Callable, Mapping

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
MAX_REASONING_CHARS = 2000  # the agent's own note shown to the approver (``Call.reasoning``)
CONTEXT_FIELD = "withhuman_context"  # the gateway's top-level note field
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


def _declares_context(schema: Any) -> bool:
    props = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(props, dict) and CONTEXT_FIELD in props


MAX_SCHEMA_DEPTH = 10  # how deep ``$ref``/``allOf``/``anyOf``/``oneOf`` are followed before giving up
MAX_PATTERN_CHARS = 200  # a longer ``patternProperties`` regex is not run (it comes from outside): unknown
_UNRESOLVED = object()


def _resolve_local_ref(ref: Any, root: Any) -> Any:
    """The part of ``root`` a local JSON pointer (``#``, ``#/$defs/X``, ``~0``/``~1`` unescaped) names, or
    ``_UNRESOLVED`` for anything else (remote, relative file, not a string, no such place)."""
    if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
        return _UNRESOLVED
    node = root
    for part in ([] if ref == "#" else ref[2:].split("/")):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _UNRESOLVED
    return node


def _declares_context_in(schema: Any, root: Any, seen: frozenset = frozenset(), depth: int = 0) -> bool | None:
    """Does ``schema`` (the rules of one nested object) declare a ``withhuman_context`` property?
    True: yes. False: no. None: cannot tell (remote or unresolvable ``$ref``, a bad pattern, a cycle, too deep).
    A schema that is not an object (including plain ``true``/``false``) names no property: False. Sibling keywords
    combine the way ``allOf``/``anyOf``/``oneOf`` branches do: any True wins, then any None, else False."""
    if depth > MAX_SCHEMA_DEPTH:
        return None
    if not isinstance(schema, dict):
        return False
    found: list[bool | None] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        target = _resolve_local_ref(ref, root)
        found.append(None if target is _UNRESOLVED or ref in seen
                     else _declares_context_in(target, root, seen | {ref}, depth + 1))
    props = schema.get("properties")
    if isinstance(props, dict) and CONTEXT_FIELD in props:
        found.append(True)
    patterns = schema.get("patternProperties")
    if isinstance(patterns, dict):
        for pattern in patterns:
            try:
                if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN_CHARS:
                    found.append(None)
                elif re.search(pattern, CONTEXT_FIELD):
                    found.append(True)
            except re.error:
                found.append(None)
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword in schema:
            branches = schema[keyword]
            if not isinstance(branches, list):
                found.append(None)
                continue
            found.extend(_declares_context_in(branch, root, seen, depth + 1) for branch in branches)
    if True in found:
        return True
    return None if None in found else False


def lift_gateway_context(args: Any, schema: dict | None, on_unknown: Callable[[str], None] | None = None) -> Any:
    """Move a ``withhuman_context`` note the model nested inside an argument object up to the top level.

    The withHuman gateway's schema has ``withhuman_context`` as a sibling of the tool's own fields and strips only
    the top-level one before forwarding, so a nested copy would reach the server as an unknown parameter. Only
    tools whose ``schema`` advertises the field at the top level are touched, one level deep, in sorted key order.
    For each top-level key holding a nested ``withhuman_context``, the tool's own rules for that key decide
    (``_declares_context_in``: ``properties``, ``patternProperties``, ``allOf``/``anyOf``/``oneOf``, and local
    ``$ref`` such as ``#/$defs/X`` resolved inside ``schema``): they declare the field, so the server really has it
    and the key is left alone; they do not, so the note is removed; they cannot be judged (remote ``$ref``, a bad
    pattern, a cycle) so the key is left alone and ``on_unknown(key)`` is called (the key name only, never a
    value). The first removed non-empty string becomes the top-level note unless the top level already has the key
    (it always wins); other removed values are dropped. A JSON-string value is not parsed. Pure and idempotent: the
    input is never mutated (a deep copy is returned when anything is touched, the same object otherwise), and a
    bug while judging or reporting never raises, it counts as "cannot tell"."""
    if not isinstance(args, dict) or not _declares_context(schema):
        return args
    props = schema["properties"]
    out = copy.deepcopy(args)
    note: str | None = None
    for key in sorted(out, key=str):
        value = out[key]
        if not isinstance(value, dict) or CONTEXT_FIELD not in value:
            continue
        try:
            declared = _declares_context_in(props.get(key), schema)
        except Exception:  # noqa: BLE001 - never raise out of the lift
            declared = None
        if declared is not False:
            if declared is None and on_unknown is not None:
                try:
                    on_unknown(str(key))
                except Exception:  # noqa: BLE001 - a reporting bug must not stop the lift
                    pass
            continue
        removed = value.pop(CONTEXT_FIELD)
        if note is None and isinstance(removed, str) and removed:
            note = removed
    if note is not None and CONTEXT_FIELD not in out:
        out[CONTEXT_FIELD] = note
    return out


def build_call(row: ParkRow) -> Call:
    """The AAP call for a ledger row: a pure function of the row (the executor replays it and must send the
    identical body). Raises ``ValueError`` for arguments that are not strict JSON (e.g. NaN)."""
    stored = json.loads(row.args_json)
    arguments, truncated = redact_arguments(stored)
    json.dumps(arguments, allow_nan=False)
    note = stored.get(CONTEXT_FIELD) if isinstance(stored, dict) else None
    reasoning = note[:MAX_REASONING_CHARS] if isinstance(note, str) and note else None
    context: dict[str, Any] = {
        "args_sha256": row.args_digest, "platform": platform_of(row.session_key), "session_key": row.session_key}
    if truncated:
        context["args_truncated"] = True
    return Call(
        key=row.call_key,
        tool=row.tool,
        arguments=arguments,
        server=row.server,
        reasoning=reasoning,
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
    The token is read only when (re)building and is never logged; ``OSError`` propagates. A replaced gate's
    client (SDK ``Client.close()`` closes its httpx client) is closed one rebuild later, never at once: a call
    that took the old gate just before the change may still be using it."""

    def __init__(self, base_url: str, token_file: str):
        self._base_url = base_url
        self._token_file = token_file
        self._lock = threading.Lock()
        self._gate: Gate | None = None
        self._retired: Gate | None = None
        self._stamp: tuple[int, int] | None = None

    def get(self) -> Gate:
        with self._lock:
            st = os.stat(self._token_file)
            stamp = (st.st_mtime_ns, st.st_size)
            if self._gate is None or stamp != self._stamp:
                with open(self._token_file, encoding="utf-8") as handle:
                    token = handle.read().strip()
                new_gate = make_gate(self._base_url, token)
                if self._retired is not None:
                    try:
                        self._retired.client.close()
                    except Exception:  # noqa: BLE001 - closing an old client must never fail a call
                        pass
                self._retired, self._gate = self._gate, new_gate
                self._stamp = stamp
            return self._gate
