"""Gateway bridge: the withHuman pipeline webhook block that keeps it to ONE human approval per action.

When the model runs an approved parked call (``parked_action_execute``), the call goes out through the
withHuman MCP gateway. The gateway's approval pipeline POSTs a signed ``request_approval_block.webhook``
here and asks what to do with it. We answer:

* ``approve`` exactly once, when the delivery is a live one for the configured agent and matches an
  ``executing`` park (same Hermes tool name, exact same arguments) whose gateway pass is not used yet;
* ``human`` for everything else, including every error.

Signature scheme (withHuman webhook endpoints; confirmed against github.com/withHumanAI/withhuman
``internal/webhookdelivery``): header ``WithHuman-Signature: t=<unix>,v1=<hex>``; ``v1`` is HMAC-SHA256
over ``"<t>." + raw body`` keyed with the WHOLE ``whsec_...`` string as UTF-8 text (not base64-decoded);
timestamp tolerance 300 s either side; hex digest compared in constant time.

The response JSON carries ONLY ``outcome`` and ``reason`` (withHuman rejects unknown fields).
Argument values and the secret are never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from durable_park.ledger import Ledger, args_digest

log = logging.getLogger("durable_park.bridge")

BLOCK_TYPE = "request_approval_block.webhook"
TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_SECRET_FILE = "/etc/durable-park/bridge_secret"
DEFAULT_EXPECTED_AGENT = "proto-agents"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
ROUTE = "/wh/bridge"
NO_PARK_REASON = "No approved park for this exact call"
DELIVERIES_FILENAME = "bridge_deliveries.db"

_SIGNATURE_HEX = re.compile(r"[0-9a-fA-F]{64}")
_UNSAFE_LOG_CHARS = re.compile(r"[^\w.:@/+-]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    idempotency_key TEXT PRIMARY KEY,
    delivery_id     TEXT,
    outcome         TEXT NOT NULL,
    park_id         TEXT,
    created_at      TEXT NOT NULL
);
"""


def verify_signature(header: str | None, body: bytes, secret: str | None, now: float) -> bool:
    """True only for a well-formed, in-tolerance ``WithHuman-Signature`` that matches ``body``."""
    if not header or secret is None:
        return False
    fields: dict[str, str] = {}
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep or key in fields:  # malformed, or a repeated key (the scheme sends exactly one v1)
            return False
        fields[key] = value
    stamp, given = fields.get("t"), fields.get("v1")
    if stamp is None or given is None:
        return False
    if not (stamp.isascii() and stamp.isdigit() and len(stamp) <= 12):
        return False
    if _SIGNATURE_HEX.fullmatch(given) is None:
        return False
    if abs(now - int(stamp)) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given.lower())


def load_secret(path: str) -> str | None:
    """Read the signing secret; None (never raises, never logs the value) if missing/unreadable/empty."""
    try:
        secret = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        log.error("bridge signing secret unavailable (%s reading %s): every delivery will get 401",
                  type(exc).__name__, path)
        return None
    if not secret:
        log.error("bridge signing secret file %s is empty: every delivery will get 401", path)
        return None
    return secret


def _clean(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else repr(type(value).__name__)
    return _UNSAFE_LOG_CHARS.sub("?", text)[:limit] or "-"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _DeliveryStore:
    """Persistent record keyed by the WithHuman idempotency key (stable across retries of one delivery).

    Lives in its own sqlite file next to the ledger so the ledger schema is untouched. ``decide_once`` runs
    the decision while holding the write lock, so concurrent retries of one key get one answer.
    """

    def __init__(self, path: str):
        self.path = path
        self._ready = False
        self._lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=WAL")
            if not self._ready:
                con.executescript(_SCHEMA)
                self._ready = True
            yield con
        finally:
            con.close()

    def decide_once(self, key: str, delivery_id: str,
                    decide: Callable[[], tuple[str, str | None]]) -> tuple[str, str | None, bool]:
        """Return ``(outcome, park_id, replayed)``; ``decide`` runs only for the first delivery of ``key``."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT outcome, park_id FROM deliveries WHERE idempotency_key=?",
                                  (key,)).fetchone()
                if row is not None:
                    con.execute("COMMIT")
                    return row[0], row[1], True
                outcome, park_id = decide()
                con.execute("INSERT INTO deliveries (idempotency_key, delivery_id, outcome, park_id, created_at)"
                            " VALUES (?,?,?,?,?)", (key, delivery_id, outcome, park_id, _now_iso()))
                con.execute("COMMIT")
                return outcome, park_id, False
            except BaseException:
                con.execute("ROLLBACK")
                raise


class Bridge:
    def __init__(self, ledger_path: str, expected_agent: str, deliveries_path: str | None = None):
        self.ledger_path = ledger_path
        self.expected_agent = expected_agent
        self._ledger: Ledger | None = None
        self._ledger_lock = threading.Lock()
        self._deliveries = _DeliveryStore(
            deliveries_path or str(Path(ledger_path).expanduser().parent / DELIVERIES_FILENAME))

    def _get_ledger(self) -> Ledger:
        with self._ledger_lock:
            if self._ledger is None:
                self._ledger = Ledger(str(Path(self.ledger_path).expanduser()))
            return self._ledger

    def _match(self, name: str, digest: str) -> tuple[str, str | None]:
        ledger = self._get_ledger()
        row = ledger.find_executing_match(name, digest)
        if row is not None and ledger.mark_gateway_passed(row.park_id):
            return "approve", row.park_id
        return "human", None

    def process(self, raw: bytes, header_delivery_id: str | None, header_key: str | None) -> dict[str, str]:
        """Decide one signature-verified delivery. Never raises; every failure is ``human``."""
        info = {"delivery": _clean(header_delivery_id or "-"), "server": "-", "tool": "-", "keys": "-",
                "digest": "-"}
        outcome, park_id, why = "human", None, "error"
        try:
            outcome, park_id, why = self._decide(raw, header_key, info)
        except Exception as exc:  # noqa: BLE001 - any bug must fall back to a human, never an approval
            outcome, park_id, why = "human", None, f"error:{type(exc).__name__}"
        log.info("bridge delivery=%s server=%s tool=%s arg_keys=[%s] digest=%s outcome=%s why=%s",
                 info["delivery"], info["server"], info["tool"], info["keys"], info["digest"], outcome, why)
        if outcome == "approve":
            return {"outcome": "approve", "reason": f"Matches approved park {park_id}"}
        return {"outcome": "human", "reason": NO_PARK_REASON}

    def _decide(self, raw: bytes, header_key: str | None, info: dict[str, str]) -> tuple[str, str | None, str]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return "human", None, "body-not-object"
        info["delivery"] = _clean(payload.get("delivery_id") or info["delivery"])
        request = payload.get("request")
        agent = payload.get("agent")
        if not isinstance(request, dict) or not isinstance(agent, dict):
            return "human", None, "malformed"
        server, tool, arguments = request.get("server"), request.get("tool"), request.get("arguments")
        info["server"], info["tool"] = _clean(server), _clean(tool)
        if not isinstance(arguments, dict):
            return "human", None, "arguments-not-object"
        digest = args_digest(arguments)
        info["digest"] = digest
        info["keys"] = ",".join(_clean(k, 40) for k in sorted(map(str, arguments))[:40])
        if payload.get("type") != BLOCK_TYPE:
            return "human", None, "type"
        if payload.get("preview") is True:
            return "human", None, "preview"
        if agent.get("slug") != self.expected_agent:
            return "human", None, "agent"
        if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
            return "human", None, "malformed"
        if not header_key:
            return "human", None, "no-idempotency-key"
        body_key = payload.get("idempotency_key")
        if body_key is not None and body_key != header_key:
            return "human", None, "idempotency-mismatch"
        name = f"mcp__wh__{server}__{tool}"
        outcome, park_id, replayed = self._deliveries.decide_once(
            header_key, str(payload.get("delivery_id") or ""), lambda: self._match(name, digest))
        if outcome == "approve":
            return "approve", park_id, "replay" if replayed else f"park={park_id}"
        return "human", None, "replay" if replayed else "no-park"


async def _read_body(request: Request) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(*, ledger_path: str, secret: str | None, expected_agent: str = DEFAULT_EXPECTED_AGENT,
               deliveries_path: str | None = None, clock: Callable[[], float] = time.time) -> Starlette:
    """Build the ASGI app. With ``secret=None`` it still starts but answers 401 to everything."""
    bridge = Bridge(ledger_path, expected_agent, deliveries_path)

    async def endpoint(request: Request) -> Response:
        raw = await _read_body(request)  # raw bytes first: the signature covers them exactly
        if raw is None:
            log.warning("bridge rejected: body too large (413)")
            return Response(status_code=413)
        if not verify_signature(request.headers.get("WithHuman-Signature"), raw, secret, clock()):
            log.warning("bridge rejected: missing, malformed, stale or wrong signature (401)")
            return Response(status_code=401)
        result = await run_in_threadpool(bridge.process, raw, request.headers.get("WithHuman-Delivery-Id"),
                                         request.headers.get("WithHuman-Idempotency-Key"))
        return JSONResponse(result)

    return Starlette(routes=[Route(ROUTE, endpoint, methods=["POST"])])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="durable-park gateway bridge (withHuman webhook block)")
    parser.add_argument("--ledger", default=os.environ.get("DURABLE_PARK_LEDGER"),
                        help="path of the durable-park ledger.db (env DURABLE_PARK_LEDGER)")
    parser.add_argument("--secret-file",
                        default=os.environ.get("DURABLE_PARK_BRIDGE_SECRET_FILE", DEFAULT_SECRET_FILE),
                        help="file holding the withHuman whsec_ signing secret")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--expected-agent",
                        default=os.environ.get("DURABLE_PARK_EXPECTED_AGENT", DEFAULT_EXPECTED_AGENT),
                        help="agent slug a delivery must carry to be approvable")
    ns = parser.parse_args(argv)
    if not ns.ledger:
        parser.error("--ledger (or env DURABLE_PARK_LEDGER) is required")
    return ns


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(ledger_path=args.ledger, secret=load_secret(args.secret_file),
                     expected_agent=args.expected_agent)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
