"""The circuit breaker: stop asking withHuman while it is unreachable; record open/close for the sweep."""

import json
import logging
import os

from durable_park import breaker as breaker_mod
from durable_park.breaker import STATUS_FILENAME, Breaker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path, clock, **kw):
    stamps = iter(f"2026-10-21T10:00:{i:02d}Z" for i in range(60))
    return Breaker(str(tmp_path / STATUS_FILENAME), clock=clock, wallclock=lambda: next(stamps), **kw)


def test_opens_after_three_failures_and_blocks_for_cooldown(tmp_path):
    clock = Clock()
    b = make(tmp_path, clock)
    b.record_failure()
    b.record_failure()
    assert b.allow_attempt() is True
    b.record_failure()
    assert b.allow_attempt() is False
    clock.now += 29
    assert b.allow_attempt() is False
    clock.now += 2
    assert b.allow_attempt() is True


def test_success_resets_failure_count(tmp_path):
    b = make(tmp_path, Clock())
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.allow_attempt() is True


def test_failure_after_cooldown_reopens_and_success_closes(tmp_path):
    clock = Clock()
    b = make(tmp_path, clock)
    for _ in range(3):
        b.record_failure()
    clock.now += 31
    assert b.allow_attempt() is True  # half-open: one try
    b.record_failure()
    assert b.allow_attempt() is False  # fresh cooldown
    clock.now += 29
    assert b.allow_attempt() is False
    clock.now += 2
    assert b.allow_attempt() is True
    b.record_success()
    assert b.allow_attempt() is True
    b.record_failure()
    b.record_failure()
    assert b.allow_attempt() is True  # failure count restarted at zero


def test_status_file_written_only_on_change(tmp_path, monkeypatch):
    writes = []
    real = os.replace
    monkeypatch.setattr(breaker_mod.os, "replace", lambda a, b: (writes.append(b), real(a, b))[1])
    clock = Clock()
    b = make(tmp_path, clock)
    path = tmp_path / STATUS_FILENAME
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert writes == [] and not path.exists()
    b.record_failure()  # opens
    assert len(writes) == 1
    opened = json.loads(path.read_text())
    assert set(opened) == {"open", "opened_at", "closed_at"}
    assert opened["open"] is True and opened["opened_at"] == "2026-10-21T10:00:00Z" and opened["closed_at"] is None
    clock.now += 31
    b.record_failure()  # re-open after a failed half-open try: no change in open/close
    assert len(writes) == 1
    b.record_success()  # closes
    assert len(writes) == 2
    closed = json.loads(path.read_text())
    assert closed["open"] is False and closed["opened_at"] == "2026-10-21T10:00:00Z"
    assert closed["closed_at"] is not None and closed["closed_at"] != closed["opened_at"]
    b.record_success()
    assert len(writes) == 2


def test_default_wallclock_is_utc_iso_seconds(tmp_path):
    b = Breaker(str(tmp_path / STATUS_FILENAME))
    for _ in range(3):
        b.record_failure()
    opened_at = json.loads((tmp_path / STATUS_FILENAME).read_text())["opened_at"]
    from datetime import datetime
    datetime.strptime(opened_at, "%Y-%m-%dT%H:%M:%SZ")


def test_unwritable_status_path_never_raises(tmp_path, caplog):
    b = Breaker(str(tmp_path / "missing-dir" / STATUS_FILENAME), clock=Clock())
    with caplog.at_level(logging.WARNING, logger="durable_park"):
        for _ in range(3):
            b.record_failure()
        assert b.allow_attempt() is False  # state still works
        b.record_success()
    assert b.allow_attempt() is True
    assert any("breaker" in r.getMessage() for r in caplog.records)


def test_no_status_path_still_works(tmp_path):
    b = Breaker(None, clock=Clock())
    for _ in range(3):
        b.record_failure()
    assert b.allow_attempt() is False
