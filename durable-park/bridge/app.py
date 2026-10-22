"""Gateway bridge: the withHuman pipeline webhook block that keeps it to ONE human approval per action.

When the model runs an approved parked call (``parked_action_execute``), the call goes out through the
withHuman MCP gateway. The gateway's approval pipeline POSTs a signed ``request_approval_block.webhook``
here and asks what to do with it. We answer:

* ``approve`` exactly once, when the delivery is a live one for the configured agent and withHuman itself
  holds one approved, consumed park request for the same Hermes tool name and exact same arguments that no
  other gateway request has used yet (the rule is ``bridge.verify.decide``; the questions go to withHuman
  through ``bridge.whapi.RequestLister``);
* ``human`` for everything else, including every error.

The bridge keeps no state and never opens the ledger (a file the bot's Linux user can write).

Signature scheme (withHuman webhook endpoints; confirmed against github.com/withHumanAI/withhuman
``internal/webhookdelivery``): header ``WithHuman-Signature: t=<unix>,v1=<hex>``; ``v1`` is HMAC-SHA256
over ``"<t>." + raw body`` keyed with the WHOLE ``whsec_...`` string as UTF-8 text (not base64-decoded);
timestamp tolerance 300 s either side; hex digest compared in constant time.

The response JSON carries ONLY ``outcome`` and ``reason`` (withHuman rejects unknown fields).
Argument values, the signing secret and the organization key are never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from bridge.verify import GatewayCall, decide, expected_park, hermes_tool_name  # noqa: F401 - re-exported
from bridge.whapi import DEFAULT_API_BASE, DEFAULT_KEY_FILE, ListError, RequestLister
from durable_park.ledger import args_digest

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
LIST_LIMIT = 50
LIST_BUDGET_SECONDS = 3.5  # both withHuman calls together; withHuman's webhook block gives up at 5 s

_SIGNATURE_HEX = re.compile(r"[0-9a-fA-F]{64}")
_UNSAFE_LOG_CHARS = re.compile(r"[^\w.:@/+-]")


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


class Bridge:
    def __init__(self, expected_agent: str, lister: Any, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.expected_agent = expected_agent
        self.lister = lister
        self.clock = clock
        self.monotonic = monotonic

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
            return {"outcome": "approve", "reason": f"Matches approved park request {park_id}"}
        return {"outcome": "human", "reason": NO_PARK_REASON}

    def _decide(self, raw: bytes, header_key: str | None, info: dict[str, str]) -> tuple[str, str | None, str]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return "human", None, "body-not-object"
        info["delivery"] = _clean(payload.get("delivery_id") or info["delivery"])
        request = payload.get("request")
        agent = payload.get("agent")
        server = tool = arguments = None
        if isinstance(request, dict):  # best effort, for the log line only
            server, tool, arguments = request.get("server"), request.get("tool"), request.get("arguments")
            info["server"], info["tool"] = _clean(server), _clean(tool)
            if isinstance(arguments, dict):
                info["digest"] = args_digest(arguments)
                info["keys"] = ",".join(_clean(k, 40) for k in sorted(map(str, arguments))[:40])
        if payload.get("type") != BLOCK_TYPE:
            return "human", None, "type"
        if payload.get("preview", False) is not False:  # only an explicit boolean false (or absent) proceeds
            return "human", None, "preview"
        if not isinstance(request, dict) or not isinstance(agent, dict):
            return "human", None, "malformed"
        if not isinstance(arguments, dict):
            return "human", None, "arguments-not-object"
        slug = agent.get("slug")
        if not isinstance(slug, str) or slug != self.expected_agent:
            return "human", None, "agent"
        if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
            return "human", None, "malformed"
        if not header_key:
            return "human", None, "no-idempotency-key"
        body_key = payload.get("idempotency_key")
        if body_key is not None and body_key != header_key:
            return "human", None, "idempotency-mismatch"
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:  # real deliveries always carry it; no id, no verdict
            return "human", None, "no-request-id"
        call = GatewayCall(request_id, server, tool, arguments, slug)
        park_tool, _ = expected_park(server, tool)
        deadline = self.monotonic() + LIST_BUDGET_SECONDS
        try:
            parks = self.lister.list(agent_slug=slug, q=park_tool, limit=LIST_LIMIT, deadline=deadline)
            neighbours = self.lister.list(agent_slug=slug, q=tool, limit=LIST_LIMIT, deadline=deadline)
        except ListError as exc:  # withHuman slow, unreachable or unhappy: a person decides
            return "human", None, exc.code
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        verdict = decide(call, parks, neighbours, now)
        if verdict.approve:
            return "approve", verdict.park_request_id, verdict.why
        return "human", None, verdict.why


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


def create_app(*, secret: str | None, lister: Any, expected_agent: str = DEFAULT_EXPECTED_AGENT,
               clock: Callable[[], float] = time.time,
               monotonic: Callable[[], float] = time.monotonic) -> Starlette:
    """Build the ASGI app. With ``secret=None`` it still starts but answers 401 to everything."""
    bridge = Bridge(expected_agent, lister, clock, monotonic)

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
    parser.add_argument("--secret-file",
                        default=os.environ.get("DURABLE_PARK_BRIDGE_SECRET_FILE", DEFAULT_SECRET_FILE),
                        help="file holding the withHuman whsec_ signing secret")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--expected-agent",
                        default=os.environ.get("DURABLE_PARK_EXPECTED_AGENT", DEFAULT_EXPECTED_AGENT),
                        help="agent slug a delivery must carry to be approvable")
    parser.add_argument("--api-base", default=os.environ.get("DURABLE_PARK_WH_API_BASE", DEFAULT_API_BASE),
                        help="withHuman API base URL, https only (env DURABLE_PARK_WH_API_BASE)")
    parser.add_argument("--key-file", default=os.environ.get("DURABLE_PARK_WH_KEY_FILE", DEFAULT_KEY_FILE),
                        help="file holding the withHuman organization key, read on every call "
                             "(env DURABLE_PARK_WH_KEY_FILE)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        lister = RequestLister(args.api_base, args.key_file)
    except ListError:  # never echo the URL (it could carry credentials) or read the key
        raise SystemExit("bridge: --api-base (env DURABLE_PARK_WH_API_BASE) must be an https:// URL") from None
    app = create_app(secret=load_secret(args.secret_file), lister=lister, expected_agent=args.expected_agent)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
