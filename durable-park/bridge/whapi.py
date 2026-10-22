"""Read-only lister of withHuman approval requests for the bridge.

The bridge asks withHuman (``GET <base>/requests``) instead of reading the local ledger, with the organization
key in ``/etc/durable-park/audit_key`` (it can read requests and audit events, nothing else). The key file is
read on every call so a rotation takes effect without a restart; the key is never logged, never part of an
exception message and never in a ``repr``. Any failure is a ``ListError`` with a short safe ``code``; the
caller turns it into "ask a human".
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

DEFAULT_API_BASE = "https://app.withhuman.ai/api/v1"
DEFAULT_KEY_FILE = "/etc/durable-park/audit_key"
MAX_LIMIT = 200


class ListError(Exception):
    """A listing failed. ``code`` is one of: ``no-key``, ``deadline``, ``http-<status>``, ``shape``,
    ``transport``, ``bad-base-url``. It never carries the key, a URL or a response body."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RequestLister:
    def __init__(self, base_url: str, key_file: str, *, per_call_timeout: float = 3.0,
                 transport: httpx.BaseTransport | None = None, clock=time.monotonic):
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ListError("bad-base-url")
        self._base_url = base_url.rstrip("/")
        self._key_file = key_file
        self._per_call_timeout = per_call_timeout
        self._transport = transport
        self._clock = clock

    def __repr__(self) -> str:
        return f"RequestLister(base_url={self._base_url!r}, key_file={str(self._key_file)!r})"

    def _read_key(self) -> str:
        try:
            key = Path(self._key_file).read_text().strip()
        except (OSError, ValueError):  # missing, unreadable, or not text (UnicodeDecodeError)
            key = ""
        if not key:
            raise ListError("no-key")
        return key

    def list(self, *, agent_slug: str, q: str, limit: int = 50, deadline: float | None = None) -> list:
        timeout = self._per_call_timeout
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ListError("deadline")
            timeout = min(timeout, remaining)
        key = self._read_key()
        params = {"agent_slug": agent_slug, "q": q, "limit": max(1, min(MAX_LIMIT, int(limit)))}
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False) as client:
                response = client.get(f"{self._base_url}/requests", params=params, headers=headers,
                                      timeout=timeout)
        except Exception:  # any httpx (or lower-level) failure; its text may echo the request, so drop it
            raise ListError("transport") from None
        if response.status_code != 200:
            raise ListError(f"http-{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise ListError("shape") from None
        requests = body.get("requests") if isinstance(body, dict) else None
        if not isinstance(requests, list):
            raise ListError("shape")
        return requests
