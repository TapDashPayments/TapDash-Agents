# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 0 | **Contain the agent's tools:** decided: Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1) then Phase 2 before local tools; docker backend and `approvals.deny` dropped. Must be done before any real credentials. | The user `hermes` has passwordless sudo and Hermes approvals are off. The v1 lockdown (re-applied 2026-10-21 03:16 UTC) withholds file and terminal tools, but it is a configuration guard: an agent that got such a tool could read `/etc/durable-park/*` (QuickBooks secret and refresh token, withHuman tunnel token), change `/opt/durable-park`, forge ledger rows and call QuickBooks directly, bypassing withHuman. Accepted by Yaakov on 2026-10-20 for the sandbox exploration phase. |
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only 12 money types do; 32 other writes (customers, vendors, items, accounts...) are approved automatically. |
| 5 | Tie the bridge's stored answers to the withHuman request id (Important #5) | Defence in depth; today it relies on withHuman never reusing a delivery key. |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)); planned as spec §4.5 (§8 item 10), required before file, terminal or code tools open | The lockdown is the only thing keeping the agent from writing the ledger. A separate owner is **not enough on its own**: while the agent's user has passwordless sudo, no file permission is a boundary, so do item 0 first; after that this is the route to a real boundary around the ledger. |

## Later

- Alerts to Microsoft Teams instead of Telegram (Hermes's Teams platform is now set up in the same gateway, since
  2026-10-21; only the sweep's delivery target has to change).
- Widen `risky_tools` beyond QuickBooks money moves: withHuman decides every call (spec 2026-10-21 §4.2); today the
  agent has no terminal or file tools, so nothing else needs gating yet.
- More than one approver, or a backup approver (a withHuman escalation-path change; no code).
- The second agent (content) and one Hermes profile per person.
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

- Check the agent's tool policy at every gateway start (Important #6): the `hermes-gateway` drop-in runs
  `deploy/tool_policy_check.sh` before each start; the plugin refuses every call when the report is missing, failed
  or older than 15 minutes, and the sweep alerts on failure and on recovery. Only relevant while the v1 lockdown
  (or an allow file) is applied.
- Bridge matches hyphenated tool names (Important #1).
- `2.0` and `2` treated as the same amount (minor).
- Form check before parking.
- Docs, diagrams and the docs-update check (Important #7).
- A `parked` row that never got a request id is cancelled by the sweep after 10 minutes (Important #3).
