#!/usr/bin/env python3
"""durable-park sweep: a Hermes cron ``--no-agent`` script (every 5 minutes).

* a ``parked`` row with an approval request          -> post a wake (a decision may have been missed)
* an ``approved`` row the chat was not told about    -> post a wake (the first injection failed)
* a denied/expired/cancelled row with a request that the chat was not told about -> post a wake
* a ``parked`` row with no request id attached older than 10 minutes -> cancel it and print one alert line
  (a request may still be open at withHuman; approving it does nothing)
* an ``executing`` row older than 10 minutes         -> mark ``unknown`` and print one alert line
* ``allowed``/``refused``/``cancelled`` rows older than ``--retention-days`` (default 30) are deleted, silently
* the gateway's form-check report (``formcheck_status.json`` next to the ledger) missing, older than
  75 minutes or failing on two sweeps in a row        -> print one alert line (and one when it recovers)
* the park hook's circuit-breaker report (``breaker_status.json``: withHuman unreachable) opened or closed
                                                       -> print one alert line for each change

Only stdout is delivered by cron, so it prints nothing when nothing needs attention; problems with posting
wakes go to stderr. Standard library only: it loads the plugin's ledger module by path (without importing
the plugin package, which needs the AAP SDK) and ``receiver/wake_post.py``, both found relative to this
file's real path, so it can be run through a symlink or from any directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import tempfile
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "receiver"))

from wake_post import DEFAULT_WAKE_URL, post_wake  # noqa: E402

DEFAULT_SECRET_FILE = "/etc/durable-park/wake_secret"
STALE_EXECUTING = timedelta(minutes=10)
ORPHAN_PARK = timedelta(minutes=10)  # a parked row with no request id this old will never get one attached
DEFAULT_RETENTION_DAYS = 30
FORMCHECK_STATUS = "formcheck_status.json"
FORMCHECK_STATE = "formcheck_alert_state.json"
FORMCHECK_STALE = timedelta(minutes=75)  # the gateway re-checks every 30 minutes
FORMCHECK_BAD_RUNS = 2  # one bad sweep may be a restart in progress
BREAKER_STATUS = "breaker_status.json"
BREAKER_STATE = "breaker_alert_state.json"
POLICY_STATUS = "tool_policy_status.json"
POLICY_STATE = "tool_policy_alert_state.json"
POLICY_LOAD = "tool_policy_load.json"  # the plugin's own verdict at load (it judges the report's age)
POLICY_LOAD_STATE = "tool_policy_load_alert_state.json"
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]+")
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def _load_ledger_module():
    path = os.path.join(_REPO, "plugin", "durable_park", "ledger.py")
    spec = importlib.util.spec_from_file_location("durable_park_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered while it executes
    spec.loader.exec_module(module)
    return module


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="durable-park sweep (Hermes cron --no-agent script)")
    parser.add_argument("--ledger", required=True, help="path of the ledger SQLite file")
    parser.add_argument("--wake-secret-file", default=DEFAULT_SECRET_FILE)
    parser.add_argument("--wake-url", default=DEFAULT_WAKE_URL)
    parser.add_argument("--retention-days", type=_positive_int, default=DEFAULT_RETENTION_DAYS,
                        help="delete allowed/refused/cancelled rows not updated for this many days (>= 1)")
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


def _cancel_orphan_parks(ledger, now: datetime) -> None:
    """Cancel ``parked`` rows with no request id attached so they stop blocking their chat. Usually the gateway
    died before asking, but a request MAY exist at withHuman: the gateway died between withHuman answering
    ``Pending`` and ``set_request_id``, or a cancel of a request created before an error failed. Such a request
    can stay open there; approving it does nothing (the row is ``cancelled``, so the bridge and the executor
    fail closed). The CAS also loses cleanly to a hook that attaches a request at the same moment
    (``set_request_id`` needs ``parked``)."""
    for row in ledger.list_by_status("parked"):
        if row.request_id:
            continue
        try:
            created = datetime.strptime(row.created_at, _STAMP).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if now - created <= ORPHAN_PARK:
            continue
        if ledger.transition(row.park_id, "parked", "cancelled",
                             note="no approval request after 10 minutes (cancelled by the sweep)"):
            print(f"durable-park: {row.park_id} ({row.tool}) never got an approval request; "
                  "cancelled after 10 minutes", flush=True)


def _apply_retention(ledger, now: datetime, days: int) -> None:
    """Delete finished rows with nothing left to decide (``allowed``, ``refused``, ``cancelled``) once they are
    ``days`` old. Prints nothing; SQLite keeps the freed pages for reuse (the file shrinks only on VACUUM)."""
    ledger.delete_finished_before((now - timedelta(days=days)).strftime(_STAMP))


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


def _clean(text: str, limit: int = 400) -> str:
    text = _UNPRINTABLE.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _formcheck_problem(status_path: str, now: datetime) -> str | None:
    """What is wrong with the gateway's form-check report, or ``None`` when it is fresh and passing."""
    try:
        with open(status_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except FileNotFoundError:
        return "no form-check report from the gateway (is it running with durable-park?)"
    except (OSError, ValueError):
        return "the form-check report is unreadable"
    if not isinstance(report, dict):
        return "the form-check report is unreadable"
    try:
        checked = datetime.strptime(str(report.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "the form-check report is unreadable"
    age = now - checked
    if age > FORMCHECK_STALE:
        return f"the gateway's form check last ran {int(age.total_seconds() // 60)} minutes ago (is the gateway running?)"
    if report.get("ok") is not True:
        problems = report.get("problems")
        if isinstance(problems, list) and problems:
            return "; ".join(_clean(p, 200) for p in problems[:3])
        return "the form check failed"
    return None


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".formcheck-alert-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _formcheck_alert(ledger_path: str, now: datetime) -> None:
    """Alert once when the form check stays broken for two sweeps, and once when it recovers."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    state_path = os.path.join(directory, FORMCHECK_STATE)
    problem = _formcheck_problem(os.path.join(directory, FORMCHECK_STATUS), now)
    state = _load_state(state_path)
    bad_runs = state.get("bad_runs") if isinstance(state.get("bad_runs"), int) else 0
    alerted = state.get("alerted") is True
    if problem is not None:
        bad_runs += 1
        if bad_runs >= FORMCHECK_BAD_RUNS and not alerted:
            print(f"durable-park ALERT: the form checker is not working \u2014 {_clean(problem)}. "
                  "Risky actions will be refused until it is fixed.", flush=True)
            alerted = True
    else:
        if alerted:
            print("durable-park: the form checker is working again.", flush=True)
        bad_runs, alerted = 0, False
    _save_state(state_path, {"bad_runs": bad_runs, "alerted": alerted})


def _breaker_alert(ledger_path: str, now: datetime) -> None:
    """Announce each withHuman outage once when it opens and once when it closes (``now`` is unused: the
    status file carries its own times). The state file keeps the ``opened_at``/``closed_at`` already announced,
    so an outage that opened and closed between two sweeps still prints both lines."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    status = _load_state(os.path.join(directory, BREAKER_STATUS))  # missing or unreadable -> {}
    opened_at, closed_at = status.get("opened_at"), status.get("closed_at")
    state_path = os.path.join(directory, BREAKER_STATE)
    state = _load_state(state_path)
    announced = dict(state)
    if isinstance(opened_at, str) and opened_at and opened_at != state.get("opened_at"):
        print(f"durable-park ALERT: withHuman could not be reached (since {_clean(opened_at, 40)}); "
              "every tool call is being refused until it answers again.", flush=True)
        announced["opened_at"] = opened_at
    if status.get("open") is False and isinstance(closed_at, str) and closed_at and closed_at != state.get("closed_at"):
        print(f"durable-park: withHuman answers again (since {_clean(closed_at, 40)}); "
              "tool calls are decided normally.", flush=True)
        announced["closed_at"] = closed_at
    if announced != state:
        _save_state(state_path, announced)


def _policy_problem(status_path: str) -> str | None:
    """What is wrong with the report deploy/tool_policy_check.sh wrote at the last gateway start, or ``None``
    when it passed. Age is not judged here: the report is written only at gateway start, so an old passing
    report on a long-running gateway is normal (the plugin judges freshness at load)."""
    try:
        with open(status_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except FileNotFoundError:
        return "no tool policy report"
    except (OSError, ValueError):
        return "the tool policy report is unreadable"
    if not isinstance(report, dict):
        return "the tool policy report is unreadable"
    if report.get("ok") is True:
        return None
    problems = report.get("problems")
    if isinstance(problems, list) and problems:
        return "; ".join(_clean(p, 200) for p in problems[:3])
    return "the tool policy check failed"


def _policy_alert(ledger_path: str, now: datetime) -> None:
    """Alert once when the tool policy check fails at gateway start, and once when it passes again (``now`` is
    unused: the report carries its own time)."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    problem = _policy_problem(os.path.join(directory, POLICY_STATUS))
    state_path = os.path.join(directory, POLICY_STATE)
    alerted = _load_state(state_path).get("alerted") is True
    if problem is not None and not alerted:
        print(f"durable-park ALERT: the tool policy check failed at gateway start \u2014 {_clean(problem)}. "
              "Every tool call is refused until it passes.", flush=True)
        alerted = True
    elif problem is None and alerted:
        print("durable-park: the tool policy check passes again.", flush=True)
        alerted = False
    _save_state(state_path, {"alerted": alerted})
    _policy_load_alert(directory)


def _policy_load_alert(directory: str) -> None:
    """Alert once when the plugin reports at load that the tool policy check did not pass (it also catches a
    stale report, which the check above cannot see), and once when a later load passes. A missing or unreadable
    verdict file is silent (an older plugin, or one that has not loaded yet)."""
    verdict = _load_state(os.path.join(directory, POLICY_LOAD))  # missing or unreadable -> {}
    ok = verdict.get("ok")
    state_path = os.path.join(directory, POLICY_LOAD_STATE)
    alerted = _load_state(state_path).get("alerted") is True
    if ok is False and not alerted:
        problem = verdict.get("problem")
        reason = _clean(problem, 200) if isinstance(problem, str) and problem else "the tool policy check failed"
        print("durable-park ALERT: the tool policy check did not pass when the plugin loaded \u2014 "
              f"{reason}. Every tool call is refused until an admin fixes it and restarts the gateway.", flush=True)
        alerted = True
    elif ok is True and alerted:
        print("durable-park: the tool policy check passed when the plugin loaded.", flush=True)
        alerted = False
    _save_state(state_path, {"alerted": alerted})


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
    try:
        _cancel_orphan_parks(ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"orphan cancel failed ({type(exc).__name__})")
    try:
        _formcheck_alert(args.ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the self-check alert must never stop the sweep
        _err(f"form-check alert failed ({type(exc).__name__})")
    try:
        _breaker_alert(args.ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the breaker alert must never stop the sweep
        _err(f"breaker alert failed ({type(exc).__name__})")
    try:
        _policy_alert(args.ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the policy alert must never stop the sweep
        _err(f"tool policy alert failed ({type(exc).__name__})")
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
    try:
        _apply_retention(ledger, datetime.now(timezone.utc), args.retention_days)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the sweep
        _err(f"retention failed ({type(exc).__name__})")
    return status


if __name__ == "__main__":
    sys.exit(main())
