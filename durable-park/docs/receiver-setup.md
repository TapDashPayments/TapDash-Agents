# durable-park receiver, provisioning and deploy (proto VM)

How withHuman's decision gets back into the Hermes chat:

```
withHuman (request.resolved, Standard-Webhooks signed)
  -> https://proto-agents.taila95862.ts.net/aap/events          (Tailscale Funnel)
  -> durable-park-receiver  127.0.0.1:8790 /aap/events           (system unit, user aaprecv)
       verifies the signature, answers 204, reads the request with the instance credential,
       context.resume = {"framework": "hermes", "park_id": ...}
  -> POST http://127.0.0.1:8644/webhooks/durable-park-wake        (Hermes webhook route, X-Hub-Signature-256)
  -> durable-park plugin, pre_gateway_dispatch: re-reads the decision from withHuman, updates the ledger,
     injects the APPROVED / DENIED / ... text into the original chat. The wake never reaches the agent.
```

A cron job (`durable-park-sweep`, every 5 min, no agent) re-posts wakes for anything a lost notification
left behind and turns `executing` rows older than 10 min into `unknown` with one alert line.

**Retries.** The SDK receiver answers withHuman `204` as soon as the signature checks out and only then,
in the background, reads the request and posts the wake (agentapprovalprotocol 0.3.3 `receiver.py:158-166`).
withHuman therefore counts the notification as delivered even if the wake later fails (Hermes down, wake
refused, withHuman read failed) and never retries it. Recovery is the sweep: within 5 minutes it re-posts a
wake for every `parked` row and every decided row the chat was not told about, so a lost wake costs at most
one sweep interval. withHuman's own retries (up to 7 days) only cover the receiver being unreachable or
answering non-2xx (e.g. 401 before provisioning).

## Files and owners

| Path | Owner / mode | Written by | Holds |
|---|---|---|---|
| `/etc/durable-park/wake_secret` | root:dpwake 0640 | configure_hermes.sh (once) | HMAC secret for the local wake route (members of dpwake: hermes, aaprecv) |
| `/etc/durable-park/provisioner_token` | root:root 0600 | Yaakov (Step 6) | `whp_` provisioner token; delete after provisioning |
| `/etc/durable-park/receiver.json` | aaprecv:aaprecv 0600 | provision.py | signing secret, registration id, idempotency key, instance id, the receiver's own copy of the instance token |
| `/etc/durable-park/instance_token` | root:hermes 0640 | provision.py | `whc_` instance credential (plugin gate + gateway) |
| `/opt/durable-park/` | root 0755 | configure_hermes.sh | receiver venv (`agentapprovalprotocol==0.3.3`, `uvicorn==0.54.0`, `starlette==1.9.0`) and a copy of `receiver/*.py` |
| `~hermes/.hermes/plugin-data/durable-park/ledger.db` | hermes | the plugin | the ledger (`plugins.entries.durable-park.settings.ledger_path`; the sweep and the bridge use the same path) |
| `~hermes/.hermes/scripts/durable_park_sweep.sh` | hermes 0750 | configure_hermes.sh | cron wrapper: `exec /usr/bin/python3 -I <repo>/scripts/durable_park_sweep.py --ledger <ledger> --wake-secret-file /etc/durable-park/wake_secret` |

The webhook route's secret in `config.yaml` is the reference `${env:DURABLE_PARK_WAKE_SECRET}`. Hermes's
`command` secret source (`secrets.command.command: /opt/durable-park/libexec/hermes-secrets`, a root-owned
helper written by configure_hermes.sh) supplies the value at every Hermes start, so the secret exists at rest
only in `/etc/durable-park/wake_secret`. Once `instance_token` exists the same helper also prints
`DURABLE_PARK_WH_TOKEN` for the `wh` MCP server (deploy/hermes_mcp.sh). These variables are in the gateway's
environment and Hermes does not strip custom names from terminal-tool children; that exposes nothing new on
this VM (the agent's terminal runs as `hermes`, which can read both files anyway).
Side effect: every `hermes` CLI command prints `Command helper: applied 1 secret` and a harmless
`Config ref '${env:DURABLE_PARK_WAKE_SECRET}' ... is not set` line (the CLI reads config before the
secret source runs; the gateway gets the real value, verified with a signed test wake).

## Deploy (Step 5, done by Claude, re-runnable)

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

## Step 6 — Yaakov: provisioner token, then provision (human step)

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

## Telegram alerts from the sweep

The cron job delivers to Telegram's **home channel**. Hermes on this VM has no Telegram home channel
yet, so alert lines would not be delivered. Fix: in the Telegram DM with the bot, send `/sethome`.

## Agent tool policy (v1 fix for the ledger bypass)

The agent's tools run as the `hermes` user. That user owns the ledger and can read `instance_token`. So
an agent with a terminal or file tool, for example one hijacked by a prompt injection, could write an
`executing` row and get the bridge to approve a gateway call no human approved.

v1 closes this by giving the model no local file, command, code, browser or scheduling tools at all.
`deploy/hermes_tool_policy.py` (applied and then checked by `configure_hermes.sh` and `hermes_mcp.sh`)
sets the following:

- `platform_toolsets.<every platform>` = `[durable_park, clarify, wh]`. Until the `wh` MCP server
  exists, the list holds `no_mcp` instead of `wh`.
- `known_plugin_toolsets.<every platform>` = all plugin toolsets.
- `agent.disabled_toolsets` = every other toolset (terminal, file, code_execution, delegation, cronjob,
  skills, browser*, computer_use, memory, session_search, kanban, vision, web, …).
- `tools.tool_search.enabled: off`.

After every deploy, `check` prints each platform's effective tools. The deploy fails if anything other
than `parked_action_execute`, `clarify` and `mcp__wh__*` is reachable, or if `parked_action_execute` is
missing. The sweep is a no-agent cron script, so the policy does not affect it.

## v2: separate ledger owner

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
