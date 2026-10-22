# withhuman_context lift — plan (2026-10-22)

**Goal:** when the model puts the gateway's `withhuman_context` note inside a tool argument object (seen live:
`{"params": {"criteria": ..., "withhuman_context": "Find customer ..."}}`), move it to the top level where the
withHuman gateway expects it, so QuickBooks no longer gets an unknown parameter (its "WARNING: ... does not support
this parameter: withhuman_context. It was IGNORED" text wastes tokens), and the approver sees the agent's reason.
Decided by the admin (Yaakov) on 2026-10-22: "Yes move it".

**Why it happens (live evidence, Langfuse 2026-10-22 04:50Z):** the gateway's tool schema is
`{params: {...}, withhuman_context: string}` (siblings). The gateway strips only the top-level
`withhuman_context` before forwarding (research-withhuman.md line 50; R9/R22 in the prototype ledger). The free
model sometimes nests it inside `params`, so it is forwarded to the QuickBooks server, which ignores it and warns.

## Global Constraints

- AGENTS.md Rule 2 holds: exactly one human approval per risky call, nothing runs twice, unclear outcomes never
  retried, fail closed. **What withHuman is asked about, what the ledger digests, and what Hermes dispatches must be
  the same lifted arguments.** A lift that changes the dispatched call after approval is a defect.
- The lift never raises out of the hook: any exception inside the lift → use the original arguments unchanged
  (and log one warning with the exception type only). It is a cleanup, not a gate.
- Never print argument values or the note in logs (log the tool name only).
- Hermes hook contract (hermes_cli/plugins.py `_get_pre_tool_call_directive_details`, Hermes v0.22.3):
  `{"action": "modify", "args": {...}}` shallow-merges its keys into the original args before dispatch; every
  dispatch path (model_tools.py, agent/agent_runtime_helpers.py, agent/tool_executor.py) uses the merged args.
  A modify cannot delete a key, only replace or add one.
- Docs rule (AGENTS.md Rule 1): update `durable-park/docs/architecture.md` (park hook steps / gateway arguments)
  and `durable-park/docs/backlog.md` if it lists this; run
  `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD`. Map any new file in
  `.github/docs-map.json` (prefer no new code file).
- Tests: `cd durable-park && <venv>/bin/pytest -q -W error` all pass; failing test first. The worktree has no
  venv: use `/home/hermes/tapdash-agents/durable-park/.venv/bin/pytest` from the worktree's `durable-park/` dir.
- Commits: conventional (`fix(park): ...`), author `Claude (AI agent) <noreply@anthropic.com>`, trailer
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Branch `fix/withhuman-context-lift`. Do not push,
  do not merge, do not restart anything, do not touch the live clone `/home/hermes/tapdash-agents`.

## Task 1: lift the nested note, use the lifted args everywhere, show it to the approver

**Files:** modify `durable-park/plugin/durable_park/gatecall.py` (new pure function + `build_call` reasoning),
`durable-park/plugin/durable_park/park_hook.py` (use it), tests in `durable-park/tests/test_gatecall.py` and
`durable-park/tests/test_park_hook.py`, docs as above.

**Function** `lift_gateway_context(args: Any, schema: dict | None) -> Any` in `gatecall.py`:
1. Returns `args` unchanged (same object is fine) when `args` is not a dict, or `schema` is not a dict, or
   `schema.get("properties")` is not a dict or does not contain `"withhuman_context"` (only gateway tools
   advertise it at the top level; any other tool is untouched).
2. Otherwise works on a deep copy. For each top-level key, in sorted key order, whose value is a dict that
   contains `"withhuman_context"`: skip that key if the tool's own schema for it
   (`schema["properties"][key]`, when a dict with a dict `properties`) itself declares `withhuman_context`
   (the server really has such a field). Else remove `withhuman_context` from that nested dict.
3. The first removed value that is a non-empty `str` becomes the top-level `withhuman_context`, but only when the
   top level has none (an existing top-level value always wins). Non-string or later values are just dropped.
4. Only one level deep. A top-level value that is a JSON *string* is left alone (pre_tool_call fires before
   Hermes's type coercion; parsing strings here is out of scope).
5. Idempotent: `lift(lift(x)) == lift(x)`. Never mutates its input.

**Hook** (`make_pre_tool_call`): right after `args` is read in step 3 (after the session/call-id checks, before the
form check), compute `lifted = lift_gateway_context(args, <schema>)` where `<schema>` comes from the same registry
lookup the form check uses (`formcheck.registry_schema`, injectable for tests — add a `schema_fn` keyword to
`make_pre_tool_call`, default `registry_schema`; a schema lookup error → no lift). From there on use `lifted` for
the form check, `ledger.park(args=...)`, the id-reuse digest comparison, and everything else.
When the call is allowed (step 8, `Allow`), return `{"action": "modify", "args": lifted}` instead of `None` if
`lifted != args`; return `None` when nothing changed. Block paths are unchanged (the parked row stores `lifted`,
so the executor later dispatches the lifted call). Exempt tools and `tool_call` (steps 0-1) are untouched.

**Approver note** (`build_call`): when the row's arguments (`json.loads(row.args_json)`) are a dict with a
top-level non-empty string `withhuman_context`, pass it as `Call(reasoning=...)`, cut to 2000 characters.
`build_call` stays a pure function of the row (the executor replays it and must send the identical body).

**Tests (write first, must fail before the change):**
- gatecall: nested note moved to top; existing top-level wins and nested is dropped; non-string nested dropped,
  not moved; schema without top-level `withhuman_context` → unchanged; nested schema declaring the field →
  unchanged for that key; two nested dicts → sorted-first string wins, both removed; non-dict args unchanged;
  JSON-string value unchanged; idempotent; input not mutated; `build_call` sets `reasoning` (and cuts at 2000) and
  leaves it `None` without a note.
- park_hook (FakeProvider as existing tests do): an allowed gateway call with a nested note returns
  `{"action": "modify", "args": <lifted>}` and the provider saw the lifted arguments; an allowed call with nothing
  to lift returns `None`; a parked call stores the lifted args in the ledger (row args have the top-level note,
  `params` has none) and the request's `reasoning` is the note; a replay of the same nested call (same
  session_id/tool_call_id) after parking is treated as the same call (not "id reused"); a schema lookup that
  raises → original args used, call still handled.
- Run the whole suite with `-W error`.

**Report:** `durable-park/.superpowers/sdd/withhuman-context-lift/task-1-report.md` (what changed, test command +
output summary, anything you were unsure about).
