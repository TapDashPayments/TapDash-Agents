"""SQLite ledger of the tool calls durable-park asked withHuman about.

One row per tool call that was asked about (exempt tools get none): decided at once (``allowed``/``refused``),
or held for a human decision (``parked`` and on). All state
changes go through ``Ledger.transition`` (a compare-and-swap on ``status``)
so concurrent hooks, the sweep and the gateway bridge cannot race a row
into two states.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Iterator

ACTIVE_STATUSES = ("parked", "approved", "executing")

# Allowed (from_status -> to_status) edges. ``executing -> expired`` is used
# only when the approval gate refuses before anything ran. ``allowed``/``refused`` are terminal rows for calls
# withHuman decided instantly (nothing parked, nothing to execute later).
TRANSITIONS: dict[str, frozenset[str]] = {
    "parked": frozenset({"approved", "denied", "expired", "cancelled", "allowed", "refused"}),
    "approved": frozenset({"executing"}),
    "executing": frozenset({"executed", "failed", "unknown", "expired"}),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parks (
    park_id          TEXT PRIMARY KEY,
    session_key      TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    tool_call_id     TEXT NOT NULL,
    tool             TEXT NOT NULL,
    server           TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    args_digest      TEXT NOT NULL,
    call_key         TEXT NOT NULL,
    request_id       TEXT,
    status           TEXT NOT NULL,
    consume_key      TEXT,
    result_json      TEXT,
    note             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    executing_at     TEXT,
    notified_at      TEXT,
    gateway_passed_at TEXT,
    decided_by       TEXT,
    user_id          TEXT,
    profile          TEXT,
    UNIQUE (session_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS parks_session ON parks (session_key, status);
CREATE INDEX IF NOT EXISTS parks_status ON parks (status);
CREATE INDEX IF NOT EXISTS parks_match ON parks (tool, args_digest, status);
"""


@dataclass(frozen=True)
class ParkRow:
    park_id: str
    session_key: str
    session_id: str
    tool_call_id: str
    tool: str
    server: str
    args_json: str
    args_digest: str
    call_key: str
    request_id: str | None
    status: str
    consume_key: str | None
    result_json: str | None
    note: str | None
    created_at: str
    updated_at: str
    executing_at: str | None
    notified_at: str | None
    gateway_passed_at: str | None
    decided_by: str | None = None  # who decided in withHuman ("Name (channel)"), when the audit log said
    user_id: str | None = None  # the Hermes session's user and profile at park time (NULL = unknown or a row
    profile: str | None = None  # from before they were saved); the approval call is rebuilt from the row alone


_COLUMNS = tuple(f.name for f in fields(ParkRow))
# Columns the caller may set through ``transition``; park_id/status/created_at/
# updated_at are owned by the ledger.
_SETTABLE = frozenset(_COLUMNS) - {"park_id", "status", "created_at", "updated_at"}


def canonical_json(args: Any) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


# R22 (live finding): the withHuman gateway adds ``withhuman_scope: "default"`` to the arguments it delivers to
# the pipeline (the model's call usually has none), and the server/pipeline never receive ``withhuman_context``
# (the model may send it). Park-time and bridge-time digests both go through this one function.
GATEWAY_SCOPE_KEY = "withhuman_scope"
GATEWAY_CONTEXT_KEY = "withhuman_context"
GATEWAY_DEFAULT_SCOPE = "default"


def _whole_floats_to_ints(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_floats_to_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_floats_to_ints(v) for v in value]
    return value


def normalize_args(args: Any) -> Any:
    """Arguments as the gateway's approval pipeline sees them, for digesting only (never for dispatch).

    Deep copy; top-level ``withhuman_context`` dropped; top-level ``withhuman_scope`` defaulted to
    ``"default"`` when absent (an explicit scope is kept, so the scope stays part of the match); whole-number
    floats become ints everywhere (live finding: the model sent ``2.0``, the gateway delivered ``2``, the
    same number to the server). Non-object arguments are returned as a copy, otherwise unchanged.
    """
    normalized = _whole_floats_to_ints(copy.deepcopy(args))
    if isinstance(normalized, dict):
        normalized.pop(GATEWAY_CONTEXT_KEY, None)
        normalized.setdefault(GATEWAY_SCOPE_KEY, GATEWAY_DEFAULT_SCOPE)
    return normalized


def args_digest(args: Any) -> str:
    """sha256 of the canonical JSON of the NORMALIZED arguments (see ``normalize_args``)."""
    return hashlib.sha256(canonical_json(normalize_args(args)).encode()).hexdigest()


def call_key(session_id: str, tool_call_id: str) -> str:
    """Stable key of one tool call: sha256 hex of ``"<session_id>:<tool_call_id>"``."""
    return hashlib.sha256(f"{session_id}:{tool_call_id}".encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(raw: sqlite3.Row | None) -> ParkRow | None:
    return None if raw is None else ParkRow(**{c: raw[c] for c in _COLUMNS})


class Ledger:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as con:
            con.executescript(_SCHEMA)
            self._add_missing_columns(con)

    @staticmethod
    def _column_names(con: sqlite3.Connection) -> list[str]:
        return [r[1] for r in con.execute("PRAGMA table_info(parks)")]

    @staticmethod
    def _add_missing_columns(con: sqlite3.Connection) -> None:
        """Add ``decided_by``, ``user_id`` and ``profile`` to a ledger created before they existed (old rows get
        NULL). Idempotent; the gateway, the sweep and the bridge all open this file through this class, so another
        process may add a column first: that ("duplicate column name") is fine."""
        for name in ("decided_by", "user_id", "profile"):
            if name in Ledger._column_names(con):
                continue
            try:
                con.execute(f"ALTER TABLE parks ADD COLUMN {name} TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A short-lived connection per operation keeps the ledger safe to use
        # from any thread or process (hooks, sweep, bridge share the file).
        con = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
        finally:
            con.close()

    def park(self, *, session_key: str, session_id: str, tool_call_id: str, tool: str,
             args: dict, call_key: str, server: str, user_id: str | None = None,
             profile: str | None = None) -> ParkRow:
        """Record a parked call; idempotent on (session_id, tool_call_id) (a replay keeps the first row's user and
        profile)."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                existing = con.execute(
                    "SELECT * FROM parks WHERE session_id=? AND tool_call_id=?",
                    (session_id, tool_call_id)).fetchone()
                if existing is not None:
                    con.execute("COMMIT")
                    return _row(existing)
                park_id = self._new_park_id(con)
                now = _now()
                con.execute(
                    "INSERT INTO parks (park_id, session_key, session_id, tool_call_id, tool, server,"
                    " args_json, args_digest, call_key, status, created_at, updated_at, user_id, profile)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'parked',?,?,?,?)",
                    (park_id, session_key, session_id, tool_call_id, tool, server,
                     canonical_json(args), args_digest(args), call_key, now, now, user_id, profile))
                row = con.execute("SELECT * FROM parks WHERE park_id=?", (park_id,)).fetchone()
                con.execute("COMMIT")
                return _row(row)
            except BaseException:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def _new_park_id(con: sqlite3.Connection) -> str:
        while True:  # the write lock is held, so a free id stays free
            park_id = "P-" + secrets.token_hex(4).upper()
            if con.execute("SELECT 1 FROM parks WHERE park_id=?", (park_id,)).fetchone() is None:
                return park_id

    def get(self, park_id: str) -> ParkRow | None:
        with self._connect() as con:
            return _row(con.execute("SELECT * FROM parks WHERE park_id=?", (park_id,)).fetchone())

    def get_by_call(self, session_id: str, tool_call_id: str) -> ParkRow | None:
        with self._connect() as con:
            return _row(con.execute("SELECT * FROM parks WHERE session_id=? AND tool_call_id=?",
                                    (session_id, tool_call_id)).fetchone())

    def active_for_session(self, session_key: str) -> list[ParkRow]:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM parks WHERE session_key=? AND status IN ({marks}) ORDER BY created_at, rowid",
                (session_key, *ACTIVE_STATUSES)).fetchall()
        return [_row(r) for r in rows]

    def list_by_status(self, *statuses: str) -> list[ParkRow]:
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM parks WHERE status IN ({marks}) ORDER BY created_at, rowid", statuses).fetchall()
        return [_row(r) for r in rows]

    def set_request_id(self, park_id: str, request_id: str) -> bool:
        """Attach the approval request to a still-``parked`` row that has none; True when it was attached."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE parks SET request_id=?, updated_at=? WHERE park_id=? AND status='parked' AND request_id IS NULL",
                (request_id, _now(), park_id))
            return cur.rowcount == 1

    def delete_finished_before(self, cutoff: str) -> int:
        """Delete ``allowed``/``refused``/``cancelled`` rows last updated before ``cutoff`` (the ledger's own
        ``%Y-%m-%dT%H:%M:%S.%fZ`` stamp, compared as a string); returns the count. Rows that carry a decision
        or an outcome someone may still need to look up (executed, denied, expired, failed, unknown) stay."""
        with self._connect() as con:
            cur = con.execute(
                "DELETE FROM parks WHERE status IN ('allowed','refused','cancelled') AND updated_at < ?",
                (cutoff,))
            return cur.rowcount

    def mark_notified(self, park_id: str) -> None:
        now = _now()
        with self._connect() as con:
            con.execute("UPDATE parks SET notified_at=?, updated_at=? WHERE park_id=?", (now, now, park_id))

    def transition(self, park_id: str, from_status: str, to_status: str, **fields_: Any) -> bool:
        """Move a row from ``from_status`` to ``to_status``; False if the CAS matched no row.

        Raises ValueError for an edge outside the allowed table or an unknown/managed field.
        """
        if to_status not in TRANSITIONS.get(from_status, frozenset()):
            raise ValueError(f"illegal ledger transition {from_status!r} -> {to_status!r}")
        bad = set(fields_) - _SETTABLE
        if bad:
            raise ValueError(f"cannot set ledger fields: {sorted(bad)}")
        sets = ["status=?", "updated_at=?"] + [f"{name}=?" for name in fields_]
        params = [to_status, _now(), *fields_.values(), park_id, from_status]
        with self._connect() as con:
            cur = con.execute(f"UPDATE parks SET {', '.join(sets)} WHERE park_id=? AND status=?", params)
            return cur.rowcount == 1

    def find_executing_match(self, tool: str, args_digest: str) -> ParkRow | None:  # noqa: A002 - interface name
        """The executing park for this tool and args digest, preferring one the gateway has not passed yet."""
        with self._connect() as con:
            return _row(con.execute(
                "SELECT * FROM parks WHERE status='executing' AND tool=? AND args_digest=?"
                " ORDER BY gateway_passed_at IS NOT NULL, rowid DESC LIMIT 1",
                (tool, args_digest)).fetchone())

    def mark_gateway_passed(self, park_id: str) -> bool:
        """Record that the gateway bridge approved this park's call; True only the first time."""
        now = _now()
        with self._connect() as con:
            cur = con.execute(
                "UPDATE parks SET gateway_passed_at=?, updated_at=? WHERE park_id=? AND gateway_passed_at IS NULL",
                (now, now, park_id))
            return cur.rowcount == 1
