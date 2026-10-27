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
# folds "-" into "_" and never hashes. A server and a tool are each words joined by single "-" or "_" (the
# server's own tool-name shape): "__" would split the park slug (``gateway_slug``) in the wrong place, and the
# gateway pipeline's money rule accepts an optional ``quickbooks__`` prefix that the park rule lacks, so
# ``quickbooks__create-bill`` must never pair with a park for ``create-bill``.
_SAFE_NAME = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")


def _names_are_safe(server, tool) -> bool:
    """Rule 1: plain-ASCII names whose unhashed Hermes tool name fits in 64 characters."""
    if not isinstance(server, str) or not isinstance(tool, str):
        return False
    if _SAFE_NAME.fullmatch(server) is None or _SAFE_NAME.fullmatch(tool) is None:
        return False
    unhashed = f"mcp__{HERMES_MCP_SERVER}__{server.replace('-', '_')}__{tool.replace('-', '_')}"
    return len(unhashed) <= _HERMES_NAME_MAX


@dataclass(frozen=True)
class GatewayCall:
    """The gateway request the webhook block asks about (already checked for shape by the bridge).

    ``person`` is the withHuman agent instance that made the call: one instance per person (per-person roles,
    option R), read by the bridge from withHuman's own record of the request, never from the delivery.
    """
    request_id: str
    server: str
    tool: str
    arguments: dict
    agent_slug: str
    person: str


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
    # The same person: another person on the same role agent may send the very same call, but their call is
    # never backed by this person's approval. An empty name never counts as "the same".
    if not call.person or park.get("agent_instance_id") != call.person:
        return False
    if park.get("server") != park_server or park.get("status") != "approved":
        return False
    decision, consumption, context = park.get("decision"), park.get("consumption"), park.get("context")
    if not isinstance(decision, dict) or decision.get("status") != "approved":
        return False
    if not isinstance(consumption, dict) or not isinstance(context, dict) or context.get("args_truncated"):
        return False
    # The approval's own expiry is never read: the product listing carries no ``expires_at`` on a decision, and
    # withHuman refuses to consume an expired approval before it records ``consumed_at`` (same transaction), so a
    # parseable ``consumed_at`` is the proof. The window below is what retires an old consumed park.
    consumed = parse_time(consumption.get("consumed_at"))
    if consumed is None:
        return False
    if consumed > now + timedelta(seconds=SKEW_SECONDS) or now - consumed > timedelta(seconds=WINDOW_SECONDS):
        return False
    arguments = park.get("arguments")
    return isinstance(arguments, dict) and args_digest(arguments) == digest


def _is_conflict(other: dict, call: GatewayCall, digest: str, consumed: datetime) -> bool:
    """Rule 4: ``other`` may be a second use of the park approval consumed at ``consumed``.

    A request without a readable ``created_at``, or created less than ``SKEW_SECONDS`` before the release, counts.
    """
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
    if created is not None and created < consumed - timedelta(seconds=SKEW_SECONDS):
        return False  # clearly older than the release (both stamps come from withHuman's clock)
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
    """Rule 2: the gateway request itself must be listed, created at or after the release, and be exactly the call.

    ``created_at`` and ``consumed_at`` both come from withHuman's own clock, so there is no skew to allow for.
    """
    listed = next((o for o in neighbours if isinstance(o, dict) and o.get("id") == call.request_id), None)
    if listed is None:
        return Verdict(False, "gateway-request-not-listed")
    created = parse_time(listed.get("created_at"))
    if created is None or created < consumed:
        return Verdict(False, "gateway-request-before-release")
    arguments = listed.get("arguments")
    if ((listed.get("agent_slug"), listed.get("agent_instance_id"), listed.get("server"), listed.get("tool")) !=
            (call.agent_slug, call.person, call.server, call.tool)
            or not isinstance(arguments, dict) or args_digest(arguments) != digest):
        return Verdict(False, "gateway-request-mismatch")
    return None


def _listing_is_unsorted(neighbours: list) -> bool:
    """Rule 3a: every item needs a readable ``created_at`` and the list must run newest first (ties allowed)."""
    stamps = [parse_time(o.get("created_at")) if isinstance(o, dict) else None for o in neighbours]
    if any(t is None for t in stamps):
        return True
    return any(newer > older for older, newer in zip(stamps, stamps[1:]))


def _listing_is_truncated(neighbours: list, consumed: datetime) -> bool:
    """Rule 3b: the listing must reach back to a request created before ``consumed - SKEW_SECONDS``.

    That is normally the park itself (the same agent's request). Without one, a request made after the release
    could sit beyond a page boundary that we cannot see. Call after ``_listing_is_unsorted`` (all stamps readable).
    """
    cutoff = consumed - timedelta(seconds=SKEW_SECONDS)
    return not any(parse_time(o.get("created_at")) < cutoff for o in neighbours)


def decide(call: GatewayCall, parks: list, neighbours: list, now: datetime) -> Verdict:
    """Approve only when exactly one approved, consumed park backs ``call`` and nothing else used it.

    ``neighbours`` is the agent's requests as withHuman listed them; the rule does not trust the page size or the
    order, it checks that the list is newest first and reaches back before the release. Pure and total: any
    unexpected exception becomes ``Verdict(False, "error:<ExcClass>")`` (the class name only, never the message,
    which could carry argument values).
    """
    try:
        return _decide(call, parks, neighbours, now)
    except Exception as exc:  # noqa: BLE001 - any bug must fall back to a human, never an approval
        return Verdict(False, f"error:{type(exc).__name__}")


def _decide(call: GatewayCall, parks: list, neighbours: list, now: datetime) -> Verdict:
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
    if _listing_is_unsorted(neighbours):
        return Verdict(False, "neighbours-unsorted")
    if _listing_is_truncated(neighbours, consumed):
        return Verdict(False, "neighbours-truncated")
    refusal = _own_request_verdict(call, digest, neighbours, consumed)
    if refusal is not None:
        return refusal
    if any(_is_conflict(o, call, digest, consumed) for o in neighbours if isinstance(o, dict)):
        return Verdict(False, "already-used")
    return Verdict(True, "park-request=" + matched["id"], matched["id"])
