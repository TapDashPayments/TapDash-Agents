"""Gateway bridge: the withHuman pipeline webhook block that keeps it to ONE human approval per action.

When the model runs an approved parked call (``parked_action_execute``), the call goes out through the
withHuman MCP gateway. The gateway's approval pipeline POSTs a signed ``request_approval_block.webhook``
here and asks what to do with it. We answer:

* ``approve`` exactly once, when the delivery is a live one for one of the configured agents and withHuman
  itself holds one approved, consumed park request, made by the same person (the same withHuman agent
  instance), for the same Hermes tool name and exact same arguments that no other gateway request has used
  yet (the rule is ``bridge.verify.decide``; the questions go to withHuman through ``bridge.whapi.RequestLister``).
  The person is read from withHuman's own record of the gateway request, never from the delivery;
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
from typing import Any, Callable, Iterable

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
# The Viewer role agent may only look things up: it is never trusted for an approval, whatever the settings say.
NEVER_TRUSTED_AGENTS = frozenset({"ops-viewer"})
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
ROUTE = "/wh/bridge"
NO_PARK_REASON = "No approved park for this exact call"
PARK_LIST_LIMIT = 50
NEIGHBOUR_LIST_LIMIT = 200  # withHuman's maximum page; ``decide`` does not trust it: it wants an old item in the list
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


def clean_agent_list(agents: Iterable[str]) -> tuple[str, ...]:
    """The trusted agents, each once, in order; ValueError (saying what) for an empty list, an empty or
    whitespace-holding name, or an agent that is never trusted (the Viewer)."""
    seen: list[str] = []
    for name in agents:
        if not isinstance(name, str) or not name or name != name.strip() or any(c.isspace() for c in name):
            raise ValueError(f"expected agents: {name!r} is not a plain agent name")
        if name in NEVER_TRUSTED_AGENTS:
            raise ValueError(f"expected agents: {name} may only look things up and is never trusted")
        if name not in seen:
            seen.append(name)
    if not seen:
        raise ValueError("expected agents: the list is empty")
    return tuple(seen)


class Bridge:
    def __init__(self, expected_agents: Iterable[str], lister: Any, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.expected_agents = frozenset(clean_agent_list(expected_agents))
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
        if not isinstance(slug, str) or slug not in self.expected_agents:
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
        park_tool, _ = expected_park(server, tool)
        deadline = self.monotonic() + LIST_BUDGET_SECONDS
        try:
            parks = self.lister.list(agent_slug=slug, q=park_tool, limit=PARK_LIST_LIMIT, deadline=deadline)
            # every recent request of the agent, whatever its tool: Hermes folds "-" into "_" in tool names, so a
            # filter on the tool name would miss a second use under the other spelling
            neighbours = self.lister.list(agent_slug=slug, q=None, limit=NEIGHBOUR_LIST_LIMIT, deadline=deadline)
        except ListError as exc:  # withHuman slow, unreachable or unhappy: a person decides
            return "human", None, exc.code
        # The person who asked is the withHuman instance on withHuman's own record of this gateway request (the
        # delivery carries only the agent). Not listed, or listed without an instance: a person decides.
        own = next((o for o in neighbours if isinstance(o, dict) and o.get("id") == request_id), None)
        if own is None:
            return "human", None, "gateway-request-not-listed"
        person = own.get("agent_instance_id")
        if not isinstance(person, str) or not person:
            return "human", None, "no-person"
        call = GatewayCall(request_id, server, tool, arguments, slug, person)
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


def create_app(*, secret: str | None, lister: Any, expected_agents: Iterable[str] = (DEFAULT_EXPECTED_AGENT,),
               clock: Callable[[], float] = time.time,
               monotonic: Callable[[], float] = time.monotonic) -> Starlette:
    """Build the ASGI app. With ``secret=None`` it still starts but answers 401 to everything."""
    bridge = Bridge(expected_agents, lister, clock, monotonic)

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
    parser.add_argument("--host", default=None, help=f"listen address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"listen port (default {DEFAULT_PORT})")
    parser.add_argument("--systemd-socket", action="store_true",
                        default=os.environ.get("DURABLE_PARK_BRIDGE_SYSTEMD_SOCKET") == "1",
                        help="serve on the listening socket systemd passes as fd 3 (socket activation) instead of "
                             "binding --host/--port (env DURABLE_PARK_BRIDGE_SYSTEMD_SOCKET=1)")
    parser.add_argument("--expected-agents", default=None,
                        help="comma-separated agent slugs a delivery may carry to be approvable, e.g. "
                             "proto-agents,ops-admin,ops-ops (env DURABLE_PARK_EXPECTED_AGENTS); never ops-viewer")
    parser.add_argument("--expected-agent", default=None,
                        help="the same for one agent (the old flag; env DURABLE_PARK_EXPECTED_AGENT)")
    parser.add_argument("--api-base", default=os.environ.get("DURABLE_PARK_WH_API_BASE", DEFAULT_API_BASE),
                        help="withHuman API base URL, https only (env DURABLE_PARK_WH_API_BASE)")
    parser.add_argument("--key-file", default=os.environ.get("DURABLE_PARK_WH_KEY_FILE", DEFAULT_KEY_FILE),
                        help="file holding the withHuman organization key, re-read on every call (under the "
                             "systemd unit it is systemd's copy made at service start, so a rotated key needs "
                             "a service restart) (env DURABLE_PARK_WH_KEY_FILE)")
    args = parser.parse_args(argv)
    if args.expected_agents is not None and args.expected_agent is not None:
        parser.error("--expected-agents and --expected-agent cannot be combined")
    # flags beat the environment; the list beats the old single name; the default is the one original agent
    if args.expected_agents is not None:
        listed = args.expected_agents
    elif args.expected_agent is not None:
        listed = args.expected_agent
    else:
        # "set" means set, even to "": an emptied list must stop the bridge, never fall back to the default
        listed = DEFAULT_EXPECTED_AGENT
        for name in ("DURABLE_PARK_EXPECTED_AGENTS", "DURABLE_PARK_EXPECTED_AGENT"):
            if os.environ.get(name) is not None:
                listed = os.environ[name]
                break
    try:
        args.expected_agents = clean_agent_list(listed.split(","))
    except ValueError as exc:
        parser.error(str(exc))
    del args.expected_agent
    if args.systemd_socket:
        if args.host is not None or args.port is not None:
            parser.error("--systemd-socket cannot be combined with --host or --port")
    else:
        args.host = DEFAULT_HOST if args.host is None else args.host
        args.port = DEFAULT_PORT if args.port is None else args.port
    return args


def systemd_listen_fd(environ: Any, pid: int) -> int:
    """The fd of the one socket systemd handed us (always 3), or exit: serve nothing without it.

    systemd sets ``LISTEN_PID`` to the pid it started and ``LISTEN_FDS`` to the number of sockets, passed from
    fd 3. A stale or inherited environment (another pid, another count) must never make us serve.
    """
    if environ.get("LISTEN_PID") != str(pid) or environ.get("LISTEN_FDS") != "1":
        raise SystemExit("--systemd-socket: no socket from systemd (LISTEN_PID/LISTEN_FDS)")
    return 3


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = parse_args(argv)
    fd = systemd_listen_fd(os.environ, os.getpid()) if args.systemd_socket else None
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("bridge trusts agents: %s", ", ".join(args.expected_agents))  # so a change is visible in the journal
    try:
        lister = RequestLister(args.api_base, args.key_file)
    except ListError:  # never echo the URL (it could carry credentials) or read the key
        raise SystemExit("bridge: --api-base (env DURABLE_PARK_WH_API_BASE) must be an https:// URL") from None
    app = create_app(secret=load_secret(args.secret_file), lister=lister, expected_agents=args.expected_agents)
    if fd is None:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
        return
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):  # not for children of ours to inherit
        os.environ.pop(name, None)
    uvicorn.run(app, fd=fd, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
