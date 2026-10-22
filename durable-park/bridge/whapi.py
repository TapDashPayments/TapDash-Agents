"""Read-only lister of withHuman approval requests for the bridge.

The bridge asks withHuman (``GET <base>/requests``) instead of reading the local ledger, with the organization
key in ``/etc/durable-park/audit_key`` (it can read requests and audit events, nothing else). The key file is
read on every call so a rotation takes effect without a restart; the key is never logged, never part of an
exception message and never in a ``repr``. Any failure is a ``ListError`` with a short safe ``code``; the
caller turns it into "ask a human".
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx

DEFAULT_API_BASE = "https://app.withhuman.ai/api/v1"
DEFAULT_KEY_FILE = "/etc/durable-park/audit_key"
MAX_LIMIT = 200
MAX_BODY_BYTES = 4 * 1024 * 1024


class ListError(Exception):
    """A listing failed. ``code`` is one of: ``no-key``, ``deadline``, ``http-<status>``, ``shape``,
    ``transport``, ``too-large``, ``bad-base-url``. It never carries the key, a URL or a response body."""

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

    def list(self, *, agent_slug: str, q: str | None = None, limit: int = 50,
             deadline: float | None = None) -> list:
        start = self._clock()
        timeout = self._per_call_timeout
        if deadline is not None:
            remaining = deadline - start
            if remaining <= 0:
                raise ListError("deadline")
            timeout = min(timeout, remaining)
        # The wall-clock limit for the whole call, body included: the caller's deadline, else start + timeout.
        limit_at = deadline if deadline is not None else start + self._per_call_timeout
        key = self._read_key()
        params = {"agent_slug": agent_slug, "limit": max(1, min(MAX_LIMIT, int(limit)))}
        if q is not None:
            params["q"] = q
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        # httpx's timeouts are per phase and per read (httpcore fixes the read timeout when the body starts), so a
        # server that stalls just before the budget ends could hold the caller for one more timeout, per phase. The
        # fetch therefore runs in a worker thread and the caller waits for it only until the budget is spent. An
        # abandoned worker ends by itself at its own httpx timeout and holds no state but its own connection.
        outcome: dict = {}

        def work() -> None:
            try:
                outcome["requests"] = self._fetch(params, headers, timeout, limit_at)
            except ListError as exc:
                outcome["error"] = exc
            except BaseException:  # noqa: BLE001 - a worker thread must never die with an unhandled exception
                outcome["error"] = ListError("transport")

        worker = threading.Thread(target=work, name="bridge-whapi-list", daemon=True)
        worker.start()
        worker.join(max(0.0, limit_at - self._clock()))
        if worker.is_alive():
            raise ListError("deadline")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["requests"]

    def _fetch(self, params: dict, headers: dict, timeout: float, limit_at: float) -> list:
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", f"{self._base_url}/requests", params=params, headers=headers,
                                   timeout=timeout) as response:
                    if response.status_code != 200:
                        raise ListError(f"http-{response.status_code}")
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BODY_BYTES:
                            raise ListError("too-large")
                        chunks.append(chunk)
                        if not self._clock() < limit_at:
                            raise ListError("deadline")
        except ListError:
            raise
        except Exception:  # any httpx (or lower-level) failure; its text may echo the request, so drop it
            raise ListError("transport") from None
        try:
            body = json.loads(b"".join(chunks))
        except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
            raise ListError("shape") from None
        requests = body.get("requests") if isinstance(body, dict) else None
        if not isinstance(requests, list):
            raise ListError("shape")
        return requests
