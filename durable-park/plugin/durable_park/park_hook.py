"""The ``pre_tool_call`` hook: block risky tool calls, record them, open one approval request."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from agentapprovalprotocol import Allow, Pending

from .gatecall import build_call
from .ledger import Ledger, args_digest, call_key

log = logging.getLogger("durable_park")

# Regex ``fullmatch`` against the Hermes tool name (``mcp__wh__<slug>__<tool>``).
DEFAULT_RISKY_TOOLS: tuple[str, ...] = (
    r"mcp__wh__quickbooks__(create|update|delete)[_-]"
    r"(payment|bill_payment|transfer|deposit|journal_entry|refund_receipt|sales_receipt"
    r"|credit_memo|vendor_credit|purchase|invoice|bill)",
    r"mcp__wh__docusign__(createEnvelope|updateEnvelope|triggerWorkflow|cancelWorkflowInstance"
    r"|pauseNewWorkflowInstances|resumeWorkflow)",
)

PARK_MESSAGE = (
    'PARKED {park_id}: "{tool}" needs human approval in withHuman and has NOT run. '
    "Do not retry it or try another way to do it. Tell the user it is waiting for approval; "
    "you will be told here when it is decided."
)
NO_SESSION_MESSAGE = "durable-park: no chat session; risky tool refused"
NO_CALL_ID_MESSAGE = "durable-park: no tool call id; risky tool refused"
COULD_NOT_OPEN_MESSAGE = "durable-park: could not open an approval request; the call was not run"
ALREADY_WAITING_MESSAGE = (
    "durable-park: {park_id} is already waiting for approval in this chat; "
    "this call was not run. Do not retry it; wait until {park_id} is decided."
)
ID_REUSED_MESSAGE = (
    "durable-park: this tool call id was already used for a different call; the call was not run"
)
NOT_PARKED_MESSAGE = "durable-park: {park_id} is already {status}; this call was not run again"

_SLUG = re.compile(r"mcp__wh__(.+?)__")


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def gateway_slug(tool_name: str) -> str:
    """``mcp__wh__quickbooks__create_payment`` -> ``quickbooks`` (empty if the name has no slug)."""
    match = _SLUG.match(tool_name)
    return match.group(1) if match else ""


def default_session_key() -> str:
    """The gateway chat session key of the running turn; empty outside a chat."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    return get_session_env("HERMES_SESSION_KEY", "") or ""


def make_pre_tool_call(ledger: Ledger, gate_factory: Callable[[], Any], risky: list[re.Pattern],
                       session_key_fn: Callable[[], str] | None = None) -> Callable[..., dict | None]:
    """Build the hook. ``gate_factory`` is called per park so the token file is read lazily."""

    def is_risky(tool_name: str) -> bool:
        return any(p.fullmatch(tool_name) for p in risky)

    def pre_tool_call(**kw: Any) -> dict[str, str] | None:
        tool = str(kw.get("tool_name") or "")
        if not is_risky(tool):
            return None
        park_id: str | None = None
        request_opened = False
        try:
            session_key = (session_key_fn or default_session_key)() or ""
            if not session_key:
                return _block(NO_SESSION_MESSAGE)
            session_id = str(kw.get("session_id") or "")
            tool_call_id = str(kw.get("tool_call_id") or "")
            if not session_id or not tool_call_id:
                return _block(NO_CALL_ID_MESSAGE)
            args = kw.get("args")
            args = args if isinstance(args, dict) else {}

            def other_active(*, earlier_than: str | None = None):
                for row in ledger.active_for_session(session_key):  # oldest first
                    if row.park_id == earlier_than:
                        return None  # only rows created before ours count
                    if (row.session_id, row.tool_call_id) != (session_id, tool_call_id):
                        return row
                return None

            if (busy := other_active()) is not None:
                return _block(ALREADY_WAITING_MESSAGE.format(park_id=busy.park_id))

            row = ledger.park(
                session_key=session_key, session_id=session_id, tool_call_id=tool_call_id, tool=tool,
                args=args, call_key=call_key(session_id, tool_call_id), server="park:" + gateway_slug(tool))
            park_id = row.park_id
            if row.tool != tool or row.args_digest != args_digest(args):
                # Providers may reuse a tool_call_id; never answer for a different call with the old park.
                log.warning("tool call id reused for a different call: %s tool=%s", row.park_id, tool)
                park_id = None  # the existing row belongs to the earlier call; leave it alone
                return _block(ID_REUSED_MESSAGE)
            if row.status != "parked":
                return _block(NOT_PARKED_MESSAGE.format(park_id=row.park_id, status=row.status))
            if row.request_id:
                return _block(PARK_MESSAGE.format(park_id=row.park_id, tool=tool))

            # Two risky calls in one turn can both pass the check above; the earlier row wins.
            if (earlier := other_active(earlier_than=row.park_id)) is not None:
                ledger.transition(row.park_id, "parked", "cancelled", note="superseded by earlier park")
                park_id = None
                return _block(ALREADY_WAITING_MESSAGE.format(park_id=earlier.park_id))

            verdict = gate_factory().check(build_call(row))
            if isinstance(verdict, Pending):
                ledger.set_request_id(row.park_id, verdict.request_id)
                request_opened = True
                log.info("parked %s tool=%s", row.park_id, tool)
                return _block(PARK_MESSAGE.format(park_id=row.park_id, tool=tool))
            # Deny (any status, including "error") or an unexpected Allow: fail closed.
            reason = getattr(verdict, "status", "allow" if isinstance(verdict, Allow) else "unknown")
            log.warning("could not open approval request for %s tool=%s (%s)", row.park_id, tool, reason)
            ledger.transition(row.park_id, "parked", "cancelled", note=f"approval request not opened: {reason}")
            park_id = None
            return _block(COULD_NOT_OPEN_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - a bug must never let a risky call through
            log.error("park hook failed for tool=%s: %s", tool, type(exc).__name__)
            if park_id is not None and not request_opened:
                try:
                    ledger.transition(park_id, "parked", "cancelled", note=f"park hook error: {type(exc).__name__}")
                except Exception:  # noqa: BLE001
                    pass
            return _block(COULD_NOT_OPEN_MESSAGE)

    return pre_tool_call


def fail_closed_hook(risky: list[re.Pattern]) -> Callable[..., dict | None]:
    """Hook used when the ledger is unusable: every risky call is refused."""

    def pre_tool_call(**kw: Any) -> dict[str, str] | None:
        tool = str(kw.get("tool_name") or "")
        return _block(COULD_NOT_OPEN_MESSAGE) if any(p.fullmatch(tool) for p in risky) else None

    return pre_tool_call
