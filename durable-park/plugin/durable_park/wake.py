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

# Statuses a parked row can move to; for these the chat is told (once, tracked by notified_at).
_DECIDED = ("approved", "denied", "expired", "cancelled")

APPROVED_TEXT = (
    '[durable-park] {park_id} was APPROVED in withHuman. Call parked_action_execute with park_id "{park_id}" '
    'now to run it exactly as saved. Do not call "{tool}" directly.')
DENIED_TEXT = (
    '[durable-park] {park_id} ("{tool}") was {status} in withHuman{note}. It did not run. '
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



def inject_text(row: ParkRow) -> str | None:
    """The message for a decided row, or None when the chat is not told about this status."""
    if row.status == "approved":
        return APPROVED_TEXT.format(park_id=row.park_id, tool=row.tool)
    if row.status in ("denied", "cancelled"):
        note = one_line(row.note)
        return DENIED_TEXT.format(park_id=row.park_id, tool=row.tool, status=row.status.upper(),
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


async def _decide_from_provider(row: ParkRow, ledger: Ledger, async_client_factory: Callable[[], Any]) -> ParkRow | None:
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
    ledger.transition(row.park_id, "parked", status, note=note)
    return ledger.get(row.park_id)


async def _process(event: Any, ledger: Ledger, async_client_factory: Callable[[], Any],
                   inject: Callable[..., bool]) -> None:
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
        row = await _decide_from_provider(row, ledger, async_client_factory)
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
                      inject: Callable[..., bool]) -> dict | None:
    """``pre_gateway_dispatch`` body. Returns None for any event that is not ours; for a wake it always
    returns the skip directive, even on failure (the row stays unnotified and the sweep re-wakes it)."""
    if not is_wake_event(event):
        return None
    try:
        await _process(event, ledger, async_client_factory, inject)
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
