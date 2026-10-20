"""The ``parked_action_execute`` tool: run a parked call once, after a human approved it in withHuman.

The approval is single use: ``gate.check`` on the saved call *consumes* it. Everything after the consume
is decided by the ledger row (``approved -> executing -> executed|failed|expired``), never by the model.

``ctx.dispatch_tool`` goes straight to ``registry.dispatch`` and does not run ``pre_tool_call`` hooks
(hermes_cli/plugins.py:674-684, tools/registry.py:893-921), so the park hook cannot block this dispatch.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from agentapprovalprotocol import Allow, Deny

from .gatecall import build_call
from .ledger import Ledger, ParkRow
from .park_hook import default_session_key

log = logging.getLogger("durable_park")

EXECUTE_SCHEMA: dict[str, Any] = {
    "name": "parked_action_execute",
    "description": "Run a previously parked action after you were told it was approved.",
    "parameters": {
        "type": "object",
        "properties": {
            "park_id": {"type": "string", "description": "The park id from the PARKED message, e.g. P-1A2B3C4D."},
        },
        "required": ["park_id"],
        "additionalProperties": False,
    },
}

EXPIRED_MESSAGE = "The approval expired before it could run. Ask the user whether to request approval again."
NO_SUCH_MESSAGE = "durable-park: no such parked action"
NOT_YOURS_MESSAGE = "durable-park: not your session"
NOT_APPROVED_MESSAGE = "durable-park: {park_id} is {status}, not approved; it cannot run"
LOST_CAS_MESSAGE = "durable-park: {park_id} is already running or done"
FAILED_MESSAGE = "durable-park: {park_id} failed: {note}"
INTERNAL_MESSAGE = "durable-park: could not run {park_id}; see the gateway log"
CHECK_FAILED_NOTE = "approval check failed; nothing ran"
CRASH_EXIT_CODE = 70
_NOTE_LIMIT = 200


def crash_after_consume_enabled() -> bool:
    """Fault injection for the live crash test: die right after the approval is consumed."""
    return os.environ.get("DURABLE_PARK_CRASH_AFTER_CONSUME") == "1"


def _hard_exit(code: int) -> None:  # separate function so tests can replace it
    os._exit(code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _one_line(text: Any, limit: int = _NOTE_LIMIT) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _reply(status: str, park_id: str, **extra: Any) -> str:
    return json.dumps({"status": status, "park_id": park_id, **extra}, ensure_ascii=False, default=str)


def _refused(park_id: str, message: str) -> str:
    return _reply("refused", park_id, message=message)


def _decode(result: Any) -> Any:
    """The tool result as data: parsed JSON for a JSON string, the string itself otherwise."""
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def _tool_error(payload: Any) -> str | None:
    """Hermes tools report failure as a JSON object with an ``error`` key (tools/registry.py:1022
    ``tool_error``, tools/mcp_tool_handlers.py:142 ``_result_is_error``). An empty/null value is no error."""
    if isinstance(payload, dict) and payload.get("error") not in (None, "", False, [], {}):
        error = payload["error"]
        return _one_line(error if isinstance(error, str) else json.dumps(error, default=str))
    return None


def make_executor(ledger: Ledger, gate_factory: Callable[[], Any], dispatch: Callable[..., Any],
                  session_key_fn: Callable[[], str] | None = None, crash_after_consume: bool = False,
                  exit_fn: Callable[[int], Any] | None = None) -> Callable[..., str]:
    """Build the tool handler. ``dispatch(tool, args)`` is ``ctx.dispatch_tool``; ``exit_fn`` is for tests."""

    def fail(row: ParkRow, note: str) -> str:
        ledger.transition(row.park_id, "executing", "failed", note=note)
        log.warning("failed %s tool=%s: %s", row.park_id, row.tool, note)
        return _reply("failed", row.park_id, message=FAILED_MESSAGE.format(park_id=row.park_id, note=note))

    def execute(park_id: str) -> str:
        row = ledger.get(park_id) if park_id else None
        if row is None:
            return _refused(park_id, NO_SUCH_MESSAGE)
        if row.session_key != ((session_key_fn or default_session_key)() or ""):
            return _refused(park_id, NOT_YOURS_MESSAGE)
        if row.status != "approved":
            return _refused(park_id, NOT_APPROVED_MESSAGE.format(park_id=park_id, status=row.status))
        if not ledger.transition(park_id, "approved", "executing", executing_at=_now()):
            return _refused(park_id, LOST_CAS_MESSAGE.format(park_id=park_id))

        # From here the row is ``executing``: every path below must leave it executed/failed/expired.
        try:
            verdict = gate_factory().check(build_call(row))
        except Exception as exc:  # noqa: BLE001 - nothing ran; fail closed
            log.error("approval check raised for %s: %s", park_id, type(exc).__name__)
            return fail(row, CHECK_FAILED_NOTE)
        if isinstance(verdict, Deny):
            if verdict.status == "expired":
                ledger.transition(park_id, "executing", "expired", note="approval expired before it could run")
                log.warning("approval expired for %s tool=%s", park_id, row.tool)
                return _reply("expired", park_id, message=EXPIRED_MESSAGE)
            if verdict.status == "error":
                return fail(row, CHECK_FAILED_NOTE)
            return fail(row, f"approval refused ({_one_line(verdict.status, 40)}); nothing ran")
        if not isinstance(verdict, Allow):  # Pending or anything unexpected: the approval was not consumed
            return fail(row, CHECK_FAILED_NOTE)

        # The approval is consumed. Past this point the call is either run or recovered by hand.
        if crash_after_consume:
            (exit_fn or _hard_exit)(CRASH_EXIT_CODE)
        try:
            result = dispatch(row.tool, json.loads(row.args_json))
        except Exception as exc:  # noqa: BLE001 - never put exception text (it may echo args) in notes
            return fail(row, f"dispatch raised {type(exc).__name__}")
        payload = _decode(result)
        raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        error = _tool_error(payload)
        if error is not None:
            ledger.transition(park_id, "executing", "failed", note=f"tool error: {error}", result_json=raw)
            log.warning("tool error for %s tool=%s", park_id, row.tool)
            return _reply("failed", park_id, message=FAILED_MESSAGE.format(
                park_id=park_id, note=f"tool error: {error}"))
        if not ledger.transition(park_id, "executing", "executed", result_json=raw):
            log.error("%s ran but the ledger row was no longer executing", park_id)
        log.info("executed %s tool=%s", park_id, row.tool)
        return _reply("executed", park_id, result=payload)

    def handler(args: Any = None, **_kw: Any) -> str:
        park_id = str(args.get("park_id") or "").strip() if isinstance(args, dict) else ""
        try:
            return execute(park_id)
        except Exception as exc:  # noqa: BLE001 - e.g. ledger I/O error; never raise into the agent loop
            log.error("executor failed for %s: %s", park_id or "?", type(exc).__name__)
            try:  # a claimed row must not stay executing because of our own bug
                ledger.transition(park_id, "executing", "failed", note=f"executor error: {type(exc).__name__}")
            except Exception:  # noqa: BLE001
                pass
            return _refused(park_id, INTERNAL_MESSAGE.format(park_id=park_id))

    return handler
