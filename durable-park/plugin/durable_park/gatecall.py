"""Wiring between ledger rows and the Agent Approval Protocol SDK gate."""

from __future__ import annotations

import atexit
import copy
import errno
import fcntl
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

import httpx
from agentapprovalprotocol import Call, Client, Gate

from .ledger import ParkRow, canonical_json

log = logging.getLogger("durable_park")

AAP_TIMEOUT = "168h"

# The Hermes pre_tool_call hook gets ~60s in total (and a timeout blocks every tool for a while), so the
# SDK must give up well before that: 8s of retries plus at most 10s per HTTP attempt.
RETRY_BUDGET_SECONDS = 8.0
HTTP_TIMEOUT_SECONDS = 10.0

# What the approval request carries about the arguments (spec 4.2): secret-named keys are replaced, and a
# request whose canonical JSON is over 16 KiB gets every string cut to 1 KiB.
MAX_ARGS_BYTES = 16384
MAX_STRING_CHARS = 1024
MAX_REASONING_UNITS = 2000  # the agent's own note shown to the approver (``Call.reasoning``), in UTF-16 code units
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
    if isinstance(value, Mapping):
        return {k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _redact(v)
                for k, v in value.items()}
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
    """A redacted (and, when large, truncated) copy of ``args`` and whether it was truncated. Input untouched.

    ⚡ Optimization: Skip redundant copy.deepcopy() before _redact().
    _redact() already recursively constructs new dict and list objects, producing
    a fresh copy without deepcopy reflection overhead (~2.5x speedup).
    """
    redacted = _redact(args)
    if len(canonical_json(redacted).encode("utf-8")) > MAX_ARGS_BYTES:
        return _cut_strings(redacted), True
    return redacted, False


def cut_utf16(text: str, limit: int = MAX_REASONING_UNITS) -> str:
    """``text`` cut to at most ``limit`` UTF-16 code units (a character outside the BMP, such as an emoji, is two),
    never in the middle of a pair: withHuman may count the way JavaScript and Java do, not in code points."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def note_is_cut(text: str, limit: int = MAX_REASONING_UNITS) -> bool:
    """Would ``cut_utf16`` shorten ``text``? (The hook reports it; ``build_call`` itself records nothing.)"""
    return cut_utf16(text, limit) != text


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


def declares_context(schema: Any) -> bool:
    """True when ``schema`` lists ``withhuman_context`` under its own ``properties`` (a gateway tool's top level)."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(props, dict) and CONTEXT_FIELD in props


MAX_SCHEMA_DEPTH = 10  # how deep ``$ref``/``allOf``/``anyOf``/``oneOf`` are followed before giving up
MAX_PATTERN_CHARS = 200  # a longer ``patternProperties`` regex is not run (it comes from outside): unknown
MAX_SCHEMA_NODES = 2000  # schema nodes visited per top-level key: more is "cannot tell", and reported
_UNRESOLVED = object()


class _Budget:
    """The schema nodes one lift may still visit: branches that fan out (every ``anyOf`` offering many ``$ref``s
    that do the same) would otherwise take exponential time. ``exhausted`` is set once it ran out."""

    def __init__(self, nodes: int = MAX_SCHEMA_NODES):
        self.left = nodes
        self.exhausted = False

    def take(self) -> bool:
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


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


def _declares_context_in(schema: Any, root: Any, seen: frozenset = frozenset(), depth: int = 0,
                         budget: _Budget | None = None) -> bool | None:
    """Does ``schema`` (the rules of one nested object) declare a ``withhuman_context`` property?
    True: yes. False: no. None: cannot tell (remote or unresolvable ``$ref``, a bad pattern, a cycle, too deep,
    or ``budget`` ran out).
    A schema that is not an object (including plain ``true``/``false``) names no property: False. Sibling keywords
    combine the way ``allOf``/``anyOf``/``oneOf`` branches do: any True wins, then any None, else False."""
    if depth > MAX_SCHEMA_DEPTH or (budget is not None and not budget.take()):
        return None
    if not isinstance(schema, dict):
        return False
    found: list[bool | None] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        target = _resolve_local_ref(ref, root)
        found.append(None if target is _UNRESOLVED or ref in seen
                     else _declares_context_in(target, root, seen | {ref}, depth + 1, budget))
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
            found.extend(_declares_context_in(branch, root, seen, depth + 1, budget) for branch in branches)
    if True in found:
        return True
    return None if None in found else False


_NOT_ALNUM = re.compile(r"[^a-z0-9]")
_NOTE_PROSE = re.compile(r"with[-_]?human[-_]?context", re.IGNORECASE)


def is_note_key(key: Any) -> bool:
    """A key that is (a spelling of) the gateway's note field: ``withhuman_context`` ignoring case and every
    character that is not a letter or digit (``withHumanContext``, ``with-human-context``, ``_withhuman_context_``).
    Only the exact ``withhuman_context`` is ever moved; every other spelling found inside is reported.

    ⚡ Optimization: Fast path length check and exact string equality (~1.58x speedup).
    "withhumancontext" is 16 chars; strings shorter than 16 chars cannot equal it when stripped.
    """
    if type(key) is not str or len(key) < 16:
        return False
    if key == CONTEXT_FIELD:
        return True
    return _NOT_ALNUM.sub("", key.lower()) == "withhumancontext"


def _key_declares(props: dict, key: Any, root: Any, budget: _Budget | None = None) -> bool | None:
    """Do the tool's rules for the top-level ``key`` declare their own ``withhuman_context``? A key the rules do not
    list (Hermes may show the model a sanitized spelling of a property name) is judged by every other property:
    when any of them declares the field the key is left alone; "cannot tell" stays "cannot tell" (the caller says so
    when the budget ran out)."""
    if key in props:
        return _declares_context_in(props[key], root, budget=budget)
    unknown = False
    for name, rules in props.items():
        if name == CONTEXT_FIELD:
            continue
        own = _Budget() if budget is not None else None  # each sibling has its own budget: the order never matters
        verdict = _declares_context_in(rules, root, budget=own)
        if verdict is True:
            return True
        if verdict is None:
            unknown = True
            if own is not None and own.exhausted:
                budget.exhausted = True
    return None if unknown else False


def lift_gateway_context(args: Any, schema: dict | None,
                         on_problem: Callable[[str, str, str], None] | None = None) -> Any:
    """Move a ``withhuman_context`` note the model nested inside an argument object up to the top level.

    The withHuman gateway's schema has ``withhuman_context`` as a sibling of the tool's own fields and strips only
    the top-level one before forwarding, so a nested copy would reach the server as an unknown parameter. Only
    tools whose ``schema`` advertises the field at the top level are touched, and the move is one level deep
    (the values of top-level keys), in sorted key order; deeper notes are not moved (``find_leftover_notes`` finds
    them). For each top-level key holding a nested ``withhuman_context``, the tool's own rules for that key decide
    (``_declares_context_in``: ``properties``, ``patternProperties``, ``allOf``/``anyOf``/``oneOf``, and local
    ``$ref`` such as ``#/$defs/X`` resolved inside ``schema``). Only a positive declaration leaves the key alone
    (the server really has that field): ``on_problem("declared", key, "")``. Rules that do not declare it, or cannot
    be judged (remote ``$ref``, a bad pattern, a cycle, more than ``MAX_SCHEMA_NODES`` schema nodes: then
    ``on_problem("schema_too_complex", key, "")`` too), mean the note is removed. The first removed non-empty
    string becomes the top-level note unless the top level already has the key. Every removed value that did not
    become the note, and is not an exact copy of the note the approver will see, is reported:
    ``on_problem("note_dropped", key, reason)`` with reason ``not text``, ``empty``,
    ``another note was already at the top`` or ``more than one note``. ``on_problem`` gets key names, never
    values. A JSON-string value is not parsed here (the hook runs Hermes's type fixing first). Pure and
    idempotent: the input is never mutated (a deep copy is returned when anything is touched, the same object
    otherwise), and a bug while judging or reporting never raises."""
    if not isinstance(args, dict) or not declares_context(schema):
        return args
    # ⚡ Optimization: Avoid copy.deepcopy() unless a context note is present (~40x speedup).
    if CONTEXT_FIELD not in args and not any(isinstance(v, dict) and CONTEXT_FIELD in v for v in args.values()):
        return args
    props = schema["properties"]
    out = copy.deepcopy(args)
    top_present = CONTEXT_FIELD in out
    note: str | None = None

    def report(kind: str, key: str, detail: str = "") -> None:
        if on_problem is not None:
            try:
                on_problem(kind, key, detail)
            except Exception:  # noqa: BLE001 - a reporting bug must not stop the lift
                pass

    for key in sorted(out, key=str):
        value = out[key]
        if not isinstance(value, dict) or CONTEXT_FIELD not in value:
            continue
        budget = _Budget()  # each key's rules get their own: a complex key must not decide its siblings
        try:
            declared = _key_declares(props, key, schema, budget)
        except Exception:  # noqa: BLE001 - never raise out of the lift; "cannot tell" lifts
            declared = None
        if declared is None and budget.exhausted:
            report("schema_too_complex", str(key))  # lifted as "cannot tell", and said so
        if declared is True:
            report("declared", str(key))
            continue
        removed = value.pop(CONTEXT_FIELD)
        if isinstance(removed, str) and removed.strip():
            if note is None and not top_present:
                note = removed
                continue
            if removed == (out[CONTEXT_FIELD] if top_present else note):
                continue  # an exact copy of the note the approver will see: nothing is lost
            reason = "more than one note" if note is not None else "another note was already at the top"
        else:
            reason = "empty" if isinstance(removed, str) else "not text"  # blank text is empty: nobody could read it
        report("note_dropped", str(key), reason)
    if note is not None and not top_present:
        out[CONTEXT_FIELD] = note
    return out


def _text_form_object(value: Any) -> dict | None:
    """The object a JSON string decodes to when it holds ``withhuman_context`` (else ``None``)."""
    if not isinstance(value, str) or CONTEXT_FIELD not in value:
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) and CONTEXT_FIELD in decoded else None


def text_form_note_keys(args: Any) -> list[str]:
    """The top-level keys (sorted) whose value is a JSON string of an object holding the note."""
    if not isinstance(args, dict):
        return []
    return [str(key) for key in sorted(args, key=str) if _text_form_object(args[key]) is not None]


MAX_WALK_DEPTH = 50
MAX_WALK_NODES = 10_000
MAX_LEFTOVER_HITS = 20
MAX_PATH_CHARS = 200
_NOT_JSON = object()


def _decode_text(value: str) -> Any:
    """What a string strictly decodes to when it looks like JSON (an object, array or string), else ``_NOT_JSON``."""
    stripped = value.lstrip()
    if not stripped or stripped[0] not in '{["':
        return _NOT_JSON
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001 - not JSON (a RecursionError on absurd nesting counts too)
        return _NOT_JSON


def find_leftover_notes(args: Any, exclude: frozenset | set = frozenset()) -> tuple[list[str], bool]:
    """Where a ``withhuman_context`` note is still inside ``args``, as key paths (key names only, never values; every
    list item is written ``[*]``, so a note in each of 30 items is one place), plus whether the walk was cut short.
    Walks dicts and lists at any depth and every string that strictly decodes to an object, array or string; a
    string that does not decode but names the field (``withhuman_context``, ``withHumanContext``,
    ``with-human-context``) counts as a leftover too, so prose that merely mentions it is reported. A key counts as
    the note when ``is_note_key`` says so. The top-level ``withhuman_context`` itself is the real note and is never
    reported; the paths in ``exclude`` (positively declared locations) are skipped. Bounded: depth 50, 10 000
    nodes, 20 hits (a bound reached means incomplete)."""
    hits: list[str] = []
    state = {"nodes": 0, "incomplete": False}

    def add(path: str) -> None:
        if path in exclude or path[:MAX_PATH_CHARS] in hits:
            return
        if len(hits) >= MAX_LEFTOVER_HITS:
            state["incomplete"] = True
            return
        hits.append(path[:MAX_PATH_CHARS])

    def walk(value: Any, path: str, depth: int) -> None:
        state["nodes"] += 1
        if depth > MAX_WALK_DEPTH or state["nodes"] > MAX_WALK_NODES:
            state["incomplete"] = True
            return
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if is_note_key(key):
                    if not (depth == 0 and key == CONTEXT_FIELD):
                        add(child_path)
                    continue  # never look inside a note
                walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, f"{path}[*]", depth + 1)
        elif isinstance(value, str):
            decoded = _decode_text(value)
            if decoded is not _NOT_JSON:
                walk(decoded, path, depth + 1)
            elif _NOTE_PROSE.search(value):
                add(path)

    walk(args, "", 0)
    return hits, state["incomplete"]


LIFT_STATUS_FILENAME = "lift_status.json"
LIFT_UNWRITABLE_FILENAME = "lift_status.unwritable"  # prefix: each process leaves ``<prefix>.<owner>`` while it cannot write
MAX_LIFT_ENTRIES = 200
LIFT_REWRITE_SECONDS = 60.0  # a repeat of a known problem rewrites the file at most this often (the count lags)
LIFT_LOCK_WAIT_SECONDS = 0.2  # the most one tool call (or one flush) ever waits, in total, to write the report
LIFT_LOCK_RETRY_SECONDS = 0.02
LIFT_KINDS = ("declared", "note_dropped", "schema_unavailable", "args_unreadable", "lift_error",
              "note_left_inside", "check_incomplete", "report_reset", "schema_too_complex", "note_cut", "note_not_text")
LIFT_OVERFLOW_TOOL = "*"  # problems beyond 200 waiting ones are counted here (per kind), never dropped
LIFT_OVERFLOW_KEY = "(more than 200 problems waiting)"
LIFT_RESET_TOOL = LIFT_STATUS_FILENAME  # ``report_reset``: the report was unreadable and was replaced
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_MARKER_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LockTimeout(Exception):
    """The report could not be written within ``LIFT_LOCK_WAIT_SECONDS``: another process (or thread) held it."""


def _sweep_order(entry: dict) -> tuple:
    return (entry["last_seen"], entry["first_seen"], entry["tool"], entry["kind"], entry["key"], entry["detail"])


def _ident(entry: dict) -> tuple[str, str, str, str]:
    return (entry["tool"], entry["kind"], entry["key"], entry["detail"])


def _flush_at_exit(ref: "weakref.ReferenceType[LiftStatus]") -> None:
    status = ref()
    if status is not None:
        status.flush()


class NotARegularFile(OSError):
    """Something other than a regular file sits where the report's lock or a marker belongs (a planted FIFO,
    device or directory): it is never used."""


def _open_shared(path: str, flags: int, mode: int) -> int:
    """Open ``path`` creating it with exactly ``mode`` (the umask does not narrow it): files that several users read
    or write (the report's lock, the markers) must work for a process running as another user. The open never
    blocks (``O_NONBLOCK``: a FIFO nobody reads fails at once instead of hanging the tool call) and never follows a
    symlink; anything but a regular file is refused (``NotARegularFile``). Only a regular file this process's user
    owns gets its mode set: a file another user created keeps the mode they gave it."""
    fd = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotARegularFile(errno.EINVAL, "not a regular file", path)
        if info.st_uid == os.geteuid():
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
    except BaseException:
        os.close(fd)
        raise
    return fd


class LiftStatus:
    """Where the lift reports what it could not do, for the sweep to alert on (``lift_status.json`` next to the
    ledger). One entry per (tool, kind, key, detail) with ``first_seen``, ``last_seen`` and ``count``: names and
    times, never an argument value or note text (key names are chosen by the model and can themselves be data).
    At most 200 entries are kept on disk: when the file is full the least recently seen WRITTEN entry makes room;
    an entry that has not been written yet is never dropped (problems beyond 200 waiting ones are counted in one
    ``*`` entry per kind).

    Several processes, possibly running as different users, may write the file (the gateway, a CLI run that
    loads the plugin): every write takes an ``flock`` on a sidecar lock file, re-reads the file, adds this
    process's new occurrences to what is there and replaces it atomically, so nobody drops another's entries.
    The report is mode 0644 and the lock 0666, so another user can read the one and take the other (who may write
    at all is the directory's permission).

    A tool call records ALL its problems in one ``record_many`` and waits at most 0.2 s in total, whatever the
    number of problems or threads: the in-process mutex is never held during file I/O, the thread that writes
    waits for the flock until a deadline, and a thread that cannot get its turn by then gives up. What could
    not be written stays in memory (written on the next record, by ``flush`` and at process exit).

    Whenever this process has entries it could not write (a lock timeout included) it leaves its OWN marker
    ``lift_status.unwritable.<pid>`` (UTC time and error type, nothing else; mode 0644) for the sweep to alert
    on, unless the whole directory is unwritable (the form-check report goes stale then and says so). Only the
    process that made a marker removes it, after its next successful write: another process's success never
    hides this one's failure. A file that cannot be parsed is kept as ``lift_status.json.corrupt`` before it is
    replaced, and the replacement holds a ``report_reset`` entry so the sweep says so. A repeat of a problem
    already in the file rewrites it only every 60 s (the count lags; ``flush`` catches up). Thread-safe and
    never raises. ``path=None`` only remembers."""

    def __init__(self, path: str | None = None, clock: Callable[[], float] = time.monotonic,
                 owner: str | None = None):
        self._path = path
        self._clock = clock
        self._owner = owner
        self._lock = threading.Lock()  # guards the fields below; never held during file I/O
        self._io = threading.Lock()  # one writing thread at a time in this process
        self._marker_lock = threading.Lock()  # a failure's marker and a success's cleanup never interleave
        self._fail_gen = 0  # counts failures: a success only clears the outage if none happened since it began
        self._last_exc: BaseException | None = None  # the latest failure, for a marker written late by another thread
        self._pending: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._known: set[tuple[str, str, str, str]] = set()
        self._last_write: float | None = None
        self._write_failed = False
        self._marker_tried = False  # one attempt per outage to write the marker
        self._corrupt_seen = False
        try:
            self._known = set(self._read_disk(keep_corrupt=False))
        except Exception:  # noqa: BLE001 - only used to tell a new problem from a repeat
            self._known = set()
        if path:
            atexit.register(_flush_at_exit, weakref.ref(self))  # what could not be written yet is tried once more

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def marker_path(self) -> str | None:
        if not self._path:
            return None
        owner = self._owner if self._owner is not None else str(os.getpid())
        return os.path.join(os.path.dirname(os.path.abspath(self._path)), f"{LIFT_UNWRITABLE_FILENAME}.{owner}")

    def _read_disk(self, keep_corrupt: bool = True) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """The entries now in the file ({} when missing). An unparseable file is moved aside when ``keep_corrupt``;
        any other read error raises (a write must not replace what it could not read)."""
        if not self._path:
            return {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                rows = json.load(handle)["entries"]
            entries: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for row in rows[-MAX_LIFT_ENTRIES:]:
                ident = (row["tool"], row["kind"], row["key"], row.get("detail", ""))
                if all(isinstance(part, str) for part in ident) and isinstance(row["count"], int):
                    entries[ident] = {"tool": ident[0], "kind": ident[1], "key": ident[2], "detail": ident[3],
                                      "first_seen": str(row["first_seen"]), "last_seen": str(row["last_seen"]),
                                      "count": row["count"]}
            return entries
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            if keep_corrupt:
                os.replace(self._path, self._path + ".corrupt")
                self._corrupt_seen = True
                log.warning("%s was unreadable; kept as %s.corrupt", LIFT_STATUS_FILENAME, LIFT_STATUS_FILENAME)
            return {}

    def _add(self, ident: tuple[str, str, str, str], delta: int, first_seen: str, last_seen: str) -> bool:
        """Add occurrences to ``_pending`` (``_lock`` held). True when the slot's problem is new to the report."""
        if ident not in self._pending and len(self._pending) >= MAX_LIFT_ENTRIES:
            # Never evict an entry that has not been written: count the newcomer in the overflow entry.
            ident = (LIFT_OVERFLOW_TOOL, ident[1], LIFT_OVERFLOW_KEY, "")
        pending = self._pending.get(ident)
        if pending is None:
            self._pending[ident] = {"delta": delta, "first_seen": first_seen, "last_seen": last_seen}
        else:
            pending["delta"] += delta
            pending["first_seen"] = min(pending["first_seen"], first_seen)
            pending["last_seen"] = max(pending["last_seen"], last_seen)
        return ident not in self._known

    def record(self, tool: str, kind: str, key: str = "", detail: str = "") -> None:
        self.record_many(tool, [(kind, key, detail)])

    def record_many(self, tool: str, problems: Iterable[tuple[str, str, str]]) -> None:
        """Record every problem of one tool call with one write attempt, one wait of at most 0.2 s in total."""
        try:
            deadline = time.monotonic() + LIFT_LOCK_WAIT_SECONDS
            now = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
            with self._lock:
                recorded = False
                new = False
                for kind, key, detail in problems:
                    recorded = True
                    new |= self._add((str(tool), str(kind), str(key), str(detail)), 1, now, now)
                due = recorded and (new or self._write_failed or self._last_write is None
                                    or self._clock() - self._last_write >= LIFT_REWRITE_SECONDS)
            if due:
                self._flush(deadline)
        except Exception as exc:  # noqa: BLE001 - reporting must never break a tool call
            log.error("could not record a lift problem (%s)", type(exc).__name__)

    def flush(self) -> None:
        """Write anything still waiting (a failed write, or counts held back), and clear this process's marker once
        the report can be written again. Never raises."""
        try:
            deadline = time.monotonic() + LIFT_LOCK_WAIT_SECONDS
            marker = self.marker_path
            with self._lock:
                waiting = bool(self._pending)
            if waiting or (marker is not None and os.path.lexists(marker)):
                self._flush(deadline)
        except Exception as exc:  # noqa: BLE001
            log.error("could not flush lift problems (%s)", type(exc).__name__)

    def _acquire(self, lock_fd: int, deadline: float) -> None:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released when the descriptor closes
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout() from None
                time.sleep(LIFT_LOCK_RETRY_SECONDS)

    def _write_marker_file(self, exc: BaseException) -> None:
        """Tell the sweep this process cannot write the report: UTC time and error type only. Best effort."""
        try:
            fd = _open_shared(self.marker_path, os.O_WRONLY | os.O_TRUNC, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).strftime(_MARKER_STAMP_FORMAT)} {type(exc).__name__}\n")
        except OSError as err:  # the directory itself is unwritable (the form-check alert covers that), or a planted link
            log.warning("could not write the lift unwritable marker (%s)", type(err).__name__)

    def _clear_marker(self) -> None:
        try:
            os.unlink(self.marker_path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # the marker then stays and the sweep keeps alerting: loud, not silent
            log.error("could not remove %s (%s)", os.path.basename(self.marker_path), type(exc).__name__)

    def _failed(self, exc: BaseException, deadline: float) -> None:
        """This process could not write: remember it and leave the marker (once per outage). Waits for another
        thread's marker work only until ``deadline`` (plus one retry tick); a marker not written now is written by
        the next failure."""
        with self._lock:
            was_failed = self._write_failed
            self._write_failed = True
            self._fail_gen += 1  # a write that began before this failure must not end the outage
            self._last_exc = exc
            want = not self._marker_tried and bool(self._path)
        if not was_failed:
            log.error("could not write %s (%s)", LIFT_STATUS_FILENAME, type(exc).__name__)
        if not want:
            return
        if not self._marker_lock.acquire(timeout=max(LIFT_LOCK_RETRY_SECONDS, deadline - time.monotonic())):
            return  # the thread busy with the marker writes it for this failure once it is done (see ``_flush``)
        try:
            with self._lock:
                write = not self._marker_tried
                self._marker_tried = True
            if write:
                self._write_marker_file(exc)
        finally:
            self._marker_lock.release()

    def _flush(self, deadline: float) -> None:
        if not self._path:
            with self._lock:
                self._known |= set(self._pending)
                self._pending.clear()
            return
        if not self._io.acquire(timeout=max(0.0, deadline - time.monotonic())):
            self._failed(LockTimeout(), deadline)  # another thread is still writing; what this call added stays pending
            return
        try:
            with self._lock:
                batch, self._pending = self._pending, {}
                generation = self._fail_gen
            try:
                kept = self._write(batch, deadline)
            except Exception as exc:  # noqa: BLE001 - keep the occurrences and retry
                with self._lock:
                    for ident, pending in batch.items():
                        self._add(ident, pending["delta"], pending["first_seen"], pending["last_seen"])
                self._failed(exc, deadline)
                return
            with self._marker_lock:
                with self._lock:
                    self._known = {_ident(e) for e in kept}
                    self._last_write = self._clock()
                    # The outage is over only if no thread failed since this write began and nothing is waiting:
                    # otherwise this process still has entries it could not write, and its marker stays.
                    over = self._fail_gen == generation and not self._pending
                    if over:
                        self._write_failed = False
                        self._marker_tried = False
                if over:
                    self._clear_marker()
                with self._lock:  # a failure that could not get its turn at the marker meanwhile: write it now
                    late = self._fail_gen != generation and not self._marker_tried and self._last_exc is not None
                    if late:
                        self._marker_tried = True
                        exc = self._last_exc
                if late:
                    self._write_marker_file(exc)
        finally:
            self._io.release()

    def _write(self, batch: dict[tuple[str, str, str, str], dict[str, Any]], deadline: float) -> list[dict[str, Any]]:
        """Merge ``batch`` into the file under the flock; returns the entries now in it. Raises on any failure."""
        lock_fd = _open_shared(self._path + ".lock", os.O_RDWR, 0o666)
        try:
            self._acquire(lock_fd, deadline)
            entries = self._read_disk()
            if self._corrupt_seen:
                self._corrupt_seen = False
                now = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
                reset = batch.setdefault((LIFT_RESET_TOOL, "report_reset", "", ""),
                                         {"delta": 0, "first_seen": now, "last_seen": now})
                reset["delta"] += 1
                reset["last_seen"] = now
            for ident, pending in batch.items():
                entry = entries.get(ident)
                if entry is None:
                    entries[ident] = {"tool": ident[0], "kind": ident[1], "key": ident[2], "detail": ident[3],
                                      "first_seen": pending["first_seen"], "last_seen": pending["last_seen"],
                                      "count": pending["delta"]}
                else:
                    entry["count"] += pending["delta"]
                    entry["first_seen"] = min(entry["first_seen"], pending["first_seen"])
                    entry["last_seen"] = max(entry["last_seen"], pending["last_seen"])
            # Only entries already on disk before this write may be pushed out; what this write adds stays.
            fresh = [e for e in entries.values() if _ident(e) in batch]
            older = sorted((e for e in entries.values() if _ident(e) not in batch), key=_sweep_order)
            room = max(0, MAX_LIFT_ENTRIES - len(fresh))
            kept = sorted((older[-room:] if room else []) + fresh, key=_sweep_order)
            data = {"entries": kept, "updated_at": datetime.now(timezone.utc).strftime(_STAMP_FORMAT)}
            fd, tmp = tempfile.mkstemp(prefix=".lift-", dir=os.path.dirname(os.path.abspath(self._path)))
            try:
                os.fchmod(fd, 0o644)  # mkstemp makes it 0600: the sweep (another user) must read the report
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return kept
        finally:
            os.close(lock_fd)


def build_call(row: ParkRow) -> Call:
    """The AAP call for a ledger row: a pure function of the row (the executor replays it and must send the
    identical body, so the user and profile are the ones saved with the row at park time, never the current
    session's). Raises ``ValueError`` for arguments that are not strict JSON (e.g. NaN)."""
    stored = json.loads(row.args_json)
    arguments, truncated = redact_arguments(stored)
    json.dumps(arguments, allow_nan=False)
    note = stored.get(CONTEXT_FIELD) if isinstance(stored, dict) else None
    reasoning = cut_utf16(note) if isinstance(note, str) and note else None
    context: dict[str, Any] = {
        "args_sha256": row.args_digest, "platform": platform_of(row.session_key), "session_key": row.session_key}
    if row.user_id:
        context["user_id"] = row.user_id
    if row.profile:
        context["profile"] = row.profile
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
