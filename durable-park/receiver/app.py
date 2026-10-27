"""durable-park AAP receiver: withHuman ``request.resolved`` webhook -> signed wake to Hermes.

Runs as the ``durable-park-receiver`` system service (user ``aaprecv``) on 127.0.0.1:8790; Tailscale
Funnel publishes ``POST /aap/events``. The SDK ``Receiver`` verifies the Standard-Webhooks signature,
answers withHuman's ``webhook.verification`` challenge for our registration id, and for
``request.resolved`` answers 204 at once and reads the request with the instance credential in the
background. Its ``context.resume`` handle (written by the plugin's gate,
``{"framework": "hermes", "park_id": ..., "session_key": ...}``) selects the ``hermes`` resumer, which
POSTs ``{"type": "wake", "park_id": ...}`` to Hermes's local webhook route, signed with the wake secret.

A wake only makes the plugin re-read the decision from withHuman itself, so the receiver keeps no state.

Configuration is ``/etc/durable-park/receiver.json`` (0600 aaprecv), written by ``provision.py``:
``signing_secret`` + ``registration_id`` first (enough to answer verification), then ``instance_id``
and ``instance_token`` (the receiver's own copy of the instance credential: it cannot read the
root:hermes ``instance_token`` file). A missing or broken file is not fatal: the service starts and
answers every event with 401 until provisioning writes a usable file and restarts it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from agentapprovalprotocol import AsyncClient, Receiver
from starlette.applications import Starlette
from starlette.routing import Route

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from wake_post import DEFAULT_BASE_URL, DEFAULT_WAKE_URL, post_wake, wake_url_for  # noqa: E402

log = logging.getLogger("durable_park.receiver")

DEFAULT_CONFIG = "/etc/durable-park/receiver.json"
DEFAULT_WAKE_SECRET_FILE = "/etc/durable-park/wake_secret"
DEFAULT_WAKE_BASE = DEFAULT_BASE_URL  # where Hermes's webhook platform listens; other profiles live under /p/<profile>
DEFAULT_AAP_BASE_URL = "https://app.withhuman.ai/api/aap"
EVENTS_PATH = "/aap/events"
HOST = "127.0.0.1"
PORT = 8790
FRAMEWORK = "hermes"


def load_config(path) -> dict[str, Any]:
    """The parsed receiver.json, or ``{}`` when it is missing or unusable (never logs its content)."""
    try:
        config = json.loads(Path(path).read_text())
    except FileNotFoundError:
        log.warning("%s does not exist yet: refusing all events until provisioning", path)
        return {}
    except (OSError, ValueError) as exc:
        log.error("%s is unreadable (%s): refusing all events", path, type(exc).__name__)
        return {}
    if not isinstance(config, dict):
        log.error("%s is not a JSON object: refusing all events", path)
        return {}
    return config


def _string(config: dict, key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def build_receiver(config: dict, resumer, client_factory: Callable[[], AsyncClient] | None = None) -> Receiver:
    """An SDK Receiver for *config*. An unusable signing secret leaves it without one (every event: 401).

    The client that reads resolved requests comes from ``client_factory`` or, normally, from the
    ``instance_token`` in receiver.json; without either (before provisioning) wakes are skipped.
    """
    secret = _string(config, "signing_secret")
    token = _string(config, "instance_token")
    base_url = _string(config, "aap_base_url") or DEFAULT_AAP_BASE_URL
    factory = client_factory
    if factory is None and token is not None:
        def factory():
            return AsyncClient(base_url, token)
    kwargs = dict(resumers={FRAMEWORK: resumer}, client=factory,
                  registration_id=_string(config, "registration_id"), instance_id=_string(config, "instance_id"))
    try:
        return Receiver(secret=secret, **kwargs)
    except ValueError:  # not a whsec_ secret / not base64: refuse everything instead of crash-looping
        log.error("signing_secret in receiver.json is not a valid whsec_ secret: refusing all events")
        return Receiver(secret=None, **kwargs)


def _read_wake_secret(path) -> str:
    try:
        secret = Path(path).read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"wake secret file unreadable ({type(exc).__name__})") from None
    if not secret:
        raise RuntimeError("wake secret file is empty")
    return secret


def profile_of(handle: dict) -> str | None:
    """The Hermes profile in the handle's session key ``agent:<profile>:<platform>:...``, or None for the default
    profile (``agent:main:...``, no session key, or one that does not look like that)."""
    key = handle.get("session_key")
    parts = key.split(":") if isinstance(key, str) else []
    if len(parts) < 2 or parts[0] != "agent" or parts[1] in ("", "main", "default"):
        return None
    return parts[1]


def make_resumer(wake_secret_file, wake_url: str, post=post_wake, wake_base: str = DEFAULT_WAKE_BASE):
    """The ``hermes`` resumer: ``resumer(handle, request)`` -> signed wake for ``handle["park_id"]``.

    The wake goes to the profile that made the call (Hermes lets a plugin post only into its own profile's chats):
    the default profile to ``wake_url``, any other profile to its own route under ``wake_base``. A profile name
    that is not valid is not posted to; the sweep retries from the ledger row.

    Errors propagate to the SDK, which logs "resuming the run for request ... failed"; the cron sweep
    re-posts wakes for anything still undecided/unnotified, so nothing is lost.
    """

    async def resume(handle, request) -> None:
        park_id = handle.get("park_id")
        if not isinstance(park_id, str) or not park_id:
            log.warning("request %s: hermes resume handle has no park_id; not waking", request.id)
            return
        profile = profile_of(handle)
        url = wake_url
        if profile is not None:
            try:
                url = wake_url_for(profile, wake_base)
            except ValueError:
                log.warning("request %s: %s has a session key with an invalid profile name; not waking "
                            "(the sweep will retry)", request.id, park_id)
                return
        secret = _read_wake_secret(wake_secret_file)
        status = await asyncio.to_thread(post, park_id, secret, url)
        if 200 <= status < 300:
            log.info("woke %s for request %s (%s): HTTP %s", park_id, request.id, request.status, status)
        else:
            log.warning("Hermes refused the wake for %s (request %s): HTTP %s; the sweep will retry",
                        park_id, request.id, status)

    return resume


def create_app(*, config_path=DEFAULT_CONFIG, wake_secret_file=DEFAULT_WAKE_SECRET_FILE,
               wake_url: str = DEFAULT_WAKE_URL, wake_base: str = DEFAULT_WAKE_BASE, client_factory=None,
               post=post_wake) -> Starlette:
    """The Starlette app: ``POST /aap/events`` only. Configuration is read once, at start."""
    receiver = build_receiver(load_config(config_path), make_resumer(wake_secret_file, wake_url, post, wake_base),
                              client_factory)
    app = Starlette(routes=[Route(EVENTS_PATH, receiver.asgi(), methods=["POST"])])
    app.state.receiver = receiver
    return app


def main() -> None:  # pragma: no cover - exercised by the systemd unit
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(config_path=os.environ.get("DURABLE_PARK_RECEIVER_CONFIG", DEFAULT_CONFIG)),
                host=HOST, port=PORT, proxy_headers=False, server_header=False, date_header=False,
                log_config=None)


if __name__ == "__main__":
    main()
