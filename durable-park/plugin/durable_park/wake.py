"""Wake handling: the ``pre_gateway_dispatch`` hook body.

A decision in withHuman reaches the gateway as a signed POST to the local webhook route
``durable-park-wake`` (see ``receiver/wake_post.py``). Hermes turns that into a MessageEvent from
``webhook/durable-park-wake``; this hook intercepts it BEFORE auth and dispatch, reads the decision from
the provider, updates the ledger and injects one message into the parked chat. The wake itself is always
dropped, so it never becomes an agent turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Callable

import httpx
from agentapprovalprotocol import AsyncClient

from .gatecall import HTTP_TIMEOUT_SECONDS, RETRY_BUDGET_SECONDS
from .ledger import Ledger, ParkRow

log = logging.getLogger("durable_park")

WAKE_ROUTE = "durable-park-wake"
WAKE_CHAT_NAME = f"webhook/{WAKE_ROUTE}"  # gateway/platforms/webhook.py: chat_name=f"webhook/{route_name}"
SKIP_ACTION = {"action": "skip", "reason": "durable-park wake"}
# pre_gateway_dispatch is NOT bounded by plugins.hook_callback_timeout, so bound the provider read ourselves.
WAKE_TIMEOUT_SECONDS = 30.0
MAX_NOTE_CHARS = 300
# The decider lookup (withHuman audit log) is optional garnish: bounded on its own, fails soft.
DECIDER_TIMEOUT_SECONDS = 5.0
MAX_DECIDER_CHARS = 80
AUDIT_PATH = "/api/v1/audit"
_logged_no_audit_key = False

# Statuses a parked row can move to; for these the chat is told (once, tracked by notified_at).
_DECIDED = ("approved", "denied", "expired", "cancelled")

APPROVED_TEXT = (
    '[durable-park] {park_id} was APPROVED in withHuman{by}. Call parked_action_execute with park_id "{park_id}" '
    'now to run it exactly as saved. Do not call "{tool}" directly.')
DENIED_TEXT = (
    '[durable-park] {park_id} ("{tool}") was {status} in withHuman{by}{note}. It did not run. '
    "Tell the user and continue without it.")
EXPIRED_TEXT = (
    '[durable-park] {park_id} ("{tool}") got no decision before its deadline. It did not run. '
    "Ask the user if they want to request approval again.")


def is_wake_event(event: Any) -> bool:
    """True for an event the webhook platform built for our route (a chat merely *titled* like it is not)."""
    source = getattr(event, "source", None)
    if source is None or getattr(source, "chat_name", None) != WAKE_CHAT_NAME:
        return False
    platform = getattr(source, "platform", None)
    return getattr(platform, "value", platform) == "webhook"


def one_line(note: str | None) -> str:
    """A reviewer note as one line of at most ``MAX_NOTE_CHARS`` (also used by the park hook's refusal)."""
    return " ".join((note or "").split())[:MAX_NOTE_CHARS]



def clean_decider(name: str | None) -> str:
    """A decider's name as one printable line of at most ``MAX_DECIDER_CHARS`` ("" when nothing is left)."""
    printable = "".join(ch if ch.isprintable() else " " for ch in (name or ""))
    return " ".join(printable.split())[:MAX_DECIDER_CHARS].strip()


def inject_text(row: ParkRow) -> str | None:
    """The message for a decided row, or None when the chat is not told about this status."""
    decider = clean_decider(row.decided_by)
    by = f" by {decider}" if decider else ""
    if row.status == "approved":
        return APPROVED_TEXT.format(park_id=row.park_id, tool=row.tool, by=by)
    if row.status in ("denied", "cancelled"):
        note = one_line(row.note)
        return DENIED_TEXT.format(park_id=row.park_id, tool=row.tool, status=row.status.upper(), by=by,
                                  note=f": {note}" if note else "")
    if row.status == "expired":
        return EXPIRED_TEXT.format(park_id=row.park_id, tool=row.tool)
    return None


def make_async_client_factory(base_url: str, token_file: str) -> Callable[[], AsyncClient]:
    """A factory for the wake's AsyncClient; the token file is read per wake so a missing or rotated
    token never breaks plugin load (and is never logged)."""

    def factory() -> AsyncClient:
        token = Path(token_file).read_text().strip()
        return AsyncClient(base_url, token, retry_budget=RETRY_BUDGET_SECONDS,
                           http=httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False))

    return factory


class AuditDisabled(Exception):
    """No usable audit key file: the decider lookup is switched off (not an error)."""


def make_audit_client_factory(base_url: str, key_file: str,
                              transport: httpx.AsyncBaseTransport | None = None) -> Callable[[], httpx.AsyncClient]:
    """A factory for the audit lookup's ``httpx.AsyncClient`` (org key with ``audit.read`` only). The key file
    is read per lookup, never at load and never logged; missing, unreadable or empty raises ``AuditDisabled``
    (one ``log.info`` per process). ``transport`` is for tests."""

    def factory() -> httpx.AsyncClient:
        global _logged_no_audit_key
        try:
            key = Path(key_file).read_text().strip()
        except OSError:
            key = ""
        if not key:
            if not _logged_no_audit_key:
                _logged_no_audit_key = True
                log.info("durable-park: no audit key file; decisions are announced without the decider's name")
            raise AuditDisabled
        return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"},
                                 timeout=DECIDER_TIMEOUT_SECONDS, follow_redirects=False, transport=transport)

    return factory


def _decider_from_events(events: Any) -> str | None:
    """``"<name> (<channel>)"`` / ``"<name>"`` from the newest ``approval.decided`` event when a human made it."""
    if not isinstance(events, list):
        return None
    decided = [e for e in events if isinstance(e, dict) and e.get("event_type") == "approval.decided"]
    if not decided:
        return None
    newest = max(decided, key=lambda e: str(e.get("occurred_at") or ""))
    if newest.get("actor_type") != "human":
        return None
    name = clean_decider(newest.get("actor_display_name") if isinstance(newest.get("actor_display_name"), str) else "")
    if not name:
        return None
    data = newest.get("data")
    channel = data.get("channel") if isinstance(data, dict) else None
    channel = clean_decider(channel) if isinstance(channel, str) else ""
    return clean_decider(f"{name} ({channel})" if channel else name) or None


async def lookup_decider(request_id: str, audit_client_factory: Callable[[], Any] | None) -> str | None:
    """Who decided this approval request, from the withHuman audit log; None for anyone but a human or on ANY
    failure (no key, HTTP error, timeout, bad JSON, no event). Never raises, never logs a secret or a body.
    Bounded by ``DECIDER_TIMEOUT_SECONDS``; the caller records the decision whatever this returns."""
    if audit_client_factory is None:
        return None
    try:
        client = audit_client_factory()
    except AuditDisabled:
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("durable-park wake: decider lookup unavailable (%s)", type(exc).__name__)
        return None
    try:
        async def fetch() -> Any:
            response = await client.get(AUDIT_PATH, params=[
                ("subject_type", "approval_request"), ("subject_id", request_id),
                ("event_type", "approval.decided"), ("order", "desc"), ("limit", "5")])
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("events"), list):
                raise ValueError("unexpected audit response shape")
            return body["events"]
        return _decider_from_events(await asyncio.wait_for(fetch(), DECIDER_TIMEOUT_SECONDS))
    except Exception as exc:  # noqa: BLE001 - the name is optional; the decision must never depend on it
        log.warning("durable-park wake: decider lookup failed (%s); the decider is not named", type(exc).__name__)
        return None
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


async def _decide_from_provider(row: ParkRow, ledger: Ledger, async_client_factory: Callable[[], Any],
                                audit_client_factory: Callable[[], Any] | None = None) -> ParkRow | None:
    """Read a parked row's request and record a decision; None when still pending or not decidable."""
    if not row.request_id:
        log.warning("durable-park wake: %s has no approval request yet; left for the sweep", row.park_id)
        return None
    client = async_client_factory()
    try:
        request = await asyncio.wait_for(client.get(row.request_id), WAKE_TIMEOUT_SECONDS)
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
    status = request.status
    if status == "pending":
        return None
    if status not in _DECIDED:
        log.warning("durable-park wake: %s has unexpected request status %r; left as is", row.park_id, status)
        return None
    note = request.decision.note if request.decision and request.decision.note else None
    # False means another wake got there first; either way the row we re-read below is the truth.
    # Asked BEFORE the one write and never allowed to delay or skip it (it returns None on any failure).
    decided_by = (await lookup_decider(row.request_id, audit_client_factory)
                  if status in ("approved", "denied", "cancelled") else None)
    ledger.transition(row.park_id, "parked", status, note=note, decided_by=decided_by)
    return ledger.get(row.park_id)


async def _process(event: Any, ledger: Ledger, async_client_factory: Callable[[], Any],
                   inject: Callable[..., bool], audit_client_factory: Callable[[], Any] | None = None) -> None:
    payload = getattr(event, "raw_message", None)  # the parsed JSON body (webhook.py: raw_message=payload)
    park_id = payload.get("park_id") if isinstance(payload, dict) and payload.get("type") == "wake" else None
    if not isinstance(park_id, str) or not park_id:
        log.warning("durable-park wake: malformed payload ignored")
        return
    row = ledger.get(park_id)
    if row is None:
        log.warning("durable-park wake: unknown park_id %.40r ignored", park_id)
        return
    if row.status == "parked":
        row = await _decide_from_provider(row, ledger, async_client_factory, audit_client_factory)
        if row is None:
            return
    # No await below: on the gateway loop the check, the inject and mark_notified run atomically, so two
    # overlapping wakes for one park inject once.
    if row.status not in _DECIDED or row.notified_at is not None:
        return
    if row.status != "approved" and not row.request_id:
        # a park-hook cancel (superseded / request not opened) was already told to the agent by the hook
        log.info("durable-park wake: %s is %s without an approval request; not announced", row.park_id, row.status)
        return
    if inject(inject_text(row), session_key=row.session_key):
        ledger.mark_notified(row.park_id)
    else:
        log.warning("durable-park wake: gateway did not accept the message for %s; left unnotified", row.park_id)


async def handle_wake(event: Any, *, ledger: Ledger, async_client_factory: Callable[[], Any],
                      inject: Callable[..., bool],
                      audit_client_factory: Callable[[], Any] | None = None) -> dict | None:
    """``pre_gateway_dispatch`` body. Returns None for any event that is not ours; for a wake it always
    returns the skip directive, even on failure (the row stays unnotified and the sweep re-wakes it)."""
    if not is_wake_event(event):
        return None
    try:
        await _process(event, ledger, async_client_factory, inject, audit_client_factory)
    except Exception as exc:  # noqa: BLE001 - never let a wake reach the agent or crash the gateway
        log.warning("durable-park wake failed (%s); the sweep will retry", type(exc).__name__)
    return dict(SKIP_ACTION)


def drop_wake_hook():
    """A ``pre_gateway_dispatch`` hook for when the ledger is unavailable: still keep wakes from the agent."""

    async def durable_park_drop_wake(event=None, **_kw):
        if is_wake_event(event):
            log.warning("durable-park wake dropped: ledger unavailable")
            return dict(SKIP_ACTION)
        return None

    return durable_park_drop_wake
