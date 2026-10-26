# Durable "it ran" notice (GitHub #66, Linear TAP2-167) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an approved action runs, the person in the original chat is always told the outcome, even if the gateway is restarted or crashes before the AI writes its reply.

**Architecture:** Two layers. (1) Planned stops and restarts wait for replies in progress: Hermes `agent.restart_drain_timeout` goes from `0` to `45` seconds. (2) durable-park itself posts a fixed outcome line into the original chat the moment the action finishes (`ctx.post_message`, no AI involved) and records that in the ledger (`outcome_notified_at`). Anything not recorded as sent (a crash in the gap, a failed post, a run the sweep marked `unknown`) is re-sent by the 5-minute sweep through the existing wake path.

**Tech Stack:** Python 3.14 (Hermes v0.22.3 plugin API: `PluginContext.post_message`, `inject_message`), SQLite ledger, pytest (`-W error`), the existing sweep and wake route.

**Spec:** GitHub issue #66 / Linear TAP2-167 (what happened on 2026-10-26, why, the two-layer fix and the live acceptance).

## Global Constraints

- `agent.restart_drain_timeout: 45`; systemd `TimeoutStopSec` stays `70` (the drain must stay at least 15 s under it).
- Outcome lines start with `[durable-park] {park_id}`, are one line and at most 400 characters, and never contain argument values or secrets (result text is cut to 200 characters and passed through `wake.one_line`).
- The outcome line never carries an approval link (links go only to approvers).
- The executor's return value to the model is unchanged by this work; a failed post never raises and never changes a ledger status.
- `outcome_notified_at` is only ever set once (compare-and-set on `NULL`); no row is announced twice by the recovery path.
- AGENTS.md Rule 2: the executor and the wake handler are on the approval path, so the branch gets an independent review on the strongest model before merge.
- Before any gateway restart: no row `parked`, `approved` or `executing` (runbook rule), and from Task 1 on, restarts drain in-flight replies.
- Tests: `pytest -q -W error` from `durable-park/`; docs check and Mermaid check must pass; every changed code file updates its mapped doc.

## Known limits (accepted, from review)

- **Delivery is at least once, at most one duplicate:** a crash after a successful post but before `mark_outcome_notified` makes the sweep re-send once. Hermes `post_message` has no idempotency key, so transport-level de-duplication is not possible here.
- **The recovery path trusts `inject` acceptance:** like the approval wake (backlog item 2), a re-send counts as told once the gateway accepts it; a crash before the agent relays it loses that re-send. The migration is atomic (ALTER and backfill in one `BEGIN IMMEDIATE` transaction), as implemented.

## Review Focus

1. **Old rows at migration:** every row already `executed`, `failed` or `unknown` when the column is added must be marked as notified, or the first sweep would announce weeks of old actions. (Test in Task 2.)
2. **Post fails or hangs:** `post_message` returns `ok=False` (no conversation, adapter down) or times out (Hermes caps it at 30 s): the executor still returns its normal reply, the row stays un-notified, and the sweep re-sends later. (Test in Task 3.)
3. **Double notice:** the executor posts and the sweep runs in the same minute: recovery only picks rows whose last update is older than 2 minutes and marks them by compare-and-set, so a notice is sent by the recovery path at most once. (Test in Task 4.)
4. **A chat that can never receive it** (session gone, CLI run, profile mismatch): recovery stops trying 24 hours after the row's last update and prints one sweep alert, instead of retrying forever. (Test in Task 4.)
5. **A huge or odd result** (a long JSON result, newlines, a non-text value): the outcome line stays one line and at most 400 characters. (Test in Task 3.)

---

### Task 1: Planned restarts wait for replies (restart drain)

**Files:**
- Modify: `durable-park/deploy/configure_hermes.sh` (set `agent.restart_drain_timeout` to `45`, idempotent, like the other `hermes config set` lines)
- Test: `durable-park/tests/test_configure_script.py`
- Docs: `durable-park/docs/runbook.md` ("Live Hermes settings" row; "Deploy a change": a restart now waits up to 45 s for replies)

**Interfaces:** Produces the live setting `agent.restart_drain_timeout: 45`.

- [ ] **Step 1: Write the failing test** `test_configure_sets_restart_drain_timeout`: the script sets `agent.restart_drain_timeout` to `45`, and `45 <= TimeoutStopSec - 15` for the `TimeoutStopSec` in `durable-park/deploy/hermes-gateway*.service` (or the live unit value `70` if the unit is not in the repo; the test says which).
- [ ] **Step 2: Run** `pytest -q -W error tests/test_configure_script.py -k drain` → FAIL.
- [ ] **Step 3: Implement** the one setting line in `configure_hermes.sh`.
- [ ] **Step 4: Run** the test → PASS; full suite → PASS.
- [ ] **Step 5: Commit** `feat(durable-park): restarts wait up to 45 s for replies (restart_drain_timeout)`.

### Task 2: Ledger records whether the outcome was told

**Files:**
- Modify: `durable-park/plugin/durable_park/ledger.py`
- Test: `durable-park/tests/test_ledger.py`
- Docs: `durable-park/docs/architecture.md` (ledger columns section)

**Interfaces:**
- Produces: `ParkRow.outcome_notified_at: str | None`; `Ledger.mark_outcome_notified(park_id: str) -> bool` (CAS: sets it only while `NULL`); `Ledger.outcome_unnotified(older_than_minutes: int = 2, give_up_hours: int = 24) -> list[ParkRow]` (status `executed`, `failed` or `unknown`, `outcome_notified_at IS NULL`, last update older than `older_than_minutes` and newer than `give_up_hours`); `Ledger.outcome_given_up(give_up_hours: int = 24) -> list[ParkRow]` (same, but older than `give_up_hours`).

- [ ] **Step 1: Write the failing tests:** `test_outcome_column_added_and_old_terminal_rows_backfilled` (a ledger file created by the current code with one `executed`, one `failed`, one `unknown` and one `parked` row; opening it with the new code adds the column, sets it on the three terminal rows only, and a second open changes nothing); `test_mark_outcome_notified_once` (second call returns `False`); `test_outcome_unnotified_window` (rows 1 min old: excluded; 3 min: included; 25 h: excluded and in `outcome_given_up`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement:** add the column through the existing idempotent `ALTER TABLE` migration; the backfill `UPDATE parks SET outcome_notified_at = updated_at WHERE outcome_notified_at IS NULL AND status IN ('executed','failed','unknown')` runs **only in the same migration step that adds the column**.
- [ ] **Step 4: Run** → PASS; full suite → PASS (the bridge's root copy of `ledger.py` is unaffected: it does not open the ledger).
- [ ] **Step 5: Commit** `feat(durable-park): ledger records whether the outcome was told`.

### Task 3: The executor posts the outcome itself

**Files:**
- Create: `durable-park/plugin/durable_park/outcome.py`
- Modify: `durable-park/plugin/durable_park/executor.py`, `durable-park/plugin/durable_park/__init__.py`, `.github/docs-map.json` (map `outcome.py` with the executor's rule)
- Test: `durable-park/tests/test_outcome.py`, `durable-park/tests/test_executor.py`
- Docs: `durable-park/docs/architecture.md` (executor row, approval-journey diagram: the notice step)

**Interfaces:**
- Consumes: Task 2's `mark_outcome_notified`.
- Produces: `outcome.outcome_text(row: ParkRow) -> str | None` (`None` for any status other than `executed`, `failed`, `unknown`); `make_executor(..., post_fn: Callable[[str], Any] | None = None, crash_after_execute: bool = False)`; env switch `DURABLE_PARK_CRASH_AFTER_EXECUTE=1` (test-only, like `DURABLE_PARK_CRASH_AFTER_CONSUME`).
- Texts (exact): executed `[durable-park] {park_id} ran: {tool}. Result: {result}`; failed `[durable-park] {park_id} ({tool}) did NOT complete: {note}`; unknown `[durable-park] {park_id} ({tool}): outcome UNKNOWN. Check it before trying again.` (`{result}` = the decoded tool result, one line, at most 200 characters; whole line at most 400.)

- [ ] **Step 1: Write the failing tests:** `test_outcome_text_per_status` (exact texts above; `parked`/`approved` → `None`); `test_outcome_text_is_one_line_and_capped` (a 5,000-character result with newlines → one line, ≤ 400); `test_executor_posts_outcome_once_and_marks_it` (stub `post_fn` returning an object with `ok=True` → called once with the executed text, row marked); `test_executor_post_failure_keeps_reply_and_row_unmarked` (`post_fn` returns `ok=False`, and a second case raises: the executor's return value equals the no-`post_fn` return value, the row stays `executed` and un-notified); `test_failed_and_unknown_runs_post_too`; `test_crash_after_execute_exits_before_posting` (with the switch on, `_hard_exit` is called after the row is `executed` and before `post_fn`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement:** `outcome.py` (standard library plus `wake.one_line`); in the executor, after each terminal transition, `post_fn(text)` inside `try/except Exception`, and `mark_outcome_notified` only when the returned object's `ok` is true; wire `post_fn=ctx.post_message` in `__init__.py`.
- [ ] **Step 4: Run** → PASS; full suite → PASS.
- [ ] **Step 5: Commit** `feat(durable-park): the executor tells the chat the outcome itself`.

### Task 4: The sweep re-sends anything not told

**Files:**
- Modify: `durable-park/scripts/durable_park_sweep.py`, `durable-park/plugin/durable_park/wake.py`
- Test: `durable-park/tests/test_sweep.py`, `durable-park/tests/test_wake.py`
- Docs: `durable-park/docs/runbook.md` (alerts: the give-up line; "How you hear about an approval": the outcome line), `durable-park/docs/backlog.md` (item 2 → done for outcomes), `durable-park/docs/architecture.md` (sweep and wake rows)

**Interfaces:**
- Consumes: Task 2's `outcome_unnotified`, `outcome_given_up`, `mark_outcome_notified`; Task 3's `outcome_text`.
- Produces: `_wake_targets` also returns `outcome_unnotified()` park ids; the wake handler, for a row `executed`/`failed`/`unknown` with `outcome_notified_at IS NULL`, injects `outcome_text(row) + " Tell the user this in one short message. Do not run anything."` into `row.session_key` and calls `mark_outcome_notified` when `inject` returns `True`. Sweep line for give-ups (once per park, state file `outcome_giveup_state.json`): `durable-park ALERT: could not tell the chat the outcome of {park_id} ({tool}) for 24 hours; check it.`

- [ ] **Step 1: Write the failing tests:** `test_sweep_rewakes_untold_outcomes_after_two_minutes` (1 min old: no wake; 3 min: one wake); `test_wake_injects_outcome_once` (inject `True` → marked; a second wake → no inject); `test_wake_outcome_inject_false_leaves_row_untold`; `test_sweep_gives_up_after_24h_with_one_alert`; `test_sweep_marks_stale_executing_unknown_then_outcome_is_told` (the existing stale-executing step, then the next run wakes it).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the three pieces above; the approval wake behaviour for `parked`/`approved`/decided rows stays unchanged.
- [ ] **Step 4: Run** → PASS; full suite → PASS; docs check and Mermaid check → pass.
- [ ] **Step 5: Commit** `feat(durable-park): the sweep re-sends an untold outcome`.

### Task 5: Independent review, deploy, live acceptance

- [ ] **Step 1:** Whole-branch review by a fresh reviewer on the strongest model (AGENTS.md Rule 2), with the Review Focus list as its checklist; fix findings; PR to `main`.
- [ ] **Step 2 (after merge):** pull the live clone; set `agent.restart_drain_timeout: 45` (configure script or `hermes config set`); restart the gateway only with no `parked`/`approved`/`executing` row; check the load proof and the tool policy report.
- [ ] **Step 3: Live test A (restart mid-reply):** ask the bot a question that takes a while to answer, run `systemctl --user restart hermes-gateway` while it is answering; expected: the answer arrives, and the journal shows the drain waited instead of "timed out after 0.0s".
- [ ] **Step 4: Live test B (crash after the run):** set `DURABLE_PARK_CRASH_AFTER_EXECUTE=1` through a user-unit drop-in, park and approve a $1 QuickBooks sandbox payment; expected: one payment created, the gateway exits and restarts, the outcome line `[durable-park] P-… ran: …` arrives in the original chat within about 7 minutes, exactly once (and not again on the next two sweeps). Remove the drop-in and restart.
- [ ] **Step 5: Live test C (normal run):** park and approve one more $1 payment with no switch: the outcome line arrives at once, before or beside the bot's own reply, once.
- [ ] **Step 6:** Record results in `durable-park/docs/acceptance-2026-10-26-outcome-notice.md`; comment on GitHub #66 and close it (Linear TAP2-167 follows).
