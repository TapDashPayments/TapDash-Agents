# durable-park: acceptance record, Langfuse tracing (2026-10-22)

**Result: PASSED. Langfuse tracing is live on the test VM `proto-agents` and does not change how approvals work.**
Every chat message, model call and tool call is copied, with its full content, to Langfuse Cloud (US). A $1
payment still asked a human once in withHuman and ran once. Five gaps need code (recommendations in the
[backlog](backlog.md#langfuse-tracing-gaps-from-the-2026-10-22-audit)); none changes what runs.

- Environment: test VM `proto-agents` (QuickBooks sandbox), Hermes Agent v0.22.3, Nous Research `langfuse` plugin v2.0.0
  @ `c2a9596`, Langfuse SDK 4.15.2, Langfuse Cloud US, Hobby (free) plan, project `proto-agents`.
- How it works and how to run it: [runbook, Tracing (Langfuse)](runbook.md#tracing-langfuse).
- Audit method: the Langfuse agent skill (`github.com/langfuse/skills` @ `127253d`, `references/instrumentation.md`
  baseline table) and the Langfuse best-practices page (fetched fresh on 2026-10-22).
- Previous record: [acceptance-2026-10-21-hermes-0.22.3.md](acceptance-2026-10-21-hermes-0.22.3.md).

## What happened (all times UTC, 2026-10-22)

| Time | Step |
|---|---|
| 04:24 | Plugin installed, left disabled; its hook `hermes_plugins.langfuse.on_pre_tool_call` added to `observer_hooks` next to the Raft one |
| about 04:35 | Yaakov typed the Langfuse keys into `~hermes/.hermes/.env` |
| 04:37:45 | Ledger had no active row; `hermes plugins enable langfuse`; gateway restarted (PID 370372 to 409800) |
| 04:40 to 04:52 | Yaakov's Telegram test, one chat session (four traces, below) |
| 04:57:37 | Added `MAX_DEPTH=10`, `MAX_CHARS=50000`, `RELEASE=hermes-v0.22.3`; ledger empty; restart (PID 409800 to 413587) |
| 04:59 | Smoke turn (`hermes chat -q`) and the first secret scan |
| 13:39 | Yaakov's second invoice lookup still showed `<max-depth>` |
| 13:42:18 | `MAX_DEPTH=16`; ledger empty; restart (PID 413587 to 469072); secret scan repeated |

## Checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Plugin on, durable-park still loads | PASS | `hermes plugins list` shows `langfuse` enabled. `agent.log`: `durable-park loaded: ... 2 observer hook(s) ...` and no `foreign pre_tool_call hook(s) present` line, on the live gateway. The Langfuse plugin logs no key warning (`Langfuse plugin:` / `Langfuse tracing:`). The line `capability_check plugin=langfuse capability=tools.override decision=deny` appears at every start and is harmless. |
| 2 | Gateway and policy healthy | PASS | `hermes-gateway` active after the restarts. `tool_policy_status.json` `"ok": true` and `plugin_load.json` present (checked after the 04:57 restart). |
| 3 | Plain chat message | PASS | Trace `af1fefb9` ("hi, what can you do?"): `Hermes turn` with the full message in and the reply out, `LLM call` generations with model and token counts. |
| 4 | QuickBooks read | PASS | Trace `5dc98827` (invoice lookup): the tool call is a `TOOL` observation with its arguments and the full result. A second lookup at 13:39 (trace `1db0bec4`) is what showed the depth cut-off, fixed by `MAX_DEPTH=16`. |
| 5 | Payment that parks: one human ask, runs once | PASS | Trace `be88850d`: `create_payment` of $1 was parked as ledger row **P-420E1D2C**; withHuman asked Yaakov once; he approved after about 54 s; the row went to `executed` once and one payment was created. The parked call shows in Langfuse as a `TOOL` at level `WARNING`, status `plugin_block`. The resume turn is trace `95056bf4`, in the same session. No second payment. |
| 6 | Whole chat is one session | PASS | The four traces (`af1fefb9`, `5dc98827`, `be88850d`, `95056bf4`) share session `20261022_022112_68c2fc10`, including the turn after the approval. |
| 7 | Settings take effect | PASS | Smoke trace `581b168e`: environment `proto-agents`, release `hermes-v0.22.3`. Offline check with the installed plugin's own serializer on the payment's data: depth 10 gave 1 + 5 cut-offs, depth 16 gave 0 + 0. |
| 8 | Secret scan of Langfuse | PASS | See below. |

Ledger before every restart: no row `parked`, `approved` or `executing`.

## Audit against the Langfuse baseline

Each item from the skill's baseline table and the best-practices page.

| Item | Verdict | What was seen |
|---|---|---|
| Model name | Meets | Every generation carries the model name. |
| Token usage | Meets | Input and output tokens recorded on each generation. |
| Observation types | Meets | Each model call is a `GENERATION`, each tool call a `TOOL`, the turn a `CHAIN`. |
| Trace input / output | Meets | The root `Hermes turn` input is the user's message and its output is the final reply. |
| Session grouping | Meets | One session id for all turns of a chat, resume included. |
| Environment | Meets | `proto-agents` on every observation. |
| Parked call visible | Meets | `TOOL`, level `WARNING`, status message `plugin_block`. |
| Release | Fixed by config | Was missing; `HERMES_LANGFUSE_RELEASE=hermes-v0.22.3` (04:57Z). Observations before that carry none, which is expected. |
| Deep data cut off | Fixed by config | The default depth (4) cut 16 observations including `create_payment` arguments; 10 still cut `linked_txn` and tool schemas; `HERMES_LANGFUSE_MAX_DEPTH=16` (13:42Z) cuts nothing. |
| Long fields cut | Fixed by config | Root outputs of about 14,000 characters were cut at 12,000; `HERMES_LANGFUSE_MAX_CHARS=50000`. |
| Cost shows 0 | Not a gap | The model is a free one (`upstage/solar-mini4:free`) that Langfuse has no price for. |
| No `userId` on any trace | Needs code | The plugin never sets a user id. Recommendation: pass the chat user id (upstream PR to the Nous plugin), or a small durable-park-side tag. Useful once there is more than one user. |
| Every trace named `Hermes turn`, every call `LLM call N` | Needs code | Names do not say platform or purpose. Recommendation: upstream, name by platform and purpose (for example `telegram-chat-turn`). |
| Park, approval and resume are separate traces | Needs code | They are joined only by the park id in the text; the inner `create_payment` is not a child of `parked_action_execute`. Recommendation: a durable-park follow-up with its own plan, tests and independent review (AGENTS.md Rule 2): add the park id and the withHuman request id as trace metadata or a tag (for example `park:P-...`) and an event for the approval decision. |
| Root `CHAIN` points to a span that is never exported | Needs code | Cosmetic (upstream). |
| Langfuse public key in every span's metadata | Not a gap | `scope.attributes.public_key`, an SDK default. An identifier, not a secret. |

No code was changed in this work: only configuration and docs. The code gaps are recommendations for Yaakov to decide.

## Secret scan

Run at 04:59 UTC over 26 observations and repeated at 13:42 UTC, in one process that printed only counts: the
candidates were the secret-looking `.env` values and the readable files in `/etc/durable-park/`, plus the shapes of a
Langfuse key, a Telegram bot token, a Bearer token, a withHuman `who_` key and card-number-like digit runs (method:
[runbook](runbook.md#secret-scan-how-it-was-done-and-how-to-repeat-it)).

- Found nothing: no Langfuse secret key, no `.env` secret value, no readable `/etc/durable-park` secret, no Telegram bot
  token, no Bearer token, no withHuman `who_` key, no card-number-like digits.
- Only hit: the Langfuse **public** key in every span's metadata (above).
- Not scanned: `/etc/durable-park/receiver.json` and `tunnel.env` (owner-only; `hermes` cannot read them).

## Open items

- Decide the code gaps above: user id, trace names, park/approval/resume linking, the cosmetic root parent
  ([backlog](backlog.md#langfuse-tracing-gaps-from-the-2026-10-22-audit)).
- **Critical, found on the way, not caused by tracing:** the Telegram bot token is written in plain text to the
  `hermes-gateway` journal. Rotate it and quiet the logger
  ([backlog](backlog.md#before-real-books), item 10).
- The QuickBooks server answers "withhuman_context was IGNORED": the agent sends a parameter that is dropped. Look into it.
- Langfuse retention (30 days of data access on Hobby against the 365-day policy) and holding full conversations at an
  outside company: decide before real books ([backlog](backlog.md#before-real-books), items 8 and 9).
- `hermes plugins enable` created a new Hermes environment; the old ones are still on disk (clean up later).
- Keep `HERMES_LANGFUSE_RELEASE` in step with the Hermes version after an upgrade.
- Mem0 leftovers are still in place and untouched (Yaakov's decision).
