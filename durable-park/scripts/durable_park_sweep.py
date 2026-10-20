#!/usr/bin/env python3
"""durable-park sweep: a Hermes cron ``--no-agent`` script (every 5 minutes).

* a ``parked`` row with an approval request          -> post a wake (a decision may have been missed)
* an ``approved`` row the chat was not told about    -> post a wake (the first injection failed)
* a denied/expired/cancelled row with a request that the chat was not told about -> post a wake
* an ``executing`` row older than 10 minutes         -> mark ``unknown`` and print one alert line

Only stdout is delivered by cron, so it prints nothing when nothing needs attention; problems with posting
wakes go to stderr. Standard library only: it loads the plugin's ledger module by path (without importing
the plugin package, which needs the AAP SDK) and ``receiver/wake_post.py``, both found relative to this
file's real path, so it can be run through a symlink or from any directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "receiver"))

from wake_post import DEFAULT_WAKE_URL, post_wake  # noqa: E402

DEFAULT_SECRET_FILE = "/etc/durable-park/wake_secret"
STALE_EXECUTING = timedelta(minutes=10)
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def _load_ledger_module():
    path = os.path.join(_REPO, "plugin", "durable_park", "ledger.py")
    spec = importlib.util.spec_from_file_location("durable_park_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered while it executes
    spec.loader.exec_module(module)
    return module


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="durable-park sweep (Hermes cron --no-agent script)")
    parser.add_argument("--ledger", required=True, help="path of the ledger SQLite file")
    parser.add_argument("--wake-secret-file", default=DEFAULT_SECRET_FILE)
    parser.add_argument("--wake-url", default=DEFAULT_WAKE_URL)
    return parser.parse_args(argv)


def _err(message: str) -> None:
    print(f"durable-park sweep: {message}", file=sys.stderr)


def _age_start(row) -> datetime | None:
    for value in (row.executing_at, row.updated_at):  # executing_at is always set by the executor
        if value:
            try:
                return datetime.strptime(value, _STAMP).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _mark_stale_executing(ledger, now: datetime) -> None:
    """Mark stale executing rows unknown and print one alert line each, flushed right after the winning
    CAS: the row never returns to ``executing``, so a line lost to a later failure would be lost for good."""
    for row in ledger.list_by_status("executing"):
        started = _age_start(row)
        if started is None or now - started <= STALE_EXECUTING:
            continue
        # CAS: only the sweep that wins the transition alerts, so the line is printed once.
        if ledger.transition(row.park_id, "executing", "unknown", note="no result recorded after 10 minutes"):
            print(f"durable-park: {row.park_id} outcome UNKNOWN \u2014 check {row.tool} in the sandbox "
                  "before retrying", flush=True)


def _wake_targets(ledger) -> list[str]:
    parked = [r.park_id for r in ledger.list_by_status("parked") if r.request_id]
    approved = [r.park_id for r in ledger.list_by_status("approved") if r.notified_at is None]
    # A decision the wake recorded but could not announce (inject refused): only rows that have a request
    # (a park-hook cancel without one was never a withHuman decision and is not announced).
    decided = [r.park_id for r in ledger.list_by_status("denied", "expired", "cancelled")
               if r.notified_at is None and r.request_id]
    return parked + approved + decided


def _read_secret(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not os.path.isfile(args.ledger):
        _err(f"ledger not found: {args.ledger}")
        return 1
    ledger = _load_ledger_module().Ledger(args.ledger)

    _mark_stale_executing(ledger, datetime.now(timezone.utc))
    status = 0
    targets = _wake_targets(ledger)
    if targets:
        secret = _read_secret(args.wake_secret_file)
        if secret is None:
            _err(f"wake secret unreadable or empty: {args.wake_secret_file}")
            status = 1
        else:
            for park_id in targets:
                try:
                    code = post_wake(park_id, secret, args.wake_url)
                except Exception as exc:  # noqa: BLE001 - one dead wake must not stop the others
                    _err(f"wake {park_id} failed ({type(exc).__name__})")
                    continue
                if not 200 <= code < 300:
                    _err(f"wake {park_id} returned HTTP {code}")
    return status


if __name__ == "__main__":
    sys.exit(main())
