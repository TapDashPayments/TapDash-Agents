# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 0 | **Contain the agent's tools:** Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1) is **DONE (2026-10-22 02:22 UTC, 2026-10-21 10:22 PM ET)**: the connector `hermes-shell-mcp` runs as `ubuntu`, `/etc/sudoers.d/90-hermes` is removed (`hermes` has no sudo), SSH password login is off, the stale `ubuntu` gateway and dashboard units are removed, and all services passed the check. **Still open from Phase 1:** restrict Tailscale SSH to `ubuntu` in the Tailscale admin console (the owner's action; `COMMANDS.md` step 10), and optionally rotate the Hermes dashboard login (`COMMANDS.md` step 3). **Phase 2 is still open** (item 7); docker backend and `approvals.deny` dropped. Must be done before any real credentials. | `hermes` can no longer sudo, so an agent that got a terminal or file tool could not read `tunnel.env` (QuickBooks secret and refresh token, withHuman tunnel token) or change `/opt/durable-park`. It could still read the secrets `hermes` uses by design (`instance_token`, `audit_key`, `wake_secret`, `bridge_secret`) and forge ledger rows, because the ledger is still owned by `hermes`; Hermes approvals are off. The v1 lockdown (re-applied 2026-10-21 03:16 UTC) withholds file and terminal tools and **must stay in place until Phase 2**. Accepted by Yaakov on 2026-10-20 for the sandbox exploration phase (that risk was wider before Phase 1). |
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. The sweep already re-wakes an `approved` row that was never announced; the gap is only a message Hermes accepted then dropped. **Deferred by Yaakov 2026-10-21: not an issue now; revisit before real books.** |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only the 12 money types ask a person. The non-money writes (customers, vendors, items, accounts...) are approved by pipeline rules `pa-gateway-other` and `pa-park-quickbooks-other`. |
| 5 | Tie the bridge's stored answers to the withHuman request id (Important #5) | Defence in depth; today it relies on withHuman never reusing a delivery key (answers are already bound to tool name, arguments and agent). Also prune stored answers older than ~30 days. **Deferred by Yaakov 2026-10-21: not an issue now.** |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)); planned as spec §4.5 (§8 item 10), required before file, terminal or code tools open | The lockdown is the only thing keeping the agent from writing the ledger. A separate owner is **not enough on its own** while the agent's user could sudo; that part of item 0 is done (Clean Setup Phase 1, 2026-10-22), so this is now the route to a real boundary around the ledger. **Still open.** Until it is done, `hermes` owns `ledger.db` and is in `dpwake`, so file, terminal and code tools must stay off. |
| 8 | Langfuse retention: the Hobby plan gives 30 days of data access and the retention cannot be changed, against the 365-day retention policy (TapDash Agents security decisions, 2026-10-15). Decide a paid plan or self-hosting ([Tracing](runbook.md#tracing-langfuse)) | After 30 days the traces can no longer be read. Fine for the test VM; not for real books. |
| 9 | Langfuse is another outside company that holds full conversations (capture mode `full`: chat text, model inputs and outputs, tool arguments and results). Decide whether that is acceptable for production, and which capture mode to use | With real books those would include real customer and money data. |
| 10 | **Critical (found 2026-10-22, not caused by tracing):** the Telegram bot token is written in plain text to the `hermes-gateway` journal (the `httpx` INFO request lines). Rotate the token in BotFather and update `TELEGRAM_BOT_TOKEN` in `~/.hermes/.env` (typed in with `read -rs`, never in chat), then set the `httpx` logger to WARNING for the gateway so it is not logged again | Anyone who can read the journal can control the bot. Until it is rotated, treat the old token as exposed. |
| 11 | **Important (found 2026-10-22 by the strong review of PR #32; durable-park follow-up):** make durable-park fail closed across a live plugin reload, or block live reloads. Needs its own plan, tests and independent review before any deploy (AGENTS.md Rule 2: it touches the park hook). Until then the runbook rule is: stop `hermes-gateway` before any `hermes plugins enable` / `disable` and never use the dashboard's plugin toggle while the gateway runs ([warning](runbook.md#never-change-plugins-while-the-gateway-runs)) | `hermes plugins enable|disable` of ANY plugin makes the running gateway reload every plugin (`discover_plugins(force=True)`). Evidence, 2026-10-22 04:37:28 UTC ([record](acceptance-2026-10-22-langfuse.md)): `gateway.run_plugin_rewire` re-wired the adapters and durable-park logged `tool policy check failed (stale); refusing every tool call` until the restart (about 18 s). Two defects: (a) after a reload durable-park judges the start-time policy report stale and refuses every call until a restart (an outage, not a loss of safety); (b) for about 0.1 s while plugins are unloaded and loaded, the `pre_tool_call` hook list is empty, so a call that starts then is not checked by durable-park (withHuman's gateway and the bridge still hold QuickBooks calls; no call was recorded in the 2026-10-22 window). |

## Langfuse tracing gaps (from the 2026-10-22 audit)

Found by auditing the live traces ([acceptance-2026-10-22-langfuse.md](acceptance-2026-10-22-langfuse.md)). Tracing is
live and works. Yaakov decided on 2026-10-22 to fix items 1 to 3 in a patched copy of the plugin,
[`tracing/tapdash_langfuse`](../../tracing/tapdash_langfuse) ([design](../../docs/specs/2026-10-22-langfuse-trace-gaps-design.md);
changes against upstream: its `PATCHES.md`). The copy is **built and tested, not deployed yet**: items 1 to 3 are live
only after the deploy ([runbook](runbook.md#switch-to-the-patched-plugin-deploy)). Item 4 is cosmetic and item 5 is only noted.
No durable-park code changes.

1. **No `userId` on any trace.** The Nous plugin never sets one. **Fixed in `tracing/tapdash_langfuse`, live after the
   deploy:** `userId` is the chat sender id (and `user_name`, the sender's display name, goes in the root observation's metadata). A turn with no sender (cron,
   some CLI turns) has no `userId`; a sender id that is not US-ASCII or is over 200 characters is left out, never altered (the display name is kept as it is, cut to 200 characters).
2. **Every trace is named `Hermes turn` and every model call `LLM call N`.** **Fixed in `tracing/tapdash_langfuse`, live
   after the deploy:** traces are named `telegram-turn`, `teams-turn`, `cli-turn`, `cron-run` or `hermes-turn` (platform
   unknown). The model calls are still named `LLM call N`; that part was not changed.
3. **Park, approval and resume are separate traces,** joined only by the park id in the text; the inner `create_payment`
   is not a child of `parked_action_execute`. **Fixed for finding them, in `tracing/tapdash_langfuse`, live after the
   deploy:** the new setting `HERMES_LANGFUSE_TAG_PATTERNS` (value to set: `park=\bP-[0-9A-F]{8}\b`) tags each trace that
   mentions a park id with `park:P-XXXXXXXX`, so one tag filter returns the parking turn, the approval turn and the run
   ([how](runbook.md#find-everything-about-one-approval)). It needed no durable-park change. **Still open:** the traces
   are not linked as parent and child, the withHuman request id is not tagged, and there is no event for the approval
   decision itself (out of scope of the design; it would touch the park, wake and executor code and would need its own
   plan, tests and independent review, AGENTS.md Rule 2).
4. **The root `CHAIN` has a parent observation id that points at a span that is never exported.** Cosmetic; upstream.
5. **Noted, not a gap: the Langfuse public key is in every span's metadata** (`scope.attributes.public_key`, an SDK default). An
   identifier, not a secret; nothing to do unless the SDK adds an option to leave it out.

6. **Run `tracing/run_tests.sh` in CI.** The patched plugin's tests run only on the test VM today
   (`bash tracing/run_tests.sh` as `hermes`: they need the Hermes environment's Python and a `hermes-agent`
   checkout), so CI does not run them. Add a CI job that checks out `hermes-agent` and runs them.

Also found on the way:

- The QuickBooks server answers "withhuman_context was IGNORED": the agent sends a parameter that is dropped.
  Investigate where it comes from and whether anything relied on it.
- `hermes plugins enable langfuse` created a new Hermes environment directory; the old ones remain. Clean up the old
  environments later, when disk space matters.

## Later

- Alerts to Microsoft Teams instead of Telegram (Hermes's Teams platform is now set up in the same gateway, since
  2026-10-21; only the sweep's delivery target has to change).
- Widen `risky_tools` beyond QuickBooks money moves: withHuman decides every call (spec 2026-10-21 §4.2); today the
  agent has no terminal or file tools, so nothing else needs gating yet.
- More than one approver, or a backup approver (a withHuman escalation-path change; no code).
- Tool Search: on since 2026-10-21 19:59 UTC and verified ([record](acceptance-2026-10-21-tool-search.md)); what is left is `docs/tool-rollout.md` (spec [section 4.4](../../docs/specs/2026-10-21-all-tools-withhuman-design.md), section 8 item 9).
- Replace the GitHub login on the VM (`gh` as the admin user `ubuntu`) with a **fine-grained token limited to this repository**. Today it is a broad classic OAuth token (scopes `repo`, `workflow`, `read:org`, `gist`) that reaches every repository of that account ([runbook](runbook.md#open-a-pull-request-from-the-vm)).
- Optional: run the sweep from a systemd timer instead of Hermes cron, so it can alert when the gateway is down (today it runs inside the gateway, so a dead gateway also silences it). The cost is about 1 s of CPU per run, effectively negligible.
- File two upstream Hermes issues: the `${env:VAR}` warning is printed before `secrets.command` has run, and the plugin catalog rejects unknown tiers in older clients.
- Mem0 plugin files are left installed but disabled on `proto-agents` (`~/.hermes/plugins/mem0`, `~/.hermes/mem0.json`, and `MEM0_TELEMETRY` in `~/.hermes/.env`). Yaakov decides: keep or remove.
- Delete the stale `~/.hermes/plugin-data/durable-park/plugin_load.json.hidden` (left by a manual test on 2026-10-21 17:00 UTC; harmless, nothing reads it) when convenient.
- The bot sometimes sends malformed QuickBooks calls (for example `get_invoice_pdf` without `invoice_id`). Hermes rejects them before the park hook, so nothing runs, but 3 in a row pause the `wh` MCP server for about 60 s. Reported by the owner; look at the model's instructions and the tool descriptions.
- Dashboard pages: pending withHuman approvals, full withHuman view, live trace of tool calls (spec section 8 item 12).
- The archived `ubuntu` Hermes install still holds Teams secrets (spec section 8 item 16).
- Rev 8 rollback hazard: rev 8 auto-approves `hermes`/`mcp:` calls, so never activate it while the ask-every-call code runs ([runbook](runbook.md#rollback)).
- Name the human decider in the refusal note (withHuman management API lookup; in progress).
- The second agent (content) and one Hermes profile per person.
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

- 2026-10-21: Tool Search compatibility (PR #26) and Tool Search switched on and verified live, with one approved $1 payment run once through the bridge ([acceptance-2026-10-21-tool-search.md](acceptance-2026-10-21-tool-search.md)).
- 2026-10-21: stale policy log level fixed (PR #22); executor told how to find the park id (PR #23); load check waits the full timeout (PR #25); the sweep's subprocess test is hermetic (PR #27); secret references written bare so the CLI is quiet, and a true 5-minute sweep on a `*/5 * * * *` schedule (PR #28; both applied live).
- 2026-10-21: Hermes v0.22.3 is the tested version (`SUPPORTED_HERMES_VERSION`): live approve -> run-once re-test passed;
  record in [acceptance-2026-10-21-hermes-0.22.3.md](acceptance-2026-10-21-hermes-0.22.3.md).
- 2026-10-21: ask every call (PR #7): every tool call goes to withHuman for a decision, except the exempt tools
  (`parked_action_execute`, `clarify`); acceptance record in [acceptance-2026-10-21-ask-every-call.md](acceptance-2026-10-21-ask-every-call.md) (PR #12).
- 2026-10-21: tool policy from one list, with a start check (PRs #8, #9).
- 2026-10-21: bridge copies the whole plugin package, `_vendor` included, and gates on a live bridge (PR #10).
- 2026-10-21: the refusal note says whether a policy rule or a person decided (PR #11).
- 2026-10-21: one policy alert when the tool policy report fails (PR #13).
- 2026-10-21: load check: the gateway is stopped (`ExecStartPost`, `deploy/plugin_load_check.sh`) when the plugin left no
  load proof (`plugin_load.json`) for its pid; the sweep alerts as a backup; the drop-in limits restarts to 3 in 10 minutes.
- 2026-10-21: check the agent's tool policy at every gateway start (Important #6), built as spec §8 item 7 part 2: the `hermes-gateway` drop-in runs
  `deploy/tool_policy_check.sh` before each start; the plugin refuses every call when the report is missing, failed
  or older than 15 minutes, and the sweep alerts on failure and on recovery. It checks the v1 lockdown that is
  applied on proto-agents.
- Bridge matches hyphenated tool names (Important #1).
- `2.0` and `2` treated as the same amount (minor).
- Form check before parking.
- Docs, diagrams and the docs-update check (Important #7).
- A `parked` row that never got a request id is cancelled by the sweep after 10 minutes (Important #3).
