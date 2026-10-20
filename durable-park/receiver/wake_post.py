"""Post a signed wake to Hermes's local webhook route (standard library only).

Used by the receiver (resumer) and by the cron sweep, so it must not need any third-party package.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request

DEFAULT_WAKE_URL = "http://127.0.0.1:8644/webhooks/durable-park-wake"
TIMEOUT_SECONDS = 10.0


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
