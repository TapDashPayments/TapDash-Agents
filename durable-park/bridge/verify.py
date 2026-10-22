"""The pure decision rule of the gateway bridge: is this gateway call backed by exactly one approved park?

The bridge lists withHuman's own request records (park approvals and neighbouring gateway requests) and
hands them to ``decide``. Nothing here reads a file, the network or the clock, so every rule is unit
testable and a bug can only produce a verdict, never a side effect. Anything unexpected is ``human``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from durable_park.gatecall import REDACTED, gateway_slug, redact_arguments
from durable_park.ledger import args_digest

# Hermes MCP tool naming (hermes-agent tools/mcp_tool_schema.py, v0.22.2): every char outside
# [A-Za-z0-9_] becomes "_" and names over 64 chars get a deterministic 8-hex sha256 suffix.
# withHuman sends the downstream server's own tool name (e.g. "create-bill"), so the bridge must
# build the name exactly the way Hermes registered it, or a parked call never matches.
HERMES_MCP_SERVER = "wh"
_HERMES_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_HERMES_NAME_MAX = 64
_HERMES_NAME_HASH = 8


def hermes_tool_name(server: str, tool: str) -> str:
    """The name Hermes registers for gateway tool <server>__<tool> under MCP server `wh`."""
    full = (f"mcp__{HERMES_MCP_SERVER}__{_HERMES_NAME_UNSAFE.sub('_', server)}"
            f"__{_HERMES_NAME_UNSAFE.sub('_', tool)}")
    if len(full) <= _HERMES_NAME_MAX:
        return full
    suffix = "_" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:_HERMES_NAME_HASH]
    return full[:_HERMES_NAME_MAX - len(suffix)] + suffix


def expected_park(server: str, tool: str) -> tuple[str, str]:
    """``(park_tool, park_server)``: how withHuman records the park request for gateway tool server/tool."""
    park_tool = hermes_tool_name(server, tool)
    return park_tool, "park:" + gateway_slug(park_tool)


def parse_time(value) -> datetime | None:
    """A timezone-aware datetime for an ISO 8601 string (``Z`` or an offset); None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return None if parsed.utcoffset() is None else parsed.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 - garbage in, None out; this must never raise
        return None


WINDOW_SECONDS = 120
SKEW_SECONDS = 5

# Names the bridge can map to a Hermes tool name without ambiguity (rule 1): Hermes sanitisation then only
# folds "-" into "_" and never hashes. A server is words joined by single "-" or "_"; "__" would split the
# park slug (``gateway_slug``) in the wrong place.
_SAFE_SERVER = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
_SAFE_TOOL = re.compile(r"[A-Za-z0-9_-]+")


def _names_are_safe(server, tool) -> bool:
    """Rule 1: plain-ASCII names whose unhashed Hermes tool name fits in 64 characters."""
    if not isinstance(server, str) or not isinstance(tool, str):
        return False
    if _SAFE_SERVER.fullmatch(server) is None or _SAFE_TOOL.fullmatch(tool) is None:
        return False
    unhashed = f"mcp__{HERMES_MCP_SERVER}__{server.replace('-', '_')}__{tool.replace('-', '_')}"
    return len(unhashed) <= _HERMES_NAME_MAX


@dataclass(frozen=True)
class GatewayCall:
    """The gateway request the webhook block asks about (already checked for shape by the bridge)."""
    request_id: str
    server: str
    tool: str
    arguments: dict
    agent_slug: str


@dataclass(frozen=True)
class Verdict:
    approve: bool
    why: str
    park_request_id: str | None = None


def _is_match(park: dict, call: GatewayCall, park_tool: str, park_server: str, digest: str,
              now: datetime) -> bool:
    """Rule 2: every condition must hold for ``park`` to back ``call``."""
    if park.get("agent_slug") != call.agent_slug or park.get("tool") != park_tool:
        return False
    if park.get("server") != park_server or park.get("status") != "approved":
        return False
    decision, consumption, context = park.get("decision"), park.get("consumption"), park.get("context")
    if not isinstance(decision, dict) or decision.get("status") != "approved":
        return False
    if not isinstance(consumption, dict) or not isinstance(context, dict) or context.get("args_truncated"):
        return False
    consumed = parse_time(consumption.get("consumed_at"))
    expires = parse_time(decision.get("expires_at"))
    if consumed is None or expires is None or consumed > expires:
        return False
    if consumed > now + timedelta(seconds=SKEW_SECONDS) or now - consumed > timedelta(seconds=WINDOW_SECONDS):
        return False
    arguments = park.get("arguments")
    return isinstance(arguments, dict) and args_digest(arguments) == digest


def _is_conflict(other: dict, call: GatewayCall, digest: str, consumed: datetime) -> bool:
    """Rule 4: ``other`` may be a second use of the park approval consumed at ``consumed``."""
    if other.get("id") == call.request_id:
        return False
    if other.get("agent_slug") != call.agent_slug:
        return False
    server, tool = other.get("server"), other.get("tool")
    if not isinstance(server, str) or not isinstance(tool, str) or server.startswith("park:"):
        return False  # not a gateway request we can compare (a park request is not a use of the park)
    try:
        if expected_park(server, tool) != expected_park(call.server, call.tool):
            return False  # Hermes maps this request to another park: the approval was not meant for it
    except Exception:  # noqa: BLE001 - a name we cannot map cannot be the same park
        return False
    created = parse_time(other.get("created_at"))
    if created is not None and created < consumed:
        return False
    arguments = other.get("arguments")
    return not isinstance(arguments, dict) or args_digest(arguments) == digest


def _holds_redacted(value) -> bool:
    """True when ``value`` contains the redaction placeholder anywhere (dict values, list items, nested)."""
    if isinstance(value, dict):
        return any(_holds_redacted(v) for v in value.values())
    if isinstance(value, list):
        return any(_holds_redacted(v) for v in value)
    return value == REDACTED


def _was_not_fully_visible(park: dict, call: GatewayCall) -> bool:
    """Rule 5: the approver could not have read every value of the call (it was redacted or cut short)."""
    return _holds_redacted(park["arguments"]) or redact_arguments(call.arguments)[0] != call.arguments


def _own_request_verdict(call: GatewayCall, digest: str, neighbours: list, consumed: datetime) -> Verdict | None:
    """Rule 2: the gateway request itself must be listed, created after the release, and be exactly the call."""
    listed = next((o for o in neighbours if isinstance(o, dict) and o.get("id") == call.request_id), None)
    if listed is None:
        return Verdict(False, "gateway-request-not-listed")
    created = parse_time(listed.get("created_at"))
    if created is None or created < consumed - timedelta(seconds=SKEW_SECONDS):
        return Verdict(False, "gateway-request-before-release")
    arguments = listed.get("arguments")
    if ((listed.get("agent_slug"), listed.get("server"), listed.get("tool")) !=
            (call.agent_slug, call.server, call.tool)
            or not isinstance(arguments, dict) or args_digest(arguments) != digest):
        return Verdict(False, "gateway-request-mismatch")
    return None


def _listing_is_truncated(neighbours: list, limit: int | None, consumed: datetime) -> bool:
    """Rule 3: a full listing whose oldest item is not older than the consumption may hide later requests."""
    if limit is None or len(neighbours) < limit:
        return False
    stamps = [parse_time(o.get("created_at")) if isinstance(o, dict) else None for o in neighbours]
    return any(t is None for t in stamps) or min(stamps) >= consumed


def decide(call: GatewayCall, parks: list, neighbours: list, now: datetime, *,
           neighbours_limit: int | None = None) -> Verdict:
    """Approve only when exactly one approved, consumed park backs ``call`` and nothing else used it.

    ``neighbours`` is the newest-first listing of the agent's requests; ``neighbours_limit`` is the page size it
    was asked with (a full page may be cut off). Pure and total: any unexpected exception becomes
    ``Verdict(False, "error:<ExcClass>")`` (the class name only, never the message, which could carry argument
    values).
    """
    try:
        return _decide(call, parks, neighbours, now, neighbours_limit)
    except Exception as exc:  # noqa: BLE001 - any bug must fall back to a human, never an approval
        return Verdict(False, f"error:{type(exc).__name__}")


def _decide(call: GatewayCall, parks: list, neighbours: list, now: datetime,
            neighbours_limit: int | None) -> Verdict:
    if not _names_are_safe(call.server, call.tool):
        return Verdict(False, "unsafe-name")
    park_tool, park_server = expected_park(call.server, call.tool)
    digest = args_digest(call.arguments)
    matches = [p for p in parks if isinstance(p, dict)
               and _is_match(p, call, park_tool, park_server, digest, now)]
    if not matches:
        return Verdict(False, "no-approved-park")
    if len(matches) > 1:
        return Verdict(False, "ambiguous-park")
    matched = matches[0]
    if not isinstance(matched.get("id"), str) or not matched["id"]:  # nothing to name: let a person look
        return Verdict(False, "no-approved-park")
    if _was_not_fully_visible(matched, call):
        return Verdict(False, "redacted-args")
    if matched["consumption"].get("outcome") is not None:  # the executor already reported a result for it
        return Verdict(False, "already-reported")
    consumed = parse_time(matched["consumption"]["consumed_at"])
    refusal = _own_request_verdict(call, digest, neighbours, consumed)
    if refusal is not None:
        return refusal
    if _listing_is_truncated(neighbours, neighbours_limit, consumed):
        return Verdict(False, "neighbours-truncated")
    if any(_is_conflict(o, call, digest, consumed) for o in neighbours if isinstance(o, dict)):
        return Verdict(False, "already-used")
    return Verdict(True, "park-request=" + matched["id"], matched["id"])
