# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 0 | **Contain the agent's tools:** decided: Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1) then Phase 2 before local tools; docker backend and `approvals.deny` dropped. Must be done before any real credentials. | The user `hermes` has passwordless sudo and Hermes approvals are off. The v1 lockdown (re-applied 2026-10-21 03:16 UTC) withholds file and terminal tools, but it is a configuration guard: an agent that got such a tool could read `/etc/durable-park/*` (QuickBooks secret and refresh token, withHuman tunnel token), change `/opt/durable-park`, forge ledger rows and call QuickBooks directly, bypassing withHuman. Accepted by Yaakov on 2026-10-20 for the sandbox exploration phase. |
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. The sweep already re-wakes an `approved` row that was never announced; the gap is only a message Hermes accepted then dropped. **Deferred by Yaakov 2026-10-21: not an issue now; revisit before real books.** |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only the 12 money types ask a person. The non-money writes (customers, vendors, items, accounts...) are approved by pipeline rules `pa-gateway-other` and `pa-park-quickbooks-other`. |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)); planned as spec §4.5 (§8 item 10), required before file, terminal or code tools open. **The bridge part is done** by the [bridge change](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md): the bridge no longer reads or writes the ledger, so a forged row cannot get a gateway call approved. What is left is the ledger as the park hook, wake handler, executor and sweep use it (see "Option 2" under Later for removing it) | The lockdown is the only thing keeping the agent from writing the ledger (it no longer buys the bridge's approval, but a forged row could still confuse the executor or the sweep). A separate owner is **not enough on its own**: while the agent's user has passwordless sudo, no file permission is a boundary, so do item 0 first; after that this is the route to a real boundary around the ledger. |

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
- Delete the stale `~/.hermes/plugin-data/durable-park/plugin_load.json.hidden` (left by a manual test on 2026-10-21 17:00 UTC; harmless, nothing reads it) when convenient.
- The bot sometimes sends malformed QuickBooks calls (for example `get_invoice_pdf` without `invoice_id`). Hermes rejects them before the park hook, so nothing runs, but 3 in a row pause the `wh` MCP server for about 60 s. Reported by the owner; look at the model's instructions and the tool descriptions.
- Dashboard pages: pending withHuman approvals, full withHuman view, live trace of tool calls (spec section 8 item 12).
- The archived `ubuntu` Hermes install still holds Teams secrets (spec section 8 item 16).
- Rev 8 rollback hazard: rev 8 auto-approves `hermes`/`mcp:` calls, so never activate it while the ask-every-call code runs ([runbook](runbook.md#rollback)).
- Name the human decider in the refusal note (withHuman management API lookup; in progress).
- The second agent (content) and one Hermes profile per person.
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

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
