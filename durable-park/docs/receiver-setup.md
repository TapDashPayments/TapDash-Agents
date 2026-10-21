# durable-park receiver, provisioning and deploy (proto VM)

How withHuman's decision gets back into the Hermes chat:

```
withHuman (request.resolved, Standard-Webhooks signed)
  -> https://proto-agents.taila95862.ts.net/aap/events          (Tailscale Funnel)
  -> durable-park-receiver  127.0.0.1:8790 /aap/events           (system unit, user aaprecv)
       verifies the signature, answers 204, reads the request with the instance credential,
       context.resume = {"framework": "hermes", "park_id": ..., "session_key": ...}
  -> POST http://127.0.0.1:8644/webhooks/durable-park-wake        (Hermes webhook route, X-Hub-Signature-256)
  -> durable-park plugin, pre_gateway_dispatch: re-reads the decision from withHuman, updates the ledger,
     injects the APPROVED / DENIED / ... text into the original chat. The wake never reaches the agent.
```

A cron job (`durable-park-sweep`, every 5 min, no agent) re-posts wakes for anything a lost notification
left behind and turns `executing` rows older than 10 min into `unknown` with one alert line.

**Retries.** The SDK receiver answers withHuman `204` as soon as the signature checks out and only then,
in the background, reads the request and posts the wake (agentapprovalprotocol 0.3.3 `receiver.py:158-166`).
withHuman therefore counts the notification as delivered even if the wake later fails (Hermes down, wake
refused, withHuman read failed) and never retries it. Recovery is the sweep: on its next run (every 5 minutes,
about 6 in practice) it re-posts a wake for every `parked` row and every decided row the chat was not told
about, so a lost wake costs at most one sweep interval. withHuman's own retries (up to 7 days) only cover the receiver being unreachable or
answering non-2xx (e.g. 401 before provisioning).

## Files and owners

| Path | Owner / mode | Written by | Holds |
|---|---|---|---|
| `/etc/durable-park/wake_secret` | root:dpwake 0640 | configure_hermes.sh (once) | HMAC secret for the local wake route (members of dpwake: hermes, aaprecv) |
| `/etc/durable-park/provisioner_token` | root:root 0600 | an admin (Step 6) | `whp_` provisioner token; delete after provisioning |
| `/etc/durable-park/receiver.json` | aaprecv:aaprecv 0600 | provision.py | signing secret, registration id, idempotency key, instance id, the receiver's own copy of the instance token |
| `/etc/durable-park/instance_token` | root:hermes 0640 | provision.py | `whc_` instance credential (plugin gate + gateway) |
| `/etc/durable-park/bridge_secret` | root:dpwake 0640 | an admin | the `durable-park-bridge` webhook endpoint's signing secret (bridge) |
| `/etc/durable-park/audit_key` | root:hermes 0640 | an admin (optional, [runbook](runbook.md#name-the-decider-optional)) | withHuman org API key, `audit.read` only; names who decided |
| `/etc/durable-park/tunnel.env` | root:root 0600 | an admin (`enter-secrets.sh`) | QuickBooks app keys, refresh token, realm id, withHuman tunnel token |
| `/opt/durable-park/` | root 0755 | configure_hermes.sh | the receiver/bridge venv (`agentapprovalprotocol==0.3.3`, `uvicorn==0.54.0`, `starlette==1.9.0`); root-owned copies of `receiver/{app,wake_post,provision}.py`, `bridge/*.py` and `plugin/durable_park/*.py` (the bridge uses `ledger.py`); `libexec/hermes-secrets` |
| `~hermes/.hermes/plugin-data/durable-park/ledger.db` | hermes | the plugin | the ledger (`plugins.entries.durable-park.settings.ledger_path`; the sweep and the bridge use the same path) |
| `~hermes/.hermes/plugin-data/durable-park/bridge_deliveries.db` | hermes | the bridge | per-delivery answers, for withHuman's retries |
| `~hermes/.hermes/plugin-data/durable-park/formcheck_status.json`, `formcheck_alert_state.json` | hermes | the gateway's form self-check / the sweep | form checker health and the alert state |
| `~hermes/.hermes/scripts/durable_park_sweep.sh` | hermes 0750 | configure_hermes.sh | cron wrapper: `exec /usr/bin/python3 -I <repo>/scripts/durable_park_sweep.py --ledger <ledger> --wake-secret-file /etc/durable-park/wake_secret --retention-days 30` |

The webhook route's secret in `config.yaml` is the reference `${DURABLE_PARK_WAKE_SECRET}`. Hermes's
`command` secret source (`secrets.command.command: /opt/durable-park/libexec/hermes-secrets`, a root-owned
helper written by configure_hermes.sh) supplies the value at every Hermes start, so the secret exists at rest
only in `/etc/durable-park/wake_secret`. Once `instance_token` exists the same helper also prints
`DURABLE_PARK_WH_TOKEN` for the `wh` MCP server (deploy/hermes_mcp.sh). These variables are in the gateway's
environment and Hermes does not strip custom names from terminal-tool children; that exposes nothing new on
this VM (if the agent ever got a terminal, `hermes` could read both files).
Side effect: every `hermes` CLI command prints `Command helper: applied N secrets`.

**Why the references are bare `${VAR}`, not `${env:VAR}`.** Hermes expands config references when it first
loads config at import, which is before the `command` secret source has put the values in the environment. For
the `env:`-prefixed form `_env_expand_match` (`hermes_cli/config.py`) then logs a warning in every Hermes
process (`Config ref '...' ... is not set ... keeping the literal placeholder`); for a bare `${VAR}` it stays
silent. Both forms resolve identically: Hermes documents `${env:VAR}` as exactly `${VAR}` with the prefix
stripped, the loader cache records the referenced variables and re-expands when they change, and the gateway's
webhook route and the `mcp_servers` headers read the expanded values (checked in-process with fake values: the
first load keeps the placeholder, the next load after the variable is set returns the real value, for both
forms). An upstream issue for the ordering is to be filed with Hermes; until it is fixed, use the bare form.

A secret that is missing is still noticed, by different checks per use:

| Reference | If the variable is not set | Where it shows |
|---|---|---|
| `DURABLE_PARK_WH_TOKEN` (the `wh` MCP header) | Hermes refuses to connect the server (`_require_rendered_remote`, `tools/mcp_tool_config.py`): "...is not set in this profile's .env or secret source". It never sends the literal text. | `mcp__wh__*` tools are absent; `deploy/hermes_mcp.sh` waits for one and stops |
| `DURABLE_PARK_WAKE_SECRET` (the webhook route) | The route's secret stays the literal placeholder, so the signed wakes the receiver and the sweep send (signed with `/etc/durable-park/wake_secret`) are rejected with a non-2xx status. Hermes itself does not refuse to start. | `journalctl -u durable-park-receiver` logs `Hermes refused the wake for <id> ...: HTTP <code>; the sweep will retry`; the sweep writes `wake <id> returned HTTP <code>` to stderr (cron delivers only stdout); the chat never hears the decision, see [The bot never got the decision](runbook.md#the-bot-never-got-the-decision) |

Neither the plugin nor the receiver checks the references themselves. This is the same behaviour as with the
`env:` form, which also kept the placeholder; only the warning is gone.

## Deploy (Step 5, admin or AI agent, re-runnable)

> **On proto-agents this re-applies the current tool policy.** The script re-applies the v1 tool lockdown (re-applied
> there 2026-10-21 03:16 UTC as a stopgap) and restarts the gateway: run it only when no ledger row is `parked`,
> `approved` or `executing`. See [Agent tool policy](#agent-tool-policy-v1-lockdown-re-applied-on-proto-agents)
> and the [spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) section 4.1.

```sh
sudo bash /home/hermes/durable-park/deploy/configure_hermes.sh
```

Idempotent. It never prints a secret. Before provisioning, the receiver runs and answers every event
with 401 (no `receiver.json` yet). It restarts the hermes user manager once (so the gateway and its
cron scripts get the new `dpwake` group), later runs only restart `hermes-gateway`.

Checks after a deploy:

```sh
curl -s http://127.0.0.1:8644/health                               # {"status": "ok", "platform": "webhook"}
grep "durable-park loaded" ~/.hermes/logs/agent.log | tail -1        # plugin loaded, ledger path
sudo tailscale funnel status                                       # /aap/events proxy http://127.0.0.1:8790/aap/events
systemctl status durable-park-receiver                             # active (running)
hermes cron list                                                   # durable-park-sweep, every 5m, telegram
~/.hermes/scripts/durable_park_sweep.sh                            # prints nothing when all is well
```

## Step 6 — Admin: provisioner token, then provision (human step)

1. In withHuman: **Agents** → select the service agent **`proto-agents`** → open the menu beside
   **New instance** → **Issue provisioner token**. Copy the `whp_…` token (it is shown once).

2. Open a shell on the VM: `tailscale ssh ubuntu@proto-agents`, then paste this line exactly. It asks for
   the token without echoing it; the token never appears on screen, in shell history or in any command's
   arguments (`read` and `printf` are shell builtins):

   ```sh
   IFS= read -rs -p 'withHuman provisioner token (whp_...): ' T; echo; printf '%s\n' "$T" | sudo install -m 0600 -o root -g root /dev/stdin /etc/durable-park/provisioner_token; unset T; sudo stat -c '%a %U:%G %s bytes' /etc/durable-park/provisioner_token; sudo grep -q '^whp_' /etc/durable-park/provisioner_token && echo 'token stored (starts with whp_)'
   ```

   Expected: `600 root:root <n> bytes` and `token stored (starts with whp_)`.

3. Provision the instance (prints only the instance id):

   ```sh
   sudo /opt/durable-park/bin/python -I /opt/durable-park/receiver/provision.py --provisioner-token-file /etc/durable-park/provisioner_token --url https://proto-agents.taila95862.ts.net/aap/events --name proto-agents-1
   ```

   What it does: writes a pending `receiver.json` (new `whsec_` signing secret + registration id), restarts
   the receiver, checks that the receiver answers a signed verification challenge both on
   `127.0.0.1:8790` and through the public Funnel URL (nothing is sent to withHuman if this fails), then
   calls `POST https://app.withhuman.ai/api/aap/v1/instances` (withHuman verifies the receiver live
   within 10 s), then writes the final `receiver.json` + `instance_token` and restarts the receiver.
   Re-running it is safe: a pending registration is retried with the same idempotency key (no second
   instance), a finished one is never re-provisioned. Exit 2 = refused before anything was sent;
   exit 1 = a step failed, fix and re-run.

4. After it printed the instance id: `sudo rm /etc/durable-park/provisioner_token`. It is needed only to
   create the instance (keep it while a run is failing and you want to retry). Once provisioned, a re-run
   never calls withHuman and works without the file: it only checks `receiver.json` and restores a missing
   or wrong `instance_token` (0640 root:hermes):

   ```sh
   sudo /opt/durable-park/bin/python -I /opt/durable-park/receiver/provision.py --url https://proto-agents.taila95862.ts.net/aap/events --name proto-agents-1
   ```

The plugin reads `instance_token` lazily on each risky call, so no Hermes restart is needed.

## Alerts from the sweep

The cron job delivers to Telegram's **home channel**, set to the admin's DM with `@TapDashAgentsBot` (`/sethome`
in that DM changes it). Moving alerts to Microsoft Teams is planned: Hermes's Teams platform is already connected
in the same gateway (since 2026-10-21), so only the `durable-park-sweep` cron job's delivery target has to change.

## Agent tool policy (v1 lockdown, re-applied on proto-agents)

> **Status:** the v1 lockdown is applied on proto-agents since 2026-10-21 03:16 UTC, as a stopgap. It had been lifted by hand (decided 2026-10-20) and was re-applied 2026-10-21 03:16 UTC. The user `hermes` still has passwordless sudo until Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1), so the lockdown is a configuration guard, not a permission boundary (accepted for the sandbox phase; see [architecture](architecture.md#the-three-guarantees)). Hermes approvals stay off. Tools open again only through that spec (§4.4). Re-running `configure_hermes.sh` or `hermes_tool_policy.py` re-applies the current state and restarts the gateway, so the "no parked/approved/executing row" rule applies. The text below says what the lockdown does and why.

The agent's tools run as the `hermes` user. That user owns the ledger and can read `instance_token`. So
an agent with a terminal or file tool, for example one hijacked by a prompt injection, could write an
`executing` row and get the bridge to approve a gateway call no human approved.

v1 closes this by giving the model no local file, command, code, browser or scheduling tools at all.
`deploy/hermes_tool_policy.py` (applied by `configure_hermes.sh` and `hermes_mcp.sh`, then checked before
and after the gateway restart; a failed pre-restart check aborts without restarting anything) sets:

- `platform_toolsets.<key>` = `[durable_park, clarify, wh]` (until the `wh` MCP server exists: `no_mcp`)
  for every platform key an agent can run under in Hermes v0.22.2, derived from the source at deploy
  time: the built-in table (incl. `cron`), every gateway `Platform` (incl. `local`→`cli`,
  `msgraph_webhook`, `relay`, `sms`), every bundled platform plugin and its manifest alias (e.g. `teams`,
  `google_chat`, `irc`, `teams-platform`), every runtime-registered platform, `acp`, `tui`, `desktop`, and
  any key already in the config (58 keys on the VM in October 2026);
- `known_plugin_toolsets.<key>` = all plugin toolsets (so none switches itself on);
- `agent.disabled_toolsets` = every other toolset (terminal, file, code_execution, delegation, cronjob,
  skills, browser*, computer_use, memory, session_search, kanban, vision, web, …). Hermes subtracts it last
  at tool granularity, so it also covers sessions that do not use `platform_toolsets` (TUI coding posture,
  ACP, `HERMES_TUI_TOOLSETS`, cron jobs, subagents); `check` verifies those postures too;
- `tools.tool_search.enabled: off`;
- `gateway.strict: true`, `gateway.trust_recent_files: false`: a model can make the gateway upload a file
  just by naming it (`MEDIA:/path`) - no tool needed. Non-strict, the ledger was deliverable. Now only the
  Hermes media caches are.

**One stored list of opened toolsets.** `/etc/durable-park/allowed_toolsets` (root-owned, `0640 root:hermes`;
`configure_hermes.sh` creates it empty if missing and never overwrites it) names the toolsets opened beyond
the lockdown, one per line, `#` comments allowed. `apply` and `check` both read it (`--allow-file PATH`
overrides the path): each listed toolset leaves `agent.disabled_toolsets` and joins every platform list, and
`check` then accepts exactly the tools Hermes defines for those toolsets, plus the three above. An empty or
missing file gives the lockdown exactly as described here. A name that is not a Hermes toolset, or one of
`terminal`, `file`, `code_execution`, `delegation`, `cronjob`, `skills`, `computer_use` (these stay off until
Clean Setup Phase 2), makes the script print the reason on stderr and exit 2 without writing anything.
`check --status-file PATH` also writes `{"ok", "checked_at", "problems", "allowed"}` as JSON (atomically,
also when the check fails or crashes) for the gateway-start check. Exit codes: 0 ok, 1 check failed, 2 usage
error or invalid allow file. How to open one: [runbook](runbook.md#open-a-toolset).

`check` fails (and the deploy with it) if any tool other than `parked_action_execute`, `clarify`,
`mcp__wh__quickbooks__*` and the tools of the opened toolsets is reachable on any key or posture, if
`parked_action_execute` is missing, or if
the wake secret, the ledger or a freshly written file would be deliverable. `hermes_mcp.sh` also fails if
the gateway registers any `mcp__wh__` tool outside `mcp__wh__quickbooks__*` (it then stops the gateway). The
sweep is a no-agent cron script and is not affected.

## v2: separate ledger owner (design only, not built)

The v1 tool policy is a configuration guard. A Hermes upgrade, a new plugin toolset or a hand edit could
re-open the bypass. v2 makes it a privilege boundary instead:

- The ledger and the bridge's delivery records belong to a dedicated system user `dpledger`
  (`/var/lib/durable-park`, 0700). `hermes` can no longer open the SQLite files.
- A small ledger service runs as `dpledger` and listens on a unix socket
  (`/run/durable-park/ledger.sock`, 0660 `dpledger:dpledger-clients`). It exposes only the operations the
  plugin, sweep and bridge need: park, set_request_id, transition along the allowed edges,
  mark_notified, list, find_executing_match and mark_gateway_passed.
- The service checks each caller with `SO_PEERCRED`:
  - the Hermes gateway uid may park, read, and move rows only along plugin-owned edges;
  - only the bridge uid may call find_executing_match and mark_gateway_passed;
  - only the sweep may use the stale-executing → unknown edge.
- The `parked → approved` and `approved → executing` edges are allowed only after the service itself
  re-reads the decision from withHuman. So the instance token is readable only by `dpledger`, and the
  plugin asks the service to open and consume approvals instead of holding the token itself.
- The bridge runs as its own user and reads through the socket. A forged row is then impossible without
  code execution as `dpledger`. Even with full terminal access as `hermes`, the agent can only ask the
  service for transitions it would accept anyway.
- The gateway's `wh` MCP credential would still need a separate per-instance credential, or a
  service-side proxy, so that `hermes` cannot call the gateway directly. That is a withHuman-side
  question for v2.
