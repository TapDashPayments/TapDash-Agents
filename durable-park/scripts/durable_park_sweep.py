#!/usr/bin/env python3
"""durable-park sweep: a Hermes cron ``--no-agent`` script (every 5 minutes).

* a ``parked`` row with an approval request          -> post a wake (a decision may have been missed)
* an ``executed``/``failed``/``unknown`` row whose outcome the chat was not told, ended 2 minutes to 24 hours ago
                                                       -> post a wake (the wake handler tells the chat; GitHub #66)
* the same, ended more than 24 hours ago               -> one alert line per park (``outcome_giveup_state.json``)
* an ``approved`` row the chat was not told about    -> post a wake (the first injection failed)
* a denied/expired/cancelled row with a request that the chat was not told about -> post a wake
* a ``parked`` row with no request id attached older than 10 minutes -> cancel it and print one alert line
  (a request may still be open at withHuman; approving it does nothing)
* an ``executing`` row older than 10 minutes         -> mark ``unknown`` and print one alert line
* ``allowed``/``refused``/``cancelled`` rows older than ``--retention-days`` (default 30) are deleted, silently
* the gateway's form-check report (``formcheck_status.json`` next to the ledger) missing, older than
  75 minutes or failing on two sweeps in a row        -> print one alert line (and one when it recovers)
* the gateway has no load proof (``plugin_load.json`` missing, unreadable or for another pid than the running
  gateway's MainPID)                                 -> print one alert line (and one when it recovers)
* the park hook's circuit-breaker report (``breaker_status.json``: withHuman unreachable) opened or closed
                                                       -> print one alert line for each change
* the park hook's note-lift report (``lift_status.json``: the agent's ``withhuman_context`` note was not moved
  to the top level, was dropped, could not be checked for, or a tool declares its own field)
                                                       -> print one alert line per (tool, kind) with new places
  (a (tool, kind, key, detail) not alerted before; state in ``lift_alert_state.json``; the line names the first
  place and says "+N more places, e.g. ..." for others), again once per 24 h while a count keeps growing (never for
  ``declared``, ``note_cut`` and ``note_not_text``, which are said once per tool), one when the report itself is unreadable (until it reads again), and one
  when the check itself fails (``lift_check_alert_state.json``; a recovery line follows)
* a ``parked`` row with an approval request nobody answered (TAP2-121, only when ``--approvers-file`` exists):
  after ``primary_minutes`` it is routed to the configured backup approver (one alert line naming them and ending
  with the link to the request's page in withHuman (``--withhuman-url``), also
  posted to their own Teams webhook when one is configured); after the backup's ``minutes`` too, or at once
  when no backup approver is configured, it is DENIED with a note saying why (the chat is told by the usual wake)
* the markers ``lift_status.unwritable.<pid>`` (one per process that could not write ``lift_status.json``)
                                                       -> print one alert line per marker (and one when the last
  marker is gone; state in ``lift_unwritable_alert_state.json``)

* Hermes is not on the pinned commit in ``deploy/hermes-pin.json``, or the update block (the checkout's git
  ``http.https://github.com/NousResearch/.proxy`` pointing at a dead address, GitHub #76) is off
                                                       -> print one alert line (again when the problem changes or
  after 24 hours, and one when it is fixed; state in ``hermes_pin_alert_state.json``)
* every per-status check above (form check, breaker, tool policy, lift, load proof) also runs for each routed
  person: their status directory is ``<status root>/profiles/<profile>/`` (the root is ``--status-root``, default
  the ledger's directory). A profile's lines name it: ``durable-park ALERT (profile sarah): ...``; the default
  profile's lines are unchanged
* a wake that could not be posted (an exception, or a non-2xx such as 404 for a missing wake route) on 2 sweeps in
  a row, once, then again after 24 hours (``wake_post_alert_state.json``)
* a ``parked`` or ``approved`` row whose profile has no key file (``missing_key_alert_state.json``), once per row
* a ``parked`` row that withHuman refused the key for 3 or more times (``wake_refusals.json``, which the plugin
  writes in each status directory), once per row
Only stdout is delivered by cron, so it prints nothing when nothing needs attention; problems with posting
wakes go to stderr. Standard library only: it loads the plugin's ledger module by path (without importing
the plugin package, which needs the AAP SDK) and ``receiver/wake_post.py``, both found relative to this
file's real path, so it can be run through a symlink or from any directory.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "receiver"))

from wake_post import DEFAULT_BASE_URL, DEFAULT_WAKE_URL, post_wake, wake_url_for  # noqa: E402

DEFAULT_SECRET_FILE = "/etc/durable-park/wake_secret"
DEFAULT_APPROVERS_FILE = "/etc/durable-park/approvers.json"  # TAP2-121; absent = no timeout, parks wait as before
APPROVER_STATE = "approver_alert_state.json"
PENDING_STATE = "pending_alert_state.json"  # GitHub #73 / TAP2-170: which waiting approvals were already announced
DEFAULT_PENDING_ALERT_MINUTES = 3  # 0 turns the fallback off
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
LIFT_STATUS = "lift_status.json"  # what the park hook could not do with the agent's withhuman_context note
LIFT_STATE = "lift_alert_state.json"
LIFT_CHECK_STATE = "lift_check_alert_state.json"  # the lift check itself failed
LIFT_UNWRITABLE = "lift_status.unwritable"  # prefix: ``<prefix>.<pid>`` exists while that process cannot write the report
LIFT_UNWRITABLE_STATE = "lift_unwritable_alert_state.json"
LIFT_REALERT = timedelta(hours=24)  # a problem that keeps happening is said again this often
LIFT_NO_REPEAT = frozenset({"declared", "note_cut", "note_not_text"})  # said once per tool, never again
LIFT_EXAMPLES = 3  # at most this many other places are named in an aggregated line
LIFT_TEXTS = {
    "declared": ("durable-park ALERT: {tool} has its own withhuman_context field inside {key}; the agent's note is "
                 "left there and goes to the server, not to the approver."),
    "schema_unavailable": ("durable-park ALERT: could not read {tool}'s tool rules, so the agent's note was not moved; "
                           "it may reach the server."),
    "args_unreadable": ("durable-park ALERT: could not read {tool}'s arguments, so the agent's note was not moved; "
                        "it may reach the server."),
    "lift_error": "durable-park ALERT: moving the agent's note failed for {tool}; it may reach the server.",
    "note_left_inside": ("durable-park ALERT: the agent's note is still inside {key} for {tool} after the move; it "
                         "may reach the server."),
    "note_dropped": ("durable-park ALERT: the agent's note inside {key} for {tool} was removed but not shown to the "
                     "approver ({reason})."),
    "check_incomplete": ("durable-park ALERT: could not finish checking {tool}'s arguments for the agent's note "
                         "(they are too large or nested too deep); it may reach the server."),
    "note_cut": ("durable-park ALERT: the agent's note for {tool} was longer than 2000 characters and was shortened "
                 "for the approver."),
    "note_not_text": ("durable-park ALERT: the agent's note for {tool} was empty or not text, so the approver saw "
                      "no reason."),
    "report_reset": ("durable-park ALERT: {tool} was unreadable and was replaced; the earlier lift problems are kept "
                     "in {tool}.corrupt."),
    "schema_too_complex": ("durable-park ALERT: the rules of {tool} are too complex to read for the agent's note "
                           "(inside {key}); the note was moved as if they declared no such field."),
}
PROFILES_DIR = "profiles"  # <status root>/profiles/<profile>/ holds a routed person's status files
WAKE_POST_STATE = "wake_post_alert_state.json"
WAKE_POST_BAD_RUNS = 2  # one bad sweep may be a gateway restart in progress
WAKE_POST_REALERT = timedelta(hours=24)
MISSING_KEY_STATE = "missing_key_alert_state.json"  # keys: ``nokey:<park id>``, ``unusable:<park id>``, ``refused:<park id>``, ``unreadable:<profile>``
WAKE_REFUSALS = "wake_refusals.json"  # written by the plugin: park id -> {profile, count, first_at}
REFUSED_ALERT_COUNT = 3
PLUGIN_LOAD = "plugin_load.json"  # the load proof the plugin writes in the gateway (pid of that process)
PLUGIN_LOAD_STATE = "plugin_load_alert_state.json"
HERMES_PIN_FILE = os.path.join(_REPO, "deploy", "hermes-pin.json")  # the one Hermes commit we run (GitHub #76)
HERMES_PIN_STATE = "hermes_pin_alert_state.json"
HERMES_BLOCK_KEY = "http.https://github.com/NousResearch/.proxy"  # set by deploy/hermes_pin.sh block
HERMES_BLOCK_VALUE = "http://127.0.0.1:9"  # nothing listens there, so every fetch from GitHub fails
HERMES_PIN_REALERT = timedelta(hours=24)
GIT_TIMEOUT = 5  # seconds
GATEWAY_UNIT = "hermes-gateway"
SYSTEMCTL_TIMEOUT = 5  # seconds
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]+")
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def _load_ledger_module():
    path = os.path.join(_REPO, "plugin", "durable_park", "ledger.py")
    spec = importlib.util.spec_from_file_location("durable_park_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered while it executes
    spec.loader.exec_module(module)
    return module


def _load_links_module():
    path = os.path.join(_REPO, "plugin", "durable_park", "links.py")
    spec = importlib.util.spec_from_file_location("durable_park_links", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_links = _load_links_module()


def _load_keys_module():
    path = os.path.join(_REPO, "plugin", "durable_park", "keys.py")
    spec = importlib.util.spec_from_file_location("durable_park_keys", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_keys = _load_keys_module()


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="durable-park sweep (Hermes cron --no-agent script)")
    parser.add_argument("--ledger", required=True, help="path of the ledger SQLite file")
    parser.add_argument("--wake-secret-file", default=DEFAULT_SECRET_FILE)
    parser.add_argument("--wake-url", default=DEFAULT_WAKE_URL, help="wake address of the default profile")
    parser.add_argument("--wake-base", default=DEFAULT_BASE_URL,
                        help="Hermes webhook address; a routed person's wake address is built from it")
    parser.add_argument("--status-root", default=None,
                        help="directory with the gateway's status files; a routed person's are in "
                             "<root>/profiles/<profile>/ (default: the ledger's directory)")
    parser.add_argument("--instances-dir", default=_keys.INSTANCES_DIR,
                        help="where each routed person's withHuman key file is (<dir>/<profile>.token)")
    parser.add_argument("--default-token-file", default=_keys.DEFAULT_TOKEN_FILE,
                        help="the default profile's withHuman key file")
    parser.add_argument("--approvers-file", default=DEFAULT_APPROVERS_FILE,
                        help="backup approver / unanswered-approval timeout config (TAP2-121); absent = off")
    parser.add_argument("--withhuman-url", default=_links.DEFAULT_APP_URL,
                        help="the withHuman app's address, for links to a request's page")
    parser.add_argument("--pending-alert-minutes", type=_non_negative_int, default=DEFAULT_PENDING_ALERT_MINUTES,
                        help="announce an approval still waiting after this many minutes, once, on the sweep's "
                             "alert channels (Telegram + TEAMS_ALERT_WEBHOOK_URL); 0 = off (GitHub #73)")
    parser.add_argument("--retention-days", type=_positive_int, default=DEFAULT_RETENTION_DAYS,
                        help="delete allowed/refused/cancelled rows not updated for this many days (>= 1)")
    return parser.parse_args(argv)


def _err(message: str) -> None:
    print(f"durable-park sweep: {message}", file=sys.stderr)


TEAMS_WEBHOOK_ENV = "TEAMS_ALERT_WEBHOOK_URL"  # a secret: read from the environment only, never printed
TEAMS_TIMEOUT_SECONDS = 10


class _Tee:
    """stdout that also keeps what was printed, so the alert lines can be copied to Teams."""

    def __init__(self, inner):
        self.inner = inner
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        self.lines.append(text)
        return self.inner.write(text)

    def flush(self) -> None:
        self.inner.flush()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _post_teams(webhook_url: str, text: str) -> None:
    """POST ``text`` to the Teams webhook (a Power Automate "Post card in a chat or channel" flow, which rejects
    plain ``{"text": ...}``) as an Adaptive Card. A failure is never silent and never stops the sweep: one plain
    ALERT line on stdout (so it reaches the chat like any other alert) and one line on stderr, both with only the
    error type or HTTP code, because the URL is a secret."""
    try:
        card = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "version": "1.4", "msteams": {"width": "Full"}, "summary": text,
                        "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
        request = urllib.request.Request(webhook_url, data=json.dumps(card).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TEAMS_TIMEOUT_SECONDS):
            pass
    except Exception as exc:  # noqa: BLE001 - a dead Teams webhook must not stop the sweep
        reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        _err(f"teams alert failed ({reason})")
        print(f"durable-park ALERT: Teams post failed ({reason}).", flush=True)


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


class ApproversConfigError(ValueError):
    pass


def load_approvers(path: str) -> dict | None:
    """The TAP2-121 settings, or None when the file does not exist (the feature is off and parks wait for their
    approver as before). Shape::

        {"primary_minutes": 60,
         "backup_approver": null | {"name": "<person>", "escalation_path": "<withHuman path key>",
                                    "minutes": 60, "teams_webhook_env": "<ENV VAR holding their webhook URL>"}}

    ``backup_approver`` null means: after ``primary_minutes`` an unanswered approval is denied. Any other shape
    raises ``ApproversConfigError`` (the sweep then alerts and changes nothing)."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ApproversConfigError(f"not JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise ApproversConfigError("not a JSON object")

    def minutes(value, key):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ApproversConfigError(f"{key} must be a whole number of minutes >= 1")
        return value

    out = {"primary_minutes": minutes(data.get("primary_minutes"), "primary_minutes"), "backup": None}
    backup = data.get("backup_approver")
    if backup is not None:
        if not isinstance(backup, dict):
            raise ApproversConfigError("backup_approver must be null or an object")
        name = backup.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ApproversConfigError("backup_approver.name must be a non-empty string")
        path_key = backup.get("escalation_path")
        if path_key is not None and (not isinstance(path_key, str) or not path_key.strip()):
            raise ApproversConfigError("backup_approver.escalation_path must be a non-empty string or absent")
        env = backup.get("teams_webhook_env")
        if env is not None and (not isinstance(env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env)):
            raise ApproversConfigError("backup_approver.teams_webhook_env must be an environment variable name")
        out["backup"] = {"name": " ".join(name.split())[:80],
                         "escalation_path": path_key.strip() if path_key else None,
                         "minutes": minutes(backup.get("minutes"), "backup_approver.minutes"),
                         "teams_webhook_env": env}
    return out


def _route_unanswered(ledger, ledger_path: str, approvers_path: str, now: datetime,
                      app_url: str = _links.DEFAULT_APP_URL) -> None:
    """TAP2-121: a backup approver, or a clear denial, for approvals nobody answered.

    Only ``parked`` rows WITH a request id are waiting on a person (rows without one are the orphan check's).
    Routing prints one alert line per row (state in ``approver_alert_state.json``, so it is said once) and posts it
    to the backup's own Teams webhook when ``teams_webhook_env`` names a set variable. A denial is a CAS
    ``parked -> denied``: it never races an approval that landed first, and an approval that lands later at
    withHuman does nothing (the row is no longer ``parked``, so the executor refuses and the gateway replay finds
    no approved row). The chat hears the denial through the usual wake (a decided row not yet notified)."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    state_path = os.path.join(directory, APPROVER_STATE)
    state = _load_state(state_path)
    try:
        config = load_approvers(approvers_path)
    except ApproversConfigError as exc:
        problem = _clean(str(exc), 200)
        if state.get("config_problem") != problem:
            print(f"durable-park ALERT: {approvers_path} is invalid ({problem}); no backup approver and no "
                  "timeout until it is fixed (approvals wait as before).", flush=True)
            state["config_problem"] = problem
            _save_state(state_path, state)
        return
    except OSError as exc:
        problem = f"unreadable ({type(exc).__name__})"
        if state.get("config_problem") != problem:
            print(f"durable-park ALERT: {approvers_path} is {problem}; no backup approver and no timeout until "
                  "it is fixed (approvals wait as before).", flush=True)
            state["config_problem"] = problem
            _save_state(state_path, state)
        return
    if state.pop("config_problem", None) is not None:
        print(f"durable-park: {approvers_path} reads again.", flush=True)
    if config is None:
        if state:
            _save_state(state_path, {})
        return
    routed = state.setdefault("routed", {})
    primary = timedelta(minutes=config["primary_minutes"])
    backup = config["backup"]
    waiting = set()
    for row in ledger.list_by_status("parked"):
        if not row.request_id:
            continue
        waiting.add(row.park_id)
        try:
            created = datetime.strptime(row.created_at, _STAMP).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        age = now - created
        if age <= primary:
            continue
        if backup is not None and age <= primary + timedelta(minutes=backup["minutes"]):
            if row.park_id in routed:
                continue
            where = f" (withHuman escalation path {backup['escalation_path']})" if backup["escalation_path"] else ""
            line = (f"durable-park: approval for {row.park_id} ({row.tool}) unanswered for "
                    f"{config['primary_minutes']} min; routed to backup approver {backup['name']}{where}. "
                    f"Decide it in withHuman within {backup['minutes']} min or it is denied: "
                    f"{_links.approval_url(row.request_id, app_url)}")
            print(line, flush=True)
            routed[row.park_id] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            webhook = os.environ.get(backup["teams_webhook_env"] or "", "").strip() if backup["teams_webhook_env"] else ""
            if webhook:
                _post_teams(webhook, line)
            continue
        total = config["primary_minutes"] + (backup["minutes"] if backup else 0)
        if backup is not None:
            note = (f"no approver answered within {total} minutes (backup approver {backup['name']} included); "
                    "denied by the sweep")
        else:
            note = (f"no approver answered within {total} minutes and no backup approver is configured; "
                    "denied by the sweep")
        if ledger.transition(row.park_id, "parked", "denied", note=note):
            print(f"durable-park: {row.park_id} ({row.tool}) DENIED: {note}.", flush=True)
            routed.pop(row.park_id, None)
    for park_id in list(routed):
        if park_id not in waiting:
            del routed[park_id]
    _save_state(state_path, state)


def _pending_fallback(ledger, ledger_path: str, minutes: int, now: datetime,
                      app_url: str = _links.DEFAULT_APP_URL) -> None:
    """GitHub #73 / TAP2-170: a fallback for withHuman's own instant alert, which once never reached Teams.

    A ``parked`` row WITH a request id that is still waiting ``minutes`` after it was created gets one line on
    stdout. ``main`` copies stdout to the Teams alert chat (``TEAMS_ALERT_WEBHOOK_URL``) and Hermes' cron delivers it
    to the admin's Telegram DM. Those chats must stay approvers/admins only, because the line holds the approval link
    (same rule as the TAP2-121 backup-approver alert). Said once per park (state in ``pending_alert_state.json``);
    parks that stop waiting are forgotten. ``minutes`` 0 turns it off."""
    state_path = os.path.join(os.path.dirname(os.path.abspath(ledger_path)), PENDING_STATE)
    state = _load_state(state_path)
    if minutes <= 0:
        if state:
            _save_state(state_path, {})
        return
    announced = state.setdefault("announced", {})
    if not isinstance(announced, dict):
        announced = state["announced"] = {}
    waiting = set()
    changed = False
    for row in ledger.list_by_status("parked"):
        if not row.request_id:
            continue
        waiting.add(row.park_id)
        if row.park_id in announced:
            continue
        try:
            created = datetime.strptime(row.created_at, _STAMP).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        age = now - created
        if age < timedelta(minutes=minutes):
            continue
        waited = int(age.total_seconds() // 60)
        print(f"durable-park: approval for {row.park_id} ({row.tool}) still waiting after {waited} min. "
              f"Decide it in withHuman: {_links.approval_url(row.request_id, app_url)}", flush=True)
        announced[row.park_id] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        changed = True
    for park_id in list(announced):
        if park_id not in waiting:
            del announced[park_id]
            changed = True
    if changed:
        _save_state(state_path, state)


def _apply_retention(ledger, now: datetime, days: int) -> None:
    """Delete finished rows with nothing left to decide (``allowed``, ``refused``, ``cancelled``) once they are
    ``days`` old. Prints nothing; SQLite keeps the freed pages for reuse (the file shrinks only on VACUUM)."""
    ledger.delete_finished_before((now - timedelta(days=days)).strftime(_STAMP))


def _wake_targets(ledger) -> list[tuple[str, str | None]]:
    """(park id, the row's profile) for every row that needs a wake, so each goes to its own person's route."""
    parked = [r for r in ledger.list_by_status("parked") if r.request_id]
    approved = [r for r in ledger.list_by_status("approved") if r.notified_at is None]
    # A decision the wake recorded but could not announce (inject refused): only rows that have a request
    # (a park-hook cancel without one was never a withHuman decision and is not announced).
    decided = [r for r in ledger.list_by_status("denied", "expired", "cancelled")
               if r.notified_at is None and r.request_id]
    untold = list(ledger.outcome_unnotified())  # GitHub #66: tell the chat how the run ended
    return [(r.park_id, r.profile) for r in parked + approved + decided + untold]


OUTCOME_GIVEUP_STATE = "outcome_giveup_state.json"


def _outcome_giveup_alert(ledger, ledger_path: str) -> None:
    """GitHub #66: a run whose outcome could not be told for 24 hours gets one alert line, then is left alone."""
    state_path = os.path.join(os.path.dirname(os.path.abspath(ledger_path)), OUTCOME_GIVEUP_STATE)
    state = _load_state(state_path)
    said = state.get("said") if isinstance(state.get("said"), list) else []
    given_up = ledger.outcome_given_up()
    current = [r.park_id for r in given_up]
    for row in given_up:
        if row.park_id not in said:
            print(f"durable-park ALERT: could not tell the chat the outcome of {row.park_id} ({row.tool}) for 24 "
                  "hours; check it.", flush=True)
    if current != said:  # forget parks no longer given up (deleted, or told after all)
        _save_state(state_path, {"said": current})


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
    _policy_load_alert(directory, report_failed=problem is not None)


def _policy_load_alert(directory: str, *, report_failed: bool) -> None:
    """Alert once when the plugin reports at load that the tool policy check did not pass (it also catches a
    stale report, which the check above cannot see), and once when a later load passes. A missing or unreadable
    verdict file is silent (an older plugin, or one that has not loaded yet). When the start-time report failed,
    the plugin refused for the same reason and the alert above already said so: this one is skipped entirely
    (no print, no state change), so a failure gives one ALERT and one recovery line."""
    if report_failed:
        return
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


def _parse_stamp(text) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _lift_alert(ledger_path: str, now: datetime) -> None:
    """Alert once per (tool, kind, key, detail) in the park hook's ``lift_status.json`` (next to the ledger), and
    again once per 24 hours while its count keeps growing (the leak is still happening). A missing file is silent
    (nothing has gone wrong yet); an unreadable or malformed one alerts once, until it reads again. The state
    remembers only the entries still in the report (bounded), each with the count and time it was last announced;
    an entry that leaves the report and comes back alerts again. May raise: ``_lift_check`` reports that."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    state_path = os.path.join(directory, LIFT_STATE)
    state = _load_state(state_path)
    stamp_now = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    announced: dict[tuple[str, str, str, str], dict] = {}
    for item in state.get("alerted", []) if isinstance(state.get("alerted"), list) else []:
        if isinstance(item, dict) and all(isinstance(item.get(f), str) for f in ("tool", "kind", "key")):
            detail = item["detail"] if isinstance(item.get("detail"), str) else ""
            count = item["count"] if isinstance(item.get("count"), int) and not isinstance(item.get("count"), bool) else 1
            announced[(item["tool"], item["kind"], item["key"], detail)] = {"count": count, "at": item.get("at")}
    unreadable_alerted = state.get("unreadable") is True
    unreadable_reason = None
    entries: list = []
    try:
        with open(os.path.join(directory, LIFT_STATUS), encoding="utf-8") as handle:
            report = json.load(handle)
        if not isinstance(report, dict) or not isinstance(report.get("entries"), list):
            unreadable_reason = "unexpected format"
        else:
            entries = report["entries"]
    except FileNotFoundError:
        pass
    except OSError as exc:
        unreadable_reason = type(exc).__name__
    except ValueError:
        unreadable_reason = "not valid JSON"
    except Exception:  # noqa: BLE001 - for example RecursionError on a file nested absurdly deep
        unreadable_reason = "unexpected format"
    current: dict[tuple[str, str, str, str], dict] = {}
    said: dict[tuple[str, str, bool], list] = {}  # (tool, kind, repeat) -> [(key, text, count)] in report order
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str) or not isinstance(entry.get("kind"), str):
            continue
        key = entry["key"] if isinstance(entry.get("key"), str) else ""
        detail = entry["detail"] if isinstance(entry.get("detail"), str) else ""
        count = entry["count"] if isinstance(entry.get("count"), int) and not isinstance(entry.get("count"), bool) else 1
        ident = (entry["tool"], entry["kind"], key, detail)
        template = LIFT_TEXTS.get(entry["kind"], "durable-park ALERT: a note problem ({kind}) for {tool}; it may reach the server.")
        text = template.format(tool=_clean(entry["tool"], 200), key=_clean(key, 200), reason=_clean(detail, 100),
                               kind=_clean(entry["kind"], 40))
        before = announced.get(ident)
        if ident in current:  # a duplicate line in the report: one alert
            current[ident]["count"] = max(current[ident]["count"], count)
            continue
        if before is None:
            said.setdefault((ident[0], ident[1], False), []).append((key, text, count))
            current[ident] = {"count": count, "at": stamp_now}
        else:
            last = _parse_stamp(before["at"])
            if (count > before["count"] and entry["kind"] not in LIFT_NO_REPEAT
                    and (last is None or now - last >= LIFT_REALERT)):
                said.setdefault((ident[0], ident[1], True), []).append((key, text, count))
                current[ident] = {"count": count, "at": stamp_now}
            else:  # same leak, said recently or no new occurrence; a smaller count (file restarted) lowers the base
                current[ident] = {"count": min(count, before["count"]), "at": before["at"] or stamp_now}
    for (_tool, _kind, repeat), places in said.items():
        first_key, text, first_count = places[0]
        if repeat:
            text = text.replace("durable-park ALERT:", f"durable-park ALERT (still happening, {first_count} times):", 1)
        others = places[1:]
        if others:
            examples = [_clean(other_key, 200) for other_key, _text, _count in others if other_key][:LIFT_EXAMPLES]
            text += f" (+{len(others)} more places" + (f", e.g. {', '.join(examples)}" if examples else "") + ")"
        print(text, flush=True)
    if unreadable_reason is not None:
        if not unreadable_alerted:
            print(f"durable-park ALERT: could not read {LIFT_STATUS} ({_clean(unreadable_reason, 100)}).", flush=True)
        current = announced  # keep what was announced: the report may come back unchanged
    new_state = {"alerted": [{"tool": t, "kind": k, "key": key, "detail": d, **v}
                             for (t, k, key, d), v in sorted(current.items())],
                 "unreadable": unreadable_reason is not None}
    if new_state != state:
        _save_state(state_path, new_state)


def _lift_unwritable_alert(ledger_path: str, now: datetime) -> None:
    """Alert once per marker when a process left ``lift_status.unwritable.<pid>`` (it could not write the lift
    report, so its note problems are not being reported), and once when the last marker is gone. Every writing
    process has its own marker, so one process's recovery never hides another's outage. May raise: ``_lift_check``
    reports that."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    state_path = os.path.join(directory, LIFT_UNWRITABLE_STATE)
    state = _load_state(state_path)
    before = state.get("alerted")
    alerted = {name for name in before if isinstance(name, str)} if isinstance(before, list) else set()
    present = sorted(name for name in os.listdir(directory) if name.startswith(LIFT_UNWRITABLE))
    for name in present:
        if name in alerted:
            continue
        text = ""
        error = None
        try:  # never block (a planted FIFO), never follow a symlink, never read anything but a regular file
            fd = os.open(os.path.join(directory, name), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY)
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    text = os.read(fd, 200).decode("utf-8", errors="replace")
                else:
                    error = "not a regular file"
            finally:
                os.close(fd)
        except OSError:  # unreadable, or a symlink: still an outage, error unknown
            pass
        if error is None:
            parts = text.split()
            error = parts[1] if len(parts) >= 2 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", parts[1]) else "unknown error"
        print(f"durable-park ALERT: lift problems could not be saved ({_clean(error, 60)}); the agent's note may "
              "be reaching servers unreported. Check the permissions of lift_status.json and its lock next to "
              "the ledger.", flush=True)
    if alerted and not present:
        print("durable-park: lift problems can be saved again.", flush=True)
    if sorted(alerted) != present:
        _save_state(state_path, {"alerted": present})


def _lift_check(ledger_path: str, now: datetime) -> None:
    """Run ``_lift_alert``; when the check itself fails (a bug, an unwritable state file, anything), print ONE stdout
    ALERT per error type until it works again, then one recovery line: a check that cannot run must not be silent."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    fail_path = os.path.join(directory, LIFT_CHECK_STATE)
    failure: Exception | None = None
    for check in (_lift_unwritable_alert, _lift_alert):  # one failing never hides the other
        try:
            check(ledger_path, now)
        except Exception as exc:  # noqa: BLE001
            failure = failure or exc
    if failure is not None:
        name = type(failure).__name__
        try:
            already = _load_state(fail_path).get("failed") == name
        except Exception:  # noqa: BLE001
            already = False
        if not already:
            print(f"durable-park ALERT: could not check for lift problems ({_clean(name, 60)}).", flush=True)
            try:
                _save_state(fail_path, {"failed": name})
            except Exception:  # noqa: BLE001 - it may then repeat on the next run: loud, not silent
                pass
        return
    try:
        if _load_state(fail_path).get("failed"):
            print("durable-park: lift problems can be checked again.", flush=True)
            _save_state(fail_path, {"failed": None})
    except Exception:  # noqa: BLE001
        pass


def _gateway_mainpid() -> int | None:
    """MainPID of the ``hermes-gateway`` user unit, or ``None`` when it cannot be learned (systemctl missing,
    failing, timing out, printing junk, or ``0`` = no main process). ``None`` means the load-proof alert stays
    silent: this must never make the sweep fail."""
    env = None
    if not os.environ.get("XDG_RUNTIME_DIR"):  # a cron job may lack it; systemctl --user needs it
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    try:
        done = subprocess.run(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "MainPID", "--value"],
                              capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT, check=False, env=env)
        if done.returncode != 0:
            return None
        pid = int(done.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return pid if pid > 0 else None


_PID_UNSET = object()


def _load_proof_alert(ledger_path: str, now: datetime, pid=_PID_UNSET) -> None:
    """Defence in depth for ``deploy/plugin_load_check.sh``: alert once when the running gateway has no load
    proof for its own pid (the plugin did not load in it, so no ``pre_tool_call`` hook runs and every tool call
    may be unchecked), and once when it has one again. ``now`` is unused: the proof carries its own pid.

    Known limit: this sweep is a Hermes cron job that runs INSIDE the gateway, so a stopped gateway (for
    example one the start check stopped) cannot alert; the bot going silent is the signal. Unknown MainPID
    (``_gateway_mainpid`` is ``None``): do nothing, change no state."""
    if pid is _PID_UNSET:
        pid = _gateway_mainpid()
    if pid is None:
        return
    directory = os.path.dirname(os.path.abspath(ledger_path))
    proof = _load_state(os.path.join(directory, PLUGIN_LOAD))  # missing or unreadable -> {}
    loaded = isinstance(proof.get("pid"), int) and not isinstance(proof.get("pid"), bool) and proof["pid"] == pid
    state_path = os.path.join(directory, PLUGIN_LOAD_STATE)
    alerted = _load_state(state_path).get("alerted") is True
    if not loaded and not alerted:
        print(f"durable-park ALERT: the running gateway (pid {pid}) has no durable-park load proof; every tool "
              "call may be running unchecked. Restart the gateway with no active rows and check the journal.",
              flush=True)
        alerted = True
    elif loaded and alerted:
        print("durable-park: the durable-park plugin is loaded in the running gateway.", flush=True)
        alerted = False
    _save_state(state_path, {"alerted": alerted})


_LINE_HEAD = re.compile(r"^durable-park( ALERT)?(?: \(([^)]*)\))?:")


def _label_line(line: str, profile: str) -> str:
    """``durable-park ALERT: x`` -> ``durable-park ALERT (profile sarah): x`` (also keeps an existing
    ``(still happening, 3 times)`` note: ``(profile sarah, still happening, 3 times)``)."""
    if not line.strip():
        return line
    match = _LINE_HEAD.match(line)
    if match is None:
        return f"(profile {profile}) {line}"
    extra = f", {match.group(2)}" if match.group(2) else ""
    return f"durable-park{match.group(1) or ''} (profile {profile}{extra}){line[match.end() - 1:]}"


class _Labeled:
    """stdout for one person's checks: every line it prints names the profile."""

    def __init__(self, inner, profile: str):
        self.inner = inner
        self.profile = profile
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self.inner.write(_label_line(line, self.profile) + "\n")
        return len(text)

    def flush(self) -> None:
        self.inner.flush()

    def finish(self) -> None:
        if self._pending:
            self.inner.write(_label_line(self._pending, self.profile))
            self._pending = ""
        self.inner.flush()

    def __getattr__(self, name):
        return getattr(self.inner, name)


@contextlib.contextmanager
def _labeled(profile: str):
    inner = sys.stdout
    out = _Labeled(inner, profile)
    sys.stdout = out
    try:
        yield
    finally:
        sys.stdout = inner
        out.finish()


def _profile_dirs(root: str) -> list[tuple[str, str]]:
    """(profile, status directory) for each ``<root>/profiles/<profile>/``: sorted, real directories only, and only
    names a profile may have. Anything else in there is ignored."""
    base = os.path.join(root, PROFILES_DIR)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(base, name)
        if _keys.valid_profile(name) and os.path.isdir(path) and not os.path.islink(path):
            found.append((name, path))
    return found


def _profile_status_alerts(root: str, pid) -> None:
    """Run the per-status checks once for every routed person's status directory, each with its own alert-state
    files there. One failing step never stops the others or the other people (it is one stderr line naming the
    profile). The checks only use the directory part of the path, so a made-up ``ledger.db`` name stands in."""
    for profile, directory in _profile_dirs(root):
        reference = os.path.join(directory, "ledger.db")
        steps = (("form-check alert", _formcheck_alert), ("breaker alert", _breaker_alert),
                 ("tool policy alert", _policy_alert), ("lift alert", _lift_check),
                 ("load proof alert", lambda path, when: _load_proof_alert(path, when, pid)))
        for name, check in steps:
            try:
                with _labeled(profile):
                    check(reference, datetime.now(timezone.utc))
            except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
                _err(f"{name} failed for profile {profile} ({type(exc).__name__})")


def _stamp_now(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile_label(profile) -> str:
    if _keys.is_default_profile(profile):
        return "default"
    return profile if _keys.valid_profile(profile) else "(invalid name)"


def _wake_post_alerts(root: str, failures: dict, now: datetime) -> None:
    """Teams-visible alert for a wake that cannot be posted. ``failures`` maps park id -> (profile label, kind,
    detail, runs needed) for this sweep: ``http`` (detail = status code), ``error`` (detail = error type) or
    ``invalid`` (the stored profile name cannot make an address; says nothing of the name). A row is alerted once it
    failed on enough sweeps in a row, once, and again after 24 hours while it keeps failing. A row that is not
    failing now (posted fine, or no longer a target) is forgotten."""
    state_path = os.path.join(root, WAKE_POST_STATE)
    state = _load_state(state_path)
    before = state.get("rows") if isinstance(state.get("rows"), dict) else {}
    rows: dict = {}
    for park_id, (label, kind, detail, needed) in failures.items():
        old = before.get(park_id) if isinstance(before.get(park_id), dict) else {}
        runs = old.get("runs") if isinstance(old.get("runs"), int) and not isinstance(old.get("runs"), bool) else 0
        runs += 1
        alerted_at = old.get("alerted_at") if isinstance(old.get("alerted_at"), str) else None
        last = _parse_stamp(alerted_at)
        if runs >= needed and (last is None or now - last >= WAKE_POST_REALERT):
            route = wake_url_for(None if label == "default" else label, "") if kind != "invalid" else ""
            if kind == "invalid":
                text = (f"durable-park ALERT: cannot wake {_clean(park_id, 40)}: its stored profile name is not "
                        "valid, so no wake address can be built and the chat is not told. Check that row's "
                        "profile in the ledger.")
            elif kind == "http":
                where = ("that person's wake route (the route is missing from the Hermes config or the gateway "
                         f"did not load it): {route}" if detail == 404 and label != "default"
                         else f"the gateway and the wake route {route}")
                text = (f"durable-park ALERT: waking {_clean(park_id, 40)} for profile {label} failed with HTTP "
                        f"{detail} on {runs} sweeps in a row; check {where}.")
            else:
                text = (f"durable-park ALERT: waking {_clean(park_id, 40)} for profile {label} failed "
                        f"({_clean(detail, 60)}) on {runs} sweeps in a row; check that the gateway is running "
                        f"and listening for the wake route {route}.")
            print(text, flush=True)
            alerted_at = _stamp_now(now)
        rows[park_id] = {"runs": runs, "alerted_at": alerted_at}
    new_state = {"rows": rows} if rows else {}
    if new_state != state:
        _save_state(state_path, new_state)


def _key_file_usable(path: str) -> bool:
    """True when the key file can be read as text and holds something besides whitespace: the rule of the plugin's
    ``wake.key_file_usable`` (the sweep cannot import the plugin package, so it keeps its own copy; a test proves
    the two agree). The content is only tested, never printed or logged."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode) or not os.access(path, os.R_OK):  # never open a FIFO or device
            return False
        with open(path, encoding="utf-8") as handle:
            return bool(handle.read().strip())
    except (OSError, ValueError):  # missing, a directory, unreadable, or not text (UnicodeDecodeError)
        return False


def _missing_key_alert(ledger, root: str, default_token_file: str, instances_dir: str, now: datetime) -> None:
    """Once per row: a ``parked`` or ``approved`` row whose profile has no key file cannot resume (the receiver
    could not ask withHuman for it), nor can one whose key file is there but empty or unreadable (the plugin's
    wake refuses it silently; ``unusable:<park id>``). An invalid profile name counts too (no path is built from
    it). A row is forgotten when its key becomes usable or it leaves those statuses."""
    state_path = os.path.join(root, MISSING_KEY_STATE)
    state = _load_state(state_path)
    said = dict(state.get("said")) if isinstance(state.get("said"), dict) else {}
    current = set()
    for row in ledger.list_by_status("parked", "approved"):
        key = f"nokey:{row.park_id}"
        try:
            path = _keys.token_file_for(row.profile, default=default_token_file, instances_dir=instances_dir)
        except ValueError:
            path = None
        if path is not None and os.path.isfile(path):
            if _key_file_usable(path):
                continue
            key = f"unusable:{row.park_id}"  # there, but the plugin cannot use it
            current.add(key)
            if key not in said:
                print(f"durable-park ALERT: the key file for profile {_profile_label(row.profile)} is empty or "
                      f"unreadable; {_clean(row.park_id, 40)} cannot resume. Check {_clean(path, 200)} "
                      "(people.sh).", flush=True)
                said[key] = _stamp_now(now)
            continue
        current.add(key)
        if key in said:
            continue
        if path is None:
            print(f"durable-park ALERT: {_clean(row.park_id, 40)} has a profile name that is not valid, so its "
                  "key file cannot be found and it cannot resume. Fix the row's profile or remove the person "
                  "(people.sh).", flush=True)
        else:
            print(f"durable-park ALERT: no key file for profile {_profile_label(row.profile)}; "
                  f"{_clean(row.park_id, 40)} cannot resume. Add the key or remove the person (people.sh).",
                  flush=True)
        said[key] = _stamp_now(now)
    for key in [k for k in said if k.startswith(("nokey:", "unusable:")) and k not in current]:
        del said[key]
    if said != (state.get("said") if isinstance(state.get("said"), dict) else {}):
        state["said"] = said
        _save_state(state_path, state)


def _read_refusals(directory: str):
    """(entries, problem): the plugin's ``wake_refusals.json`` of one status directory. A missing file is fine."""
    try:
        with open(os.path.join(directory, WAKE_REFUSALS), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, type(exc).__name__
    except ValueError:
        return {}, "not valid JSON"
    except Exception:  # noqa: BLE001 - for example RecursionError on a file nested absurdly deep
        return {}, "unexpected format"
    if not isinstance(data, dict):
        return {}, "unexpected format"
    return data, None


def _refusal_alert(ledger, root: str, default_token_file: str, instances_dir: str, now: datetime) -> None:
    """Once per row: withHuman refused a ``parked`` row's key ``REFUSED_ALERT_COUNT`` or more times (the plugin counts
    them in ``wake_refusals.json``, in the root and each person's status directory). Entries for rows that are
    unknown or not ``parked`` are ignored. An unreadable file is one alert, until it reads again."""
    state_path = os.path.join(root, MISSING_KEY_STATE)
    state = _load_state(state_path)
    original = state.get("said") if isinstance(state.get("said"), dict) else {}
    said = dict(original)
    current = set()
    unreadable_now = set()
    for profile, directory in [("default", root), *_profile_dirs(root)]:
        entries, problem = _read_refusals(directory)
        ukey = f"unreadable:{profile}"
        if problem is not None:
            unreadable_now.add(ukey)
            if ukey not in said:
                print(f"durable-park ALERT: {WAKE_REFUSALS} for profile {profile} is unreadable ({problem}), so "
                      "repeated key refusals cannot be checked there until it reads again.", flush=True)
                said[ukey] = _stamp_now(now)
            continue
        for park_id, entry in entries.items():
            count = entry.get("count") if isinstance(entry, dict) else None
            if isinstance(count, bool) or not isinstance(count, int) or count < REFUSED_ALERT_COUNT:
                continue
            row = ledger.get(park_id) if isinstance(park_id, str) else None
            if row is None or row.status != "parked":
                continue
            key = f"refused:{park_id}"
            current.add(key)
            if key in said:
                continue
            label = _profile_label(row.profile)
            try:
                token = _keys.token_file_for(row.profile, default=default_token_file, instances_dir=instances_dir)
            except ValueError:
                token = "that person's key file"
            print(f"durable-park ALERT: withHuman refuses the key for profile {label}, so {_clean(park_id, 40)} "
                  "cannot resume and blocks that chat and restarts. Check that "
                  f"{_clean(token, 200)} is that person's instance key (people.sh), then the sweep retries.",
                  flush=True)
            said[key] = _stamp_now(now)
    for key in list(said):
        if key.startswith("unreadable:") and key not in unreadable_now:
            del said[key]
        elif key.startswith("refused:") and key not in current and not unreadable_now:
            del said[key]  # not pruned while a file is unreadable: its entries may come back unchanged
    if said != original:
        state["said"] = said
        _save_state(state_path, state)


def _hermes_checkout() -> str:
    """The Hermes source checkout (``HERMES_AGENT_DIR``, default ``~/.hermes/hermes-agent``)."""
    return os.environ.get("HERMES_AGENT_DIR") or os.path.expanduser("~/.hermes/hermes-agent")


def _git(checkout: str, *args: str) -> str | None:
    """``git -C <checkout> <args>`` stdout, stripped; ``None`` on any failure (never raises)."""
    try:
        done = subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _pin_problems(pin_path: str, checkout: str) -> tuple[str | None, list[str]]:
    """(pinned version, problems). No problems means Hermes runs the pinned commit and updates are blocked."""
    pin = _load_state(pin_path)  # missing, unreadable or not a mapping -> {}
    version, commit = pin.get("version"), pin.get("commit")
    if not isinstance(version, str) or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, [f"the pin file {pin_path} cannot be read"]
    problems = []
    head = _git(checkout, "rev-parse", "HEAD")
    if head is None:
        problems.append(f"could not read the Hermes checkout at {checkout}")
    elif head != commit:
        described = _git(checkout, "describe", "--tags", "--always", "HEAD") or "unknown"
        problems.append(f"Hermes is at {head[:10]} ({_clean(described, 60)}), not the pinned {version} ({commit[:10]})")
    if _git(checkout, "config", "--get", HERMES_BLOCK_KEY) != HERMES_BLOCK_VALUE:
        problems.append("the update block is off (anyone could run hermes update or send /update in chat)")
    return version, problems


def _pin_alert(ledger_path: str, now: datetime) -> None:
    """Alert when Hermes leaves the pinned commit or the update block is removed (GitHub #76): once per
    problem, again after 24 hours while it lasts, and once when it is fixed."""
    directory = os.path.dirname(os.path.abspath(ledger_path))
    state_path = os.path.join(directory, HERMES_PIN_STATE)
    state = _load_state(state_path)
    version, problems = _pin_problems(HERMES_PIN_FILE, _hermes_checkout())
    if problems:
        key = "; ".join(problems)
        last = _parse_stamp(state.get("alerted_at"))
        if key != state.get("problem") or last is None or now - last >= HERMES_PIN_REALERT:
            print(f"durable-park ALERT: Hermes is not as pinned \u2014 {_clean(key, 600)}. Updates are blocked on "
                  "purpose until a new version is tested (GitHub #76); see the runbook: Upgrade Hermes.", flush=True)
            _save_state(state_path, {"problem": key, "alerted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    elif state.get("problem"):
        print(f"durable-park: Hermes is back on the pinned {version} with updates blocked.", flush=True)
        _save_state(state_path, {})


def main(argv=None) -> int:
    """Run the sweep. With ``TEAMS_ALERT_WEBHOOK_URL`` set, what the sweep printed to stdout (its alert lines,
    which hold ids, tool names and reasons, never argument values) is also posted to Teams, even when the sweep
    itself raises (the error is not hidden). A failed post is one more plain ALERT line; the exit code is not
    changed (Hermes' cron wrapper would wrap a non-zero exit around the Telegram message)."""
    webhook = os.environ.get(TEAMS_WEBHOOK_ENV, "").strip()
    if not webhook:
        return _sweep(argv)
    tee = _Tee(sys.stdout)
    sys.stdout = tee
    try:
        return _sweep(argv)
    finally:
        sys.stdout = tee.inner
        text = "".join(tee.lines).strip()
        if text:
            _post_teams(webhook, text)


def _sweep(argv=None) -> int:
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
    root = os.path.abspath(args.status_root) if args.status_root else os.path.dirname(os.path.abspath(args.ledger))
    status_ref = os.path.join(root, os.path.basename(args.ledger))  # the status checks use only its directory

    _mark_stale_executing(ledger, datetime.now(timezone.utc))
    try:
        _cancel_orphan_parks(ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"orphan cancel failed ({type(exc).__name__})")
    try:
        _route_unanswered(ledger, args.ledger, args.approvers_file, datetime.now(timezone.utc), args.withhuman_url)
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"backup approver step failed ({type(exc).__name__})")
    try:
        _pending_fallback(ledger, args.ledger, args.pending_alert_minutes, datetime.now(timezone.utc),
                          args.withhuman_url)
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"pending approval fallback failed ({type(exc).__name__})")
    try:
        _formcheck_alert(status_ref, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the self-check alert must never stop the sweep
        _err(f"form-check alert failed ({type(exc).__name__})")
    try:
        _breaker_alert(status_ref, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the breaker alert must never stop the sweep
        _err(f"breaker alert failed ({type(exc).__name__})")
    try:
        _policy_alert(status_ref, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the policy alert must never stop the sweep
        _err(f"tool policy alert failed ({type(exc).__name__})")
    try:
        _lift_check(status_ref, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the lift alert must never stop the sweep
        _err(f"lift alert failed ({type(exc).__name__})")
    try:
        _load_proof_alert(status_ref, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - the load-proof alert must never stop the sweep
        _err(f"load proof alert failed ({type(exc).__name__})")
    try:
        _profile_status_alerts(root, _gateway_mainpid())
    except Exception as exc:  # noqa: BLE001 - the per-person checks must never stop the sweep
        _err(f"per-profile status alerts failed ({type(exc).__name__})")
    try:
        _pin_alert(args.ledger, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - never stop the sweep, never hide the failure
        print(f"durable-park ALERT: Hermes pin check failed ({type(exc).__name__}); "
              "check the pin by hand: deploy/hermes_pin.sh check", flush=True)
    try:
        _outcome_giveup_alert(ledger, args.ledger)
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"outcome give-up alert failed ({type(exc).__name__})")
    try:
        _missing_key_alert(ledger, root, args.default_token_file, args.instances_dir, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"missing key alert failed ({type(exc).__name__})")
    try:
        _refusal_alert(ledger, root, args.default_token_file, args.instances_dir, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
        _err(f"key refusal alert failed ({type(exc).__name__})")
    status = 0
    targets = _wake_targets(ledger)
    secret = _read_secret(args.wake_secret_file) if targets else None
    if targets and secret is None:
        _err(f"wake secret unreadable or empty: {args.wake_secret_file}")
        status = 1
    else:
        failures: dict = {}
        for park_id, profile in targets:
            label = _profile_label(profile)
            where = "" if _keys.is_default_profile(profile) else f" for profile {label}"
            try:
                url = args.wake_url if _keys.is_default_profile(profile) else wake_url_for(profile, args.wake_base)
            except ValueError:
                _err(f"wake {park_id} skipped: its stored profile name is not valid")
                failures[park_id] = (label, "invalid", None, 1)
                continue
            try:
                code = post_wake(park_id, secret, url)
            except Exception as exc:  # noqa: BLE001 - one dead wake must not stop the others
                _err(f"wake {park_id}{where} failed ({type(exc).__name__})")
                failures[park_id] = (label, "error", type(exc).__name__, WAKE_POST_BAD_RUNS)
                continue
            if not 200 <= code < 300:
                _err(f"wake {park_id}{where} returned HTTP {code}")
                failures[park_id] = (label, "http", code, WAKE_POST_BAD_RUNS)
        try:
            _wake_post_alerts(root, failures, datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001 - one failed step must never stop the sweep
            _err(f"wake post alert failed ({type(exc).__name__})")
    try:
        _apply_retention(ledger, datetime.now(timezone.utc), args.retention_days)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the sweep
        _err(f"retention failed ({type(exc).__name__})")
    return status


if __name__ == "__main__":
    sys.exit(main())
