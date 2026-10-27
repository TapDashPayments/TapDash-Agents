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
import json
import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from agentapprovalprotocol import AsyncClient, CredentialRefused

from . import keys
from .gatecall import HTTP_TIMEOUT_SECONDS, RETRY_BUDGET_SECONDS
from .outcome import outcome_text
from .ledger import Ledger, ParkRow

log = logging.getLogger("durable_park")

WAKE_ROUTE = "durable-park-wake"
WAKE_CHAT_NAME = f"webhook/{WAKE_ROUTE}"  # gateway/platforms/webhook.py: chat_name=f"webhook/{route_name}"
REFUSALS_FILENAME = "wake_refusals.json"  # park id -> {profile, count, first_at}; the sweep reads it
_REFUSAL_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"
SKIP_ACTION = {"action": "skip", "reason": "durable-park wake"}
# pre_gateway_dispatch is NOT bounded by plugins.hook_callback_timeout, so bound the provider read ourselves.
WAKE_TIMEOUT_SECONDS = 30.0
MAX_NOTE_CHARS = 300
# The decider lookup (withHuman audit log) is optional garnish: bounded on its own, fails soft.
DECIDER_TIMEOUT_SECONDS = 5.0
MAX_DECIDER_CHARS = 80  # the name
MAX_CHANNEL_CHARS = 20
MAX_DECIDED_BY_CHARS = MAX_DECIDER_CHARS + MAX_CHANNEL_CHARS + 3  # "<name> (<channel>)"
_DECIDER_EXTRA_CHARS = frozenset(" .'-_@()")
AUDIT_PATH = "/api/v1/audit"
_logged_no_audit_key = False

# Statuses a parked row can move to; for these the chat is told (once, tracked by notified_at).
_DECIDED = ("approved", "denied", "expired", "cancelled")

APPROVED_TEXT = (
    '[durable-park] {park_id} was APPROVED in withHuman{by}. Call parked_action_execute with park_id "{park_id}" '
    'now to run it exactly as saved. If parked_action_execute is not in your tool list, call it through tool_call. '
    'Do not call "{tool}" directly.')
DENIED_TEXT = (
    '[durable-park] {park_id} ("{tool}") was {status} in withHuman{by}{note}. It did not run. '
    "Tell the user and continue without it.")
EXPIRED_TEXT = (
    '[durable-park] {park_id} ("{tool}") got no decision before its deadline. It did not run. '
    "Ask the user if they want to request approval again.")


OUTCOME_SUFFIX = " Tell the user this in one short message. Do not run anything."
_ENDED = frozenset({"executed", "failed", "unknown"})


def normalize_profile(profile: str | None) -> str:
    """One name per profile: None, ``""``, ``"default"`` and ``"main"`` are all the default profile."""
    return "default" if keys.is_default_profile(profile) or profile == "main" else str(profile)


_SESSION_KEY_NAMESPACE = re.compile(r"agent:([^:]+):")


def session_key_profile(session_key: str | None) -> str | None:
    """The profile a Hermes session key belongs to: the ``<ns>`` of ``agent:<ns>:...`` (``main`` and ``default`` are
    the default profile), else None for a key that does not have that shape (old rows, other platforms)."""
    match = _SESSION_KEY_NAMESPACE.match(session_key) if isinstance(session_key, str) else None
    return normalize_profile(match.group(1)) if match else None


def wake_chat_name(own_profile: str | None) -> str:
    """The chat name Hermes gives a wake of this profile: ``webhook/durable-park-wake`` for the default profile,
    else ``webhook/durable-park-wake-<profile>``."""
    own = normalize_profile(own_profile)
    return WAKE_CHAT_NAME if own == "default" else f"{WAKE_CHAT_NAME}-{own}"


def _from_webhook(event: Any) -> Any:
    """The event's source when the webhook platform built it, else None."""
    source = getattr(event, "source", None)
    if source is None:
        return None
    platform = getattr(source, "platform", None)
    return source if getattr(platform, "value", platform) == "webhook" else None


def is_wake_event(event: Any, own_profile: str | None = None) -> bool:
    """True for an event the webhook platform built for THIS profile's wake route (a chat merely *titled* like it
    is not, and neither is another profile's route)."""
    source = _from_webhook(event)
    return source is not None and getattr(source, "chat_name", None) == wake_chat_name(own_profile)


def is_any_wake_event(event: Any) -> bool:
    """True for a wake of ANY profile. Only for a copy that does not know its own profile and so refuses to let
    a wake of any profile reach the agent."""
    source = _from_webhook(event)
    name = getattr(source, "chat_name", None) if source is not None else None
    return isinstance(name, str) and (name == WAKE_CHAT_NAME or name.startswith(WAKE_CHAT_NAME + "-"))


def one_line(note: str | None) -> str:
    """A reviewer note as one line of at most ``MAX_NOTE_CHARS`` (also used by the park hook's refusal)."""
    return " ".join((note or "").split())[:MAX_NOTE_CHARS]



def clean_decider(text: str | None, limit: int = MAX_DECIDER_CHARS) -> str:
    """One line of letters, digits, spaces and ``.'-_@()`` (anything else becomes a space), whitespace
    collapsed, at most ``limit`` characters; "" when nothing is left. It goes into the agent's chat."""
    allowed = "".join(ch if ch.isalnum() or ch in _DECIDER_EXTRA_CHARS else " " for ch in (text or ""))
    return " ".join(allowed.split())[:limit].strip()


def inject_text(row: ParkRow) -> str | None:
    """The message for a decided row, or None when the chat is not told about this status."""
    decider = clean_decider(row.decided_by, MAX_DECIDED_BY_CHARS)
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


def make_async_client_for_key_file(base_url: str) -> Callable[[str], AsyncClient]:
    """Like ``make_async_client_factory`` but the key file is named per call (the row's own profile key)."""

    def build(token_file: str) -> AsyncClient:
        return make_async_client_factory(base_url, token_file)()

    return build


def key_file_usable(path: str) -> bool:
    """True when the key file is a regular file that can be read as UTF-8 text and is not empty (its content is
    never logged). Anything else (a FIFO or device, which could block the open, a directory) is checked before it
    is opened. The sweep's ``_key_file_usable`` has the same rule."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False
        with open(path, encoding="utf-8") as handle:
            return bool(handle.read().strip())
    except (OSError, ValueError):  # missing, unreadable, or not text (UnicodeDecodeError)
        return False


def _refusals_path(status_dir: str) -> str:
    return os.path.join(os.path.expanduser(status_dir), REFUSALS_FILENAME)


def _read_refusals(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_refusals(path: str, data: dict) -> None:
    """Atomically (temp file + rename, in the same directory)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".wake-refusals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record_refusal(status_dir: str | None, row: ParkRow) -> None:
    """The provider refused this profile's key for ``row``: count it in ``wake_refusals.json`` for the sweep.
    No await inside, so two wakes on the gateway loop never interleave. Never raises."""
    if not status_dir:
        return
    try:
        path = _refusals_path(status_dir)
        data = _read_refusals(path)
        entry = data.get(row.park_id)
        count = entry.get("count") if isinstance(entry, dict) else None
        first_at = entry.get("first_at") if isinstance(entry, dict) else None
        if not isinstance(count, int) or isinstance(count, bool) or not isinstance(first_at, str):
            count, first_at = 0, datetime.now(timezone.utc).strftime(_REFUSAL_STAMP)
        data[row.park_id] = {"profile": normalize_profile(row.profile), "count": count + 1, "first_at": first_at}
        _write_refusals(path, data)
    except Exception as exc:  # noqa: BLE001 - a failed record must never change what the wake does
        log.warning("durable-park wake: could not record in %s (%s)", REFUSALS_FILENAME, type(exc).__name__)


def clear_refusal(status_dir: str | None, park_id: str) -> None:
    """Forget a row's refusals (its decision was read, or it is no longer parked). Never raises."""
    if not status_dir:
        return
    try:
        path = _refusals_path(status_dir)
        data = _read_refusals(path)
        if park_id in data:
            del data[park_id]
            _write_refusals(path, data)
    except Exception as exc:  # noqa: BLE001
        log.warning("durable-park wake: could not update %s (%s)", REFUSALS_FILENAME, type(exc).__name__)


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
        except (OSError, ValueError):  # unreadable, or not text (UnicodeDecodeError)
            key = ""
        if not key:
            if not _logged_no_audit_key:
                _logged_no_audit_key = True
                log.warning("durable-park: no audit key file; decisions are announced without the decider's name")
            raise AuditDisabled
        return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"},
                                 timeout=DECIDER_TIMEOUT_SECONDS, follow_redirects=False, transport=transport)

    return factory


def _decider_from_events(events: Any, request_id: str) -> str | None:
    """``"<name> (<channel>)"`` / ``"<name>"`` from the newest ``approval.decided`` event of THIS request when a
    human made it (events about any other subject are ignored, whatever the server sent)."""
    if not isinstance(events, list):
        return None
    decided = [e for e in events if isinstance(e, dict) and e.get("event_type") == "approval.decided"
               and e.get("subject_type") == "approval_request" and e.get("subject_id") == request_id]
    if not decided:
        return None
    newest = max(decided, key=lambda e: str(e.get("occurred_at") or ""))
    if newest.get("actor_type") != "human":
        return None
    raw_name = newest.get("actor_display_name")
    name = clean_decider(raw_name if isinstance(raw_name, str) else "")
    if not name:
        return None
    data = newest.get("data")
    channel = data.get("channel") if isinstance(data, dict) else None
    channel = clean_decider(channel, MAX_CHANNEL_CHARS) if isinstance(channel, str) else ""
    return f"{name} ({channel})" if channel else name


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
        return _decider_from_events(await asyncio.wait_for(fetch(), DECIDER_TIMEOUT_SECONDS), request_id)
    except Exception as exc:  # noqa: BLE001 - the name is optional; the decision must never depend on it
        log.warning("durable-park wake: decider lookup failed (%s); the decider is not named", type(exc).__name__)
        return None
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


async def _decide_from_provider(row: ParkRow, ledger: Ledger, async_client_factory: Callable[[], Any],
                                audit_client_factory: Callable[[], Any] | None = None,
                                status_dir: str | None = None) -> ParkRow | None:
    """Read a parked row's request and record a decision; None when still pending or not decidable."""
    if not row.request_id:
        log.warning("durable-park wake: %s has no approval request yet; left for the sweep", row.park_id)
        return None
    client = async_client_factory()
    try:
        request = await asyncio.wait_for(client.get(row.request_id), WAKE_TIMEOUT_SECONDS)
    except CredentialRefused:
        # The provider does not accept this profile's key: the row stays parked, the sweep alerts on the record.
        log.warning("durable-park wake: the provider refused the key of profile %s for %s; left for the sweep",
                    normalize_profile(row.profile), row.park_id)
        record_refusal(status_dir, row)
        return None
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
    clear_refusal(status_dir, row.park_id)  # the read worked, whatever the answer
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


def _inside(path: str, directory: str) -> bool:
    """True when ``path`` is ``directory`` or lies under it, both compared after ``os.path.realpath`` (symlinks
    resolved; a sibling that merely shares a name prefix is not inside)."""
    real_path, real_dir = os.path.realpath(path), os.path.realpath(directory)
    return real_path == real_dir or real_path.startswith(real_dir.rstrip(os.sep) + os.sep)


def _own_key_problem(row: ParkRow, token_file: str, own_profile: str | None,
                     instances_dir: str | None = None) -> str | None:
    """Why this copy must not use its key for ``row`` (a short reason for the log), or None.

    The row's key file is ``keys.token_file_for(row.profile)``: for the default profile the copy's configured
    ``token_file``, for a person ``<dir of the configured token_file>/<profile>.token``. It must be exactly the
    ``token_file`` this copy was given, so a copy wired to another person's key never reads for this row. The
    default profile's ``token_file`` must also not lie inside the instances directory (``instances_dir``, default
    ``keys.INSTANCES_DIR``; both sides ``realpath``-ed), where only people's keys live: a default copy wired to a
    person's key file would otherwise read with it. Then the file must exist and hold a key.
    """
    try:
        profile = None if normalize_profile(row.profile) == "default" else row.profile
        expected = keys.token_file_for(profile, default=token_file, instances_dir=str(Path(token_file).parent))
    except ValueError:
        return "the row's profile name is not valid"
    if expected != token_file:
        return "the configured token_file is not this profile's key file"
    if profile is None and _inside(token_file, keys.INSTANCES_DIR if instances_dir is None else instances_dir):
        return "the default profile's token_file lies inside the instances directory (a person's key file)"
    if not key_file_usable(token_file):
        return f"its key file {token_file} is missing, empty or unreadable"
    return None


async def _process(event: Any, ledger: Ledger, async_client_factory: Callable[[], Any],
                   inject: Callable[..., bool], audit_client_factory: Callable[[], Any] | None = None, *,
                   own_profile: str | None = None, token_file: str | None = None, status_dir: str | None = None,
                   client_for_key_file: Callable[[str], Any] | None = None) -> None:
    payload = getattr(event, "raw_message", None)  # the parsed JSON body (webhook.py: raw_message=payload)
    park_id = payload.get("park_id") if isinstance(payload, dict) and payload.get("type") == "wake" else None
    if not isinstance(park_id, str) or not park_id:
        log.warning("durable-park wake: malformed payload ignored")
        return
    row = ledger.get(park_id)
    if row is None:
        log.warning("durable-park wake: unknown park_id %.40r ignored", park_id)
        return
    row_profile, own = normalize_profile(row.profile), normalize_profile(own_profile)
    if row_profile != own:
        # Hermes lets a plugin post only into its own profile's chats: never read or tell for another profile.
        log.warning("durable-park wake: %s belongs to profile %s but this copy serves profile %s; "
                    "left for the sweep", row.park_id, row_profile, own)
        return
    key_profile = session_key_profile(row.session_key)
    if key_profile is not None and key_profile != own:
        # The row's profile says this copy, but the chat it would be told in belongs to another profile (the profile
        # came from the turn's environment, the session key from the routing): never read or tell for it.
        log.warning("durable-park wake: %s says profile %s but its session key belongs to profile %s, and this copy "
                    "serves profile %s; left for the sweep", row.park_id, row_profile, key_profile, own)
        return
    if row.status != "parked":
        clear_refusal(status_dir, row.park_id)
    if token_file is not None and row.status in ("parked", "approved"):
        problem = _own_key_problem(row, token_file, own_profile)
        if problem is not None:
            log.warning("durable-park wake: %s of profile %s not resumed: %s; left for the sweep",
                        row.park_id, row_profile, problem)
            return
        if client_for_key_file is not None:
            key_file = token_file

            def async_client_factory() -> Any:  # noqa: F811 - the client of the row's own key file
                return client_for_key_file(key_file)
    if row.status in _ENDED:
        # GitHub #66: the run ended but the chat was not told (the executor's own post did not happen or did not
        # work). No await: the check, the inject and the mark run together on the gateway loop.
        text = outcome_text(row) if row.outcome_notified_at is None else None
        if text is None:
            return
        if inject(text + OUTCOME_SUFFIX, session_key=row.session_key):
            ledger.mark_outcome_notified(row.park_id)
        else:
            log.warning("durable-park wake: gateway did not accept the outcome of %s; left untold", row.park_id)
        return
    if row.status == "parked":
        row = await _decide_from_provider(row, ledger, async_client_factory, audit_client_factory, status_dir)
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
                      audit_client_factory: Callable[[], Any] | None = None,
                      own_profile: str | None = None, token_file: str | None = None,
                      status_dir: str | None = None,
                      client_for_key_file: Callable[[str], Any] | None = None) -> dict | None:
    """``pre_gateway_dispatch`` body. Returns None for any event that is not ours; for a wake it always
    returns the skip directive, even on failure (the row stays unnotified and the sweep re-wakes it).

    ``own_profile`` is the profile this copy serves (None = the default profile): it only treats its own wake
    route as a wake and only handles rows of its own profile. ``token_file`` is this copy's key file: when given,
    a parked or approved row is resumed only if that file is the row's profile key and holds a key, and the
    decision is read with a client from ``client_for_key_file(token_file)`` (else ``async_client_factory``).
    ``status_dir`` (optional) is where ``wake_refusals.json`` goes."""
    if not is_wake_event(event, own_profile):
        return None
    try:
        await _process(event, ledger, async_client_factory, inject, audit_client_factory, own_profile=own_profile,
                       token_file=token_file, status_dir=status_dir, client_for_key_file=client_for_key_file)
    except Exception as exc:  # noqa: BLE001 - never let a wake reach the agent or crash the gateway
        log.warning("durable-park wake failed (%s); the sweep will retry", type(exc).__name__)
    return dict(SKIP_ACTION)


def drop_wake_hook(own_profile: str | None = None, *, any_profile: bool = False):
    """A ``pre_gateway_dispatch`` hook for when the ledger is unavailable: still keep wakes from the agent.
    It drops this profile's wake route; ``any_profile=True`` (the profile setting is unusable) drops every
    profile's."""
    matches = is_any_wake_event if any_profile else (lambda event: is_wake_event(event, own_profile))

    async def durable_park_drop_wake(event=None, **_kw):
        if matches(event):
            log.warning("durable-park wake dropped: ledger unavailable")
            return dict(SKIP_ACTION)
        return None

    return durable_park_drop_wake
