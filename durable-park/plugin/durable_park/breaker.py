"""Circuit breaker for withHuman outages.

When withHuman cannot be reached the SDK answers ``Deny(status="error")`` for every call. After ``threshold``
consecutive errors the breaker opens: ``allow_attempt()`` is False for ``cooldown`` seconds, so the park hook
stops hammering the service and refuses at once. Once the cooldown has passed the next attempt goes through
(half-open): a failure re-opens with a fresh cooldown, a success closes.

The breaker is pure state plus a status file (``breaker_status.json`` next to the ledger) that is rewritten
only when it opens or closes; the sweep turns that file into one alert on open and one on close. It never
calls withHuman. A status-file write error is logged and never raised. On construction a status file that still
says open (the gateway restarted while withHuman was unreachable) is adopted as open and half-open at once, so the
first success records the close.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("durable_park")

STATUS_FILENAME = "breaker_status.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Breaker:
    def __init__(self, status_path: str | None, *, threshold: int = 3, cooldown: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, wallclock: Callable[[], str] = _utc_now) -> None:
        self._path = status_path
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._wallclock = wallclock
        self._lock = threading.Lock()
        self._failures = 0
        self._open = False
        self._retry_at = 0.0
        self._opened_at: str | None = None
        self._closed_at: str | None = None
        self._adopt_status_file()

    def _adopt_status_file(self) -> None:
        """After a restart, a status file that still says ``open: true`` is adopted as open and half-open at
        once (``retry_at = now``): the first success writes the close and the sweep sends the recovery line,
        instead of the file staying open forever. A missing or unreadable file means closed. Never raises."""
        if not self._path:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                status = json.load(handle)
        except Exception:  # noqa: BLE001 - missing or corrupt: start closed, as before
            return
        if isinstance(status, dict) and status.get("open") is True:
            self._open = True
            self._retry_at = self._clock()
            opened_at = status.get("opened_at")
            self._opened_at = opened_at if isinstance(opened_at, str) else None

    def allow_attempt(self) -> bool:
        with self._lock:
            return not (self._open and self._clock() < self._retry_at)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._open:
                self._open = False
                self._closed_at = self._wallclock()
                self._write()

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures < self._threshold:
                return
            self._retry_at = self._clock() + self._cooldown
            if not self._open:
                self._open = True
                self._opened_at = self._wallclock()
                self._write()

    def _write(self) -> None:
        """Atomically record the open/close change. Called with the lock held; never raises."""
        if not self._path:
            return
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".breaker-", dir=os.path.dirname(os.path.abspath(self._path)))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"open": self._open, "opened_at": self._opened_at, "closed_at": self._closed_at}, handle)
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001 - a status file problem must never change a decision
            log.warning("breaker status file not written (%s)", type(exc).__name__)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
