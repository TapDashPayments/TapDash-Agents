# durable-park

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin, plus two small services, that make an AI agent
**ask a person before it does anything risky**. When the agent tries to move money in QuickBooks, the call is
stopped and saved, an approver approves or denies it in [withHuman](https://withhuman.ai) - minutes or days
later - and then the exact saved call runs once, in the same chat. It survives restarts and never retries on its own.

Status: **v1 accepted on 2026-10-20**, QuickBooks **sandbox** only, on the test VM `proto-agents`
([acceptance results](docs/acceptance-2026-10-20.md)). The v1 tool lockdown was re-applied on proto-agents on 2026-10-21 03:16 UTC
(the agent reaches only `clarify`, `parked_action_execute` and `mcp__wh__quickbooks__*`). durable-park asks withHuman about
every tool call except `parked_action_execute`, `clarify` and the read-only Tool Search catalog tools (`tool_search`, `tool_describe`; Hermes Tool Search is on since 2026-10-21 19:59 UTC, [record](docs/acceptance-2026-10-21-tool-search.md), [runbook](docs/runbook.md#turn-on-tool-search)), and withHuman's pipeline decides which ones need a person
([spec](../docs/specs/2026-10-21-all-tools-withhuman-design.md)).

Since 2026-10-21 03:16 UTC the v1 tool lockdown is re-applied on proto-agents as a stopgap: the agent can reach only
`clarify`, `parked_action_execute` and `mcp__wh__quickbooks__*` (on every platform key; `hermes_tool_policy.py check` prints the key count and ends `ok`).
Hermes approvals stay off. Clean Setup Phase 1 is done (2026-10-22 02:22 UTC, 2026-10-21 10:22 PM ET;
[spec](../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1): the user `hermes` no longer has sudo. The ledger is
still owned by `hermes` (Clean Setup Phase 2, §4.5, not done), so the lockdown is still a configuration guard for the
ledger, not a permission boundary. Tools open again only through that spec (§4.4); file, terminal and code tools stay off
until Phase 2. Re-running `configure_hermes.sh` or
`hermes_tool_policy.py` re-applies the current state and restarts the gateway, so the "no parked/approved/executing
row" rule applies.

**Known risk, narrowed on 2026-10-22:** `hermes` no longer has sudo (Clean Setup Phase 1), so a hijacked agent that
found a way to run code as that user can no longer read `/etc/durable-park/` (the QuickBooks credentials) through sudo,
or change `/opt/durable-park`. What remains: the ledger and the plugin data are still owned by `hermes`, so such an agent
could forge ledger rows; the secrets `hermes` reads by design (`instance_token`, `audit_key`, `wake_secret`) stay
readable (`bridge_secret` is root-only since the bridge got its own user, `dpbridge`, on 2026-10-22); and an agent with a file or terminal tool could still reach everything `hermes` owns. durable-park
therefore is not a full security boundary yet: the lockdown (no file, terminal or code tools) must stay in place until Clean
Setup Phase 2 (a separate ledger owner, spec §4.5). Yaakov accepted the earlier, wider risk on 2026-10-20 for the QuickBooks
sandbox exploration phase. The open items are in the [backlog](docs/backlog.md#before-real-books), before any real books.

## How it works

```mermaid
flowchart LR
  agent["Agent"] -->|"risky call"| plugin["durable-park plugin<br/>stops it, checks the form,<br/>saves it"]
  plugin -->|"asks"| wh["withHuman"]
  wh -->|"approver approves"| plugin
  plugin -->|"runs the saved call once"| gw["withHuman gateway"]
  gw -->|"is this the approved call?"| bridge["bridge"]
  bridge -->|"yes, once"| gw
  bridge -->|"asks: was it approved<br/>and not used yet?"| wh
  gw --> qbo[("QuickBooks")]
```

1. The agent calls a risky tool (create, update or delete of 12 money types in QuickBooks).
2. The plugin checks the call against the tool's rules, saves it in a ledger and opens an approval request.
   The agent is told it is waiting.
3. An approver approves in withHuman. A signed webhook wakes the plugin, which tells the original chat.
4. The agent runs `parked_action_execute`; the plugin uses the approval once and replays the saved call.
5. The withHuman gateway asks the bridge whether this exact call was approved; the bridge checks in withHuman (not the ledger) and says yes once.
6. QuickBooks gets the call. The ledger records the result.

The full picture - every component, arrow, state and failure path: **[docs/architecture.md](docs/architecture.md)**.

Settings (`exempt_tools`, `form_checked_tools`, `observer_hooks`, `aap_base_url`, `token_file`, `audit_key_file`,
`audit_base_url`, `ledger_path`)
live in `plugins.entries.durable-park.settings` in Hermes's `config.yaml`: [table](docs/architecture.md#plugin-settings),
[how to change them](docs/runbook.md#plugin-settings). The old `risky_tools` setting is ignored with a warning. If
anything else registers a `pre_tool_call` callback (another plugin, a `config.yaml` shell hook or outbound webhook) or
`tool_request` / `tool_execution` middleware,
every tool call is refused until an admin reviews it and lists it in `observer_hooks`. A setting of the wrong type
also refuses every call (never fails open).
The open-source and third-party pieces it is built on (versions, licenses, links):
[Built on](../README.md#built-on).

## Folder layout

| Path | What is in it |
|---|---|
| `plugin/durable_park/` | The Hermes plugin: park hook, form check, ledger, wake handler, executor. Loaded by Hermes from this folder. |
| `bridge/` | The gateway bridge service (withHuman's pipeline asks it "is this call approved?"). |
| `receiver/` | The webhook receiver (withHuman tells it a decision was made) and one-time provisioning. |
| `scripts/` | The 5-minute sweep (safety net and alerts), run by Hermes cron. |
| `deploy/` | Deploy script, systemd units, the v1 tool lockdown (re-applied on proto-agents 2026-10-21 03:16 UTC as a stopgap), the QuickBooks tunnel container. |
| `tests/` | Unit tests (no network). `tests/live/` holds manual tests for the VM. |
| `docs/` | Architecture, runbook and setup guides (below). |

## Run the tests

```sh
cd durable-park
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q -W error
```

The unit tests never reach the network and never read real credentials (`tests/conftest.py` enforces both).
Live checks against the sandbox run only on the VM: `DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live`.
`tests/test_hermes_integration.py` runs the real Hermes functions the plugin leans on; it is skipped where Hermes is
not importable (CI) and run on the VM ([runbook](docs/runbook.md#check-durable-park-against-the-real-hermes)).

## Docs

| Doc | Read it to |
|---|---|
| [architecture.md](docs/architecture.md) | Understand how the pieces connect (diagrams, states, failure paths, where everything runs) |
| [runbook.md](docs/runbook.md) | Check health, deploy a change, act on an alert, troubleshoot, rotate secrets |
| [form-check.md](docs/form-check.md) | Understand the check that runs before anyone is asked |
| [gateway-setup.md](docs/gateway-setup.md) | Set up QuickBooks behind the withHuman gateway, the bridge and the pipeline rules |
| [receiver-setup.md](docs/receiver-setup.md) | Set up the receiver, provisioning and the v1 tool lockdown |
| [backlog.md](docs/backlog.md) | See what is still open, and what must be done before real books |
| [acceptance-2026-10-20.md](docs/acceptance-2026-10-20.md) | See what was tested for v1 (the record) |
| [acceptance-2026-10-21-ask-every-call.md](docs/acceptance-2026-10-21-ask-every-call.md) | See what was tested for "ask every call" (the record) |
| [acceptance-2026-10-21-hermes-0.22.3.md](docs/acceptance-2026-10-21-hermes-0.22.3.md) | See what was tested when Hermes v0.22.3 became the tested version (the record) |
| [acceptance-2026-10-21-tool-search.md](docs/acceptance-2026-10-21-tool-search.md) | See what was verified when Hermes Tool Search was switched on, and the other live settings changed that day (the record) |
| [plans/](docs/plans/) | The original build plan (historical) |

Changing code? Update the docs and diagrams it affects in the same change - see [CONTRIBUTING.md](../CONTRIBUTING.md).
