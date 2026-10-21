# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. |
| 3 | Clean up a `parked` row that never got a request id (Important #3) | Otherwise the chat stays blocked until someone fixes it by hand ([runbook](runbook.md#a-chat-says-already-waiting-but-nothing-is-pending-in-withhuman)). |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only 12 money types do; 32 other writes (customers, vendors, items, accounts...) are approved automatically. |
| 5 | Tie the bridge's stored answers to the withHuman request id (Important #5) | Defence in depth; today it relies on withHuman never reusing a delivery key. |
| 6 | ~~Check the agent's tool lockdown at every gateway start, not only at deploy (Important #6)~~ **Superseded 2026-10-20:** the lockdown was lifted on proto-agents (the agent has the full Hermes toolset, [spec](../../docs/specs/2026-10-20-one-agent-merge-design.md)), so there is nothing to check. Only relevant again if the lockdown is re-applied. | Was: a Hermes upgrade or setting change could quietly give the agent file or terminal tools again. |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)) | **More important since 2026-10-20, not superseded:** the lockdown was the only thing keeping the agent from writing the ledger; with terminal and file tools open, this is the only way to a real permission boundary. |

## Later

- Alerts to Microsoft Teams instead of Telegram (Hermes's Teams platform is now set up in the same gateway, since
  2026-10-21; only the sweep's delivery target has to change).
- Widen `risky_tools` beyond QuickBooks money moves (the lock-down phase): today durable-park is the only gate and
  the agent's terminal and file tools are not gated at all.
- More than one approver, or a backup approver (a withHuman escalation-path change; no code).
- The second agent (content) and one Hermes profile per person.
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

- Bridge matches hyphenated tool names (Important #1).
- `2.0` and `2` treated as the same amount (minor).
- Form check before parking.
- Docs, diagrams and the docs-update check (Important #7).
