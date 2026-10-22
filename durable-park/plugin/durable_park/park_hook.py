"""The ``pre_tool_call`` hook: ask withHuman about every tool call, record it, run or refuse or park it.

Every tool call except the exempt ones gets one ledger row and one Agent Approval Protocol request. An
instant ``Allow`` lets the tool run (row ``allowed``), an instant ``Deny`` refuses it (row ``refused``), and
``Pending`` parks it until a person decides (row ``parked``; the executor runs it later). Every path except
``Allow`` blocks the call; any error blocks it too.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Callable, Iterable

from agentapprovalprotocol import Allow, Deny, Pending

from .breaker import Breaker
from . import formcheck
from .formcheck import FormChecker, FormCheckUnavailable
from .gatecall import (
    CONTEXT_FIELD, LiftStatus, build_call, declares_context, find_leftover_notes, gateway_slug, is_note_key,
    note_is_cut,
    lift_gateway_context, server_label, text_form_note_keys)
from .ledger import Ledger, args_digest, call_key
from .wake import one_line

log = logging.getLogger("durable_park")

# Hermes "Tool Search" (tools.tool_search.enabled: on) hides MCP and plugin tools behind three bridge tools
# (Hermes v0.22.3 tools/tool_search_catalog.py ``BRIDGE_TOOL_NAMES``; they are synthesized, never registered).
# ``tool_search`` and ``tool_describe`` only read the session's own tool catalog: nothing runs, so nobody is asked.
# ``tool_call`` runs a hidden tool and is NEVER exempt: Hermes unwraps a valid single-entry call before this hook
# runs, so the hook judges the real tool; a ``tool_call`` that still reaches the hook is refused (see
# ``BRIDGE_CALL_MESSAGE``). deploy/hermes_tool_policy.py keeps its own copy of the three names (it runs
# standalone in Hermes's Python); tests/test_tool_policy.py pins the two together.
BRIDGE_SEARCH_TOOL = "tool_search"
BRIDGE_DESCRIBE_TOOL = "tool_describe"
BRIDGE_CALL_TOOL = "tool_call"
BRIDGE_READ_TOOLS: tuple[str, ...] = (BRIDGE_SEARCH_TOOL, BRIDGE_DESCRIBE_TOOL)
BRIDGE_TOOLS: tuple[str, ...] = (*BRIDGE_READ_TOOLS, BRIDGE_CALL_TOOL)

# Tools that are never asked about: the executor of an approved park (its call was approved already),
# ``clarify`` (asking the user a question) and the two read-only Tool Search catalog tools.
DEFAULT_EXEMPT_TOOLS: tuple[str, ...] = ("parked_action_execute", "clarify", *BRIDGE_READ_TOOLS)

# Tools whose arguments are checked against the tool's own rules before withHuman is asked (regex
# ``fullmatch`` against the Hermes tool name ``mcp__wh__<slug>__<tool>``). Which calls need a person is
# withHuman's pipeline rule now, not this list.
DEFAULT_FORM_CHECKED_TOOLS: tuple[str, ...] = (
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
NO_SESSION_MESSAGE = "durable-park: no chat session; the tool call was refused"
NO_CALL_ID_MESSAGE = "durable-park: no tool call id; the tool call was refused"
REFUSED_MESSAGE = (
    'durable-park: "{tool}" was refused by withHuman policy{note}. It did not run. '
    "Do not retry it or work around it; tell the user."
)
UNREACHABLE_MESSAGE = 'durable-park: withHuman could not be reached; "{tool}" did not run. Try again later.'
FOREIGN_HOOK_MESSAGE = (
    "durable-park: another plugin hook could change tool arguments after approval ({names}); every tool call "
    "is refused until an admin removes it or lists it in observer_hooks."
)
COULD_NOT_OPEN_MESSAGE = "durable-park: could not open an approval request; the call was not run"
BRIDGE_CALL_MESSAGE = (
    'durable-park: "tool_call" was NOT run: it reached the approval check still wrapped, so the tool it names '
    "could not be judged. Send exactly one entry, {\"calls\": [{\"name\": <tool>, \"arguments\": {...}}]}, for a "
    "tool that tool_search lists; a tool already in your tool list is called directly, not through tool_call."
)
ALREADY_WAITING_MESSAGE = (
    "durable-park: {park_id} is already waiting for approval in this chat; "
    "this call was not run. Do not retry it; wait until {park_id} is decided."
)
ID_REUSED_MESSAGE = (
    "durable-park: this tool call id was already used for a different call; the call was not run"
)
RULES_CHANGED_MESSAGE = (
    'durable-park: the rules for "{tool}" changed since this call was first checked, so it no longer matches the '
    "call that was parked; the call was not run."
)
NOT_PARKED_MESSAGE = "durable-park: {park_id} is already {status}; this call was not run again"
INVALID_ARGS_MESSAGE = (
    'durable-park: "{tool}" was NOT run and no approval was requested: its arguments break the tool\'s '
    'rules ({problem}). Fix the arguments and call "{tool}" again.'
)
CHECK_UNAVAILABLE_MESSAGE = (
    'durable-park: the arguments of "{tool}" could not be checked against the tool\'s rules; '
    "the call was not run and no approval was requested"
)



def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _split_notes(value: Any, notes: list[str], depth: int = 0) -> Any:
    """``value`` without any ``withhuman_context`` key (any spelling, any depth); the removed values go to ``notes``."""
    if depth > 100:
        raise ValueError("too deep")
    if isinstance(value, dict):
        kept = {}
        for key, child in value.items():
            if is_note_key(key):
                notes.append(repr(child))
            else:
                kept[key] = _split_notes(child, notes, depth + 1)
        return kept
    if isinstance(value, list):
        return [_split_notes(child, notes, depth + 1) for child in value]
    return value


def _same_call_apart_from_the_note(row: Any, original_args: Any) -> bool:
    """Is the stored row the same call as ``original_args`` except for where the note sits? That is what a replay
    looks like when the tool's rules were readable on one attempt and not on the other. Never raises."""
    try:
        if row.args_digest == args_digest(original_args):
            return True
        stored_notes: list[str] = []
        current_notes: list[str] = []
        stored = _split_notes(json.loads(row.args_json), stored_notes)
        current = _split_notes(original_args, current_notes)
        return stored == current and sorted(stored_notes) == sorted(current_notes)
    except Exception:  # noqa: BLE001
        return False


def default_session_key() -> str:
    """The gateway chat session key of the running turn; empty outside a chat."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    return get_session_env("HERMES_SESSION_KEY", "") or ""


# The SDK's denial text puts the reviewer's note after this marker (agentapprovalprotocol/_gate.py ``_settled``).
_NOTE_MARKER = "Their note: "


def _reviewer_note(reason: str) -> str:
    """The reviewer's note, flattened to one bounded line (the same treatment the wake gives notes)."""
    return one_line(reason.split(_NOTE_MARKER, 1)[1]) if _NOTE_MARKER in reason else ""


def _policy_note(note: str) -> str:
    """Who decided an instant refusal. withHuman answered while the request was being created, so no person had
    time to look at it: a pipeline rule decided. The AAP wire exposes no decider or channel (``decision`` holds only
    ``status``, ``note``, ``decided_at``, ``expires_at``), so the text says "policy" and passes the note on."""
    return "refused by withHuman policy" + (f": {note}" if note else "")


def _cancel_request(gate: Any, request_id: str, park_id: str) -> None:
    """Withdraw a request at withHuman so nobody approves a call that will never run. Never raises."""
    try:
        gate.client.cancel(request_id)
    except Exception as exc:  # noqa: BLE001 - the call is blocked either way
        log.error("could not cancel request for %s (%s)", park_id, type(exc).__name__)


def make_pre_tool_call(ledger: Ledger, gate_factory: Callable[[], Any], form_checked: list[re.Pattern], *,
                       exempt: Iterable[str] = DEFAULT_EXEMPT_TOOLS, breaker: Breaker | None = None,
                       foreign_hooks_fn: Callable[[], list[str]] | None = None,
                       session_key_fn: Callable[[], str] | None = None,
                       form_checker: FormChecker | None = None,
                       schema_fn: Callable[[str], dict | None] | None = None,
                       coerce_fn: Callable[[str, dict], dict] | None = None,
                       lift_status: LiftStatus | None = None,
                       lift_status_path: str | None = None) -> Callable[..., dict | None]:
    """Build the hook. ``gate_factory`` is called per call (e.g. ``GateHolder.get``). ``form_checker``
    validates a form-checked call's arguments against the tool's rules before anything is recorded or asked
    (default: the rules Hermes registered for the tool). ``breaker`` (optional) stops asking withHuman while
    it is unreachable. ``foreign_hooks_fn`` (optional) names other plugins' ``pre_tool_call`` hooks that could
    change arguments after approval; while it names any, every non-exempt call is refused. ``schema_fn`` looks up
    a tool's registered input rules (default: ``formcheck.registry_schema``, looked up when called);
    it decides whether a nested ``withhuman_context``
    note is lifted to the top level (see ``lift_gateway_context``). ``coerce_fn`` is Hermes's own type fixing
    (default: ``formcheck.hermes_coerce``, looked up when called), run first on a copy only when a string argument
    holds a JSON object with the note. Every case where the note was not moved, was dropped, or the check could not
    run is reported to ``lift_status`` (a ``LiftStatus``, or one built for ``lift_status_path``:
    ``lift_status.json``; the sweep alerts on it) once the call has passed its form check (a refused call never
    reaches a server), and the call goes on: with the lifted arguments, or the original ones when the lift could not
    run."""
    checker = form_checker if form_checker is not None else FormChecker()
    exempt_names = frozenset(exempt) - {BRIDGE_CALL_TOOL}  # tool_call is never exempt, whatever the caller lists

    status = lift_status if lift_status is not None else LiftStatus(lift_status_path)

    def report_leftovers(args: Any, declared: set[str], note: Callable[..., None]) -> None:
        """Note every place a note is still inside ``args`` (key names only) and an incomplete walk."""
        hits, incomplete = find_leftover_notes(args, declared)
        for path in hits:
            note("note_left_inside", path)
        if incomplete:
            note("check_incomplete")

    def lift_nested_context(tool_name: str, args: Any, problems: list[tuple[str, str, str]]) -> Any:
        """The call's arguments with a nested ``withhuman_context`` lifted to the top level. Every way the note is
        not moved, is dropped, or the check cannot run is appended to ``problems`` (the caller records them for the
        sweep's chat alert once the call has passed its form check); the original arguments are used when the lift
        cannot run, so approval is unaffected. Never raises."""

        def note(kind: str, key: str = "", detail: str = "") -> None:
            problems.append((kind, key, detail))

        if not isinstance(args, dict):
            return args
        try:
            try:
                schema = (schema_fn or formcheck.registry_schema)(tool_name)
            except Exception as exc:  # noqa: BLE001 - the lift is a cleanup, never a gate
                log.warning("could not lift withhuman_context for tool=%s (%s)", tool_name, type(exc).__name__)
                schema = None
            if not declares_context(schema):
                # Not a gateway tool: nothing to do. A gateway tool whose rules are missing or do not show the field
                # is reported, and its arguments are still searched for a note.
                if gateway_slug(tool_name):
                    if schema is None:
                        note("schema_unavailable")
                    report_leftovers(args, set(), note)
                return args
            work = dict(args)
            text_keys = text_form_note_keys(args)
            if text_keys:
                # pre_tool_call fires before Hermes's type coercion: do what dispatch will do, on a copy, and take
                # from it ONLY the values of the keys whose original value is JSON text holding a note.
                try:
                    coerced = (coerce_fn or formcheck.hermes_coerce)(tool_name, copy.deepcopy(args))
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not read arguments to lift withhuman_context for tool=%s (%s)",
                                tool_name, type(exc).__name__)
                    coerced = None
                for key in text_keys:
                    value = coerced.get(key) if isinstance(coerced, dict) else None
                    if isinstance(value, dict):
                        work[key] = value
                    else:
                        note("args_unreadable", key)
            declared: set[str] = set()

            def on_problem(kind: str, key: str, detail: str = "") -> None:
                if kind == "declared":
                    declared.add(f"{key}.{CONTEXT_FIELD}")
                note(kind, key, detail)

            mark = len(problems)
            lifted = lift_gateway_context(work, schema, on_problem=on_problem)
            if {**args, **lifted} != lifted or not set(lifted) <= set(args) | {CONTEXT_FIELD}:
                # Hermes merges a modify into the original arguments and can neither delete nor rename a key:
                # unless that merge is exactly the lifted call (and the lift added no key but the note), the lift
                # is not safe to use.
                del problems[mark:]  # what the abandoned lift found (dropped, declared) did not happen
                note("lift_error")
                return args
            report_leftovers(lifted, declared, note)
            return lifted
        except Exception as exc:  # noqa: BLE001
            log.warning("could not lift withhuman_context for tool=%s (%s)", tool_name, type(exc).__name__)
            note("lift_error")
            return args

    def is_form_checked(tool_name: str) -> bool:
        return any(p.fullmatch(tool_name) for p in form_checked)

    def pre_tool_call(**kw: Any) -> dict[str, Any] | None:
        tool = str(kw.get("tool_name") or "")
        # Step 0: Hermes unwraps a valid single-entry ``tool_call`` before this hook runs, so the hook judges the
        # real tool. A ``tool_call`` that still arrives here is a wrapper Hermes could not resolve (a batch, bad
        # JSON, a core or connector name): the tool it names cannot be judged, so it is refused. Nothing is
        # recorded or asked.
        if tool == BRIDGE_CALL_TOOL:
            log.warning("refused %s: it reached the park hook still wrapped", tool)
            return _block(BRIDGE_CALL_MESSAGE)
        # Step 1: exempt tools are never asked about.
        if tool in exempt_names:
            return None
        park_id: str | None = None
        request_opened = False  # the request id is attached to the row (the row now owns the request)
        gate: Any = None
        pending_rid: str | None = None  # a request withHuman holds open for a person, not attached yet
        try:
            # Step 2: a foreign hook could change the arguments after approval: refuse everything.
            foreign = list(foreign_hooks_fn()) if foreign_hooks_fn is not None else []
            if foreign:
                names = ", ".join(foreign)
                log.error("foreign pre_tool_call hook(s) present (%s); refusing tool=%s", names, tool)
                return _block(FOREIGN_HOOK_MESSAGE.format(names=names))

            # Step 3: a call must belong to a chat and carry an id.
            session_key = (session_key_fn or default_session_key)() or ""
            if not session_key:
                return _block(NO_SESSION_MESSAGE)
            session_id = str(kw.get("session_id") or "")
            tool_call_id = str(kw.get("tool_call_id") or "")
            if not session_id or not tool_call_id:
                return _block(NO_CALL_ID_MESSAGE)
            raw_args = kw.get("args")
            args = {} if raw_args is None else raw_args
            # From here on the lifted arguments are THE arguments: what the form check sees, what the ledger digests
            # and stores, what withHuman is asked about, and (via ``modify`` below) what Hermes dispatches.
            original_args = args
            lift_problems: list[tuple[str, str, str]] = []
            args = lift_nested_context(tool, args, lift_problems)
            if isinstance(args, dict) and CONTEXT_FIELD in args:
                # What the approver will see as the reason (``build_call`` shows it, cut): said when it is no
                # reason at all or is shortened, so the approver never silently sees less. Kinds only, no text.
                top_note = args[CONTEXT_FIELD]
                if not isinstance(top_note, str) or not top_note.strip():
                    lift_problems.append(("note_not_text", "", ""))
                elif note_is_cut(top_note):
                    lift_problems.append(("note_cut", "", ""))

            # Step 4: never ask about a call the server would reject: check the form first (form-checked
            # tools only). A replay of a call that is already recorded passed this check then and is not
            # checked again, so a later rules hiccup can never say "no approval was requested" for it.
            if is_form_checked(tool) and isinstance(args, dict) and ledger.get_by_call(session_id, tool_call_id) is None:
                try:
                    problem = checker.check(tool, args)
                except FormCheckUnavailable as exc:
                    log.error("form check unavailable for tool=%s: %s", tool, exc)
                    return _block(CHECK_UNAVAILABLE_MESSAGE.format(tool=tool))
                if problem is not None:
                    log.info("refused tool=%s: arguments break its rules", tool)
                    return _block(INVALID_ARGS_MESSAGE.format(tool=tool, problem=problem))
            # The call passed its form check: whatever the lift could not do is now real (a refused call never
            # reaches a server), so report it for the sweep's chat alert.
            status.record_many(tool, lift_problems)  # one write attempt for the whole call

            # Step 5: one row per (session_id, tool_call_id); a replay gets the existing row.
            row = ledger.park(
                session_key=session_key, session_id=session_id, tool_call_id=tool_call_id, tool=tool,
                args=args, call_key=call_key(session_id, tool_call_id), server=server_label(tool))
            park_id = row.park_id
            if row.tool != tool or row.args_digest != args_digest(args):
                # Providers may reuse a tool_call_id; never answer for a different call with the old row.
                park_id = None  # the existing row belongs to the earlier call; leave it alone
                if row.tool == tool and _same_call_apart_from_the_note(row, original_args):
                    # The same call, but the lift came out differently than the first time (the tool's rules could
                    # be read on one attempt and not on the other): fail closed, and say what happened.
                    log.warning("lift result changed on replay: %s tool=%s", row.park_id, tool)
                    return _block(RULES_CHANGED_MESSAGE.format(tool=tool))
                log.warning("tool call id reused for a different call: %s tool=%s", row.park_id, tool)
                return _block(ID_REUSED_MESSAGE)
            if row.status != "parked":
                # Decided already (including an instant allowed/refused): never run or ask again.
                park_id = None
                return _block(NOT_PARKED_MESSAGE.format(park_id=row.park_id, status=row.status))
            if row.request_id:
                return _block(PARK_MESSAGE.format(park_id=row.park_id, tool=tool))
            if not isinstance(args, dict):
                # Asking about a stand-in ({}) while the real value would run is not allowed: refuse it.
                log.info("refused %s tool=%s: arguments are not an object", row.park_id, tool)
                ledger.transition(row.park_id, "parked", "cancelled", note="arguments are not an object")
                park_id = None
                return _block(INVALID_ARGS_MESSAGE.format(tool=tool, problem="arguments must be an object"))

            # Step 6: withHuman is known to be down: refuse at once, without a request.
            if breaker is not None and not breaker.allow_attempt():
                log.warning("refused %s tool=%s: withHuman unreachable (breaker open)", row.park_id, tool)
                ledger.transition(row.park_id, "parked", "refused", note="error: withHuman unreachable (breaker open)")
                park_id = None
                return _block(UNREACHABLE_MESSAGE.format(tool=tool))

            # Step 7: ask withHuman once.
            gate = gate_factory()
            verdict = gate.check(build_call(row), can_pause=True)

            # Step 8: instant approval, already consumed by the SDK: record it, then let the tool run.
            if isinstance(verdict, Allow):
                if breaker is not None:
                    breaker.record_success()
                if not ledger.transition(row.park_id, "parked", "allowed", request_id=verdict.request_id,
                                         note="withHuman: approved"):
                    # The row left `parked` meanwhile: the consumed approval is spent and nothing runs.
                    log.warning("approval for %s could not be recorded; the call was not run", row.park_id)
                    park_id = None
                    return _block(COULD_NOT_OPEN_MESSAGE)
                park_id = None
                log.info("allowed %s tool=%s", row.park_id, tool)
                # Hermes shallow-merges ``args`` into the call: the lifted note is added, ``params`` is replaced.
                return {"action": "modify", "args": args} if args != original_args else None

            # Step 9: instant refusal (policy) or an error reaching withHuman.
            if isinstance(verdict, Deny):
                unreachable = verdict.status == "error"
                if breaker is not None:
                    if unreachable:
                        breaker.record_failure()
                    else:
                        breaker.record_success()
                if unreachable and verdict.request_id:
                    # The request exists but the SDK could not finish: withdraw it so it does not wait for a person.
                    _cancel_request(gate, verdict.request_id, row.park_id)
                note = _reviewer_note(verdict.reason)
                if verdict.status == "denied":  # the SDK's generic "A reviewer ... denied" would be false here
                    ledger_note = f"withHuman denied: {_policy_note(note)}"
                else:
                    ledger_note = f"withHuman {verdict.status}: {verdict.reason}"
                if not ledger.transition(row.park_id, "parked", "refused", request_id=verdict.request_id,
                                         note=ledger_note):
                    log.warning("refused %s could not be recorded (the row left parked); the call was not run",
                                row.park_id)
                park_id = None
                log.info("refused %s tool=%s (withHuman %s)", row.park_id, tool, verdict.status)
                if unreachable:
                    return _block(UNREACHABLE_MESSAGE.format(tool=tool))
                return _block(REFUSED_MESSAGE.format(tool=tool, note=f": {note}" if note else ""))

            if not isinstance(verdict, Pending):  # not a verdict the SDK defines: fail closed (step 11)
                raise TypeError(f"unexpected verdict {type(verdict).__name__}")

            # Step 10: a person must decide. One waiting park per chat: another row that is waiting for or
            # past a decision (a parked row counts only once its request is attached, D11) wins.
            if breaker is not None:
                breaker.record_success()
            rid = pending_rid = verdict.request_id
            busy = next((other for other in ledger.active_for_session(session_key)
                         if other.park_id != row.park_id
                         and (other.status in ("approved", "executing") or other.request_id)), None)
            if busy is not None:
                _cancel_request(gate, rid, row.park_id)
                pending_rid = None
                ledger.transition(row.park_id, "parked", "cancelled",
                                  note=f"already waiting for {busy.park_id}; request {rid} cancelled")
                park_id = None
                return _block(ALREADY_WAITING_MESSAGE.format(park_id=busy.park_id))
            if not ledger.set_request_id(row.park_id, rid):
                # The row left `parked` (the sweep cancelled it) while the request was being opened:
                # withdraw the request so nobody is asked to approve a call that will never run.
                log.warning("approval request for %s could not be attached; cancelling it", row.park_id)
                _cancel_request(gate, rid, row.park_id)
                pending_rid = None
                park_id = None
                return _block(COULD_NOT_OPEN_MESSAGE)
            request_opened = True
            pending_rid = None
            log.info("parked %s tool=%s", row.park_id, tool)
            return _block(PARK_MESSAGE.format(park_id=row.park_id, tool=tool))
        except Exception as exc:  # noqa: BLE001 - step 11: a bug must never let a call through
            log.error("park hook failed for tool=%s: %s", tool, type(exc).__name__)
            if pending_rid is not None and gate is not None:
                # withHuman holds a request the row never got: withdraw it, nobody may approve a call that won't run.
                _cancel_request(gate, pending_rid, park_id or "?")
            if park_id is not None and not request_opened:
                try:
                    ledger.transition(park_id, "parked", "cancelled", note=f"park hook error: {type(exc).__name__}")
                except Exception:  # noqa: BLE001
                    pass
            return _block(COULD_NOT_OPEN_MESSAGE)

    return pre_tool_call


def fail_closed_hook(exempt: Iterable[str], message: str = COULD_NOT_OPEN_MESSAGE) -> Callable[..., dict | None]:
    """Hook used when durable-park cannot work (e.g. the ledger is unusable): every non-exempt call is refused."""
    exempt_names = frozenset(exempt) - {BRIDGE_CALL_TOOL}

    def pre_tool_call(**kw: Any) -> dict[str, str] | None:
        tool = str(kw.get("tool_name") or "")
        return None if tool in exempt_names else _block(message)

    return pre_tool_call
