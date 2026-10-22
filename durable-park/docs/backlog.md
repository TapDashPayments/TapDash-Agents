# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 0 | **Contain the agent's tools:** Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1) is **DONE (2026-10-22 02:22 UTC, 2026-10-21 10:22 PM ET)**: the connector `hermes-shell-mcp` runs as `ubuntu`, `/etc/sudoers.d/90-hermes` is removed (`hermes` has no sudo), SSH password login is off, the stale `ubuntu` gateway and dashboard units are removed, and all services passed the check. **Still open from Phase 1:** restrict Tailscale SSH to `ubuntu` in the Tailscale admin console (the owner's action; `COMMANDS.md` step 10), and optionally rotate the Hermes dashboard login (`COMMANDS.md` step 3). **Phase 2 is still open** (item 7); docker backend and `approvals.deny` dropped. Must be done before any real credentials. | `hermes` can no longer sudo, so an agent that got a terminal or file tool could not read `tunnel.env` (QuickBooks secret and refresh token, withHuman tunnel token) or change `/opt/durable-park`. It could still read the secrets `hermes` uses by design (`instance_token`, `audit_key`, `wake_secret`; `bridge_secret` is root-only since the bridge got its own user, 2026-10-22) and forge ledger rows, because the ledger is still owned by `hermes`; Hermes approvals are off. The v1 lockdown (re-applied 2026-10-21 03:16 UTC) withholds file and terminal tools and **must stay in place until Phase 2**. Accepted by Yaakov on 2026-10-20 for the sandbox exploration phase (that risk was wider before Phase 1). |
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. The sweep already re-wakes an `approved` row that was never announced; the gap is only a message Hermes accepted then dropped. **Deferred by Yaakov 2026-10-21: not an issue now; revisit before real books.** |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only the 12 money types ask a person. The non-money writes (customers, vendors, items, accounts...) are approved by pipeline rules `pa-gateway-other` and `pa-park-quickbooks-other`. |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)); planned as spec §4.5 (§8 item 10), required before file, terminal or code tools open. **The bridge part is done** by the [bridge change](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md): the bridge no longer reads or writes the ledger, so a forged row cannot get a gateway call approved. What is left is the ledger as the park hook, wake handler, executor and sweep use it (see "Option 2" under Later for removing it) | The lockdown is the only thing keeping the agent from writing the ledger (it no longer buys the bridge's approval, but a forged row could still confuse the executor or the sweep). A separate owner is **not enough on its own** while the agent's user could sudo; that part of item 0 is done (Clean Setup Phase 1, 2026-10-22), so this is now the route to a real boundary around the ledger. **Still open.** Until it is done, `hermes` owns `ledger.db` and is in `dpwake`, so file, terminal and code tools must stay off. |
| 8 | Langfuse retention: the Hobby plan gives 30 days of data access and the retention cannot be changed, against the 365-day retention policy (TapDash Agents security decisions, 2026-10-15). Decide a paid plan or self-hosting ([Tracing](runbook.md#tracing-langfuse)) | After 30 days the traces can no longer be read. Fine for the test VM; not for real books. |
| 9 | Langfuse is another outside company that holds full conversations (capture mode `full`: chat text, model inputs and outputs, tool arguments and results). Decide whether that is acceptable for production, and which capture mode to use | With real books those would include real customer and money data. |
| 10 | **Critical (found 2026-10-22, not caused by tracing):** the Telegram bot token is written in plain text to the `hermes-gateway` journal (the `httpx` INFO request lines). Rotate the token in BotFather and update `TELEGRAM_BOT_TOKEN` in `~/.hermes/.env` (typed in with `read -rs`, never in chat), then set the `httpx` logger to WARNING for the gateway so it is not logged again | Anyone who can read the journal can control the bot. Until it is rotated, treat the old token as exposed. |
| 11 | **Important (found 2026-10-22 by the strong review of PR #32; durable-park follow-up):** make durable-park fail closed across a live plugin reload, or block live reloads. Needs its own plan, tests and independent review before any deploy (AGENTS.md Rule 2: it touches the park hook). Until then the runbook rule is: stop `hermes-gateway` before any `hermes plugins enable` / `disable` and never use the dashboard's plugin toggle while the gateway runs ([warning](runbook.md#never-change-plugins-while-the-gateway-runs)) | `hermes plugins enable|disable` of ANY plugin makes the running gateway reload every plugin (`discover_plugins(force=True)`). Evidence, 2026-10-22 04:37:28 UTC ([record](acceptance-2026-10-22-langfuse.md)): `gateway.run_plugin_rewire` re-wired the adapters and durable-park logged `tool policy check failed (stale); refusing every tool call` until the restart (about 18 s). Two defects: (a) after a reload durable-park judges the start-time policy report stale and refuses every call until a restart (an outage, not a loss of safety); (b) for about 0.1 s while plugins are unloaded and loaded, the `pre_tool_call` hook list is empty, so a call that starts then is not checked by durable-park (withHuman's gateway and the bridge still hold QuickBooks calls; no call was recorded in the 2026-10-22 window). |
| 12 | **Important (durable-park deploy code; found 2026-10-22 in the re-review of PR #32):** `deploy/configure_hermes.sh` (line 211, `hermes_cli plugins enable "$PLUGIN_ID" --no-allow-tool-override`) runs `hermes plugins enable` without stopping the gateway. Change it to stop the gateway before the enable and start it after (and only when no ledger row is `parked`, `approved` or `executing`). Own change with tests and an independent review before deploying (AGENTS.md Rule 2). Until then: **check the ledger (no `parked`, `approved` or `executing` rows), then stop `hermes-gateway` before running the script** ([Before you run the configure script](runbook.md#before-you-run-the-configure-script); [why](runbook.md#never-change-plugins-while-the-gateway-runs)); with the gateway stopped, the script's step 7 (`configure_hermes.sh:420-431`) sees no gateway process and restarts `user@1001.service`, which restarts all `hermes` user units, `hermes-dashboard` included | The enable is run when durable-park is not yet in `plugins.enabled` (a fresh VM, or after it was disabled); then, on a live VM, it reloads every plugin inside the running gateway (the same defect as item 11: a fraction of a second without durable-park's check, then every call refused as stale until a restart). On a VM where durable-park is already enabled the branch is skipped, but do not rely on it. |

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

- The QuickBooks server answers "withhuman_context was IGNORED": the model nests the gateway's note inside `params`.
  Fixed on branch `fix/withhuman-context-lift` (2026-10-22, not deployed yet): the park hook lifts it to the top
  level and shows it to the approver ([architecture section 4](architecture.md#4-tool-call-form-check-then-one-question-to-withhuman)).
  After deploy, confirm in Langfuse that the warning is gone.
  **Deploy gate:** (1) before restarting the gateway with this change, run
  `DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_reasoning_echo.py` on proto-agents (it sends a 2000-character
  reasoning with spaces, newlines and non-ASCII to withHuman and expects an instant approval, which proves the AAP
  exact-match check passes; if it fails, withHuman changes or caps `agent_reasoning`: lower `MAX_REASONING_CHARS`
  and note the limit in `gateway-setup.md`). (2) Deploy only with no ledger row `parked`, `approved` or `executing`:
  this change alters the AAP request body for rows with a note, so a row parked by the old code would not match its
  approved request when the executor replays it.
- `hermes plugins enable langfuse` created a new Hermes environment directory; the old ones remain. Clean up the old
  environments later, when disk space matters.

## Later

- **Option 2: remove the ledger; withHuman is the only state** (park context stored in the withHuman request, the list API for the sweep). The bridge already works this way ([spec](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md)); this extends it to the park hook, wake handler, executor and sweep, so there is no file the agent's user could forge. Replaces the dpledger service design in spec 2026-10-21 §4.5.
- Stop double-checking non-money QuickBooks calls: today a non-money write is asked once as `park:quickbooks` (pipeline rule `pa-park-quickbooks-other` approves it) and again at the gateway (`pa-gateway-other` approves it), so both ask withHuman for nothing. Park and replay such calls without the second pass, or skip the park for them.
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
- `bridge_check.py` and bridge-unit minors, deferred from the bridge-own-user change ([plan](plans/2026-10-22-bridge-own-user.md)): check 4 (no other process runs `bridge.app`) is a heuristic that scans `/proc/*/cmdline` and can false-alarm (any process with `bridge.app` or `bridge/app.py` among its arguments, such as an editor, fails it); an edit to `durable-park-bridge.socket` takes effect only after `sudo systemctl restart durable-park-bridge.socket` (the deploy script does not restart a running socket); the check does not look at the permissions of the `/etc/durable-park` directory (it does check the running bridge's supplementary groups, since the final review).
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

- 2026-10-22: the gateway bridge runs as its own user `dpbridge` and systemd holds its port ([plan](plans/2026-10-22-bridge-own-user.md)): `durable-park-bridge.socket` keeps `127.0.0.1:8791` permanently (the service gets it as fd 3, `--systemd-socket`, `Restart=always`), `bridge_secret` is `root:root 0600`, `bridge_secret` and `audit_key` reach the bridge through `LoadCredential` (a rotation needs a bridge restart), `deploy/bridge_check.py` verifies the isolation at every deploy, and `configure_hermes.sh` refuses to run without both keys. Closes the gap that anything running as `hermes` could kill the bridge and answer "approve" on its port. Minors deferred: see Later.
- 2026-10-22: the bridge verifies approvals in withHuman instead of the ledger ([spec](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md)): stateless, org key with `audit.read`, `request.read`, `request.read.unrouted`, 120 s window, single use, the unit has no ledger path and `ProtectHome=yes` ([runbook](runbook.md#the-gateway-bridge)). Settles the bridge part of item 7 and the old item 5 (stored answers: the bridge keeps none). The old `bridge_deliveries.db` can be deleted once the new bridge is live.
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
