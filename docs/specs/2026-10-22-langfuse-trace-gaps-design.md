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
  (`sender_id`). Langfuse requires US-ASCII and at most 200 characters for a user id: a sender id that is not is left
  out (never altered). The sender's display name (`HERMES_SESSION_USER_NAME` via
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

## Out of scope

Pull request to Nous; durable-park changes; an event for the approval decision itself; production.
