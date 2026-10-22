# Langfuse trace gaps — design

Status: approved by Yaakov 2026-10-22 (chat). Linear: TAP2-125 follow-up.

## Problem

The Langfuse audit of 2026-10-22 (`durable-park/docs/acceptance-2026-10-22-langfuse.md`) found three gaps that no
setting of Nous's `langfuse` plugin (v2.0.0 @ `c2a9596`) can fix:

1. **Who sent it:** no trace has a `userId`.
2. **Names:** every trace is "Hermes turn".
3. **Linking a payment:** the parking turn, the turn after the approval and the run are separate traces with nothing
   in common that Langfuse can filter on.

## Decisions

- **D1 — our own patched copy.** Vendor the plugin at `c2a9596` into this repo as `tracing/tapdash_langfuse/`
  (plugin name `tapdash-langfuse`), patch it there, install it on the VM as a symlink into the live clone (same
  pattern as durable-park), and disable the catalog `langfuse` plugin (kept installed for rollback). No pull request
  to Nous (Yaakov, 2026-10-22). `PATCHES.md` lists every change against upstream.
- **D2 — user id.** Trace `userId` = the chat sender id Hermes passes to the turn-scoped `pre_llm_call` hook
  (`sender_id`). Langfuse requires US-ASCII and at most 200 characters for a user id, and the code also requires at
  least 1 character and no leading or trailing whitespace: a sender id that fails any of these is left out (never
  altered; *amended after strong review: this states the rule `_ascii_attr` applies*). The sender's display name (`HERMES_SESSION_USER_NAME` via
  `gateway.session_context.get_session_env`) goes into the **root observation's** metadata as `user_name` (exported
  as `langfuse.observation.metadata.user_name`, not as trace metadata); it may be non-ASCII and is cut to 200
  characters. No sender (cron, some CLI turns) → no `userId` and no `user_name`. *Amended after strong review
  (2026-10-22): this first said "trace metadata" and that a name that is not US-ASCII or is too long is left out.*
- **D3 — trace names.** `<platform>-turn` (lower case, only `a-z 0-9 _ -`), `cron-run` for cron, `hermes-turn`
  when the platform is unknown. The root observation gets the same name. Generation names ("LLM call N") unchanged.
- **D4 — pattern tags (gap 3).** New optional setting `HERMES_LANGFUSE_TAG_PATTERNS`: `label=regex` pairs separated
  by `;`. Every match in the turn's original user message (the `user_message` Hermes passes, not the API copy that
  carries injected context; the API copy only when no original is passed; *amended after strong review*), a tool
  call's arguments or a tool's result adds the trace tag
  `label:value` (value = group 1 if the regex has a group, else the whole match). On proto-agents:
  `HERMES_LANGFUSE_TAG_PATTERNS=park=\bP-[0-9A-F]{8}\b`, so the parking turn, the approval turn
  (`[durable-park] P-… was APPROVED …`) and the run (`{"status": "executed", "park_id": "P-…"}`) all carry
  `park:P-…`. Generic: nothing in the plugin knows about durable-park.
- **D5 — late tags reach the trace.** Spike of 2026-10-22 (environment `spike`): Langfuse keeps tags per
  observation and does not merge a child's tags into the trace; a tag set on the root span's OTel attribute
  `langfuse.trace.tags` before the root ends is on the trace and is found by a `traceTags any of` filter. So a
  late match is added to the propagated tag set (later children) AND set on the root's OTel span at once.
- **D6 — limits.** At most 10 pattern tags per trace; each tag at most 200 characters and US-ASCII, else skipped;
  only the first 200 000 characters of each scanned text are scanned. Invalid setting entries (no `=`, bad label
  — not `[a-z0-9_-]{1,40}` — or a regex that does not compile) log one warning each and are ignored. With
  `HERMES_LANGFUSE_CAPTURE=metadata` no pattern tags are added (tags would carry content).
- **D7 — durable-park safety.** No durable-park code changes. `on_pre_tool_call` stays byte-identical to upstream;
  scanning happens in `on_post_tool_call` and at root start only. The observer name changes to
  `hermes_plugins.tapdash_langfuse.on_pre_tool_call`; it is re-reviewed and listed in `observer_hooks` (the old
  `hermes_plugins.langfuse.on_pre_tool_call` entry is removed in a second, ledger-checked restart, after the catalog plugin has stopped loading; the new name is listed first, so there is no moment where durable-park would refuse calls; amended after final review).
- **D8 — tests.** Upstream's suite plus new tests run on the VM with the Hermes environment's Python
  (`tracing/run_tests.sh`). Upstream's 4 `TestSystemPromptCrossesHookBoundary` tests are deselected: they run
  Hermes core's request builder, which reinstalls packages and re-executes the process, dropping the test path;
  they do not cover patched code. CI does not run these tests yet (backlog).
- **D9 — tools offered on each LLM call (addendum, TAP2-125).** Each "LLM call N" generation records the names of the
  tools the model was offered (`tools_offered`, `tools_offered_count`), read from the provider request body in
  `on_pre_llm_request`. Names only, so it is recorded in every capture mode. Details in "Tools offered on each LLM
  call (D9)" below.
- **D10 — approval decision score (addendum, TAP2-125).** New optional setting `HERMES_LANGFUSE_SCORE_PATTERNS`:
  `name=regex[=>literal]` entries separated by `;`, generic like D4 (nothing in the plugin knows about durable-park).
  The turn's ORIGINAL user message is searched (same source and cap as D4; not tool arguments or results). A match
  sends one categorical score `name` when the trace starts, with `client.create_score(...)` and the score id
  `<trace id>-<name>`; a name is scored at most once per trace (`TraceState.scored_names`), the first matching entry
  wins. Value: named group `value`, else the literal, else group 1, else the whole match, lower-cased, at most 100
  characters. Comment: the other named groups in pattern order (`comment` bare, others `group: text`, joined with
  `; `, at most 500 characters). On proto-agents it turns durable-park's wake text into `approval_decision` =
  `approved | denied | cancelled | expired` with comment `P-…; by: <decider>; note: <note>` (exact setting in the
  README). Decisions taken with Yaakov (2026-10-22):
  - **`metadata` capture mode sends no scores** (A3). The plugin is generic and cannot tell whether a value or comment
    carries content; for this use the values are fixed words, but the comment holds a reviewer's note. Same rule as D6.
  - **Retries** (A5). Langfuse replaces a re-sent score only when id, name and UTC date all match: the deterministic
    id makes a same-day retry idempotent; a retry across UTC midnight would add a second score. Not an issue in
    practice: a score is sent once per trace, at trace start.
  - **Sampling** (A6). In SDK 4.15.2 `create_score` calls `add_score_task(force_sample=False)` for a valid 32-hex trace
    id, so scores follow the trace's sampling: a sampled-out trace has no score either (consistent: the score would
    point at a trace that does not exist).
  - A drift test reads durable-park's `wake.py` (parsed with `ast`, not imported) and fails when its three message
    formats change, because the setting depends on their exact words.

## Tools offered on each LLM call (D9)

Each "LLM call N" generation records which tools the model was offered, so a trace shows whether a tool was even
available when the model did not use it (for example with Hermes Tool Search on, where only a small set is sent).
`on_pre_llm_request` reads `request["body"]["tools"]` and adds two metadata keys: `tools_offered` (sorted names) and
`tools_offered_count` (int).

- Both request styles are read: OpenAI `{"type": "function", "function": {"name": ...}}` and Anthropic / flat
  `{"name": ...}`. Anything else is skipped; if no name is found, both keys are left out. The code runs inside
  `_failsafe`, so an odd shape can never break the generation.
- Names only (never descriptions or schemas), so it is safe in every capture mode, `metadata` included.
- At most 200 names, each cut to 64 characters. Order of steps: dedupe on the full name, count (`tools_offered_count`
  is that distinct count, before the cap and the cut), cut to 64, dedupe again, sort, cap. The count can therefore be
  larger than the list when the cap applies or when long names collapse after the cut.
- Only `on_pre_llm_request` changes; `_start_child_observation`, `on_pre_tool_call` and `register` stay as they are.

## Out of scope

Pull request to Nous; durable-park changes; an event for the approval decision itself; production.
