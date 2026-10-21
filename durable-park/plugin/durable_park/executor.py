"""The ``parked_action_execute`` tool: run a parked call once, after a human approved it in withHuman.

The approval is single use: ``gate.check`` on the saved call *consumes* it. Everything after the consume
is decided by the ledger row (``approved -> executing -> executed|failed|expired|unknown``), never by the model.

``ctx.dispatch_tool`` goes straight to ``registry.dispatch`` and does not run ``pre_tool_call`` hooks
(hermes_cli/plugins.py:674-684, tools/registry.py:893-921), so the park hook cannot block this dispatch.

A result is ``unknown`` (never ``failed``) whenever the tool may have run: dispatch raised, an ambiguous transport
error came back (see ``_UNKNOWN_ERROR_PREFIXES``), or the ledger write failed after dispatch. ``failed`` means
the call definitely did not run, or the remote tool itself answered with an error.
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
UNKNOWN_MESSAGE = "durable-park: {park_id} outcome UNKNOWN \u2014 check {tool} in the sandbox before retrying"
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


# R15: an error result is ``unknown`` (the call may have reached the remote server and may have taken effect) only
# when it carries the transport-error signature below; everything else is ``failed`` (a definite non-run, or the
# remote tool's own answer). Hermes v0.22.2, ~/.hermes/hermes-agent:
#   unknown (error text starts with):
#     "MCP call failed"        tools/mcp_tool_handlers.py:350 (``_dispatch`` generic error, text from
#                              tools/mcp_tool_errors.py:170-174 ``MCP call failed: <Type>: ...``): timeouts
#                              (TimeoutError), transport errors, and the reconnect-teardown RuntimeError raised at
#                              :375 ("...may already have been dispatched..."), which arrives with this prefix.
#                              Also ``MCP call failed after respawning ...`` (:321-324, the retry's own failure).
#     "MCP call interrupted"   :342-343 user sent a new message while the request was in flight.
#     "Tool execution failed"  tools/registry.py:911-921: the handler itself raised (equivalent to dispatch raising).
#                              model_tools._sanitize_tool_error prefixes "[TOOL_ERROR] ", which is stripped first.
#   unknown (structured): payload ``outcome_uncertain: true`` (:252 session expired mid-write; :303 and :317 stdio
#                              subprocess died mid-call), or ``needs_reauth: true`` (:214): after a recovered 401
#                              ``_retry_once`` re-dispatches the write (:182-184, :205-211) and, if that retry raises,
#                              the same needs_reauth error comes back although the write may have landed. A
#                              first-attempt 401 is also reported unknown (the safe direction).
#   failed (nothing reached the server, or the server answered with an error):
#     trust-gate block :78,84, circuit breaker open :101,104, server not connected :114-124 and :620, transport
#     down / reconnect requested :137, stdio never respawned :306 / died again :319 (both pre-dispatch),
#     unknown tool registry.py:899, and errors returned by the remote tool (MCP isError -> tool_error(text)
#     :537-538).
#   An unrecognised error text is ``failed``; only the prefixes above are ``unknown``.
_UNKNOWN_ERROR_PREFIXES = ("MCP call failed", "MCP call interrupted", "Tool execution failed")
_TOOL_ERROR_TAG = "[TOOL_ERROR] "


def _tool_error(payload: Any) -> tuple[str, bool] | None:
    """``(one-line error text, outcome_unknown)`` when a Hermes tool result is an error, else None.

    Hermes tools report failure as a JSON object with an ``error`` key (tools/registry.py:1022 ``tool_error``,
    tools/mcp_tool_handlers.py:142 ``_result_is_error``). An empty/null value is no error.
    """
    if not isinstance(payload, dict) or payload.get("error") in (None, "", False, [], {}):
        return None
    error = payload["error"]
    text = error if isinstance(error, str) else json.dumps(error, default=str)
    bare = text.removeprefix(_TOOL_ERROR_TAG).lstrip()
    unknown = (payload.get("outcome_uncertain") is True or payload.get("needs_reauth") is True
               or bare.startswith(_UNKNOWN_ERROR_PREFIXES))
    return _one_line(text), unknown


def make_refusing_executor(message: str) -> Callable[..., str]:
    """A handler that refuses every call and touches nothing (no ledger, no gate): used while the tool policy
    check has failed. An approved park stays approved."""

    def handler(args: Any = None, **_kw: Any) -> str:
        park_id = str(args.get("park_id") or "").strip() if isinstance(args, dict) else ""
        return _refused(park_id, message)

    return handler


def make_executor(ledger: Ledger, gate_factory: Callable[[], Any], dispatch: Callable[..., Any],
                  session_key_fn: Callable[[], str] | None = None, crash_after_consume: bool = False,
                  exit_fn: Callable[[int], Any] | None = None) -> Callable[..., str]:
    """Build the tool handler. ``dispatch(tool, args)`` is ``ctx.dispatch_tool``; ``exit_fn`` is for tests."""

    def fail(row: ParkRow, note: str) -> str:
        ledger.transition(row.park_id, "executing", "failed", note=note)
        log.warning("failed %s tool=%s: %s", row.park_id, row.tool, note)
        return _reply("failed", row.park_id, message=FAILED_MESSAGE.format(park_id=row.park_id, note=note))

    def unknown(row: ParkRow, note: str, **fields: Any) -> str:
        """The call may have run: never ``failed`` (the model would offer a retry), never retried automatically."""
        ledger.transition(row.park_id, "executing", "unknown", note=note, **fields)
        log.warning("outcome unknown %s tool=%s: %s", row.park_id, row.tool, note)
        return _reply("unknown", row.park_id, message=UNKNOWN_MESSAGE.format(park_id=row.park_id, tool=row.tool))

    def execute(park_id: str, state: dict[str, Any]) -> str:
        """``state`` is per call (the handler can run on several threads): the claimed row, and whether the
        tool has been called, for the handler's catch-all."""
        row = ledger.get(park_id) if park_id else None
        if row is None:
            return _refused(park_id, NO_SUCH_MESSAGE)
        current = (session_key_fn or default_session_key)() or ""
        if not current or row.session_key != current:  # an empty key never matches, even an empty stored one
            return _refused(park_id, NOT_YOURS_MESSAGE)
        if row.status != "approved":
            return _refused(park_id, NOT_APPROVED_MESSAGE.format(park_id=park_id, status=row.status))
        if not ledger.transition(park_id, "approved", "executing", executing_at=_now()):
            return _refused(park_id, LOST_CAS_MESSAGE.format(park_id=park_id))

        # From here the row is ``executing``: every path below must leave it executed/failed/expired/unknown.
        state["row"] = row
        try:  # before the gate: corrupt saved arguments must not burn the approval
            call_args = json.loads(row.args_json)
        except ValueError:
            return fail(row, "saved arguments unreadable; nothing ran")
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
            state["dispatched"] = True  # from here on the call may have reached the remote server
            result = dispatch(row.tool, call_args)
        except Exception as exc:  # noqa: BLE001 - never put exception text (it may echo args) in notes
            return unknown(row, f"dispatch raised {type(exc).__name__}")
        payload = _decode(result)
        raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        classified = _tool_error(payload)
        if classified is not None:
            error, outcome_unknown = classified
            if outcome_unknown:
                return unknown(row, f"tool error: {error}", result_json=raw)
            ledger.transition(park_id, "executing", "failed", note=f"tool error: {error}", result_json=raw)
            log.warning("tool error for %s tool=%s", park_id, row.tool)
            return _reply("failed", park_id, message=FAILED_MESSAGE.format(
                park_id=park_id, note=f"tool error: {error}"))
        if not ledger.transition(park_id, "executing", "executed", result_json=raw):
            log.error("%s ran but the ledger row was no longer executing", park_id)
        log.info("executed %s tool=%s", park_id, row.tool)
        # The tool's own text, nested as a JSON *string*: Hermes v0.22.2 marks any tool result with "error" or
        # "failed" in its first 500 characters as a failure (agent/display.py:999-1037, _detect_tool_failure).
        # Nested, every quote inside the tool's answer is escaped (\"error\"), so a success can never trip that
        # check, and the reply itself never has an "error" key. Failures keep their "failed" status (flagged).
        return _reply("executed", park_id, result=raw)

    def handler(args: Any = None, **_kw: Any) -> str:
        park_id = str(args.get("park_id") or "").strip() if isinstance(args, dict) else ""
        state: dict[str, Any] = {"row": None, "dispatched": False}
        try:
            return execute(park_id, state)
        except Exception as exc:  # noqa: BLE001 - e.g. ledger I/O error; never raise into the agent loop
            log.error("executor failed for %s: %s", park_id or "?", type(exc).__name__)
            row = state["row"]
            if state["dispatched"] and row is not None:
                # The tool was already called, so it may have run: report UNKNOWN, never "could not run".
                try:
                    ledger.transition(park_id, "executing", "unknown", note=f"executor error after dispatch: {type(exc).__name__}")
                except Exception:  # noqa: BLE001 - the row stays executing; the reply is still correct
                    pass
                return _reply("unknown", park_id, message=UNKNOWN_MESSAGE.format(park_id=park_id, tool=row.tool))
            try:  # a claimed row must not stay executing because of our own bug
                ledger.transition(park_id, "executing", "failed", note=f"executor error: {type(exc).__name__}")
            except Exception:  # noqa: BLE001
                pass
            return _refused(park_id, INTERNAL_MESSAGE.format(park_id=park_id))

    return handler
