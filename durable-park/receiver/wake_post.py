"""Post a signed wake to Hermes's local webhook route (standard library only).

Used by the receiver (resumer) and by the cron sweep, so it must not need any third-party package.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8644"
DEFAULT_WAKE_URL = f"{DEFAULT_BASE_URL}/webhooks/durable-park-wake"
# The profile-name rule of plugin/durable_park/keys.py, repeated because this file is copied on its own to
# /opt/durable-park/receiver (tests/test_keys.py keeps the two equal).
PROFILE_PATTERN = r"[a-z0-9][a-z0-9_-]{0,62}"
RESERVED_PROFILES = frozenset({"default", "main"})  # ``main`` is how Hermes names the default profile in a session key
_PROFILE = re.compile(PROFILE_PATTERN)
TIMEOUT_SECONDS = 10.0


def wake_url_for(profile: str | None, base: str = DEFAULT_BASE_URL) -> str:
    """The wake address of the Hermes profile that made the call.

    Hermes lets a plugin post only into its own profile's chats, so each profile has its own webhook route
    (``/p/<profile>/webhooks/durable-park-wake-<profile>``) and its own copy of durable-park handles it. ``None``,
    ``""``, ``"default"`` and ``"main"`` (the default profile) use the original route. Raises ``ValueError`` for any
    other name that is not a valid profile name (a URL is never built from it).
    """
    base = base.rstrip("/")
    if profile is None or profile in ("", "default", "main"):
        return f"{base}/webhooks/durable-park-wake"
    if not isinstance(profile, str) or profile in RESERVED_PROFILES or _PROFILE.fullmatch(profile) is None:
        raise ValueError("not a valid profile name")
    return f"{base}/p/{profile}/webhooks/durable-park-wake-{profile}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx as a status instead of re-sending the signed body elsewhere


# No env proxies: this only ever talks to loopback Hermes.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def post_wake(park_id: str, secret: str, url: str = DEFAULT_WAKE_URL) -> int:
    """POST ``{"type":"wake","park_id":...}`` signed for Hermes's webhook platform; return the HTTP status.

    Signature (gateway/platforms/webhook.py ``_validate_signature``): header ``X-Hub-Signature-256`` =
    ``"sha256=" + hex(HMAC-SHA256(secret, raw_body))`` over the exact bytes sent.

    Any HTTP response, including 4xx/5xx and redirects, is returned as its status code (Hermes answers
    202 for an accepted wake). A connection-level failure (refused, DNS, timeout) raises ``OSError``
    (``urllib.error.URLError`` / ``TimeoutError``) for the caller to log; the secret is never in the message.
    """
    body = json.dumps({"type": "wake", "park_id": park_id}, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as err:
        err.close()
        return err.code
