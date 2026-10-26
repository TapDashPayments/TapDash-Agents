# durable-park runbook

In plain English: this is the admin's manual for the test server. It says how to check that everything is healthy,
how to put a change live, what each alert means and what to do about it, and how to roll back. If something looks
wrong, start with the [Health check](#health-check).

How to check, change and fix durable-park on the test VM. For how it works, see [architecture.md](architecture.md).

- [Access and ground rules](#access-and-ground-rules)
- [Health check](#health-check)
- [Deploy a change](#deploy-a-change)
- [Alerts and what to do](#alerts-and-what-to-do)
- [Troubleshooting](#troubleshooting)
- [Plugin settings](#plugin-settings)
- [Tracing (Langfuse)](#tracing-langfuse)
- [Add a form-checked tool](#add-a-form-checked-tool)
- [Rotate secrets before production](#rotate-secrets-before-production)
- [Rollback](#rollback)

## Access and ground rules

| What | How |
|---|---|
| VM | `proto-agents` (OVH VPS, Ubuntu 26.04, Hermes v0.22.3). Admins: `tailscale ssh ubuntu@proto-agents` (passwordless sudo). |
| AI agents (e.g. Claude) | Current route: the Proto-Agents connector (see the last row), which runs as the admin user `ubuntu` (passwordless sudo) since 2026-10-22; it reaches Hermes files and units as `hermes` (next row). Older route (jump host, production; not for this VM): through the Hermes Server connector, piping scripts into `/tmp/dp-tools/vm` (runs as `hermes`) or `/tmp/dp-tools/vmroot` (root) on the jump host. |
| Reaching Hermes as admin | The user `hermes` has no sudo (since 2026-10-22). As `ubuntu` run Hermes commands with `sudo -u hermes -H bash -lc '<command>'` (its home is 750 and `~/.hermes` is 700), and its user units with `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user <verb> <unit>`. Where this runbook says "as `hermes`" (health check, `systemctl --user ...`, `hermes cron ...`), use those forms. |
| Repo on the VM | `/home/hermes/tapdash-agents` (a clone of this repo). `/home/hermes/durable-park` links to its `durable-park/` folder. |
| GitHub CLI | `gh` 2.46.0 (apt). Signed in as the admin user `ubuntu` only, which is also the user the connector runs as, so `gh` works directly from the connector; `hermes` is not signed in (`gh auth status` as `hermes` says so). Pull requests are opened with that `gh`: [Open a pull request from the VM](#open-a-pull-request-from-the-vm). |
| withHuman | app.withhuman.ai: agent `proto-agents`, gateway server `quickbooks`, webhook endpoint `durable-park-bridge`, entry pipeline. |
| Telegram test bot | `@TapDashAgentsBot`, home channel = the admin's DM with the bot (where the sweep's cron job delivers alerts; alerts also go to Teams, see [live settings](#live-hermes-settings-on-proto-agents)). |
| Teams bot | "TapDash Agent", served by the same `hermes` gateway on `127.0.0.1:3978`; the devtunnel `proto-agents-bot` forwards to it. The tunnel runs as user `hermes` (systemd user unit `devtunnel-hermes`, program `/usr/local/bin/devtunnel`; moved from `ubuntu` on 2026-10-22). Manage it like the other `hermes` units: `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user <status\|restart> devtunnel-hermes`. |
| Web dashboard | <https://proto-agents.taila95862.ts.net:8443/> (HTTPS through Tailscale Serve; tailnet only, **not** Funnel), user `TapDash`; systemd user unit `hermes-dashboard` of user `hermes`. Fallback, plain HTTP by IP only: `http://100.99.171.75:9119/`. How it is wired and how to undo it: [Dashboard over HTTPS](#dashboard-over-https). |
| Claude connector "Proto-Agents" | system service `hermes-shell-mcp`, runs as user `ubuntu` from `/home/ubuntu/hermes-shell-mcp` (own venv, fastmcp 4.1.1) since 2026-10-22 02:22 UTC; `127.0.0.1:8792`, Tailscale Funnel path `/hermes-shell`, GitHub OAuth for one allowed account. Stopping it cuts Claude's access to the VM. Backup of the previous unit file: `/root/hermes-shell-mcp.service.bak`. If a connector login ever fails with `PathSecurityError` (entries resolve outside the new root), do not copy fastmcp's OAuth store (`~/.local/share/fastmcp/oauth-proxy`) between users: move it aside and re-authorize the connector in Claude, which creates a fresh store. |

Rules:

- **Never change the production Hermes host.** The jump host is production; only the VM is for testing.
- When piping scripts through the connector, give every heredoc a **unique** delimiter (e.g. `<<'XEOF_7'`) and
  never reuse it inside. A reused `EOF` once ran the tail of a script on the production host.
- **Never print a secret.** Secrets are typed in by an admin with `read -rs` (never pasted into chat).
- **Never stop `durable-park-bridge.socket` in normal operation.** It is what keeps `127.0.0.1:8791` away from the
  bot: without it the port is free and anything running as `hermes` could bind it and answer withHuman's webhook.
  A key change needs only `sudo systemctl restart durable-park-bridge` (the socket keeps the port meanwhile). A change
  to the socket unit itself needs `sudo systemctl restart durable-park-bridge.socket durable-park-bridge.service`; that
  closes and re-opens the listener for a moment, so check first that no ledger row is `parked`, `approved` or
  `executing` ([waiting rows](#health-check)). More: [The gateway bridge](#the-gateway-bridge).
- Root steps (copying into `/opt`, restarting system units) should be confirmed by the admin before they run. Since
  2026-10-22 they no longer need the admin at a terminal: the connector runs as `ubuntu`, which has passwordless sudo, so
  Claude can run them. Keep the rule that the admin confirms risky actions first (anything destructive, hard to undo, or
  that touches secrets, sudoers, sshd, Tailscale or a running gateway). The agent (`hermes`) has no sudo and no terminal tool.
- **One agent, one Hermes install, v1 tool lockdown.** The `hermes` install is the only agent on the VM. The agent can
  reach only `clarify`, `parked_action_execute`, `mcp__wh__quickbooks__*`, `mcp__wh__maverick__*`, `mcp__wh__acceptblue__*`, `mcp__wh__authnet__*` and the remote servers'
  `mcp__wh__supabase_crm_dev__*`, `mcp__wh__monday__*`, `mcp__wh__docusign_demo__*`, plus the three Tool Search bridge tools
  (`tool_search`, `tool_describe`, `tool_call`) while Tool Search is on. **This is not a full security boundary
  today.** How strong it is, what an agent could still do and where Clean Setup Phase 1 and Phase 2 stand:
  [architecture section 0](architecture.md#0-how-strong-is-it-today) (the one copy of that text). What is left before
  real books: the [backlog](backlog.md#before-real-books).
- **Run `hermes` commands only as the `hermes` user** (`sudo -u hermes -H ...`, see
  [Reaching Hermes as admin](#access-and-ground-rules) above). Running `hermes` as `ubuntu` or any other user can create
  a second Hermes install or a second gateway, without any of the guards. That happened: an old install under `ubuntu`
  had its gateway unit re-created on 2026-10-21 at 22:50 ET and ran an unguarded agent until it was deleted on
  2026-10-22 (TAP2-112; [what happened](architecture.md#0-how-strong-is-it-today)). If you ever find `~ubuntu/.hermes`,
  a Hermes unit under `ubuntu`, or something on port 3978 or 8644 that is not the `hermes` gateway, treat it as an
  incident and tell the admin.
- **Before you run `configure_hermes.sh`:** it re-applies the tool lockdown and restarts the gateway, and its
  `hermes plugins enable` step reloads every plugin inside a running gateway. Check the ledger, then stop
  `hermes-gateway`: [Before you run the configure script](#before-you-run-the-configure-script)
  ([warning](#never-change-plugins-while-the-gateway-runs), [backlog](backlog.md#before-real-books) item 12).
- **Restarting `hermes-gateway` also restarts the Teams bot** (and briefly drops Telegram). The dashboard is its own unit
  and keeps running when only the gateway is restarted (not when `configure_hermes.sh` runs with the gateway stopped: it
  restarts the whole `hermes` user manager, see above). The "no `parked`, `approved` or `executing` row" rule below still applies before any restart.
- QuickBooks is a **sandbox** company. Never connect real books before the [rotation](#rotate-secrets-before-production)
  and the open v2 items are done.

### Never change plugins while the gateway runs

> **WARNING: never enable, disable, install, remove or reload any plugin while `hermes-gateway` is running.** That is
> `hermes plugins enable <name>`, `hermes plugins disable <name>` (any plugin, not only Langfuse) and the plugin
> toggle in the dashboard. Hermes does **not** wait for a restart: it tells the running gateway to unload and load
> **every** plugin again, durable-park included. For a fraction of a second (about 0.1 s) durable-park's check is
> missing, so a tool call that starts in that instant runs without it (QuickBooks calls are still held by withHuman's
> gateway and the bridge). Right after, durable-park judges its tool-policy report `stale` and **refuses every tool
> call until the gateway is restarted**. This happened on 2026-10-22 at 04:37:28 UTC, when the Langfuse plugin was
> enabled ([record](acceptance-2026-10-22-langfuse.md)). It is a backlog item to make durable-park safe against it
> ([backlog](backlog.md#before-real-books), item 11); until that is built and reviewed, follow this rule.
>
> Do it this way instead, as `hermes` ([how to reach Hermes as admin](#access-and-ground-rules)):
>
> 1. Check that no ledger row is `parked`, `approved` or `executing` ([health check](#health-check)).
> 2. `systemctl --user stop hermes-gateway` (use `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user stop hermes-gateway` from `ubuntu`). Telegram and the Teams bot are offline until step 4.
> 3. Run the `hermes plugins enable|disable ...` command or edit the config. The command prints that no running
>    gateway answered: that is **expected**. Nothing is reloaded, because nothing is running.
> 4. `systemctl --user start hermes-gateway`, then run the check the procedure names (for Langfuse,
>    [Check it works](#check-it-works)).

### Agent persona (SOUL.md)

`~/.hermes/SOUL.md` on the VM carries a capability paragraph (reworded 2026-10-21). It deliberately **lists no
specific tools**: the allowed tools can change at any time (the owner's point), so a list in the persona would go
stale. Instead it tells the agent to:

- use only the tools in its current tool list, and check that list each time;
- never call a tool that is not in it (for example a shell or terminal it was not given);
- use tool search, if it has it, before saying it cannot do something;
- say plainly when it lacks a tool;
- treat a "terminal UI" chat as only the chat screen, not a terminal;
- call `parked_action_execute` only with a park ID taken from a `[durable-park]` message in this chat, and never
  invent one.

Why: the dashboard chat's Hermes system prompt says "You are in the Hermes terminal UI (TUI)", which a small model
read as having a terminal (2026-10-21). Because the paragraph names no tools, opening or closing a toolset (the
[all-tools plan](../../docs/specs/2026-10-21-all-tools-withhuman-design.md)) does not require editing it; the agent
follows whatever tool list it is given. Check that the wording is still there after any change to `SOUL.md` (it was, on the live file, at 2026-10-21 20:10 UTC).

### Live Hermes settings on proto-agents

The settings below were changed on the VM itself (not by a repo change), so they are listed here. Checked live on
2026-10-21 at about 20:10 UTC, as `hermes` (the Langfuse row was added on 2026-10-22). Print a key with `hermes config get <key>` (never print the whole file
or `~/.hermes/.env`: they hold secrets).

| Setting | Live value | Why |
|---|---|---|
| `tools.tool_search.enabled` | `on` (since 2026-10-21 19:59 UTC) | Cuts a new chat's input tokens; verification in [the record](acceptance-2026-10-21-tool-search.md). How it was switched on and off: [Turn on Tool Search](#turn-on-tool-search). |
| `platforms.api_server.enabled` | `false` | Hermes v0.22.3 refuses to start the API server platform without `API_SERVER_KEY`, and nothing here uses it. With it enabled and no key the gateway reported `DEGRADED`. |
| `platforms.ntfy.enabled` | `false` | Hermes has no adapter for ntfy and nothing here uses it; it also made the gateway report `DEGRADED`. |
| `agent.restart_drain_timeout` | `45` (set by `configure_hermes.sh`; GitHub #66) | Hermes's default `0` made any stop or restart cut off a reply in progress (2026-10-26: the "it ran" reply for P-BBD5A244 was lost). Now a stop or restart waits up to 45 s for replies to finish. Keep it at least 15 s under the gateway unit's `TimeoutStopSec` (`70`). |
| `max_concurrent_sessions` | `20` | The key was present and empty, and Hermes warned about it; the owner chose 20. |
| Secret references (`platforms.webhook.extra.routes.durable-park-wake.secret`, `mcp_servers.wh.headers.Authorization`) | bare `${VAR}` form | PR #28; no `${env:...}` is left in `config.yaml`. See [Config ref is not set](#every-hermes-command-prints-config-ref---is-not-set). |
| `TEAMS_ALERT_WEBHOOK_URL` (environment of the sweep's cron run, set in `~/.hermes/.env`; a secret, never print it) | **set** (verified with a test card on 2026-10-22 at 16:57 ET) | The URL of a Power Automate "Post card in a chat or channel" (Teams webhook) flow; the sweep sends an Adaptive Card with `"msteams": {"width": "Full"}` at its root so it fills the Teams chat width instead of wrapping in a narrow column (a plain `{"text": ...}` body is rejected with "Property 'type' must be 'AdaptiveCard'"). When set, the sweep also posts what it printed (its alert lines: ids, tool names and reasons, never argument values) to Teams once per run. When unset, nothing changes. A failed post (timeout 10 s, so an outage can delay the sweep's exit by about that long) adds one plain line to the chat alerts, `durable-park ALERT: Teams post failed (<ErrorType or HTTP code>).`, and `teams alert failed (...)` on stderr; the exit code stays 0, so the Telegram message is not wrapped in "Script exited with code 1". The URL is never printed. If the sweep itself crashes, what it printed is still posted and the crash is not hidden. |
| Cron job `durable-park-sweep` (id `5982945c6071`) | `*/5 * * * *`; delivery is still Telegram | PR #28 changed the deploy script; the live job was edited to match. See [The sweep runs every 6 minutes](#the-sweep-runs-every-6-minutes-not-5). Alerts reach Teams through `TEAMS_ALERT_WEBHOOK_URL` (row above), so a Telegram copy is now optional; stopping the Telegram delivery is an open decision ([backlog](backlog.md#later)). |
| Teams tunnel (`devtunnel-hermes`) | systemd **user unit of `hermes`**, enabled (moved from `ubuntu` on 2026-10-22). Program `/usr/local/bin/devtunnel` (root-owned). Login cache `/home/hermes/.local/share/DevTunnels` (mode 600), logged in as Yaakov@TapDash.co. Tunnel `proto-agents-bot` forwards to `127.0.0.1:3978` | The Teams bot's public address needs a tunnel to the gateway. Never print or copy the login cache. Manage it with `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user <status\|restart> devtunnel-hermes`. It no longer exists under `ubuntu`. |
| `hermes-dashboard` unit | `RestartPreventExitStatus=78` in the main unit file | See [Dashboard over HTTPS](#dashboard-over-https). |
| Langfuse tracing: plugin `tapdash-langfuse` (TapDash's patched copy, a symlink `~/.hermes/plugins/tapdash-langfuse` into the live clone's `tracing/tapdash_langfuse`) enabled; the unpatched catalog `langfuse` is no longer enabled (its folder is still in `~/.hermes/plugins`). non-secret `HERMES_LANGFUSE_*` settings in `~/.hermes/.env` (not in `config.yaml`); the two Langfuse keys come from Bitwarden since 2026-10-22/23 ([Secrets: Bitwarden](#secrets-bitwarden)) | tracing on since 2026-10-22 04:37 UTC (unpatched); patched plugin enabled by 19:52 UTC the same day ([how it was switched](#switch-to-the-patched-plugin-deploy)) | Traces of every turn for debugging and review. Names, values and reasons for each setting: [Tracing (Langfuse)](#tracing-langfuse). Print `.env` names only, never values. |
| Bitwarden Secrets Manager (`secrets.bitwarden` in `config.yaml`) | `enabled: true`, project "TapDash Agents", `encrypted_cache.max_stale_seconds: 604800`; applies 6 secrets at each start (changed live 2026-10-22/23) | The agent's Teams, Telegram and Langfuse keys come from Bitwarden, not from `.env`. `.env` keeps only `BWS_ACCESS_TOKEN`, `TEAMS_ALERT_WEBHOOK_URL` and settings that are not secret. See [Secrets: Bitwarden](#secrets-bitwarden). |

If `hermes-gateway` reports `DEGRADED` after a Hermes update, read `~/.hermes/logs/gateway.log` for the platform
that failed to start before changing anything. To use `api_server` later, an admin sets `API_SERVER_KEY` in the
secrets store first (typed in, never pasted into chat), then enables the platform.

## Dashboard over HTTPS

The dashboard itself speaks plain HTTP on the Tailscale IP only (`hermes dashboard --host 100.99.171.75 --port 9119`).
Browsers upgrade bare addresses to `https` and then fail with `ERR_SSL_PROTOCOL_ERROR`, so **Tailscale Serve** puts HTTPS in
front of it on the machine's Tailscale name, port 8443 (set up 2026-10-21):

- `tailscale serve --bg --https=8443 http://100.99.171.75:9119` (tailnet only: port 8443 is **not** in `AllowFunnel`).
  The Funnel on port 443 (`/aap/events`, `/wh/bridge`, `/hermes-shell`) is untouched.
- The dashboard rejects any Host name it was not told about ("Invalid Host header"). A systemd drop-in
  `~/.config/systemd/user/hermes-dashboard.service.d/public-url.conf` sets
  `HERMES_DASHBOARD_PUBLIC_URL=https://proto-agents.taila95862.ts.net:8443`. It is on the dashboard unit only, not in
  `config.yaml`, so the gateway is not affected.
- Check: `curl -s -o /dev/null -w '%{http_code}\n' https://proto-agents.taila95862.ts.net:8443/login` prints `200`, and
  `tailscale serve status --json` lists `AllowFunnel` for port 443 only.
- Undo: `tailscale serve --https=8443 off`, delete the drop-in, then `systemctl --user daemon-reload && systemctl --user restart hermes-dashboard`.
  The saved Serve config from before the change is in `/home/hermes/serve-backup/`.
- `RestartPreventExitStatus=78` (Hermes v0.22.3 asks for it) is a line in the **main** unit file
  `~/.config/systemd/user/hermes-dashboard.service`, which is hand-written; it is not in a drop-in. Keep it there:
  Hermes's update check (`hermes_cli/update_cmd_fleet.py`, `_repair_unit_without_fatal_exit_park`) reads only the main
  unit file, so a drop-in would still be reported as missing. Its hint, "Repair: `hermes gateway install`", only
  regenerates the **gateway** unit, so do not run it for the dashboard; edit the dashboard unit by hand, then
  `systemctl --user daemon-reload && systemctl --user restart hermes-dashboard`.
- Restarting `hermes-dashboard` does not touch the gateway, so the "no `parked`, `approved` or `executing` row" rule does not apply to it.

## Health check

As `hermes` on the VM (from `ubuntu`: [how to reach Hermes as admin](#access-and-ground-rules)):

```sh
systemctl --user is-active hermes-gateway hermes-dashboard devtunnel-hermes   # active active active
systemctl is-active hermes-shell-mcp                               # active
systemctl is-active durable-park-receiver durable-park-bridge.socket durable-park-bridge   # active active active
sudo docker ps --filter name=wh-tunnel --format '{{.Status}}'      # Up ...
curl -s http://127.0.0.1:8644/health                               # {"status": "ok", ...}
cat ~/.hermes/plugin-data/durable-park/formcheck_status.json       # "ok": true, recent checked_at
cat ~/.hermes/plugin-data/durable-park/plugin_load.json            # "pid" = systemctl --user show hermes-gateway -p MainPID --value
hermes cron list | grep -A3 durable-park-sweep                     # last run ok
~/.hermes/scripts/durable_park_sweep.sh                            # prints nothing when all is well
```

The bridge's isolation, as the admin `ubuntu` (it needs root; it only reads, and changes, restarts and signals nothing):

```sh
sudo python3 -I /opt/durable-park/deploy/bridge_check.py    # seven lines, each `ok: ...` or `FAIL: ...`; exit code 0 only if all seven pass
```

`configure_hermes.sh` runs it too and stops on a FAIL. A check that cannot finish also prints `FAIL`. What a `FAIL` means
and what to do (the bridge runs as `dpbridge` and systemd's socket holds its port: [The gateway bridge](#the-gateway-bridge)):

| `FAIL` about | Meaning | What to do |
|---|---|---|
| the socket (`durable-park-bridge.socket` is not active, or listens on something other than `127.0.0.1:8791`) | systemd no longer holds the port, so anything running as `hermes` could bind it and answer withHuman | Check no other process holds the port (`sudo ss -ltnp 'sport = :8791'`), then `sudo systemctl enable --now durable-park-bridge.socket`. If the start fails because the port is taken, treat it as the impostor listener below. |
| the service (not active, its main process runs as a user other than `dpbridge`, or it carries a supplementary group other than `dpbridge`'s own: the line names the groups, e.g. `hermes (gid 1001)` or `dpwake`) | The wrong unit file is installed (for example the old `User=hermes` one restored, or one with `SupplementaryGroups=`), or someone started the bridge another way | `systemctl cat durable-park-bridge` must show `User=dpbridge` and no `SupplementaryGroups=`. Re-install the units with `sudo bash deploy/configure_hermes.sh` (only when no row is `parked`, `approved` or `executing`, and with `hermes-gateway` stopped first: see [Before you run the configure script](#before-you-run-the-configure-script)) and read `sudo journalctl -u durable-park-bridge -n 20`. |
| the listener (a process other than systemd or the bridge holds `127.0.0.1:8791`, or nothing listens) | An impostor answers withHuman's webhook, or the port is down. The line names the pid and its user | Security incident: tell the admin. After the admin confirms, stop that process, then `sudo systemctl restart durable-park-bridge.socket durable-park-bridge.service`, and run the check again. Nothing listening: start the socket as in the first row. |
| another process runs `bridge.app` | A second copy of the bridge (for example one started by hand as `hermes`) exists next to the real one | Same as the impostor listener: the admin confirms, then stop that process. |
| a user-level unit named `durable-park-bridge*` exists | A leftover or planted `systemd --user` unit (for example under `~hermes/.config/systemd/user/`) could be started by the bot to run its own bridge | The admin confirms, then disable it as that user (`sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user disable --now <unit>`) and delete the file. |
| the `dpbridge` account (missing, a login shell, or a member of `dpwake` or `hermes`) | The bridge's user is not isolated from the bot | Re-run `sudo bash deploy/configure_hermes.sh` for a missing user (check the ledger and stop `hermes-gateway` first: see [Before you run the configure script](#before-you-run-the-configure-script)). For a group, `sudo gpasswd -d dpbridge <group>`; for the shell, `sudo usermod -s /usr/sbin/nologin dpbridge`. Then restart the bridge. |
| `/etc/durable-park/bridge_secret` (not `root:root` mode `0600`) | The bot's side can read, or can no longer be kept from reading, the webhook secret | `sudo chown root:root /etc/durable-park/bridge_secret && sudo chmod 0600 /etc/durable-park/bridge_secret`, then `sudo systemctl restart durable-park-bridge`. `configure_hermes.sh` also sets it. Missing: [store it](#store-or-rotate-the-bridges-keys). |

Waiting or stuck rows:

```sh
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro", uri=True)
for r in c.execute("select park_id, tool, status, created_at from parks "
                   "where status in ('parked','approved','executing') order by created_at"):
    print(r)
PY
```

## Deploy a change

1. **Change, test, document, push** (in the clone, as `hermes`):

   The gateway and the sweep load code straight from the live clone, so never edit there. Work in a worktree,
   then fast-forward the live clone:

   ```sh
   cd /home/hermes/tapdash-agents && git pull --ff-only
   git worktree add /home/hermes/tapdash-agents-wt/myfix -b myfix && cd /home/hermes/tapdash-agents-wt/myfix
   # edit code AND the docs it affects (see CONTRIBUTING.md); tests use the live clone's venv:
   (cd durable-park && /home/hermes/tapdash-agents/durable-park/.venv/bin/pytest -q -W error)
   git add -A && git commit
   cd /home/hermes/tapdash-agents && git merge --ff-only myfix && git push   # the hook checks the docs
   git worktree remove /home/hermes/tapdash-agents-wt/myfix && git branch -d myfix
   ```

   Worktrees always live under `/home/hermes/tapdash-agents-wt/<name>` (one path everywhere in these docs). The
   `git merge --ff-only` in the live clone is the **deliberate deploy step**: the gateway and the sweep load code from
   the live clone, so this is the moment the code on disk changes for them (the sweep uses it on its next run, the
   gateway on its next restart). Do it only for reviewed code, only as a fast-forward, and never edit files in the
   live clone by hand.

   `git push` uses the VM's deploy key for this repo (`~/.ssh/tapdash_agents_deploy`, set in the clone's
   `core.sshCommand`). One-time per clone: `git config core.hooksPath .githooks`. Bigger changes go through a
   pull request and an independent review instead (`CONTRIBUTING.md`; [how to open one from the VM](#open-a-pull-request-from-the-vm)).

2. **Make it live**, only when nothing is waiting or running (check [waiting rows](#health-check) first).

   | You changed | Make it live with |
   |---|---|
   | `plugin/` (park hook, form check, wake, executor, ledger) | `systemctl --user restart hermes-gateway` (as hermes). The ledger gains two nullable columns, `user_id` and `profile`, on the first open by the gateway, the sweep or the bridge (`ALTER TABLE ... ADD COLUMN`, idempotent, existing rows get NULL and keep the approval body they were parked with). So this change needs no ledger drain beyond the rule above: a row in flight across the deploy still executes. Do not roll the code back past this change while rows hold values: older code ignores the columns, which is harmless. |
   | `plugin/durable_park/` (any file, incl. `_vendor/`) or `bridge/` | The bridge's `/opt/durable-park` copy is the **whole package**, `plugin/durable_park/` including `_vendor/` (the bridge imports `durable_park.ledger` for `args_digest` and `durable_park.gatecall` for `gateway_slug`, which run `__init__` -> `formcheck` -> `_vendor`), not single files; a missing `_vendor` crash-loops it (`ModuleNotFoundError`). Root, admin: `sudo bash deploy/configure_hermes.sh` (after the ledger check, **stop `hermes-gateway` first**: see [Before you run the configure script](#before-you-run-the-configure-script) and the [warning](#never-change-plugins-while-the-gateway-runs); the script's last step then restarts the whole `hermes` user manager, the dashboard included). It first refuses to start, changing nothing, if `bridge_secret` or `audit_key` is missing or empty ([store them first](#store-or-rotate-the-bridges-keys)). Step 4b then creates the user `dpbridge` if needed, copies both trees recursively, removes files the repo no longer has, installs the socket unit and the service unit, restarts the bridge when something changed, and **stops with the last 20 journal lines unless the bridge answers `401` to an unsigned `POST` within 10 s**, so it never goes on to restart the gateway on a dead bridge; it then runs `bridge_check.py`, and a `FAIL` stops it too. By hand, copy the tree the same way, then check `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8791/wh/bridge` prints `401` and run [`bridge_check.py`](#health-check). The script does not restart the socket: a change to `durable-park-bridge.socket` needs `sudo systemctl restart durable-park-bridge.socket durable-park-bridge.service` (the port is free for a moment, so no row `parked`, `approved` or `executing`). The very first move from the old bridge that ran as `hermes`: [first deploy](#store-or-rotate-the-bridges-keys). Rollback of a bridge change: [below](#roll-back-a-bridge-change). |
   | any file in `receiver/` | root, admin: copy each changed file to `/opt/durable-park/receiver/` the same way, then `sudo systemctl restart durable-park-receiver` |
   | `scripts/durable_park_sweep.py` | nothing - the next cron run uses it (set `TEAMS_ALERT_WEBHOOK_URL` to also copy its alert lines to Teams as an Adaptive Card: [live settings](#live-hermes-settings-on-proto-agents); a Teams outage can delay the sweep's exit by about 10 s, the post timeout) |
   | `tracing/tapdash_langfuse/` (the patched Langfuse plugin; once it is deployed, the gateway loads it through a symlink into the live clone) | Run `bash tracing/run_tests.sh` as `hermes` first. The change is loaded at the next gateway restart **or the next plugin reload**, so follow [When the plugin changes](#when-the-plugin-changes) (hook re-review before the pull; pull with the gateway stopped) |
   | `deploy/` (units, policy, Hermes config, and the withHuman entry pipeline `deploy/withhuman/entry-pipeline.json`) | For the pipeline file: in withHuman, save it as a new entry revision and ACTIVATE it before the gateway restart that brings this code live (a save activates at once unless `activate` is false). For the rest, on proto-agents `configure_hermes.sh` re-applies the current tool policy (the v1 lockdown, re-applied 2026-10-21 03:16 UTC) and restarts the gateway: run it only when no ledger row is `parked`, `approved` or `executing`, and with the gateway stopped first ([Before you run the configure script](#before-you-run-the-configure-script), [warning](#never-change-plugins-while-the-gateway-runs), [backlog](backlog.md#before-real-books) item 12). Changing which tools are open goes through the [spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) section 4.4, not a hand edit of `~/.hermes/config.yaml`. |
   | `deploy/tunnel/` | rebuild and restart the tunnel: see [gateway-setup.md](gateway-setup.md#build-check-run-task-2-step-3-after-the-human-step-below) |

   Rollback: revert the code first (git checkout the previous `main`, restart the gateway with 0 active rows); never
   re-activate rev 8 while this code runs - rev 8 auto-approves every `hermes`/`mcp:` request.

3. **Verify**: run the [health check](#health-check). After a gateway restart the form self-check writes a fresh
   `formcheck_status.json` within about a minute.

### Before you run the configure script

Do this every time you run `sudo bash deploy/configure_hermes.sh` (or tell someone to). The script runs `hermes plugins enable`, and a plugin change while the gateway runs reloads every plugin, durable-park included ([why](#never-change-plugins-while-the-gateway-runs)).

1. Check the ledger: no row may be `parked`, `approved` or `executing` ([health check](#health-check)).
2. Stop `hermes-gateway` (as `hermes`: `systemctl --user stop hermes-gateway`; from `ubuntu`: `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user stop hermes-gateway`).
3. Run `sudo bash deploy/configure_hermes.sh`. The script starts everything again when it finishes: with the gateway stopped it restarts `user@1001.service`, so **all** of `hermes`'s user units start again, `hermes-dashboard` included.
4. Run the normal checks: the [health check](#health-check), and the check the procedure you are following names.

### Open a pull request from the VM

`hermes` has no GitHub login; `git push` works through the repo's deploy key. Pull requests are opened with the
`gh` of the admin user `ubuntu`, which is signed in. From the connector (which runs as `ubuntu`) that is just `gh`:

```sh
sudo -u hermes -H git -C /home/hermes/tapdash-agents-wt/<name> push -u origin my-branch   # the deploy key is hermes's
gh pr create --repo TapDashPayments/TapDash-Agents --head my-branch --base main \
  --title '...' --body-file /tmp/pr-body.md     # as ubuntu; delete the file afterwards
```

Fill in `.github/pull_request_template.md` for the body. Never print `gh auth token`. The login is a classic OAuth
token with the `repo`, `workflow`, `read:org` and `gist` scopes, which reaches every repository of that GitHub account
([backlog](backlog.md#later): replace it with a fine-grained token limited to this repository).

## Alerts and what to do

Alerts come from the sweep. They arrive in the Telegram DM with the bot (the cron job's delivery target) and, because `TEAMS_ALERT_WEBHOOK_URL` is set, also in Teams as an Adaptive Card.

### How you hear about an approval

A call that needs a person is announced by **withHuman itself**, the moment it needs you, on the channels set in your
withHuman account (notification settings): the withHuman phone app, text message, WhatsApp, Slack, Discord and email.
Every one links to the request's page, where you approve or deny.

- **Teams:** withHuman has no Teams channel, so the approver's withHuman email alerts go to a Teams **channel's email
  address** (channel menu, **Get email address**; the channel's advanced settings must accept mail from withHuman).
  The alert then appears in that channel at once. Set up on 2026-10-26 for the admin.
- **Telegram:** withHuman has no Telegram channel. Telegram gets a link only in the backup-approver alert (next
  bullets), through the sweep's delivery to the admin's DM.
- **The chat where you asked never gets the link.** The bot only says the call is waiting for approval. Whoever asked
  may also be an approver, or the chat may be shared, and the asker must not be handed a way to approve their own
  request. Links go only to the approver, through withHuman's alerts.
- **After an approved action runs:** durable-park posts one line into the chat where it was asked, for example
  `[durable-park] P-XXXX ran: <tool>. Result: Payment created successfully (id 159, amount 5)` (or "did NOT complete",
  or "outcome UNKNOWN"),
  straight away and with no AI in between, so a restart or crash cannot lose it. If that post did not work, the
  5-minute sweep re-sends it 2 minutes later at the earliest (GitHub #66). Restarts also wait up to 45 seconds for a
  reply in progress (`agent.restart_drain_timeout`).
  Known limits (GitHub #66): (1) if the server dies in the instant after the line was posted but before the ledger
  recorded it, the sweep posts it once more (at most one duplicate; Hermes posts have no delivery receipt or
  de-duplication key). (2) The sweep's re-send goes through the agent: it counts as told once the gateway accepts
  it, so a second crash before the agent relays it can still lose that one re-send. The run itself is always in
  the ledger (`result_json`, `note`).
- **Unanswered after the primary window:** the backup-approver alert ([below](#backup-approver-and-unanswered-approvals-tap2-121)) ends with the link too.
  Like every sweep alert, it goes to the admin's Telegram DM and to the Teams alert chat (`TEAMS_ALERT_WEBHOOK_URL`),
  and to the backup approver's own Teams webhook when one is set. **Keep those alert chats to approvers and admins**:
  anyone in them gets the link.
- **Hermes Deck cannot approve yet:** withHuman refuses decisions made with a personal API key ("Session ID is
  required."), a withHuman bug: [withHumanAI/withHuman#112](https://github.com/withHumanAI/withHuman/issues/112), fix
  proposed in [#113](https://github.com/withHumanAI/withHuman/pull/113). Approve in the withHuman app until it ships.
  Before it ships, the Deck relay must move to its own Linux user: today it runs as `hermes`, the agent's user, which
  can read Deck's withHuman key ([backlog](backlog.md)).

### Could not tell the chat the outcome

`durable-park ALERT: could not tell the chat the outcome of P-XXXX (<tool>) for 24 hours; check it.`

A run ended (executed, failed or unknown), but neither the executor's own message nor the sweep's re-sends reached
the chat for 24 hours (the chat may be gone, or it was a CLI run). The run itself is recorded in the ledger
(`result_json`, `note`). Tell the person yourself. The sweep says this once per park and then stops trying.

### Outcome UNKNOWN

`durable-park: P-XXXXXXXX outcome UNKNOWN - check <tool> in the sandbox before retrying`

The sweep alert above is the same for every `unknown`. The executor's own reply to the bot (what the bot reads
in its chat, not the Telegram alert) differs when the call timed out, so the bot can say so in plain words:

`durable-park: P-XXXXXXXX outcome UNKNOWN - <tool> did not finish in time. It may or may not have gone through. Check <tool> in the sandbox before trying again.`

It is the same state (`unknown`) and the same steps below: a timeout does not tell you whether the write landed.

The approval was used but no result was recorded (gateway crash, session expired, interrupted call). The call
**may or may not have happened**, and it will not be retried.

1. Look in QuickBooks for the action (payment, bill, invoice...). The ledger row has the exact arguments:
   `select args_json from parks where park_id='P-XXXXXXXX'`.
2. If it happened: nothing to do. If not: ask the bot again; that is a new park and a new approval.

### Parked row never got an approval request

`durable-park: P-XXXXXXXX (<tool>) never got an approval request; cancelled after 10 minutes`

No request id was ever attached to the row: usually the gateway stopped between saving the park and asking
withHuman, but the request MAY exist at withHuman (the gateway stopped between withHuman answering and the row
recording the request, or withHuman errored after creating it and the cancel failed). The sweep cancelled the row
(note `no approval request after 10 minutes (cancelled by the sweep)`); the action did not run. A request may stay
open in withHuman; approving it does nothing (fails closed); cancel or let it expire. Ask the bot again (a new park and a new approval). If this repeats, check why the gateway restarts (`journalctl --user -u hermes-gateway`).

### Ledger retention

The sweep deletes `allowed`, `refused` and `cancelled` rows 30 days after their last update (the wrapper passes
`--retention-days 30`; the value must be at least 1). It prints nothing; a failure goes to stderr as
`retention failed (<ErrorType>)` and never stops the rest of the sweep. Rows holding an outcome or a human decision
(`executed`, `failed`, `unknown`, `denied`, `expired`) are never deleted. SQLite reuses freed space but the file
does not shrink by itself; to shrink it, stop the gateway, check that no row is `parked`, `approved` or `executing`,
then run `sqlite3 <ledger> 'VACUUM;'` as `hermes`.

### Form checker not working

`durable-park ALERT: the form checker is not working - <reason>. Risky actions will be refused until it is fixed.`

Form-checked calls (QuickBooks money moves, DocuSign writes, Maverick, accept.blue and Authorize.net tools) are blocked (fail closed) until it is fixed; other
tool calls are still asked about as usual. Read `formcheck_status.json` and the reason:

| Reason | Likely cause | Fix |
|---|---|---|
| no form-check report / last ran N minutes ago | Gateway down, or durable-park did not load | Health check; `systemctl --user restart hermes-gateway`; look for `durable-park loaded` in `~/.hermes/logs/agent.log` |
| no form-checked tools are registered | Gateway lost the `wh` MCP server (withHuman down, token problem) | `hermes mcp test wh`; check the withHuman gateway and `instance_token` |
| could not read the tool registry / Hermes type fixing is unavailable | A Hermes upgrade moved an internal | Read `plugin/durable_park/compat.py` and `formcheck.py`; adjust and redeploy |
| `<tool>: rules could not be used` | A tool's schema changed in a way the checker cannot compile | Look at that tool's schema (`withHuman` tool catalog); see [form-check.md](form-check.md) |

A second line `durable-park: the form checker is working again.` follows when it recovers.

### Tool policy check failed

`durable-park ALERT: the tool policy check failed at gateway start — <problems>. Every tool call is refused until it passes.`

Applies while the v1 tool lockdown is applied (it is on proto-agents). Before every `hermes-gateway` start, the user-unit drop-in
`~hermes/.config/systemd/user/hermes-gateway.service.d/50-durable-park-policy.conf` runs
`deploy/tool_policy_check.sh` (`ExecStartPre=-`, so a failure never stops the gateway). It runs `hermes_tool_policy.py
check --status-file` and writes `plugin-data/durable-park/tool_policy_status.json` next to the ledger
(`ok`, `checked_at`, `problems`, `allowed`). The plugin reads it at load: a missing report, `ok: false`, or a report
older than 15 minutes makes it refuse every tool call except the exempt tools (`parked_action_execute`, `clarify`, `tool_search`, `tool_describe`) (the log says
`durable-park tool policy check failed (<reason>)` at ERROR, which lands in `~/.hermes/logs/errors.log`); wakes are dropped and `parked_action_execute` answers
"refused" without touching the row, so an approved park stays approved. Telegram, Teams and the sweep keep running.
The sweep alerts once when the report is missing or failed, and once (`durable-park: the tool policy check passes
again.`) when it passes.

The plugin also writes its own verdict at load, `tool_policy_load.json` next to the ledger (`ok`, `problem`,
`checked_at`), because the sweep cannot judge a report's age. Only the gateway process (`hermes gateway run`) writes
it: the cron sweep and CLI runs fail closed on their own but never overwrite the gateway's verdict. The sweep sends
this alert only when the start-time report passed, so a failed report gives one alert (the one above), not two. It
alerts once when `ok` is false (the report went stale, or the plugin judged it differently):

`durable-park ALERT: the tool policy check did not pass when the plugin loaded — <problem>. Every tool call is refused until an admin fixes it and restarts the gateway.`

and once (`durable-park: the tool policy check passed when the plugin loaded.`) when a later load passes. Fix the
`<problem>` with the table below, then restart the gateway.

| Problem | Likely cause | Fix |
|---|---|---|
| `no tool policy report` | The drop-in is not installed (re-run `deploy/configure_hermes.sh`, but check the ledger and stop `hermes-gateway` first: see [Before you run the configure script](#before-you-run-the-configure-script)), or the status directory is not writable by `hermes` | `ls ~/.config/systemd/user/hermes-gateway.service.d/`; `systemctl --user daemon-reload`; check the directory's owner |
| `hermes python not found` | Not exactly one `~/.hermes/installs/*/facts.json`, or its Python is missing | Check that directory; the same lookup is `hermes_python()` in `deploy/configure_hermes.sh` |
| `the tool policy check did not write a report` | The check was killed (the 60 s timeout), crashed, or could not write its report; the wrapper then wrote this failing report itself | `journalctl --user -u hermes-gateway` for `tool_policy_check:` lines; run `deploy/hermes_tool_policy.py check --status-file /tmp/x.json` by hand as `hermes` in Hermes's Python and read the error |
| `invalid allow file: <reason>` | `/etc/durable-park/allowed_toolsets` has a Phase 2 or unknown name, or `hermes` cannot read it | Fix the file as in [Open a toolset](#open-a-toolset) (step 2), then restart the gateway |
| a toolset or tool problem | `hermes_tool_policy.py check` found the config drifted (a Hermes upgrade, a manual change) | Run `apply` then `check` as in [Open a toolset](#open-a-toolset) |
| `LEAK tool_call` (or `tool_search`, `tool_describe`) listed without a platform's own tool list changing | A tool registered under a bridge name (Hermes synthesizes the bridge, it never registers it), or `check` is an older version that does not know Tool Search while `tools.tool_search.enabled` is `on` | Update the code and re-run; with an old `check`, set the key back to `off` ([Turn on Tool Search](#turn-on-tool-search)) |
| `BRIDGE incomplete: <names> missing` | Only some of the three bridge tools are visible | A Hermes change; read `tools/tool_search.py` `bridge_tool_schemas` and `compat.py` |
| `MISSING parked_action_execute` | The executor is not reachable: not in the tool list and, with Tool Search on, not behind a complete bridge | Check the plugin loaded (`durable-park loaded`), then the two rows above |
| `LEAK <other name>` while Tool Search is on | The tool is in the session's catalog even if the model's list hides it behind the bridge: `tool_call` could run it | Remove it as for any other leak (`apply`, then `check`) |
| `stale` | The report was written more than 15 minutes **before the gateway process started** (the gateway started without the drop-in, so no fresh report). The gateway's uptime no longer matters. **A plugin reload inside the running gateway is not fine:** on 2026-10-22 at 04:37:28 UTC the log showed durable-park judging the report `stale` right after a reload (`hermes plugins enable` of another plugin) and refusing every call until the next restart, so never reload plugins while the gateway runs ([warning](#never-change-plugins-while-the-gateway-runs)). A process started more than 15 minutes after the last gateway start (for example a manual `hermes` run) is refused, which is correct: the start check did not run for it (it fails closed but does not write the verdict) | Install the drop-in if missing; otherwise restart the gateway (the start writes a fresh report). Visible through the `tool_policy_load.json` alert above. **In a side process this is not an error:** the cron sweep (every 5 minutes) and CLI runs log `durable-park: tool policy report predates this side process (expected for the cron sweep and CLI runs); refusing every tool call in this process` once, at INFO (it does not reach `errors.log`; look for it in `agent.log`), and still refuse every tool call. Only the gateway's own `stale` is an ERROR |
| `unreadable checked_at` | The report's `checked_at` is missing or not a `YYYY-MM-DDTHH:MM:SSZ` time. Not a normal aging effect: the report was edited, truncated or written by something else. **ERROR in every process** (the sweep and CLI runs too) | Re-run the start check: `systemctl --user restart hermes-gateway` (only when nothing is waiting or running) writes a fresh report; if it comes back, find what wrote the file |
| `checked_at in the future` | The report is dated more than a minute ahead of this process's clock: clock skew (check `timedatectl`) or a hand-written report. **ERROR in every process**, never downgraded to the side-process INFO line | Fix the clock, then restart the gateway as above so the start check writes a fresh report |

Reading `errors.log`: `durable-park tool policy check failed (<reason>); refusing every tool call` at ERROR is always a
real problem, so fix it with the table above. The one verdict that is expected in a process that is not the gateway is
`stale`, meaning only that the report is too old for that process (the start check ran for the gateway, not for the
sweep or a CLI run), and that case logs the INFO line above
instead, so the sweep no longer fills `errors.log` every few minutes. Every other problem, in any process (including
`unreadable checked_at` and `checked_at in the future`), and `stale` in the gateway, still log at ERROR.

Before go-live, time the check once on the VM (`time bash deploy/tool_policy_check.sh` as `hermes`): it must
finish well under 60 s (the check's own timeout; systemd also has a start timeout, `TimeoutStartSec`, to stay inside).

Fix it, then `systemctl --user restart hermes-gateway` (only when nothing is waiting or running): the check runs
again at the start. (The same drop-in also stops the gateway when the plugin did not load: see
[Gateway will not start](#gateway-will-not-start-the-plugin-did-not-load).) If the report directory is not writable the script cannot replace an old report: it logs
`durable-park: tool policy report could not be refreshed (directory not writable?)` (journal) and the plugin's
15-minute staleness check refuses calls in any process started after the old report aged out. `systemctl --user show hermes-gateway -p ExecStartPre` shows the drop-in is active.

### No load proof for the running gateway

`durable-park ALERT: the running gateway (pid N) has no durable-park load proof; every tool call may be running unchecked. Restart the gateway with no active rows and check the journal.`

The sweep found no `plugin_load.json` for the running gateway's MainPID: the durable-park plugin did not load in
it, so Hermes runs with no `pre_tool_call` hook (only the toolset lockdown is left). Normally `ExecStartPost`
stops such a gateway at start (next procedure), so this means the unit runs without the drop-in, or the proof file
was removed. Follow [Gateway will not start: the plugin did not load](#gateway-will-not-start-the-plugin-did-not-load),
then restart the gateway (only when no row is `parked`, `approved` or `executing`). A second line
`durable-park: the durable-park plugin is loaded in the running gateway.` follows when it recovers.

Known limit: the sweep is a Hermes cron job that runs inside the gateway, so a stopped gateway cannot alert. The
bot going silent (Telegram, Teams) is the signal; look at `systemctl --user status hermes-gateway`.

### Gateway will not start: the plugin did not load

Deploy order for this check: update the clone, install the drop-in and `systemctl --user daemon-reload`
(`configure_hermes.sh` does both; check the ledger and stop `hermes-gateway` before you run it: see [Before you run the configure script](#before-you-run-the-configure-script)), then restart the gateway promptly with no `parked`, `approved` or `executing`
row. Between the clone update and the restart the running gateway has no `plugin_load.json` yet, so expect one
transient sweep ALERT ("no durable-park load proof"); it recovers after the restart. Each start deletes the
previous proof first (`ExecStartPre`), so a proof from an earlier process can never match a reused pid.

Symptom: `systemctl --user status hermes-gateway` shows `failed` or `start-limit-hit`, and the journal has
`durable-park ALERT: the plugin did not load in this gateway (pid N); stopping the gateway so no tool call runs unchecked`.
The drop-in's `ExecStartPost=` (`deploy/plugin_load_check.sh`, no `-`) fails the unit when no `plugin_load.json` for
the gateway's pid appears within `DURABLE_PARK_LOAD_TIMEOUT` seconds of real time (default 30, always waited in full: the
deadline is a wall-clock one; Hermes's own load deadline is 10 s and
the live gateway loads the plugin in about 2 s, well inside systemd's default 90 s start timeout).

1. Find the reason in the journal: `journalctl --user -u hermes-gateway -n 200 --no-pager | grep -i -E "durable|plugin"`.
   Look for `Failed to load plugin 'durable-park': ...` (an import error, a missing dependency or `_vendor`, or a
   load timeout: `plugins.load_timeout_seconds`, default 10 s), or no durable-park line at all (the plugin is
   disabled or missing: `hermes config get plugins.enabled` must list `durable-park`; re-run
   `deploy/configure_hermes.sh` after you check the ledger and stop `hermes-gateway`: see [Before you run the configure script](#before-you-run-the-configure-script)).
2. Fix it (restore the file, fix the syntax, re-run `configure_hermes.sh` (check the ledger and stop `hermes-gateway` first: see [Before you run the configure script](#before-you-run-the-configure-script)), raise `plugins.load_timeout_seconds`).
3. Confirm the plugin loads: after the start, `cat ~/.hermes/plugin-data/durable-park/plugin_load.json` shows
   `"pid"` equal to `systemctl --user show hermes-gateway -p MainPID --value`, and the journal says
   `durable-park plugin loaded (mode=asking, pid=N)`. A mode of `refusing-policy` or `refusing-error` means the
   plugin is live but refusing every call: see [Tool policy check failed](#tool-policy-check-failed) or the log line
   `durable-park failed to load (...)`.
4. Start it: `systemctl --user reset-failed hermes-gateway && systemctl --user start hermes-gateway`.

Start limit: the drop-in sets `StartLimitIntervalSec=600` and `StartLimitBurst=3` (the unit alone has no limit and
`Restart=always`, so a gateway whose plugin cannot load would loop for ever). That applies to every cause: **3
ordinary crashes in 10 minutes also stop the gateway**, and so do manual restarts and Hermes graceful restarts (exit
75): three starts of any kind in 10 minutes end in `start-limit-hit`. It stays down until `reset-failed` and `start`
as above.
The bot going silent is then the signal (the sweep runs inside the gateway and cannot alert).

### withHuman could not be reached

`durable-park ALERT: withHuman could not be reached (since <time>); every tool call is being refused until it answers again.`

The park hook's circuit breaker opened after 3 consecutive errors from withHuman, so tool calls are refused
(fail closed) without asking. Every 30 seconds it lets calls through again until one gets an outcome. Read `breaker_status.json` next to the
ledger (`open`, `opened_at`, `closed_at`); check the withHuman status page, the `instance_token`, and the VM's
network (`hermes mcp test wh`). Nothing needs restarting: it closes by itself on the first answer. A gateway
restart while it is open keeps it open but lets the first call through at once, so the close is still recorded.

`durable-park: withHuman answers again (since <time>); tool calls are decided normally.` follows when it closes. If
the outage was shorter than 5 minutes, both lines may arrive together.

### The agent's note could not be moved

The model puts a short reason in `withhuman_context`. The withHuman gateway expects it at the top level and shows it
to the approver; the park hook moves it there when the model nests it inside `params`. When it cannot (or a note is
dropped, or the hook could not even look), the call still goes on exactly as before (the original arguments, approval
is unaffected) and the sweep sends one chat alert line per tool and kind (state per key and detail in `lift_alert_state.json`; several new places of one problem are one line that names the first place and ends `(+N more places, e.g. ...)`).
The report is `lift_status.json` next to the ledger (names, times and counts, never a value; the hook writes it under
a lock, so the gateway and any other Hermes process, even one running as another user, share it; a tool call records all its
problems at once and waits at most 0.2 s in total; what could not be written is retried on the next problem, at process exit
and, in the gateway, every 30 minutes, and every process holding unsaved problems leaves its own marker
`lift_status.unwritable.<pid>` for the sweep to alert on). The same problem is said again once per 24 hours while it keeps happening
(`durable-park ALERT (still happening, <n> times): ...`), except `has its own withhuman_context field`, `was longer than
2000 characters` and `was empty or not text`, which are said once per tool. The text tells you the kind:

| Alert | Meaning | What to do |
|---|---|---|
| `<tool> has its own withhuman_context field inside <key>` | The tool's own rules declare a `withhuman_context` inside that key, so the note is left there: it goes to the server and never reaches the approver | Fine if the server really has such a field (the approver then does not see the agent's reason for this tool). If not, look at the tool's rules in the withHuman tool catalog |
| `the agent's note inside <key> for <tool> was removed but not shown to the approver (<reason>)` | A nested note was removed but could not be shown: `not text` (the model sent an object or number), `empty`, `another note was already at the top` (the top-level note, even an empty one, wins), `more than one note` | Usually a model slip; the approver saw the call without that reason. Repeats mean the model's instructions or the tool description need work |
| `could not read <tool>'s tool rules` | A gateway tool's rules were not found (the registry lookup failed, or the tool is not registered): its arguments were still searched for a note | Same as a form-check problem: [Form checker not working](#form-checker-not-working); `hermes mcp test wh`; read `compat.py` after a Hermes upgrade |
| `could not read <tool>'s arguments` | The arguments came as JSON text and Hermes's type fixing could not turn them into an object under the same key | Check Hermes's `tools/arg_coercion.py` after an upgrade (`formcheck.hermes_coerce`) |
| `moving the agent's note failed for <tool>` | An unexpected error in the lift step, or the lifted call did not pass the merge check | `journalctl --user -u hermes-gateway` for `could not lift withhuman_context for tool=<tool> (<ErrorType>)`; fix and redeploy |
| `the agent's note is still inside <key> for <tool> after the move` | A note is still inside the final arguments at that path (deeper than one level, inside a list, in JSON text, or another spelling of the name). A gateway tool whose rules do not list the top-level field is searched too | The note reaches the server (QuickBooks says "IGNORED"). Look at the tool's rules and at how the model nests it |
| `could not finish checking <tool>'s arguments for the agent's note` | The arguments are too large or too deep for the bounded search | Look at what the model sent; the note may reach the server |
| `the agent's note for <tool> was longer than 2000 characters and was shortened for the approver` | The note is longer than 2000 UTF-16 code units (an emoji counts as two). The approver sees the first 2000 units; nothing else changes and the call went on | Usually the model wrote too much; tell it to keep the reason to a sentence or two. Said once per tool |
| `the agent's note for <tool> was empty or not text, so the approver saw no reason` | The top-level `withhuman_context` exists but is empty, blank or not text (a number, object, list or null), so the approver got no reason. A nested note over such a top-level one is also reported as removed, with `another note was already at the top` | Usually a model slip; fix the model's instructions or the tool description. Said once per tool |
| `could not read lift_status.json (<reason>)` | The report is unreadable or not the expected shape | Look at the file next to the ledger; the hook keeps an unparseable one as `lift_status.json.corrupt` before writing a new one |
| `lift problems could not be saved (<ErrorType>)` | The hook cannot write `lift_status.json` (a marker `lift_status.unwritable.<pid>` is next to it, one per process that cannot write, a lock timeout included), so that process's note problems are not being reported | Fix the permissions or space of the ledger directory, `lift_status.json` and `lift_status.json.lock`; each process removes its own marker after its next successful write, and `durable-park: lift problems can be saved again.` follows once the last marker is gone. The marker of a process that has died stays: delete it by hand once writing works. `NotARegularFile` means a FIFO, device or directory sits at `lift_status.json.lock`; `not a regular file` means one sits at a marker's path: neither is ever used or followed, remove it |
| `the rules of <tool> are too complex to read for the agent's note (inside <key>)` | Judging the tool's rules took more than 2 000 schema nodes (many `anyOf`/`allOf`/`$ref` branches that fan out), so they count as "cannot tell": the note was moved as if the rules declared no such field | Look at the tool's rules in the withHuman tool catalog; if the tool really declares its own `withhuman_context` there, the note went to the approver instead of the server |
| `<file> was unreadable and was replaced` | The report was corrupt; the hook replaced it and kept the old one as `lift_status.json.corrupt` | Look at the `.corrupt` file for earlier problems |
| `could not check for lift problems (<ErrorType>)` | The sweep's own check failed (for example its state file cannot be written) | Read the sweep's stderr (`~/.hermes/logs`), fix, and a line `durable-park: lift problems can be checked again.` follows |

A problem that goes away and comes back alerts again. When adding tools from a new provider, expect these alerts
if its rules differ from the QuickBooks ones.

### No pay link (Authorize.net forms)

When an approved Authorize.net form (`authnet_create_payment_form`, `authnet_create_profile_form`) runs, the chat
gets a pay link: `[durable-park] P-XXXX made an Authorize.net form (...). Pay link: https://proto-agents.taila95862.ts.net/pay/<id> (works until 17:20 UTC)`.
Send that link to the customer. It opens a page with one "Continue to payment" button, and the customer types the
card on Authorize.net's own page. It works for 14 minutes; after that it says "This payment link has expired" and you
ask the agent for a new form ([architecture](architecture.md#1-system-map), row "pay links").

If the line says `No pay link: <reason>. Tell an admin.` instead, the form was made but there is nothing to send:

| Reason | What to do |
|---|---|
| `pay links are not set up (paylink_dir, paylink_origin)` | Re-run `configure_hermes.sh` (it sets both settings) when no ledger row is parked, approved or executing. |
| `the pay-link folder does not exist (paylink_dir)` | Same: the script creates `/var/lib/durable-park-paylinks` (`2750 hermes:dppay`). |
| `the form key in the answer was cut by the connector's safety filter` | The Authorize.net server is older than `4dd4278` ([gateway setup](gateway-setup.md#payment-server-sources-acceptblue-and-authorizenet)). Check the image tag ends in `-an-4dd4278` or later. |
| `the form page posts somewhere other than ...`, `unexpected field`, `unexpected shape` | The connector's page changed. Nothing is served. Compare `paylink.py` with the connector's `launcher_html` before allowing anything new. |

Check the service: `systemctl is-active durable-park-paylink` should say `active`, and
`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8793/pay/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` should print `410`
(an unknown link). The gateway log has `durable-park: no pay link for P-XXXX: <reason>` for every miss.

## Troubleshooting

### Backup approver and unanswered approvals (TAP2-121)

The sweep reads `/etc/durable-park/approvers.json` (root:root 0644, no secrets; the shape is in
[`deploy/approvers.example.json`](../deploy/approvers.example.json)). With no file, approvals wait for their approver as
before. With the file, a `parked` row whose approval request nobody answered:

- after `primary_minutes`, when `backup_approver` is set: one alert line names the backup approver and their withHuman
  escalation path (also posted to their own Teams webhook when `teams_webhook_env` names a set variable). The backup
  can only decide it if they are on that escalation path in withHuman (Approval pipelines -> Escalation paths): add
  them there first.
- after the backup's `minutes` as well, or right after `primary_minutes` when `backup_approver` is `null`: the row is
  DENIED with the note `no approver answered within N minutes ...; denied by the sweep`, and the chat is told by the
  usual wake. The request stays open in withHuman; approving it later does nothing (the row is no longer `parked`).
- an invalid file: one ALERT line, nothing changes until it reads again.

### An approver was asked twice for one action

The park was approved, but the bridge did not confirm the gateway call, so withHuman asked a person again. That
second ask is safe (a person decides it) but noisy. The bridge keeps no record of its answers, so the evidence is
the bridge log and the two requests in withHuman (the `park:quickbooks` one you approved and the plain
`quickbooks` one that arrived later). The ledger row shows what was parked:

```sh
python3 - <<'PY'
import sqlite3
L = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro", uri=True)
print(L.execute("select park_id, tool, status, substr(args_digest,1,16) from parks order by created_at desc limit 3").fetchall())
PY
```

The bridge's own log says why it answered `human`: `sudo journalctl -u durable-park-bridge -n 20` (as the admin `ubuntu`), tag `why=`.
Argument values and the key are never logged.

| `why=` | Meaning | What to do |
|---|---|---|
| `no-approved-park` | No park request matched: wrong tool name or arguments, not approved, not consumed yet, or consumed more than 120 s before the gateway call | See the two causes below; if the call was simply slow, ask again |
| `ambiguous-park` | Two park requests matched the same call | Look in withHuman for a duplicate park of the same call; nothing runs twice, a person decides |
| `unsafe-name` | The gateway server or tool name is not plain: `__`, a leading or trailing `-`/`_`, a character outside letters, digits, `-`, `_`, or a Hermes name over 64 characters (Hermes would hash it) | Rename the tool at the source, or add the case to the bridge with a test; until then a person approves each call (a second ask) |
| `redacted-args` | The approver saw `[redacted]` in the park's arguments, or the call carries a secret-looking key (`password`, `token`, `secret`, `authorization`, `api_key`) or arguments too large to show whole | Expected for such calls: look at the real values and decide |
| `already-reported` | withHuman's record already holds an outcome for this approval. The plugin never reports outcomes (`report_outcomes=False`), so something else used the approval | Investigate: read the park request's history in withHuman to see what reported an outcome, and find the earlier call that used the approval |
| `gateway-request-not-listed` | withHuman's list of the agent's requests did not hold the gateway request the webhook was about (not yet visible, or id mismatch) | Ask again; if it repeats, check the key's `request.read.unrouted` permission and withHuman's status |
| `gateway-request-before-release` | The gateway request's `created_at` is missing or unreadable, or is earlier than the park's `consumed_at`, by any amount (no skew is allowed: both stamps are withHuman's own clock): it is not the call the approval released | Expected for an old call someone re-ran; a person decides |
| `gateway-request-mismatch` | The listed gateway request differs from the webhook's call (agent, server, tool or arguments) | Should not happen; compare the request in withHuman with the call and tell the developer |
| `neighbours-truncated` | The agent's listing (200 newest requests) has nothing created more than 5 s before the park was consumed, so a second use could be hidden beyond its end | Expected only under heavy traffic; a person decides. Nothing is lost |
| `neighbours-unsorted` | withHuman's listing of the agent's requests was not newest-first, so the bridge cannot tell whether a second use came in from 5 s before the park was consumed onwards. Fails closed | A person decides. This should not happen with withHuman's listing as documented: tell the developer, and check whether withHuman changed the order |
| `already-used` | Another gateway request for the same call (or for the same tool with `-` and `_` swapped, with the same arguments or arguments that cannot be compared) was created from 5 s before the approval was consumed onwards, or has no readable `created_at` (a retry, or a replay). The 5 s is margin, so a request made just before the release also counts | Expected for a replay; for a first run, find the earlier gateway request |
| `no-request-id` | The delivery carried no gateway request id (only real deliveries have one; a preview or test does not) | Nothing for a test; for a real call, tell the developer |
| `body-not-object`, `type`, `preview`, `malformed`, `arguments-not-object`, `agent`, `no-idempotency-key`, `idempotency-mismatch` | Checks on the webhook delivery itself, before any listing: the body is not a JSON object (`body-not-object`); it is not the gateway-call block (`type`); it is a preview or test (`preview`); it lacks the request or agent objects, or the server or tool names (`malformed`); the arguments are not an object (`arguments-not-object`); the agent is not `proto-agents` (`agent`); it has no `WithHuman-Idempotency-Key` header (`no-idempotency-key`); or the body's key differs from the header (`idempotency-mismatch`) | A test or preview: nothing to do. For a real call: check the webhook endpoint's block and agent in withHuman, then tell the developer. The call went to a person |
| `error:<ExcClass>` | A bug raised an exception in the bridge or in the verdict code. Only the class name is logged, e.g. `error:KeyError` | The call went to a person, so nothing ran unapproved. Tell the developer the class name and the journal time, and fix it with a test |
| `no-key` | The bridge's copy of `/etc/durable-park/audit_key` is missing or empty (the service copies it when it starts) | [Store the key](#store-or-rotate-the-bridges-keys), then `sudo systemctl restart durable-park-bridge` |
| `http-<status>` | withHuman answered the listing with a non-200 status. `http-403`: the key lacks `request.read` or `request.read.unrouted`. `http-401`: the key is revoked | 403: mint a key with the three permissions and store it (see [the key](#the-gateway-bridge)), then restart the bridge. 401: store a new key and restart the bridge. Other statuses: check withHuman's status. The call went to a person |
| `deadline`, `transport`, `shape`, `too-large` | withHuman was slow (the bridge gives up after 3.5 s), unreachable, or answered something unexpected (or over 4 MiB) | Check withHuman's status; the call went to a person, so nothing is lost |

(The tags come from `bridge/app.py`, `bridge/verify.py` and `bridge/whapi.py`; if `journalctl` shows another tag, trust the code.) An approval logs `why=park-request=<id>`, naming the approved park request.

A webhook delivery over 1 MiB gets HTTP 413 (no `why=` tag; the log says `bridge rejected: body too large (413)`), and withHuman then asks a person. Nothing runs unapproved.

- **Name differs** (e.g. `create-bill` vs `create_bill`): the bridge must build names the way Hermes does
  (`bridge/verify.py: hermes_tool_name`). Was the bridge's `/opt` copy updated?
- **Two tools that differ only by `-` versus `_`** (e.g. `create-bill` and `create_bill` on one gateway server): the
  bridge cannot tell them apart, because Hermes gives both the same name, so one park approves either and a use of
  either uses it. Check the server's tool catalogue (`list_gateway_server_tools`) whenever you add a gateway server
  or it gains tools. Today quickbooks has `create-bill` but no `create_bill`.
- **Digest differs**: the gateway rewrote the arguments (seen so far: added `withhuman_scope`, `2.0` sent as `2`).
  Extend `ledger.normalize_args` for the new rewrite, add a test, deploy to the plugin and the bridge copy.

### What the bot's refusal messages mean

The park hook answers a blocked call with one of these (the model sees them and is told what to do):

| Message starts with | Meaning | What to do |
|---|---|---|
| `PARKED P-...` | withHuman sent it to a person; nothing ran. | Decide it in the withHuman app |
| `durable-park: "<tool>" was refused by withHuman policy` | A pipeline rule refused it at once (row `refused`; the rule's note after the colon if any; the ledger note reads `withHuman denied: refused by withHuman policy[: <note>]`, never "a reviewer", because withHuman exposes no decider and an instant answer cannot be a person's). | Nothing, or change the pipeline rule |
| `durable-park: withHuman could not be reached` | withHuman did not answer (or the breaker is open); row `refused`, nothing ran. | See [withHuman could not be reached](#withhuman-could-not-be-reached); ask again later |
| `durable-park: P-... is already waiting for approval in this chat` | Another call of this chat waits for a person or is running; this call's request was cancelled. | Decide the waiting one ([below](#a-chat-says-already-waiting-but-nothing-is-pending-in-withhuman)) |
| `durable-park: P-... is already <status>; this call was not run again` | The model replayed a call id that was already decided (`allowed`, `refused`, `denied`, ...). | Nothing |
| `durable-park: another plugin hook could change tool arguments after approval (<names>)` | Something other than durable-park registered a `pre_tool_call` callback (a plugin's hook, a `config.yaml` `hooks:` shell hook or an outbound webhook) or `tool_request` / `tool_execution` middleware (or a registry could not be read: `<hook registry unavailable>`); every call except the exempt ones is refused. | An admin removes it, or lists it in `observer_hooks` after reading its code ([procedure](#allow-another-hook-observer_hooks)) |
| `durable-park: no chat session; the tool call was refused` | Hermes gave this turn no session id at all. A fail-closed fallback, not a rule about surfaces: Teams, Telegram, the CLI (`hermes chat -q`), the TUI and desktop all get a session id, so all of them go through the park hook and withHuman. | Check the Hermes version (`compat.py`); this message should not appear in normal use |
| `durable-park: no tool call id; the tool call was refused` | Hermes passed no session id or call id. | Check the Hermes version (`compat.py`) |
| `durable-park: "tool_call" was NOT run: it reached the approval check still wrapped` | Tool Search is on and the model sent a `tool_call` Hermes could not unwrap: a batch of two or more entries, unparsable `calls`, a core tool such as `clarify`, a `connectors__` name. Nothing ran, no row, nobody asked. | Nothing; the message tells the model to send exactly one entry for a tool `tool_search` lists |
| `durable-park: could not open an approval request` | An error in the hook (token file unreadable, arguments that are not strict JSON, ledger error; logged as `park hook failed for tool=...`), or durable-park could not load and refuses every call (a setting of the wrong type, logged as `durable-park setting <name> must be a list of strings`, or `durable-park failed to load`). | Read `~/.hermes/logs/agent.log` |

### A CLI turn needs approval (known gap)

**In plain words.** Every surface (Teams, Telegram, the CLI, the TUI, desktop) can use tools, and every call goes
through withHuman. If withHuman decides at once (an auto-approved read), nothing more is needed: the call runs and
the ledger row is `allowed` (for example `P-336DFFBB`, a one-shot `hermes chat -q` QuickBooks read). The gap is a call
that needs a **person**, from a turn that has already ended, like a one-shot `hermes chat -q`:

1. The park hook parks the call (row `parked`) and the CLI turn ends with `PARKED`. The `hermes` process exits.
2. A person approves in withHuman. The wake reaches the **gateway**, which records `approved` and asks Hermes to tell
   the CLI session (`wake.py` `_process`: `inject(..., session_key=row.session_key)`, then `mark_notified` if it said yes).
3. Telling the session is what makes a model call `parked_action_execute`. The executor only runs for the **same**
   session key (`executor.py:177-178`) and only from an `approved` row (`executor.py:179`). A finished one-shot has no
   turn left to do that.

What the code says (Hermes files are under `/home/hermes/.hermes/hermes-agent/`; nothing was run, no real park was made):

| Question | Answer from the code |
|---|---|
| Does `inject` return False? | **It depends on the key being known to the running gateway.** Messaging gateway path: `hermes_cli/plugins_injection.py:52-67` hands `{session_key, plugin_home}` to the gateway, and `gateway/run_plugin_injection.py:70` first calls `_plugin_session_key_admissible`. |
| Key unknown to the gateway (the CLI wrote its routing row to `state.db` after the gateway started; the gateway loads that table once, `gateway/session_persistence.py:352-359`) | `_plugin_session_key_admissible` returns True for an unknown key (`run_plugin_injection.py:145-146`, "an unknown key is decided at dispatch"), so the schedule call returns **True** (`:109-110`). Dispatch then finds no entry and returns False (`:195-196`), which is only logged: "Plugin message injection was not routed" (`:107`). `wake.py:240-241` took the True as sent and calls `mark_notified`. The row stays `approved`, announced, never run. The sweep skips it (`durable_park_sweep.py:377` lists only unannounced `approved` rows). |
| Key known to the gateway (for example after a gateway restart, because `state.db` holds the CLI session with platform `local`) | The entry's platform is `local`, so `_plugin_local_session` (`run_plugin_injection.py:172-186`) needs a **live** session under that key in the gateway's session authority. A finished one-shot has none, so it returns None, `_plugin_session_key_admissible` is False and `inject` returns **False** (`:70-71`). `wake.py:243` logs "left unnotified". The row stays `approved` and unannounced, and the 5-minute sweep re-posts the wake **forever** (`durable_park_sweep.py:377`, no limit or age check in the code I read). |
| Does the approved action ever run? | Not by anything in the code: only `parked_action_execute` moves `approved` to `executing`, and it needs a model turn in the same session key. A later `hermes chat --resume` of that very session might do it; the code does not say. |
| Does this block gateway restarts? | Yes, in both cases: `approved` is one of the three statuses the [health check](#health-check) says must be empty before a restart (`ledger.py:22`, `ACTIVE_STATUSES`). Nothing in the code expires an `approved` row: the sweep deletes only `allowed`, `refused` and `cancelled` rows (`ledger.py:273-282`), and the 168 h expiry applies to withHuman requests nobody answered, not to an approved row waiting to run. |

Not settled by the code: which of the two cases a given CLI row hits (the state of the gateway's in-memory session list
at that moment), and whether `PluginContext._gateway_injection_allowed` and the TUI injector (`plugins_injection.py:36-51`)
change the result in the gateway process. Nobody has watched this live, so treat the table as a reading of the code.

**Left for now (owner, 2026-10-25): the CLI is only used for tests.** Do not add a rule that refuses CLI calls: every surface goes
through withHuman (a closed PR tried that and was wrong). Until a fix is chosen, a one-shot CLI call that withHuman
sends to a person will leave an `approved` row that blocks restarts.

### A chat says "already waiting" but nothing is pending in withHuman

Another row of the chat is `approved` (the agent has not called `parked_action_execute` yet), `executing`, or
`parked` with a request id whose decision has not reached the chat yet (the sweep re-wakes it every 5 minutes).
Look at [waiting rows](#health-check). A `parked` row **without** a request id (the gateway died between saving and
asking) does not block the chat; the sweep cancels it once it is 10 minutes old and prints
`durable-park: P-XXXXXXXX (<tool>) never got an approval request; cancelled after 10 minutes`.

### The bot never got the decision

The sweep re-posts the wake every 5 minutes. If it still does not arrive: health check (gateway up? webhook route
`/health`?), then `journalctl -u durable-park-receiver -n 20` for wake errors.

### Every `hermes` command prints `Config ref '...' ... is not set`

Harmless, and gone on a VM configured by the current `deploy/configure_hermes.sh` / `deploy/hermes_mcp.sh`: they
write the secret references bare (`${DURABLE_PARK_WAKE_SECRET}`, `Bearer ${DURABLE_PARK_WH_TOKEN}`) because Hermes
warns only for the `env:`-prefixed form, and config loads before the `command` secret source has set the
variables ([why, and how a missing secret still shows](receiver-setup.md#files-and-owners)). A VM configured
earlier still has the `env:` form in `config.yaml`; with no row `parked`, `approved` or `executing` ([waiting
rows](#health-check)), as `hermes`:

```sh
cp -p ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-refs-$(date -u +%Y%m%dT%H%M%SZ)
hermes config set 'platforms.webhook.extra.routes.durable-park-wake.secret' '${DURABLE_PARK_WAKE_SECRET}'
hermes config set 'mcp_servers.wh.headers.Authorization' 'Bearer ${DURABLE_PARK_WH_TOKEN}'
grep -c 'env:DURABLE_PARK' ~/.hermes/config.yaml                  # 0
systemctl --user restart hermes-gateway
```

Then run the [health check](#health-check); a CLI command no longer prints the warning. proto-agents has been on the bare form since
2026-10-21 (PR #28, applied live): `grep -c 'env:DURABLE_PARK' ~/.hermes/config.yaml` prints `0`.

### The sweep runs every 6 minutes, not 5

`hermes cron list | grep -A3 durable-park-sweep` shows `Schedule: every 5m`. Hermes re-arms an interval job from
the previous run's time and its ticker runs every 60 s, so such a job runs about every 6 minutes. A cron
expression is anchored to the wall clock (`cron/jobs.py` `compute_next_run`), so `deploy/configure_hermes.sh`
creates the job as `*/5 * * * *` and, when it finds the job on another schedule, moves it over with
`hermes cron edit` (a job already on `*/5 * * * *` is left alone). To change a live job without re-running the
script, as `hermes` (it only changes when the next run is due and does not touch the ledger, so rows may be waiting):

```sh
hermes cron edit "$(hermes cron list --all | awk '/^ +[0-9a-f]+ +\[/ {id=$1} $1=="Name:" && $2=="durable-park-sweep" {print id}')" --schedule '*/5 * * * *'
hermes cron list --all | grep -A3 durable-park-sweep   # Schedule: */5 * * * *, Next run on a multiple of 5 minutes
```

On proto-agents the live job `5982945c6071` was edited to `*/5 * * * *` on 2026-10-21 (after PR #28 changed the deploy
script), and `hermes cron list` shows `Schedule:  */5 * * * *`.

## Plugin settings

Settings live under `plugins.entries.durable-park.settings` in `~/.hermes/config.yaml` (user `hermes`). Edit the
file by hand (keep comments), then restart the gateway (only when nothing is waiting or running, see the
[health check](#health-check)). What each one does and its default: [architecture](architecture.md#plugin-settings).

```yaml
plugins:
  entries:
    durable-park:
      settings:
        exempt_tools: [parked_action_execute, clarify, tool_search, tool_describe]   # keep parked_action_execute
        form_checked_tools: ['mcp__wh__quickbooks__(create|update|delete)[_-](payment|bill)']  # optional
        observer_hooks: []                                # module.qualname of reviewed hooks; live values: "Hooks reviewed so far"
        # tool_policy_status: /path/to/tool_policy_status.json   # default: next to the ledger; leave unset
```

- `exempt_tools`, `form_checked_tools` and `observer_hooks` must be lists of strings. A wrong type (a number,
  `true`, a mapping, an empty `observer_hooks:` that YAML reads as null) makes durable-park refuse every call
  except the exempt tools (`parked_action_execute`, `clarify`, `tool_search`, `tool_describe`); the log says `durable-park setting <name> must be a list of
  strings`. Fix it and restart.
- `exempt_tools`: anything listed here runs with no approval and no ledger row. Keep it to
  `parked_action_execute`, `clarify` and Tool Search's two read-only catalog tools (`tool_search`, `tool_describe`).
  Any other name logs an ERROR at every gateway start; an MCP tool (`mcp__...`), a form-checked tool or `tool_call`
  is refused (`durable-park setting exempt_tools must not list MCP, form-checked or tool_call tool '<name>'`) and
  every call is refused until it is removed.
- `risky_tools` is retired: it was renamed `form_checked_tools`. If it is still set the gateway log says
  `risky_tools is renamed form_checked_tools; ignoring it`; move the value to `form_checked_tools` and delete it.
- After the restart, `~/.hermes/logs/agent.log` shows `durable-park loaded: ... exempt tool(s), ... form-checked
  pattern(s), ... observer hook(s)`.

### Name the decider (optional)

By default the chat is told "P-XXXX was DENIED in withHuman: <note>". With an audit key it says who decided: "was
DENIED in withHuman by Yaakov Yehuda (android): <note>". Without the key nothing breaks; the message stays as above.
A decision made by the pipeline (no person) is never given a name.

1. In withHuman, mint an **organization API key** (`who_...`) with `audit.read`, `request.read` and
   `request.read.unrouted` and **nothing else** (the plugin needs only `audit.read`; the gateway bridge reads the same
   file and needs the other two, see [The gateway bridge](#the-gateway-bridge)). Read-only: it cannot decide,
   change or delete anything, and it must not be able to read rules or members.
2. An admin stores it on the VM, typed with `read -rs` (never pasted into chat, never in the repo):

   ```sh
   read -rs -p "audit key: " AUDIT_KEY; echo; printf '%s' "$AUDIT_KEY" | sudo install -m 640 -o root -g hermes /dev/stdin /etc/durable-park/audit_key; unset AUDIT_KEY
   ```

3. Restart the gateway with no row `parked`, `approved` or `executing` ([health check](#health-check)). The plugin
   re-reads the key at each lookup, so a later rotation needs no gateway restart. The gateway bridge uses the same file
   but copies it when it starts, so also run `sudo systemctl restart durable-park-bridge`
   ([rotation](#store-or-rotate-the-bridges-keys)).
4. Check: decide a test request, then `sqlite3 <ledger> "select park_id, decided_by from parks order by rowid desc limit 3"`
   shows `Name (channel)`. Nothing shows? `~/.hermes/logs/agent.log` has `no audit key file` (a WARNING, logged once per gateway start: file missing, empty or unreadable) or
   `decider lookup failed (<ErrorType>)` (HTTP error such as 403 = key lacks `audit.read`, a timeout, bad JSON).

Settings `audit_key_file` and `audit_base_url`: [architecture](architecture.md#plugin-settings).

### The gateway bridge

The bridge is withHuman's webhook block for gateway calls (`POST /wh/bridge` on `127.0.0.1:8791`). It keeps no
state and never opens the ledger: for each gateway call it asks withHuman (`GET /api/v1/requests`) with the
organization key `/etc/durable-park/audit_key`, and approves only a call that matches one approved park request
consumed in the last 120 s that no other gateway request has used ([decision
rule](architecture.md#the-bridges-decision-rule)). Anything unclear, slow or failing goes to a person: a second ask
for the same action, never an auto-approve.

**Who runs it.** The bridge runs as the system user `dpbridge` (no login, no home, no group but its own; not in
`dpwake` or `hermes`), and systemd's `durable-park-bridge.socket` holds `127.0.0.1:8791` for as long as the socket unit is neither stopped nor failed (StartLimitIntervalSec=0 on the service and TriggerLimitIntervalSec=0 on the socket keep it from failing) and hands the
open socket to `durable-park-bridge.service`. The service gets `bridge_secret` and `audit_key` as private copies
(`LoadCredential`), made when it starts. Why: the bot's user cannot stop the bridge or take its port. Never stop the
socket in normal operation ([rule](#access-and-ground-rules)).

**The key.** The same file as [the decider name](#name-the-decider-optional), now with exactly three permissions:
`audit.read`, `request.read`, `request.read.unrouted`. It must not be able to read rules or members, and it cannot
approve, deny or change anything. The bridge copies it when it starts, so after storing a new one restart the bridge
([how](#store-or-rotate-the-bridges-keys)). On proto-agents
the existing key (`durable-park audit`) was given `request.read` and `request.read.unrouted` in withHuman on
2026-10-22, so the file on the VM did not change. Never paste a key into chat.

**Check** (as the admin `ubuntu`, after any bridge change; the live test runs as `hermes` because only `hermes` can read `audit_key`; nothing is printed that is secret):

```sh
systemctl is-active durable-park-bridge.socket durable-park-bridge                   # active active
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8791/wh/bridge     # 401 (unsigned)
sudo python3 -I /opt/durable-park/deploy/bridge_check.py                             # seven ok: lines, exit 0 (a FAIL: [Health check](#health-check))
sudo -u hermes -H bash -lc 'cd ~/tapdash-agents/durable-park && DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_bridge_lister_live.py'
```

The live test builds the bridge's own lister with the deployed key and lists five requests for `proto-agents`: it
passes when withHuman answers with a list. It prints neither the key nor any arguments. It fails with
`ListError` code `no-key` (file missing) or `http-403` (key too weak).

**Live acceptance after a bridge change** (a sandbox payment, with an approver at hand):

1. One real $1 payment through the chat: approve it **once**. It must run and be created once, with no second ask
   from the gateway. The bridge runs with `ProtectHome=yes`, so the ledger under `/home/hermes` is invisible to it:
   a payment that passes proves the bridge works without the ledger.
2. Replay the same call through the gateway afterwards (same tool, same arguments, the approved park is already
   used): the gateway must ask a person (log `why=already-used`). Decline it.
3. `sudo journalctl -u durable-park-bridge -n 20` shows no key or argument values.

**When withHuman is slow or down**: the bridge answers `human` within its 3.5 s budget (or withHuman's own 5 s
webhook timeout ends the block and the call goes to a person). The gateway asks a human, so the approver sees a
second ask for the action. Nothing runs unapproved and nothing runs twice. The old `bridge_deliveries.db` in
`plugin-data/durable-park/` is unused; delete it once the new bridge is live and the acceptance above passed.

#### Store or rotate the bridge's keys

The bridge needs two files, both typed in by an admin with `read -rs` (never pasted into chat, never in the repo), and
neither is created by any script:

| File | Owner and mode | Store it with |
|---|---|---|
| `/etc/durable-park/bridge_secret` (withHuman's signing secret for the `durable-park-bridge` webhook endpoint) | `root:root 0600` (systemd reads it as root; the bot's group no longer can) | `read -rs -p "bridge secret: " BRIDGE_SECRET; echo; printf '%s' "$BRIDGE_SECRET" \| sudo install -m 600 -o root -g root /dev/stdin /etc/durable-park/bridge_secret; unset BRIDGE_SECRET` |
| `/etc/durable-park/audit_key` (the organization key, three permissions) | `root:hermes 0640` (the plugin reads it for the decider's name) | the command in [Name the decider](#name-the-decider-optional) |

(In a shell, type the `\|` in the table as a plain `|`.)

**First deploy, in this order.**

1. Store both keys as above. `configure_hermes.sh` refuses to start, changing nothing, if either file is missing or
   empty, because the service cannot start without them (`LoadCredential`).
2. Only for the first move from the old bridge that ran as `hermes`: that bridge held `127.0.0.1:8791` itself, so the
   script stops it before systemd's socket can take the port, and for a moment nothing listens. **Keep the gateway
   idle for the whole first migration**: check that no ledger row is `parked`, `approved` or `executing`
   ([waiting rows](#health-check)), and do not start a new tool call in the chat until `bridge_check.py` has passed
   (step 3). Idle is not enough for the script itself: stop `hermes-gateway` before step 3, after the ledger check
   ([warning](#never-change-plugins-while-the-gateway-runs)), because the script runs `hermes plugins enable`; its last
   step then starts the gateway again. A gateway call in that moment goes to a person, never an auto-approve, but a parked or approved row
   would be left waiting on a bridge that is not answering.
3. With `hermes-gateway` stopped (see step 2), run `sudo bash deploy/configure_hermes.sh` ([deploy table](#deploy-a-change), [Before you run the configure script](#before-you-run-the-configure-script)). It creates `dpbridge`, sets
   `bridge_secret` to `root:root 0600`, installs both units, starts the socket and the service, and runs
   `bridge_check.py`. If it stops with `could not start durable-park-bridge.socket: something else may hold
   127.0.0.1:8791`, **treat it as a possible security incident**: the old bridge was stopped just before, so another
   process holds the port and may be answering withHuman's webhook. Read the `ss` output the script printed (process
   names and pids, no secrets), tell the admin, and do not continue and do not re-run the script until the holder is
   explained and stopped.
4. Run the [check](#the-gateway-bridge) below and the [live acceptance](#the-gateway-bridge).

**Rotate a key.** The service copies `bridge_secret` and `audit_key` only when it starts (`LoadCredential`), so after
storing a new one:

```sh
sudo systemctl restart durable-park-bridge        # the socket keeps the port during the restart; connections wait
systemctl is-active durable-park-bridge.socket durable-park-bridge        # active active
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8791/wh/bridge     # 401 (unsigned)
sudo python3 -I /opt/durable-park/deploy/bridge_check.py                  # seven ok: lines
```

Rotating the webhook secret: save the new secret in withHuman, store it as above, and restart straight away; until
then the bridge still holds the old one and answers `401` to withHuman's deliveries, so those calls go to a person.
The `audit_key` needs the same restart; the plugin (decider names) re-reads it on each lookup and needs no gateway
restart.

#### Roll back a bridge change

Run these as the admin `ubuntu` (`hermes` has no sudo; the copies in `/opt` and the units are root-owned). Before the change, keep the old code and **both** unit files, the service and the socket. Each backup first removes the older copy, so `cp -a` does not nest:

```sh
sudo rm -rf /opt/durable-park/bridge.rollback && sudo cp -a /opt/durable-park/bridge /opt/durable-park/bridge.rollback
sudo rm -rf /opt/durable-park/plugin.rollback && sudo cp -a /opt/durable-park/plugin /opt/durable-park/plugin.rollback
sudo rm -f /root/durable-park-bridge.service.rollback && sudo cp -a /etc/systemd/system/durable-park-bridge.service /root/durable-park-bridge.service.rollback
sudo rm -f /root/durable-park-bridge.socket.rollback && sudo cp -a /etc/systemd/system/durable-park-bridge.socket /root/durable-park-bridge.socket.rollback
```

Before the first move to `dpbridge` there is no socket unit yet: skip the last line (and the backup holds the old
`User=hermes` service file).

**Back to the previous `dpbridge` version** (the backup holds both unit files):

1. Confirm no ledger row is `parked`, `approved` or `executing` (the query under [Health check](#health-check)). If one is, stop.
2. First find out whether the socket unit file differs between the two versions (the restore below overwrites the
   evidence, so run this before it):

```sh
sudo cmp -s /root/durable-park-bridge.socket.rollback /etc/systemd/system/durable-park-bridge.socket \
  && echo "socket unit unchanged: restart only the service" || echo "socket unit changed: restart the socket too"
```

   Then restore the code and both unit files. Each `rm -rf` runs first, so the restore does not nest:

```sh
sudo rm -rf /opt/durable-park/bridge && sudo cp -a /opt/durable-park/bridge.rollback /opt/durable-park/bridge
sudo rm -rf /opt/durable-park/plugin && sudo cp -a /opt/durable-park/plugin.rollback /opt/durable-park/plugin
sudo cp -a /root/durable-park-bridge.service.rollback /etc/systemd/system/durable-park-bridge.service
sudo cp -a /root/durable-park-bridge.socket.rollback /etc/systemd/system/durable-park-bridge.socket
```

3. Reload and restart. **Restart the socket only if step 2 said the socket unit changed**: a socket restart closes
   the listener and frees `127.0.0.1:8791` for a moment. Otherwise restart only the service; the socket keeps the
   port and connections wait. Run one of the two lines:

```sh
# the socket unit file did NOT change (the usual case): restart only the service
sudo systemctl daemon-reload && sudo systemctl restart durable-park-bridge.service
# the socket unit file DID change: restart both (the port is free for a moment, so no row parked, approved or executing)
sudo systemctl daemon-reload && sudo systemctl restart durable-park-bridge.socket durable-park-bridge.service
```

Then check:

```sh
systemctl is-active durable-park-bridge.socket durable-park-bridge                   # active active
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8791/wh/bridge     # 401 (unsigned)
sudo python3 -I /opt/durable-park/deploy/bridge_check.py                             # seven ok: lines
```

**Fully back to the old bridge that ran as `hermes`** (from the backup taken before the move to `dpbridge`; it
has the old service file and no socket file):

1. Confirm no ledger row is `parked`, `approved` or `executing`. If one is, stop. For a moment nothing answers
   withHuman's webhook below; a gateway call in that moment goes to a person.
2. Take the port away from systemd first, so the old service can bind it itself. Stopping the socket also stops the
   service, which `Requires=` it:

```sh
sudo systemctl disable --now durable-park-bridge.socket
sudo systemctl stop durable-park-bridge
```

3. Restore the code and the old service file, and give `bridge_secret` back to the group the old bridge reads it
   through (the old unit runs as `hermes` with `SupplementaryGroups=dpwake` and cannot read a `root:root 0600` file):

```sh
sudo rm -rf /opt/durable-park/bridge && sudo cp -a /opt/durable-park/bridge.rollback /opt/durable-park/bridge
sudo rm -rf /opt/durable-park/plugin && sudo cp -a /opt/durable-park/plugin.rollback /opt/durable-park/plugin
sudo cp -a /root/durable-park-bridge.service.rollback /etc/systemd/system/durable-park-bridge.service
sudo chown root:dpwake /etc/durable-park/bridge_secret && sudo chmod 0640 /etc/durable-park/bridge_secret
```

4. Reload, start the old service, and check (do not run `bridge_check.py` here: it describes the `dpbridge` setup and
   prints `FAIL`, which is expected):

```sh
sudo systemctl daemon-reload && sudo systemctl start durable-park-bridge
systemctl is-active durable-park-bridge                                              # active
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8791/wh/bridge     # 401 (unsigned)
```

The user `dpbridge` can stay (no login, no home, no use without the unit). The socket unit file stays installed but
disabled and stopped; remove it with `sudo rm /etc/systemd/system/durable-park-bridge.socket && sudo systemctl daemon-reload` if you want it gone.
The old bridge needs the ledger path and `ReadWritePaths` from the old unit, which the restored file has. Do not run `configure_hermes.sh` for a rollback: it copies the clone's current code and units over the restored files. After the rollback, `git revert` the change as in [Code](#rollback).

### Allow another hook (`observer_hooks`)

durable-park refuses every non-exempt call while anything else has a `pre_tool_call` callback in Hermes's hook
registry, or `tool_request` / `tool_execution` middleware in its middleware registry, because such a callback can
change a call's arguments after withHuman approved them (a hook by returning a `modify` directive, a
`tool_execution` middleware by passing other arguments to `next_call`; it runs after `pre_tool_call`). Four things
put one there, each with its own name format:

| Source | Name in the refusal and the log |
|---|---|
| another Hermes plugin's `ctx.register_hook("pre_tool_call", ...)` | `<module>.<qualname>` of its function, e.g. `my_plugin.hooks.on_tool` |
| a `config.yaml` `hooks:` shell hook for `pre_tool_call` (Hermes `agent/shell_hooks.py:376`) | `agent.shell_hooks.shell_hook[pre_tool_call:<command>]` |
| a `config.yaml` outbound webhook subscribed to `pre_tool_call` (Hermes `agent/outbound_webhooks.py:236`) | `agent.outbound_webhooks.outbound_webhook[pre_tool_call:<label>]`, label = the webhook's `name`, else its URL |
| another Hermes plugin's `ctx.register_middleware("tool_request" or "tool_execution", ...)` (Hermes `hermes_cli/plugins.py:937`) | `<module>.<qualname>` of its function |

**Hooks with two names (TAP2-129).** Hermes Deck's hook is `hermes_plugins.hermes_deck.hermes_deck_pre_tool_call`
when Deck's bridge starts and `hermes_plugins.hermes_deck.events.on_pre_tool_call` when the bridge is off or fails to
start. Listing either one accepts both (`OBSERVER_HOOK_ALIASES` in the plugin's `__init__.py`), so a bridge that does
not come up no longer stops every tool call. List both anyway so the config says what is trusted, and review both
paths (the composed one adds the bridge handler; the events one carries the `computer_use` `modify` path).

Outbound webhooks only notify: the callback queues a POST and always returns `None`, so it can never change
arguments and is safe to list. Shell hooks can return any directive their command prints (including `modify`), and
the name holds the whole command, so review the script.

To allow one:

1. Find its name in the refusal or the log line `foreign pre_tool_call hook(s) present (<name>)`.
2. **Read the callback's code**: for a plugin, its `register` and the function, including everything it calls;
   for a shell hook, the command and the script it runs.
3. **Confirm it never returns `modify`** (or any directive other than `None`, or `block`), on any path, including
   errors. For middleware: a `tool_request` one must never return `{"args": ...}`; a `tool_execution` one must
   call `next_call()` with no argument or with the exact `args` it was given, on every path. If you cannot confirm it, do not list it: remove or disable it instead.
4. Add its exact name to `observer_hooks` (a config edit: nothing changes until the next start), check that no row is
   `parked`, `approved` or `executing` ([health check](#health-check)), restart the gateway, and check one call is asked
   about normally again.
5. Re-do this review whenever that plugin, script or command changes.

Registration order does not make a foreign hook safe: Hermes merges `modify` from every `pre_tool_call` callback, so
a hook that runs after durable-park, or sits lower in `config.yaml`, can still change the arguments. Only a reviewed
observer may be listed.

### Hooks reviewed so far

| Name | Source | What it does | Reviewed (date) | Result |
|---|---|---|---|---|
| `platforms__raft.adapter._on_pre_tool_call` (full name `hermes_plugins.platforms__raft.adapter._on_pre_tool_call`) | Hermes Raft platform plugin, on `proto-agents` | Records telemetry about each call; always returns `None`, on every path including errors. Never returns `modify`, `block` or any other directive. | 2026-10-21 | Listed in `observer_hooks`; gateway restarted; calls asked about normally again |
| `hermes_plugins.langfuse.on_pre_tool_call` | Nous Research official `langfuse` plugin v2.0.0 @ commit `c2a9596`, on `proto-agents` | Starts a Langfuse tool observation from a copy of the call's arguments (`_capture_content` builds new structures with `_safe_value`). Returns `None` on every path and never changes the arguments. Its `on_post_tool_call` only changes the plugin's own copies. | 2026-10-22 | Listed in `observer_hooks`. The plugin was switched on 2026-10-22 04:37 UTC ([Tracing (Langfuse)](#tracing-langfuse)); after that restart calls were asked about normally and a $1 payment ran once after one approval ([record](acceptance-2026-10-22-langfuse.md)). **Retired at the `tapdash-langfuse` deploy** (the catalog plugin is disabled and restarted out first; this entry is removed from `observer_hooks` in a second, ledger-checked restart); the row stays for history |
| `hermes_plugins.tapdash_langfuse.on_pre_tool_call` | TapDash patched copy of the Nous `langfuse` v2.0.0 @ commit `c2a9596` (`tracing/tapdash_langfuse`), on `proto-agents` | The same function as the upstream one reviewed on 2026-10-22 (row above): byte-identical, checked by a diff when the copy was built. Starts a Langfuse tool observation from a copy of the call's arguments. Returns `None` on every path and never changes the arguments. The patches only touch `on_post_tool_call` (pattern tags) and the start of the root trace (user id, name), never this hook | 2026-10-22 | To be listed in `observer_hooks` at the deploy (not listed yet: the plugin is not deployed). Calls must still be asked about normally after that restart ([deploy](#switch-to-the-patched-plugin-deploy)) |
| `hermes_plugins.hermes_deck.hermes_deck_pre_tool_call` | Hermes Deck plugin v1.0.0 ([`Hermes-Deck/hermes-deck`](https://github.com/Hermes-Deck/hermes-deck) @ `2b2b2b7`), on `proto-agents`. Hermes sees one callback that runs the plugin's events part and its bridge part | Shows the Deck app what the agent is doing. It writes a clipped, redacted copy of each call's arguments to a local file (`~/.hermes-deck/events/`). The bridge part returns `None` on every path. The events part returns `None` for every tool except `computer_use`; for `computer_use` it can return `block` or `modify`, but only while Bot screen is on (`DECK_BOT_SCREEN=1`, or `HERMES_CUA_DRIVER_CMD` set). Neither is set here, and the toolset lockdown gives the agent no `computer_use` tool | 2026-10-22 | Listed in `observer_hooks` by Yaakov's decision that this one `computer_use` path is acceptable while Bot screen stays off. So the "never `modify` on any path" rule above is **not met in full**: review it again before turning Bot screen on or opening `computer_use`. The gateway restarted with the plugin loaded and durable-park stayed in `asking` mode with no refusal, so the listed name matches. A real call being asked about normally: **checked 2026-10-22 at 9:53 PM ET**, one read call each from Telegram and from Teams (`get_company_info`), both logged `allowed` ([Hermes Deck](#hermes-deck-desktop-app)) |

`<hook registry unavailable>` is not a hook name: it means durable-park could not read Hermes's hook or middleware
registry (e.g. after a Hermes upgrade). It cannot be allowed; check the Hermes version against `compat.py`.

## Tracing (Langfuse)

Langfuse shows what the agent did: each message, each model call and each tool call, with its full content.
It is for people debugging and reviewing the agent. It has no say in what runs: it only watches.

**State: LIVE since 2026-10-22 04:37 UTC.** The Nous Research `langfuse` plugin (v2.0.0 @ `c2a9596`, MIT) was enabled
at 04:37:45 and the gateway restarted; the Langfuse keys were typed in by Yaakov at about 04:35. Its hook is listed in
`observer_hooks`. Every chat turn on `proto-agents` is now copied to Langfuse Cloud. The settings last changed at
13:42 UTC the same day (`HERMES_LANGFUSE_MAX_DEPTH` 16). What was checked, and the audit of what Langfuse shows:
[acceptance-2026-10-22-langfuse.md](acceptance-2026-10-22-langfuse.md).

**Patched plugin: live since 2026-10-22 (enabled by 19:52 UTC).** The three gaps in that audit (no `userId`, every trace named
`Hermes turn`, no way to find the traces of one approval) are fixed in TapDash's patched copy,
[`tracing/tapdash_langfuse`](../../tracing/tapdash_langfuse) ([The patched plugin](#the-patched-plugin-tapdash-langfuse)).
It was switched on with [Switch to the patched plugin](#switch-to-the-patched-plugin-deploy); the unpatched catalog
plugin `langfuse` is no longer enabled. The rest of this section names
the plugin `tapdash-langfuse` and its hook `hermes_plugins.tapdash_langfuse.on_pre_tool_call`, as they are after the
deploy; before it, read `langfuse` and `hermes_plugins.langfuse.on_pre_tool_call`.

What it records (a hooks-only plugin, no tools):

| Level in Langfuse | What it is |
|---|---|
| Trace | One per message turn. Input = the user's message, output = the final reply. All turns of one chat share a session id, including the turn after an approval. Named `Hermes turn` while the unpatched plugin is live; with the patched plugin `telegram-turn`, `teams-turn`, `cli-turn` (`<platform>-turn`), `cron-run` for a cron job, or `hermes-turn` when the platform is unknown. `userId` = the chat sender id (patched plugin; none for a turn without a sender). The sender's display name is in the **root observation's** metadata as `user_name` (exported as `langfuse.observation.metadata.user_name`; not trace metadata, so a trace-metadata filter will not find it; it may be non-ASCII and is cut to 200 characters). Tags: any pattern tags such as `park:P-XXXXXXXX` |
| Generation | One per model call (`LLM call 1`, `LLM call 2`, ...). Input = the system prompt + the last 12 messages; output = the answer; model name and token counts are filled in |
| Observation | One per tool call (type `TOOL`: arguments and result) and one per subagent. A call that was parked shows level `WARNING` with status message `plugin_block` |

It **fails open**: if Langfuse is down or a key is missing, the turn still runs and the trace is lost. There is no
offline spool, so a gap in Langfuse is not proof that nothing happened. The ledger and withHuman remain the record of
what ran.

**Where traces go.** Langfuse Cloud, US region (<https://us.cloud.langfuse.com>), project `proto-agents`, Hobby (free)
plan: 30 days of data access, and a monthly allowance of units (a unit is one trace, observation or score; 50,000 on
Langfuse's public pricing page, <https://langfuse.com/pricing>, not checked in this work). Retention
cannot be changed on Hobby ([backlog](backlog.md#before-real-books), items 8 and 9).

**Settings** (names only; never print `.env`). All except the two keys live in `~hermes/.hermes/.env`; the two keys
(`HERMES_LANGFUSE_PUBLIC_KEY`, `HERMES_LANGFUSE_SECRET_KEY`) come from Bitwarden ([Secrets: Bitwarden](#secrets-bitwarden)).
Read once when the gateway starts. A change needs a restart (ledger check first, see [Turn it off](#turn-it-off) step 1).

| Name | Live value | Secret | Why |
|---|---|---|---|
| `HERMES_LANGFUSE_BASE_URL` | `https://us.cloud.langfuse.com` | no | Langfuse Cloud, US region |
| `HERMES_LANGFUSE_CAPTURE` | `full` (modes: `metadata`, `sanitized` = the plugin's default, `full`) | no | Whole content, decided for the sandbox (see "What `full` means" below) |
| `HERMES_LANGFUSE_ENV` | `proto-agents` | no | The environment label on each trace |
| `HERMES_LANGFUSE_MAX_DEPTH` | `16` | no | How many levels of nested data are kept per field. The plugin default (4) cut 16 observations, including the arguments of `create_payment`. 10 still cut the payment's `linked_txn` (11 levels deep inside the tool-call envelope) and the QuickBooks tool schemas. At 16 nothing is cut (checked with the plugin's own serializer: 10 gave 1 + 5 cut-offs, 16 gave none). Cut-off data shows as `<max-depth>` |
| `HERMES_LANGFUSE_MAX_CHARS` | `50000` | no | Characters kept per field. The default (12000) cut root outputs of about 14,000 characters |
| `HERMES_LANGFUSE_RELEASE` | `hermes-v0.22.3` | no | The release label on each trace. **Update it when Hermes is upgraded** (`hermes-v<version>`), then restart, so traces can be told apart by version |
| `HERMES_LANGFUSE_TAG_PATTERNS` | to be added at the deploy (patched plugin only; the catalog plugin ignores it): `park=\bP-[0-9A-F]{8}\b` | no | Trace tags from patterns, `label=regex` pairs separated by `;`. Every match in the turn's original user message (as the person sent it, without the memory and plugin context Hermes adds to the model's copy), a tool call's arguments or a tool's result adds the trace tag `label:value` (value = group 1 if the regex has a group, else the whole match). With this value a trace that mentions park id `P-1A2B3C4D` gets the tag `park:P-1A2B3C4D`. Limits: at most 10 pattern tags per trace; a tag over 200 characters or not US-ASCII is skipped; only the first 200 000 characters of each text are scanned; arguments and results that are not plain strings are scanned as JSON text, so a JSON escape written right before an id (`\n` then `P-...`) hides it from the `\b` ([README](../../tracing/tapdash_langfuse/README.md)); with `HERMES_LANGFUSE_CAPTURE=metadata` no pattern tags are added (a tag would carry content; `userId` and the root observation's metadata `user_name` are still sent in that mode). **Pattern tags are taken from the raw text and are not passed through secret redaction, even in `sanitized` mode**: use only patterns that match non-secret ids. A pattern runs on up to 200 000 characters of each text, so it must not backtrack catastrophically (the root scan runs while the plugin holds its state lock, so a runaway pattern would stall every hook). A bad entry (no `=`, a label that is not `[a-z0-9_-]{1,40}`, or a regex that does not compile) logs one warning and is ignored; the other entries still work |
| `HERMES_LANGFUSE_SCORE_PATTERNS` | optional; not set on proto-agents until the deploy step (patched plugin only; the catalog plugin ignores it). The exact value is in the [README](../../tracing/tapdash_langfuse/README.md#score-traces-by-pattern-tapdash-patch) | no | Scores a trace from a pattern in the turn's original user message, `name=regex[=>literal]` entries separated by `;`. Meant for the approval decision (`approval_decision` = `approved`, `denied`, `cancelled` or `expired`) taken from durable-park's wake text. Not scored with `HERMES_LANGFUSE_CAPTURE=metadata`. Like pattern tags, values and comments come from the raw text and are not passed through secret redaction |
| `HERMES_LANGFUSE_PUBLIC_KEY` | the project's public key (`pk-lf-...`); in Bitwarden, not in `.env` | in Bitwarden | Half of the key pair. An identifier rather than a secret on its own, but handled like the other key |
| `HERMES_LANGFUSE_SECRET_KEY` | the project's secret key (`sk-lf-...`); in Bitwarden, not in `.env` | yes, in Bitwarden | Reads and writes the project's traces |

Backups of `.env` from this work: `.env.bak-langfuse-20261022T042450Z`, `.env.bak-langfuse2-20261022T045726Z`,
`.env.bak-langfuse3-20261022T134218Z` (and `config.yaml.bak-langfuse-20261022T042450Z`), all in `~hermes/.hermes/`.
The same settings are listed with the other live settings in [Live Hermes settings on
proto-agents](#live-hermes-settings-on-proto-agents).

**What `full` means.** Raw content, cut only when a field is longer than `HERMES_LANGFUSE_MAX_CHARS` or
nested deeper than `HERMES_LANGFUSE_MAX_DEPTH`. Everything people
type in a chat, everything the model is sent and answers, and every tool's arguments and results (QuickBooks names,
amounts, ids) leaves the VM and sits at Langfuse. That is acceptable for the QuickBooks **sandbox** on the test VM. It
is a decision to make again before real books ([backlog](backlog.md#before-real-books), item 9).

**What `metadata` and `sanitized` still send.** With the patched plugin, `HERMES_LANGFUSE_CAPTURE=metadata` adds no
pattern tags, but the trace `userId` (the chat sender id) and the root observation's metadata `user_name` (the sender's
display name) are still sent, because they are not content. In `sanitized` mode, pattern tags are not redacted: a tag carries the raw
match, so the patterns must only match non-secret ids (such as park ids), and must not backtrack catastrophically.

### The patched plugin (`tapdash-langfuse`)

**Status: live on `proto-agents` since 2026-10-22 (enabled by 19:52 UTC).** A copy of the Nous Research `langfuse` plugin (v2.0.0 @ commit `c2a9596`,
MIT) lives in this repo at [`tracing/tapdash_langfuse`](../../tracing/tapdash_langfuse), with TapDash's changes. Every
change against upstream is listed in [`PATCHES.md`](../../tracing/tapdash_langfuse/PATCHES.md). Decisions and reasons:
[design](../../docs/specs/2026-10-22-langfuse-trace-gaps-design.md) (D1 to D8). At the deploy it is installed on the VM as a symlink (there is none before then),
`~/.hermes/plugins/tapdash-langfuse` to `/home/hermes/tapdash-agents/tracing/tapdash_langfuse` (the same pattern as
durable-park), under the plugin name `tapdash-langfuse` and the import name `hermes_plugins.tapdash_langfuse`. The
catalog `langfuse` plugin is disabled at the deploy but kept installed, for rollback.

| What changes | How |
|---|---|
| `userId` on each trace | The chat sender id Hermes passes to the turn's `pre_llm_call` hook (`sender_id`). Left out when there is none (cron, some CLI turns) or when it is not US-ASCII or is over 200 characters (Langfuse's limits); never altered. The sender's display name goes in the root observation's metadata as `user_name` (it may be non-ASCII; cut to 200 characters; not trace metadata) |
| Trace names | `<platform>-turn`, `cron-run`, `hermes-turn` (see [the table above](#tracing-langfuse)). The root observation has the same name. Model calls are still `LLM call N` |
| Trace tags from patterns | `HERMES_LANGFUSE_TAG_PATTERNS` (settings table). A tag found late in a turn (in a tool result) is put on the trace before the root ends, so the filter finds it. Generic: the plugin knows nothing about durable-park |

**No durable-park code and no approval behaviour changes.** The hook durable-park cares about, `on_pre_tool_call`, is
the same function as upstream's, byte for byte (checked with a diff when the copy was built). The patches scan text in
`on_post_tool_call` and when the root trace starts, never in `on_pre_tool_call`; the plugin still only observes, returns
`None` and never changes a call's arguments. Its row is in [Hooks reviewed so far](#hooks-reviewed-so-far).

**Tests.** `bash tracing/run_tests.sh`, as `hermes` on the VM (it uses the Python of the Hermes environment the running gateway uses, falling back to the first environment that
has `langfuse` and saying which it used; it installs `pytest` into a cache directory outside the repo, and runs with
warnings as errors). It is upstream's suite plus the new tests,
without upstream's four `TestSystemPromptCrossesHookBoundary` tests, which re-run Hermes core's request builder (and
its dependency installer) and cannot run from a checkout (design D8); the script always deselects them, whatever
arguments you add. One new test runs the real Langfuse SDK in memory, with no network. CI does not run them yet ([backlog](backlog.md#langfuse-tracing-gaps-from-the-2026-10-22-audit), item 6).

### Switch to the patched plugin (deploy)

Not done yet. Do it only after the pull requests with `tracing/` and these docs are merged and the live clone is up to
date (`git -C /home/hermes/tapdash-agents pull --ff-only`, as in [Deploy a change](#deploy-a-change)). The pull also
brings the `bridge/` and `deploy/` changes merged with it: `/opt/durable-park` (the bridge's copy) stays at the old code
until an admin runs `configure_hermes.sh` as [Deploy a change](#deploy-a-change) says for the bridge (ledger check and `hermes-gateway` stopped first: [Before you run the configure script](#before-you-run-the-configure-script)); this deploy does
not need that. As `hermes`:

**Before you start.** `bash tracing/run_tests.sh` in the live clone: all tests pass. Diff `on_pre_tool_call` against
upstream: check out <https://github.com/NousResearch/hermes-official-plugins> at commit
`c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d` into a temporary directory (the plugin is its subdirectory `langfuse`), then
`diff <(sed -n '/^def on_pre_tool_call/,/^def /p' <temp dir>/langfuse/__init__.py) <(sed -n '/^def on_pre_tool_call/,/^def /p' tracing/tapdash_langfuse/__init__.py)`
must print nothing.

**Why this order has no unsafe moment.** durable-park refuses calls only when a loaded plugin has a hook that is not on
`observer_hooks`, and a name on the list whose plugin is not loaded is harmless, so the new name goes on the list first
and the old name comes off only after the catalog plugin has stopped loading: at every start, planned or after a
crash, the list is never shorter than the hooks that are loaded. The plugin switch itself (step 3) is done with the
gateway **stopped**, because `hermes plugins enable|disable` would otherwise reload every plugin inside the running
gateway ([warning](#never-change-plugins-while-the-gateway-runs)).

**The Hermes environment.** Enabling a plugin that lists `python_dependencies` (this one: `langfuse==4.15.2`) can make
Hermes create a new environment under `~/.hermes/installs/*/environments/` or switch the gateway to another one (on
2026-10-22 enabling the catalog plugin created a new directory, [record](acceptance-2026-10-22-langfuse.md)). Before
step 3 write down which environment the gateway runs:
`grep -o '/environments/[0-9a-f]*/venv' /proc/$(systemctl --user show hermes-gateway -p MainPID --value)/maps | sort -u`
(one line; the "Check it works" block below has the same command). Step 4 checks it again.

1. Back up `~/.hermes/.env` and `~/.hermes/config.yaml` with `cp -p` (suffix `.bak-langfuse-gaps-<UTC time>`; `-p` keeps
   the owner and the mode, and the `.env` copy holds the Langfuse secret key, so check with `ls -l` that it is `600`,
   `chmod 600` it if not). Then add the new
   setting once, quoted so the backslashes stay (it holds no secret):
   `printf '%s\n' 'HERMES_LANGFUSE_TAG_PATTERNS=park=\bP-[0-9A-F]{8}\b' >> ~/.hermes/.env`
   (end the last line first if it has no newline, as in [Turn it on](#turn-it-on-admin)).
   To also turn on the approval score, add `HERMES_LANGFUSE_SCORE_PATTERNS` the same way (it holds no secret). Its value contains `"`, so it must be unquoted or **single**-quoted in `.env`: a double-quoted line does not parse (python-dotenv) and the setting silently disappears. Copy the value from the [README](../../tracing/tapdash_langfuse/README.md#score-traces-by-pattern-tapdash-patch) into the single quotes (it has no `'`):
   `printf '%s\n' 'HERMES_LANGFUSE_SCORE_PATTERNS=<the value from the README>' >> ~/.hermes/.env`
   Check it afterwards: `grep -c '^HERMES_LANGFUSE_SCORE_PATTERNS=' ~/.hermes/.env` prints `1`.
2. Add `hermes_plugins.tapdash_langfuse.on_pre_tool_call` to `observer_hooks` by hand in `config.yaml`
   ([Plugin settings](#plugin-settings)), **next to** the existing entries, so the list has three:
   `hermes_plugins.platforms__raft.adapter._on_pre_tool_call`, `hermes_plugins.langfuse.on_pre_tool_call` and
   `hermes_plugins.tapdash_langfuse.on_pre_tool_call`. An entry whose plugin is not loaded does no harm
   (`observer_hooks` is only an allow-list), so this changes nothing for the running gateway. Read it back with
   `hermes config get plugins.entries.durable-park.settings.observer_hooks` (a list of 3 strings).
3. Switch the plugin **with the gateway stopped**. Check that no row is `parked`, `approved` or `executing`
   ([health check](#health-check)), then straight away `systemctl --user stop hermes-gateway`. Then
   `ln -s /home/hermes/tapdash-agents/tracing/tapdash_langfuse ~/.hermes/plugins/tapdash-langfuse`,
   `hermes plugins disable langfuse`, `hermes plugins enable tapdash-langfuse`. Each of the two commands prints that no
   running gateway answered: that is expected (nothing is running to reload).
4. `systemctl --user start hermes-gateway`, then run [Check it works](#check-it-works): `hermes plugins list` shows
   `tapdash-langfuse` enabled and `langfuse` disabled, the `durable-park loaded` line counts **3** observer hooks, there
   is no `foreign pre_tool_call` line, the tool policy is `"ok": true`, and `plugin_load.json` is present. Also check
   the environment (see above): the `grep` on the new gateway's memory map prints the same single environment as
   before step 3 (if it printed another one, note which and why before going on), and that environment's Python
   imports the SDK at the pinned version:
   `<that venv>/bin/python -c 'import langfuse; print(langfuse.__version__)'` prints `4.15.2`. If either is wrong,
   roll back (below).
5. Now the catalog plugin is no longer loaded, so take `hermes_plugins.langfuse.on_pre_tool_call` out of
   `observer_hooks` (by hand; leave Raft and `hermes_plugins.tapdash_langfuse.on_pre_tool_call`), **in a second
   stop/start with its own ledger check**: check the ledger, `systemctl --user stop hermes-gateway`, edit the list,
   read it back (a list of 2 strings), `systemctl --user start hermes-gateway`, and run
   [Check it works](#check-it-works): the `durable-park loaded` line counts **2** observer hooks and there is still no
   `foreign pre_tool_call` line.
6. Send a chat message, then a $1 payment that parks, and approve it. Find the traces by the tag
   ([Find everything about one approval](#find-everything-about-one-approval)): the parking turn, the approval turn and
   the run are returned, and the trace names are `telegram-turn` (or the platform's name). `userId` on the parking turn
   is the sender's chat id. **Check, and write down, the `userId` and name of the approval turn and the run too:** they
   are started by a message durable-park injects into the chat (`ctx.inject_message`), and whether that turn's agent
   carries the sender's id was not verified. A missing or different `userId` there is **not** a failure of the deploy
   and no reason to roll back; the park-id tag is what joins the three traces. The payment must still ask once and run
   once.

**If the gateway restarts on its own** (a crash restart) at any point above, nothing is refused: before step 3 the
catalog plugin is loaded and its name is listed; between steps 4 and 5 `tapdash-langfuse` is loaded and both names are
listed; after step 5 only the new name is needed and listed. Check the ledger and carry on, or roll back (below).
**Never run the `hermes plugins` commands of step 3 or of the rollback with the gateway running**
([warning](#never-change-plugins-while-the-gateway-runs)).

**Why the old entry is removed only in the second restart.** **Removing the name of an observer whose plugin is still
loaded makes durable-park refuse every tool call** (an outage, not a loss of safety; [Allow another
hook](#allow-another-hook-observer_hooks)). The old name is removed in step 5, after the restart of step 4 has stopped
loading the catalog plugin, never before. The same holds in reverse for the rollback.

**Rollback** (any failure above). Put back only what the deploy changed, in this order:

1. Make sure `observer_hooks` has Raft, `hermes_plugins.langfuse.on_pre_tool_call` **and**
   `hermes_plugins.tapdash_langfuse.on_pre_tool_call`: re-add the old name if step 5 already removed it. Read it back.
2. Check the ledger, `systemctl --user stop hermes-gateway`, then `hermes plugins enable langfuse` and
   `hermes plugins disable tapdash-langfuse` (each prints that no running gateway answered: expected), then
   `systemctl --user start hermes-gateway`, and run [Check it works](#check-it-works) with the old names (`langfuse`
   enabled, 3 observer hooks).
3. Only now, with `tapdash-langfuse` disabled and that start done, take
   `hermes_plugins.tapdash_langfuse.on_pre_tool_call` out of `observer_hooks` (Raft and the old name stay; read it back),
   in another stop/start with its own ledger check: check the ledger, stop `hermes-gateway`, edit, start it: 2 observer
   hooks.

Restoring the backups from step 1 instead is possible only if nothing else in `.env` or `config.yaml` has changed since
they were taken; it also needs the ledger check and a stop/start of the gateway. The symlink and the `HERMES_LANGFUSE_TAG_PATTERNS` line can stay (the catalog plugin
ignores the setting, and a disabled plugin is not loaded).

### Turn it on (admin)

This is how the catalog plugin was switched on on 2026-10-22; use the same steps to switch the tracing plugin on again
after it was turned off (names as after the deploy; the plugin directory is the symlink from step 3 of the
[deploy](#switch-to-the-patched-plugin-deploy)).

1. In Langfuse (project `proto-agents`, Settings, API keys), create a key pair. The secret key is shown once.
2. Put the keys into Bitwarden (since 2026-10-22/23 they are not kept in `.env`): in the Bitwarden web app, project
   "TapDash Agents", save them as the secrets `HERMES_LANGFUSE_PUBLIC_KEY` and `HERMES_LANGFUSE_SECRET_KEY`. Nothing
   goes in chat or the repo. Check with `sudo -u hermes -H hermes secrets bitwarden status`
   ([Secrets: Bitwarden](#secrets-bitwarden)). Before 2026-10-22/23 this step typed them into `.env` with `read -rs`.

3. Check that the non-secret settings in the table above are in `.env` (names only: `grep -c '^HERMES_LANGFUSE_' ~/.hermes/.env`
   counts the lines; `grep -o '^HERMES_LANGFUSE_[A-Z_]*' ~/.hermes/.env` lists the names). Add any that are missing.
   Also check that `hermes config get plugins.entries.durable-park.settings.observer_hooks` lists
   `hermes_plugins.tapdash_langfuse.on_pre_tool_call`. It is missing after "Remove for good" below: then **add it now**,
   by hand in `config.yaml`, and read the list back, before step 4. While the gateway still runs this is only a config
   edit and changes nothing until the next start; the name must be there before step 6 starts the gateway, because
   without it durable-park refuses every tool call once the plugin is loaded (an outage, not a loss of safety).
4. Check that no row is `parked`, `approved` or `executing` ([health check](#health-check)), then straight away
   `systemctl --user stop hermes-gateway`, so no approval can be parked in between and no plugin reload reaches a
   running gateway ([warning](#never-change-plugins-while-the-gateway-runs)).
5. `hermes plugins enable tapdash-langfuse` (and the catalog `langfuse` plugin must stay disabled: with both enabled
   every turn is traced twice, and the second hook needs its own `observer_hooks` entry). The command prints that no
   running gateway answered: that is expected.
6. `systemctl --user start hermes-gateway`, then run [Check it works](#check-it-works).

### Check it works

```sh
hermes plugins list | grep -i -e langfuse -e durable-park      # tapdash-langfuse enabled, langfuse disabled, durable-park enabled
grep -i -e 'durable-park loaded' -e 'foreign pre_tool_call' ~/.hermes/logs/agent.log | tail
grep -e 'Langfuse plugin:' -e 'Langfuse tracing:' ~/.hermes/logs/agent.log | tail     # nothing dated after the last start (older lines also match)
grep 'plugin=.*langfuse' ~/.hermes/logs/agent.log | tail -3
cat ~/.hermes/plugin-data/durable-park/tool_policy_status.json       # "ok": true
cat ~/.hermes/plugin-data/durable-park/plugin_load.json            # present, pid = the gateway's MainPID
# which Hermes environment the gateway runs (one line), and that its Python has the pinned SDK (4.15.2):
V=$(grep -o '/environments/[0-9a-f]*/venv' /proc/$(systemctl --user show hermes-gateway -p MainPID --value)/maps | sort -u)
echo "$V"; ~/.hermes/installs/*"$V"/bin/python -c 'import langfuse; print(langfuse.__version__)'
```

What good looks like (seen on 2026-10-22):

- `hermes plugins list` shows `langfuse` enabled (seen on 2026-10-22, before the patched plugin). After the deploy it
  must show `tapdash-langfuse` enabled and `langfuse` disabled.
- The `durable-park loaded: ... 2 observer hook(s) ...` line (two: Raft and Langfuse; 3 between steps 4 and 5 of the deploy, while both Langfuse names are listed), and **no**
  `foreign pre_tool_call hook(s) present` line. If there is one, the Langfuse hook name is not listed (or the other
  Langfuse plugin is still loaded), and durable-park refuses every call ([Allow another hook](#allow-another-hook-observer_hooks)).
- The Langfuse plugin writes **no "loaded" line**, so do not look for one. A missing or placeholder key is the only thing
  it complains about, with a warning that starts `Langfuse plugin:` or `Langfuse tracing:`. None must appear after the
  last start. The patched plugin also logs one warning for each bad entry of `HERMES_LANGFUSE_TAG_PATTERNS`: there
  must be none for the live value.
- Every start logs `capability_check plugin=<plugin name> capability=tools.override decision=deny`
  (`langfuse` seen on 2026-10-22; not yet seen for `tapdash-langfuse`). That is normal and
  harmless: the plugin asks for a capability it does not need and is refused.
- `tool_policy_status.json` has `"ok": true` and `plugin_load.json` exists.
- Send one chat message. After a short delay a trace appears in Langfuse (project `proto-agents`, environment
  `proto-agents`, release as in the table) with the full text: named `Hermes turn` with the unpatched plugin, and
  with the patched plugin named `telegram-turn` (or the platform's name) with `userId` = the sender's chat id. See
  [Read traces](#read-traces).
- With the patched plugin: a message or tool result that contains a park id gives the trace the tag `park:P-...`.
- Make one QuickBooks call. It must still be asked about in withHuman as before, and a parked payment must still run
  at most once. Tracing changes none of that.

### Read traces

In the Langfuse web app: project `proto-agents`, Tracing. To read them from the VM: **Langfuse Cloud answers HTTP 410
for the old v1 calls** (`/api/public/traces`, `/api/public/observations`, `/api/public/sessions`). Use the v2
observations call, or `npx langfuse-cli` (the Langfuse agent skill recommends it). Load the keys into the process
only: never put them on a command line (they would show in `ps` and in shell history) and never print them.

The two scripts below take the base URL from `~/.hermes/.env` and fetch the two keys from Bitwarden inside the
script ([Secrets: Bitwarden](#secrets-bitwarden)), with the `bws` tool Hermes installed. Nothing is printed or put on a
command line. Both were run live on 2026-10-23. Run them as `hermes`.

```sh
python3 - <<'PY'
import base64, glob, json, os, subprocess, urllib.parse, urllib.request
H = os.path.expanduser("~/.hermes")
env = {}
for line in open(H + "/.env"):                      # the base URL and the Bitwarden token are in .env
    k, _, v = line.rstrip("\n").partition("=")
    if k.startswith("HERMES_LANGFUSE_") or k == "BWS_ACCESS_TOKEN":
        env[k] = v.strip().strip("'\"")
# The two keys are in Bitwarden (see "Secrets: Bitwarden"). bws gets its token from the environment, never argv.
bws = sorted(glob.glob(H + "/tools/bws-*/bws"))[-1]
listed = subprocess.run([bws, "secret", "list", "94af3d93-ad3d-486b-908d-b4ec00050dfd", "--output", "json"],
                        capture_output=True, text=True, check=True,
                        env=dict(os.environ, BWS_ACCESS_TOKEN=env.pop("BWS_ACCESS_TOKEN")))
for s in json.loads(listed.stdout):
    if s["key"] in ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY"):
        env[s["key"]] = s["value"]
auth = base64.b64encode(
    (env["HERMES_LANGFUSE_PUBLIC_KEY"] + ":" + env["HERMES_LANGFUSE_SECRET_KEY"]).encode()).decode()
base = env["HERMES_LANGFUSE_BASE_URL"] + "/api/public/v2/observations"
params = {"fields": "core,basic,io,metadata,time", "limit": "100", "fromStartTime": "2026-10-22T04:35:00Z"}
rows = []
while True:
    req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": "Basic " + auth})
    body = json.load(urllib.request.urlopen(req, timeout=30))
    rows += body.get("data", [])
    cursor = (body.get("meta") or {}).get("cursor")
    if not cursor:
        break
    params["cursor"] = cursor
print(len(rows), "observations,", len({r["traceId"] for r in rows}), "traces")   # counts only
PY
```

Change `fromStartTime` (UTC) to the start of the window you want. Print only what you need from each row (name, type,
level, trace id): the rows hold full chat and tool content. With `npx langfuse-cli`, export the keys into the
process environment the same way (fetched from Bitwarden, without echoing) and do not pass them as arguments.

### Find everything about one approval

With the patched plugin (and `HERMES_LANGFUSE_TAG_PATTERNS` set as in the settings table) every trace that mentions
a park id carries the tag `park:P-XXXXXXXX`: the turn that parked the call (the id is in the tool result), the turn
after the approval (`[durable-park] P-... was APPROVED ...`) and the run (`"park_id": "P-..."`, in the same trace as
the approval turn). **Filter the traces by that tag.** In the Langfuse web app: Tracing, Filter, Tags, pick
`park:P-XXXXXXXX` (the exact menu path in the web app has not been checked). From the VM, call the v2 observations
endpoint with `fields=core,basic,trace_context` and a `filter` that is the URL-encoded JSON list below. The keys are
loaded into the process only, as in [Read traces](#read-traces), and never printed. The park id comes from the ledger or
the approval message.

```sh
python3 - <<'PY'
import base64, glob, json, os, subprocess, urllib.parse, urllib.request
H = os.path.expanduser("~/.hermes")
env = {}
for line in open(H + "/.env"):                      # the base URL and the Bitwarden token are in .env
    k, _, v = line.rstrip("\n").partition("=")
    if k.startswith("HERMES_LANGFUSE_") or k == "BWS_ACCESS_TOKEN":
        env[k] = v.strip().strip("'\"")
# The two keys are in Bitwarden (see "Secrets: Bitwarden"). bws gets its token from the environment, never argv.
bws = sorted(glob.glob(H + "/tools/bws-*/bws"))[-1]
listed = subprocess.run([bws, "secret", "list", "94af3d93-ad3d-486b-908d-b4ec00050dfd", "--output", "json"],
                        capture_output=True, text=True, check=True,
                        env=dict(os.environ, BWS_ACCESS_TOKEN=env.pop("BWS_ACCESS_TOKEN")))
for s in json.loads(listed.stdout):
    if s["key"] in ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY"):
        env[s["key"]] = s["value"]
auth = base64.b64encode(
    (env["HERMES_LANGFUSE_PUBLIC_KEY"] + ":" + env["HERMES_LANGFUSE_SECRET_KEY"]).encode()).decode()
flt = [{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": ["park:P-XXXXXXXX"]}]
url = (env["HERMES_LANGFUSE_BASE_URL"] + "/api/public/v2/observations?fields=core,basic,trace_context&limit=100&filter="
       + urllib.parse.quote(json.dumps(flt)))
body = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Basic " + auth}), timeout=30))
for r in body.get("data", []):
    print(r["traceId"], r.get("type"), r.get("name"))   # ids and names only: the rows hold full content
PY
```

It returns the observations of every trace that carries the tag: the parking turn, the approval turn and the run (the
same `traceId` for the last two). Follow `meta.cursor` for more than 100 rows, as in Read traces. Traces from before
the deploy have no such tag: search them by the park id in the text.

### Secret scan (how it was done, and how to repeat it)

Full capture means a secret that reaches the model or a tool result would also reach Langfuse, so the traces were
scanned for secrets (2026-10-22 at 04:59 UTC over 26 observations, repeated at 13:42 UTC). Result: **no secret key, no
`.env` secret, no readable `/etc/durable-park` secret, no Telegram bot token, no Bearer token, no withHuman `who_` key
and no card-number-like digits** were found in Langfuse. The only hit was the Langfuse **public** key, which the
SDK writes into every span's metadata (`scope.attributes.public_key`); it is an identifier, not a secret. Not scanned:
`/etc/durable-park/receiver.json` and `tunnel.env` (owner-only; `hermes` cannot read them).

To repeat it, as `hermes`, in one process that never prints a candidate or a match:

1. Build the candidate list: every `.env` value whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD` and that is
   at least 12 characters long (since 2026-10-22/23 the Teams, Telegram and Langfuse keys are in Bitwarden: add them to the list by
   fetching them the same way as in [Read traces](#read-traces)), plus the contents of each file under `/etc/durable-park/` that `hermes` can read.
2. Fetch the observations with the v2 call under [Read traces](#read-traces).
3. For each observation, search its whole JSON for each candidate as a literal substring, and for the shapes `sk-lf-`,
   `pk-lf-`, a Telegram bot token, `Bearer`, `who_` and any 13 to 19 digit run that passes the Luhn check.
4. Print only counts, the observation id and the field name where something matched. Never print the matched text.

Expect exactly one kind of hit: the public key in `scope.attributes.public_key`.

### Turn it off

1. Check that no row is `parked`, `approved` or `executing` ([health check](#health-check)).
2. `systemctl --user stop hermes-gateway`, then `hermes plugins disable tapdash-langfuse` (it prints that no running
   gateway answered: expected; never run it with the gateway running, see the
   [warning](#never-change-plugins-while-the-gateway-runs)), then `systemctl --user start hermes-gateway`. Nothing is
   sent after the start. Traces already sent stay in Langfuse until its 30-day window on the free plan has passed.

Leave `hermes_plugins.tapdash_langfuse.on_pre_tool_call` in `observer_hooks` for now: a listed hook that is not loaded
does no harm. But **do not change the plugin's code while it is enabled** without the review in
[When the plugin changes](#when-the-plugin-changes), and never enable a changed plugin before its hook has been
reviewed: the name stays listed, so a changed hook would be allowed silently at the next restart. To go back to the
unpatched catalog plugin instead of switching tracing off, use the rollback in
[Switch to the patched plugin](#switch-to-the-patched-plugin-deploy).

To remove it for good, **in this order**:

1. Check that no row is `parked`, `approved` or `executing` ([health check](#health-check)).
2. `systemctl --user stop hermes-gateway`, then `hermes plugins disable tapdash-langfuse` (or remove the plugin; the
   command prints that no running gateway answered: expected), then `systemctl --user start hermes-gateway`. The plugin
   is no longer loaded.
3. Only now take `hermes_plugins.tapdash_langfuse.on_pre_tool_call` out of `observer_hooks` (and its row in
   [Hooks reviewed so far](#hooks-reviewed-so-far)): check the ledger again, stop `hermes-gateway`, edit, start it again.
4. Delete the two Langfuse secrets from the Bitwarden project (they are no longer in `.env`) and revoke the key pair in
   Langfuse.

Why this order: while the plugin is still loaded, its hook is a foreign `pre_tool_call` hook. If it is taken out of
`observer_hooks` first, durable-park refuses every tool call ([Allow another
hook](#allow-another-hook-observer_hooks)).

### Rotate the keys

1. In Langfuse, create a new key pair. Do not delete the old one yet.
2. In the Bitwarden web app, edit the secrets `HERMES_LANGFUSE_PUBLIC_KEY` and `HERMES_LANGFUSE_SECRET_KEY` in the project
   "TapDash Agents" ([Change a key](#change-a-key)). They are no longer in `.env`.
3. Check the ledger, restart `hermes-gateway`, run "Check it works", then revoke the old key pair in Langfuse.

### When the plugin changes

The hook is listed in `observer_hooks` because of the code that was read: upstream v2.0.0 at commit `c2a9596`, and our
copy's `on_pre_tool_call`, which is byte-identical to it. The listing is by name only, so a changed hook that now does
something else would be allowed silently at the next load of the plugin: a gateway restart (a crash restart is
enough) **or any plugin reload** (`hermes plugins enable|disable` of any plugin, the dashboard's plugin toggle), which
loads the changed code too. Because, once deployed, the plugin is a symlink into the live clone, `git pull` in the live clone changes
the code that next load uses. So a change under `tracing/tapdash_langfuse/` goes live only like this:

1. In your worktree: `bash tracing/run_tests.sh` passes, and `on_pre_tool_call` still matches the upstream function
   (the diff under "Before you start" in the [deploy](#switch-to-the-patched-plugin-deploy) prints nothing), or the changed hook is
   read again (steps 2 and 3 of [Allow another hook](#allow-another-hook-observer_hooks)). If you cannot confirm it
   never returns `modify`, do not merge it. List the change in `PATCHES.md`.
2. Update its row in [Hooks reviewed so far](#hooks-reviewed-so-far) (a new date and what was read), and, for a new
   upstream version, the version in the root [Built on](../../README.md#built-on) table.
3. Only then pull into the live clone, with the gateway stopped so nothing can load the new code before it is
   started on purpose: check that no row is `parked`, `approved` or `executing` ([health check](#health-check)),
   `systemctl --user stop hermes-gateway`, `git -C /home/hermes/tapdash-agents pull --ff-only`,
   `systemctl --user start hermes-gateway`, then run [Check it works](#check-it-works) (the `durable-park loaded` line
   must still count 2 observer hooks and show no `foreign pre_tool_call` line). The pull also brings any `bridge/` and
   `deploy/` changes merged meanwhile: `/opt/durable-park` stays at the old code until an admin runs
   `configure_hermes.sh` ([Deploy a change](#deploy-a-change); check the ledger and stop `hermes-gateway` first: [Before you run the configure script](#before-you-run-the-configure-script)); a plugin update does not need that.

To take a newer upstream version: copy it over the vendored files, re-apply the changes listed in `PATCHES.md`, and
do steps 1 to 3. Never use `hermes plugins install langfuse` to update: it would change the catalog copy, not ours.

After a **Hermes upgrade**, also set `HERMES_LANGFUSE_RELEASE` to the new version (`hermes-v<version>`) and restart (ledger check first: no row `parked`, `approved` or `executing`, [health check](#health-check)), so
new traces carry the right release.

## Hermes Deck (desktop app)

Hermes Deck is a desktop app (Windows or Mac) that shows what the agent is doing right now and lets you chat with it.
It has two parts on the VM and one on your computer. It only watches; it has no say in what runs.

**State: INSTALLED on 2026-10-22 at 21:01 UTC. Relay running since 2026-10-23 01:39 UTC (9:39 PM ET, Oct 22).** Version
1.0.0 of the plugin, relay and app protocol (`Hermes-Deck/hermes-deck` @ `2b2b2b7`). Its hook is reviewed and listed
([Hooks reviewed so far](#hooks-reviewed-so-far)).

| Part | Where | Notes |
|---|---|---|
| Plugin | `~/.hermes/plugins/hermes-deck` (a **copy**, not a link) | Enabled in `plugins.enabled`. After an update, run its installer again |
| Stream bridge | `~/.hermes/hermes-deck/stream.py` | The app runs it over SSH. It uses only Python's standard library, so `/usr/bin/python3` works |
| Relay | service `hermes-deck-relay` (a user service of `hermes`), checkout `/home/hermes/hermes-deck/relay` | Listens on `100.99.171.75:9330` (the Tailscale address) only. Loopback does not answer |
| Relay token | `relay/.env`, mode `0600`, owner `hermes` | 64 random characters. Never print it, never paste it in chat |
| Events file | `~/.hermes-deck/events/*.ndjson` | Clipped, redacted copies of tool arguments (QuickBooks arguments included) |
| Config backups | `~/.hermes/config.yaml.bak-pre-hermes-deck-*` and `config.yaml.bak-hermes-deck-*` | The installer rewrote the `plugins:` block of `config.yaml` in one-line form and dropped comments inside it; the backups keep them |

The checkout `/home/hermes/hermes-deck` is a plain clone of the Deck repo. It is not part of this repo.

### Connect the app

1. Get the token in your own terminal, not in chat: `tailscale ssh ubuntu@proto-agents`, then
   `sudo -u hermes grep '^DECK_RELAY_TOKEN=' /home/hermes/hermes-deck/relay/.env | cut -d= -f2`.
2. In the app: **Settings, Relay**: `ws://100.99.171.75:9330` and the token, then **Save and connect**.
3. For direct chat the app needs an SSH login as `hermes` (your Tailscale SSH rule must allow it), the Python path
   `/usr/bin/python3` and the stream path `/home/hermes/.hermes/hermes-deck/stream.py`.

### Check it works

```
cd /home/hermes/hermes-deck && /usr/bin/python3 -B scripts/doctor.py --home /home/hermes/.hermes \
  --hermes-dir /home/hermes/.hermes/hermes-agent --relay-dir relay --relay http://100.99.171.75:9330
```

It reads only. Every line should say `ok`. The relay line proves it answers, not that a token works. Then send the bot one
read-only message and check it is asked about normally (done on 2026-10-22: one from Telegram and one from Teams).

### Risks to know

- Anyone on your tailnet who has the token can read every agent's sessions and history. Do not put the relay on a public
  address.
- The events file holds copies of tool arguments. Treat it like the ledger.
- Letting `hermes` log in over Tailscale SSH gives that login the approval secrets the account can read
  ([backlog](backlog.md#before-real-books), separate ledger owner). Fine on the test VM; revisit before real books.
- Do not turn on Bot screen (`DECK_BOT_SCREEN`) or point `HERMES_CUA_DRIVER_CMD` at a driver: that opens the one path
  where the plugin's hook can change a call.
- Keep a gateway stop short. While the gateway was down for about 3 minutes during the install, another program took
  ports 8644 and 3978 ([never change plugins while the gateway runs](#never-change-plugins-while-the-gateway-runs)).

### Update, turn off, remove

- **Update:** pull the Deck clone, run the installer's `--dry-run`, read its plan, stop the gateway (no `parked`,
  `approved` or `executing` row), run the installer, restart. Re-do the hook review in
  [Hooks reviewed so far](#hooks-reviewed-so-far) if the hook changed. Keep app, relay and plugin on one version.
- **Stop the relay:** `systemctl --user disable --now hermes-deck-relay` (as `hermes`). Nothing else changes.
- **Remove the plugin:** `/usr/bin/python3 -B hermes-plugin/hermes-deck/install.py --profile default --uninstall --dry-run`,
  then without `--dry-run`, then remove its hook name from `observer_hooks` and restart the gateway.

## Add a form-checked tool

**Risk is a pipeline rule now.** Whether a call needs a person is decided by withHuman's entry pipeline for every
tool call; the plugin no longer has a list of tools that need approval. To make a tool need a person, add a rule
to the pipeline. The plugin's list only says which tools get the [form check](form-check.md) before withHuman is
asked (and which ones the form self-check watches).

Example: put a new gateway server's write tool (say DocuSign `createEnvelope`) through the full path. Every place
that has to agree:

1. **withHuman**: register the server (e.g. slug `docusign`) and grant it to agent `proto-agents`, granting only the
   gateway servers the agent needs (spec section 7, Q5) ([gateway-setup.md](gateway-setup.md#human-step-b-task-2-step-4-register-the-server)). Add a pipeline rule that
   sends its `park:docusign` request to a person, and add it to pipeline **block 2** (`block-06ebcdd6`), so the bridge decides its
   replays (it asks withHuman for the matching park, by the Hermes name `mcp__wh__<slug>__<tool>`) ([block 2](gateway-setup.md#human-step-c-task-7-step-5-endpoint-secret-pipeline)).
2. **Hermes MCP filter**: `deploy/hermes_mcp.sh` only lets `quickbooks__*`, `maverick__*`, `acceptblue__*`, `authnet__*`,
   `supabase-crm-dev__*`, `monday__*` and `docusign-demo__*` tools in (`TOOL_INCLUDE`),
   and fails if any other `mcp__wh__` tool registers (`ALLOWED_PREFIX_RE`). Allow the new server in both.
3. **Tool lockdown**: `deploy/hermes_tool_policy.py` only allows `mcp__wh__quickbooks__*`, `mcp__wh__maverick__*`,
   `mcp__wh__acceptblue__*`, `mcp__wh__authnet__*`, `mcp__wh__supabase_crm_dev__*`, `mcp__wh__monday__*` and `mcp__wh__docusign_demo__*`
   (`ALLOWED_TOOL_PREFIXES`; plus `clarify`,
   `parked_action_execute` and, once Tool Search is on, the three bridge tools `tool_search`, `tool_describe`, `tool_call`); tools open only through the [spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) section 4.4 (the allowed
   list is `/etc/durable-park/allowed_toolsets`, spec §9 D15), not a code edit.
4. **Form-checked list**: the plugin form-checks tools matching the form-checked patterns (setting
   `plugins.entries.durable-park.settings.form_checked_tools`, see [Plugin settings](#plugin-settings); default
   `DEFAULT_FORM_CHECKED_TOOLS` in `plugin/durable_park/park_hook.py`). DocuSign's six write tools and every Maverick,
   accept.blue and Authorize.net tool are already in the default (those three last: index 0 stays the QuickBooks pattern the pipeline test pins). Leaving a tool off this list does **not** let it through: it is still asked about.
5. **Docs**: the pipeline table and system map in [architecture.md](architecture.md), this runbook, the setup guide.
6. **Test**: unit tests for the new pattern, then one live park, approve and run.

The form check needs nothing: it reads each tool's rules from Hermes automatically.

## Check durable-park against the real Hermes

CI has no Hermes, so `tests/test_hermes_integration.py` is skipped there. It runs the real Hermes functions that decide
what the park hook receives for a Tool Search `tool_call` (`agent.tool_executor._parse_tool_call`) and the real bridge
tool names, and pins the unit tests' copies of both to them. Run it on the VM after a Hermes upgrade and before
turning Tool Search on. It needs no gateway restart and no config change: `HERMES_HOME` is an empty scratch directory
and Hermes code is only imported. The Hermes venv has no pytest, so borrow the pure-Python pytest packages from the
durable-park venv (from a worktree, never the live clone's tree):

```sh
cd /home/hermes/tapdash-agents-wt/<name>/durable-park
PY=$(python3 -I -c 'import glob,json,os; f=glob.glob(os.path.expanduser("~/.hermes/installs/*/facts.json"))[0]; print(json.load(open(f))["packages"]["venv"]["environment"]+"/bin/python")')
SP=$(ls -d ~/tapdash-agents/durable-park/.venv/lib/python3*/site-packages); T=$HOME/scratch-pytest
mkdir -p $T/libs && for m in _pytest pytest pluggy iniconfig pygments py.py; do ln -sfn $SP/$m $T/libs/$m; done
mkdir -p $T/stubbin && printf '#!/bin/sh\nexit 1\n' > $T/stubbin/systemctl && chmod +x $T/stubbin/systemctl
PYTHONPATH=$HOME/.hermes/hermes-agent:$T/libs PATH=$T/stubbin:$PATH \
  $PY -m pytest -q -W error -p no:cacheprovider --basetemp=$T/base tests/test_hermes_integration.py
```

The `stubbin` line makes a fake `systemctl` (a script that just exits 1) inside the scratch folder and puts it first on
the `PATH`, so the test cannot reach the real systemd units. Nothing needs to exist beforehand (the old
`/home/hermes/stubbin` was removed on 2026-10-22).

It must pass with no skips (a skip means Hermes was not importable). A failure after an upgrade means Hermes changed
what the hook receives: read [section 4b](architecture.md#4b-hermes-tool-search-the-bridge-tools-in-the-hook-path)
and fix the plugin before turning Tool Search on. Without Hermes on the path the module skips, which is what CI sees.

## Turn on Tool Search

Hermes's Tool Search hides the 142 QuickBooks tool schemas (about 41-60k input tokens in every new chat) behind three
bridge tools, `tool_search`, `tool_describe` and `tool_call`; the model sees a name listing and loads a schema only
when it needs it. It is **on** on proto-agents since 2026-10-21 19:59 UTC (what was checked:
[the record](acceptance-2026-10-21-tool-search.md)); the steps below switch it on again, or off. How it fits the hook:
[architecture section 4b](architecture.md#4b-hermes-tool-search-the-bridge-tools-in-the-hook-path).

**The setting.** One key, in `~/.hermes/config.yaml`, set through Hermes's own CLI as the `hermes` user:

```yaml
tools:
  tool_search:
    enabled: "on"     # was "off"; leave threshold_pct, listing, listing_max_tokens and defer at their defaults
```

`hermes_tool_policy.py apply` (run by `configure_hermes.sh` and `hermes_mcp.sh`) keeps an `on` an admin set and writes
`off` for anything else (including `auto`, which Hermes treats as `on`), so a later re-apply does not undo it. Turn it
off with `hermes config set tools.tool_search.enabled off`.

**Before:**

1. [`tests/test_hermes_integration.py` passes against the real Hermes](#check-durable-park-against-the-real-hermes).
   The code of this change is live (a merged branch, [deploy a change](#deploy-a-change)), so the plugin exempts the
   two catalog tools and refuses a `tool_call` wrapper and the policy check knows the bridge. With older code the
   check fails (`LEAK tool_call`...) and every call is refused: safe, but nothing works until the code is live.
2. No ledger row is `parked`, `approved` or `executing` (the query under [Open a toolset](#open-a-toolset), step 1; a
   restart would strand it).

**Switch on:**

```sh
cp -p ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-tool-search-$(date -u +%Y%m%dT%H%M%SZ)
~/.local/bin/hermes config set tools.tool_search.enabled on
PY=$(python3 -I -c 'import glob,json,os; f=glob.glob(os.path.expanduser("~/.hermes/installs/*/facts.json"))[0]; print(json.load(open(f))["packages"]["venv"]["environment"]+"/bin/python")')
$PY -I ~/tapdash-agents/durable-park/deploy/hermes_tool_policy.py check | grep -E '^(cli|telegram):|^tool policy'
```

`check` must end `tool policy: ok on <N> platform keys (...; Tool Search on: the bridge tools are visible on <N> of
them)`, and the `cli:` line must read `clarify, tool_call, tool_describe, tool_search  (behind the Tool Search bridge:
parked_action_execute...)`. If it fails, set the key back to `off` (or restore the backup) and read the problem in
the table under [Tool policy check failed](#tool-policy-check-failed). Then `systemctl --user restart hermes-gateway`
(the start runs the check again and the plugin refuses every call if it fails).

**Verify** (new chat in Telegram or Teams; the live check, in this order):

1. `~/.hermes/logs/agent.log` shows a `tool_search activated (tier 1)` (or `tier 2`) line `... deferred`; the gateway log has
   `durable-park loaded` and no `tool policy check failed`.
2. A read: ask for the QuickBooks company info. The model uses `tool_search` / `tool_describe` / `tool_call`. In the
   ledger there is **one** row, for `mcp__wh__quickbooks__get_company_info`, and **no row** for `tool_search`,
   `tool_describe` or `tool_call`:

   ```sh
   python3 - <<'PY'
   import sqlite3
   c = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro", uri=True)
   for r in c.execute("select tool, status, count(*) from parks group by tool, status order by tool"):
       print(r)
   PY
   ```
3. A money move (a sandbox `create_payment`): the chat gets `PARKED P-...: "mcp__wh__quickbooks__create_payment"`
   (the real name), the withHuman request names the same tool and the same arguments, and the row's tool is that name.
   Approve it: the wake arrives (its text ends the executor instruction with "If parked_action_execute is not in your
   tool list, call it through tool_call."), the model finds `parked_action_execute` (through `tool_describe` and
   `tool_call`) and the row ends `executed`. If the model cannot find it, turn tool search off again (an approved row
   stays approved and runs after the restart) and report it.
4. **Run one approved park end to end and confirm it runs once.** With the park from step 3 approved, wait for the
   chat's answer, then run the ledger query above: that park's row must read `executed` (not `approved`, not
   `executing`, not `unknown`) and QuickBooks (the sandbox) shows exactly one payment for it. Ask the model in the
   same chat to run it again: `parked_action_execute` must answer `... is already running or done` and nothing new
   appears in QuickBooks. Any other result: turn tool search off again and report it.
5. `~/.hermes/logs/errors.log` has no `reached the park hook still wrapped` line. One now and then is a malformed call
   the model retried; a stream of them means the model keeps batching or naming core tools through `tool_call`:
   look at the chat, not at durable-park.
6. Cost: a new chat's input tokens (Hermes dashboard, session usage) are far lower than before.

**Turn off:** with no row waiting or running, `hermes config set tools.tool_search.enabled off`, restart the gateway,
run the [health check](#health-check). Nothing else changes.

## Open a toolset

The v1 tool lockdown is applied on proto-agents. The
allowed toolsets live in one root-owned file, `/etc/durable-park/allowed_toolsets`
([format](receiver-setup.md#agent-tool-policy-v1-lockdown-re-applied-on-proto-agents)).

1. Check no ledger row is `parked`, `approved` or `executing` (a gateway restart would strand it), with the
   ledger query from the [health check](#health-check) ("Waiting or stuck rows"); it must print nothing:

   ```sh
   python3 - <<'PY'
   import sqlite3
   c = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro", uri=True)
   for r in c.execute("select park_id, tool, status, created_at from parks "
                      "where status in ('parked','approved','executing') order by created_at"):
       print(r)
   PY
   ```
2. As admin, add the toolset's name on its own line (for example `web`), save.
   `terminal`, `file`, `code_execution`, `delegation`, `cronjob` and `computer_use` are refused until
   Clean Setup Phase 2; a typo is refused too (exit 2, message on stderr, nothing changed).
   `skills` may be opened (the agent reads the Maverick, accept.blue and Authorize.net how-to notes with `skills_list` and `skill_view`), but only
   while the entry pipeline denies `skill_manage`, which writes skill files: `check` reads
   `deploy/withhuman/entry-pipeline.json` and fails (`skills: LEAK ...`) if any block before the deny
   (`pa-hermes-local-deny`) could approve or ask about a `skill_manage` call. Keep `skill_manage` in that block.
   `check` reads the reviewed file, not withHuman. So before opening `skills`, also confirm in withHuman
   (`get_pipeline entry`, or the app) that the **active** revision is the one named in
   [architecture.md](architecture.md) and that `pa-hermes-local-deny` there still lists `skill_manage` ahead of
   every block that approves.
   Not Phase-2-gated but still risky: `desktop_ui`, `catalog`, `browser-cdp`, `a2a`, `connections`; open them
   through the file only after their pipeline rule exists ([spec section 4.4](../../docs/specs/2026-10-21-all-tools-withhuman-design.md)).
   An allow file the `hermes` user cannot read (for example a pre-existing one with mode 600 root:root) makes
   `apply` exit 2 and `configure_hermes.sh` exit 2 too (nothing applied, nothing restarted; `hermes_mcp.sh` stops
   as well), and at gateway start the report says `invalid allow file: <reason>`; fix it with
   `chgrp hermes` and `chmod 640` on the file (or, if it is empty, re-create it with
   `install -m 640 -o root -g hermes /dev/null /etc/durable-park/allowed_toolsets`).
3. Re-run `sudo bash deploy/hermes_mcp.sh`: it re-applies the `wh` server settings, runs `hermes_tool_policy.py
   apply`, then `check` (before the restart; a failure stops it with nothing restarted), restarts `hermes-gateway`
   and checks again. It needs the provisioned instance token. Without it, run `apply` then `check` by hand as the
   `hermes` user in Hermes's own Python (`<hermes env python> -I deploy/hermes_tool_policy.py apply`, then
   `... check`; the Python is the `venv.environment` path in `~/.hermes/installs/*/facts.json`). `check` must end with
   `tool policy: ok`.
4. Restart `hermes-gateway`: its start runs `check` itself (the drop-in, see [Tool policy check
   failed](#tool-policy-check-failed)) and the plugin refuses every call if it fails. Run `check` once more by hand
   (`--status-file PATH` to leave the result as JSON; any path except the live report, `tool_policy_status.json`).

To close it again, remove the line and repeat from step 3.

## Secrets: Bitwarden

In plain English: the agent's passwords (its keys) used to sit in a file on the VM. Now they sit in a locked online
safe called Bitwarden. Each time Hermes starts, it opens the safe, takes the keys it needs and uses them. The only
key left in the file is the one that opens the safe.

**Where the keys live.** Hermes pulls the agent's keys from **Bitwarden Secrets Manager** at every start, with
Hermes's built-in support (`hermes secrets bitwarden ...`; Hermes docs: `website/docs/user-guide/secrets/bitwarden.md`
in the Hermes repo). Verified live on 2026-10-22/23.

| Thing | Value |
|---|---|
| Service | Bitwarden US cloud, `https://vault.bitwarden.com`, free plan |
| Project | "TapDash Agents" (id `94af3d93-ad3d-486b-908d-b4ec00050dfd`) |
| Who opens it | A machine account with an access token. The owner will switch it to read-only ([backlog](backlog.md#secrets-bitwarden-follow-ups)) |
| `bws` command line tool | 2.0.0, installed by Hermes's package manager at `~hermes/.hermes/tools/bws-2.0.0-linux-x64/bws` |
| Startup log | `Bitwarden Secrets Manager: applied 6 secrets` and `Command helper: applied 2 secrets` |

**The 6 keys in Bitwarden:** `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET`, `TEAMS_TENANT_ID`, `TELEGRAM_BOT_TOKEN`,
`HERMES_LANGFUSE_PUBLIC_KEY`, `HERMES_LANGFUSE_SECRET_KEY`. They were removed from `~hermes/.hermes/.env`.

**Still in `~hermes/.hermes/.env` on purpose:**

- `BWS_ACCESS_TOKEN`: the one bootstrap token. It opens the safe, so it cannot be inside the safe.
- `TEAMS_ALERT_WEBHOOK_URL`: read by the sweep script, not by Hermes's secret loader.
- Settings that are not secret: allowed users, home channels, `TEAMS_HOST`, the Langfuse base URL and options.

**durable-park's own secrets did not move.** `/etc/durable-park` (`instance_token`, `receiver.json`, `wake_secret`,
`bridge_secret`, `audit_key`, `tunnel.env`) and the Hermes `command` secret source
`/opt/durable-park/libexec/hermes-secrets` (it supplies 2 secrets) stay as they were. They are deliberately **not** in
the agent's Bitwarden project, so the agent's access token cannot read them. The owner keeps copies in his personal
password vault. See [architecture section 7](architecture.md#7-secrets-and-who-can-read-them).

**Settings** in `~hermes/.hermes/config.yaml`, under `secrets.bitwarden`:

| Setting | Live value | Why |
|---|---|---|
| `enabled` | `true` | Turns the loader on |
| `project_id` | the project above | Only this project's keys are read |
| `server_url` | `https://vault.bitwarden.com` | US cloud |
| `override_existing` | `true` (the default) | A key from Bitwarden wins over the same name in `.env` |
| `encrypted_cache.enabled` | `true` | Keeps an encrypted copy of the last good answer |
| `encrypted_cache.max_stale_seconds` | `604800` (7 days) | If Bitwarden cannot be reached at start, the last good copy is used for up to 7 days. An authentication failure never uses the cache |

**The `.env` backups** (`~hermes/.hermes/.env.bak*`) were kept. The 6 key values in them were replaced by a note line
after they were checked to be identical to the ones in Bitwarden.

Run all of these as `hermes` (`sudo -u hermes -H ...`), never print a key, and check the ledger first when a step
restarts the gateway ([health check](#health-check)).

### Change a key

1. Edit the key in the Bitwarden web app.
2. Check the ledger: no row `parked`, `approved` or `executing` ([health check](#health-check)).
3. Restart `hermes-gateway`. Keys load only at start, so nothing changes until then.

### Swap the access token

1. In Bitwarden, create a new access token for the machine account.
2. Run `sudo -u hermes -H hermes secrets bitwarden token`. It asks for the token in a masked prompt: type or paste it
   there, never into chat.
3. Check it ([below](#check-that-bitwarden-works)), then revoke the old token in Bitwarden.

### Check that Bitwarden works

- `sudo -u hermes -H hermes secrets bitwarden status`
- `sudo -u hermes -H hermes secrets bitwarden sync`: a dry run.
- After a gateway start, the log says `Bitwarden Secrets Manager: applied 6 secrets` and `Command helper: applied 2 secrets`.

### Turn it off

1. Put the 6 keys back into `~hermes/.hermes/.env` first (typed in with `read -rs`). Without them Telegram and Teams
   do not start.
2. `sudo -u hermes -H hermes secrets bitwarden disable`
3. Check the ledger, then restart `hermes-gateway`.

### Not checked

- The access token was pasted in chat during setup (2026-10-22). Treat it as exposed and swap it
  ([backlog](backlog.md#secrets-bitwarden-follow-ups), and the "rotate secrets before production" list below, TAP2-109).
- The "Read traces" and "Find everything about one approval" scripts under [Tracing (Langfuse)](#tracing-langfuse)
  were written when the Langfuse keys were in `.env`. They were not re-run after the move.

## Maverick

In plain English: Maverick is the payments system TapDash boards merchants on. Its MCP server runs inside the same
tunnel container as QuickBooks, so the agent reaches it only through withHuman, like QuickBooks. Setup details:
[gateway setup](gateway-setup.md#maverick-server-source).

### Maverick settings (placeholders until sandbox access)

Until Maverick gives us sandbox access, the tunnel still needs `/etc/durable-park/maverick.env`, because the tunnel
refuses to start without it. Yaakov runs, in a terminal on the VM (not screen-shared):

```sh
sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/enter-maverick-secrets.sh
```

and types the placeholders: `pending` for the token and `https://maverick.invalid` for both URLs. It prints
`saved 3 values`. The `.invalid` name never resolves, so every Maverick call fails at once (the address cannot be
looked up) with a clear error from the server, never silently. A lookup says nothing changed. A change tool says
it is not known whether Maverick made the change (the server cannot know the address is a placeholder), so an
approved Maverick change replayed under the placeholders ends as an `unknown` ledger row that a person clears
(see [architecture](architecture.md)); that is expected, not a bug, and with this address nothing left the VM. When the real values arrive, run the script again (it asks before
overwriting) and recreate the tunnel (no ledger row `parked`, `approved` or `executing`, then
from `deploy/tunnel`: `sudo docker compose up -d --no-build wh-tunnel`).

Never `cat` the file or run `docker compose config`, `docker inspect` or `docker exec ... env`: they print it.

### Maverick switch-on (in this order)

1. Merge the reviewed change and fast-forward the live clone ([Deploy a change](#deploy-a-change)).
2. Root's checkout at the pinned commit ([gateway setup](gateway-setup.md#maverick-server-source)); `sudo bash
   deploy/tunnel/build.sh` builds the image.
3. The placeholder settings above (`maverick.env`).
4. No ledger row `parked`, `approved` or `executing` ([health check](#health-check)); then from `deploy/tunnel`:
   `sudo docker compose run --rm check` (lists 42 `maverick_` tools) and `sudo docker compose up -d --no-build wh-tunnel`.
5. In withHuman: register gateway server `maverick` through tunnel `proto-agents`, tunnel entry `maverick`, one scope
   `default` (auth none); grant it to agent `proto-agents`. The approval rules were entry revision 15 at the time (revision 16 since 2026-10-25 16:35 UTC).
6. Open `skills` in `/etc/durable-park/allowed_toolsets` ([Open a toolset](#open-a-toolset)).
7. `sudo bash deploy/hermes_mcp.sh`: it restarts the gateway, checks the tool scope and policy, and only then
   checks that `mcp__wh__maverick__maverick_read` registered (so step 5 must come first).
8. Test in Teams: a lookup runs without asking; a refund asks a person (deny it).

## accept.blue and Authorize.net

In plain English: these are two more TapDash payment servers, one for accept.blue and one for Authorize.net. Like
Maverick, each runs inside the tunnel container, so the agent reaches them only through withHuman. **It was switched on
on 2026-10-25, with placeholder settings (image live 16:31 UTC, servers registered 16:32, rules revision 16 live 16:35); the record is [acceptance-2026-10-25-acceptblue-authnet.md](acceptance-2026-10-25-acceptblue-authnet.md).** What each step needs and why:
[gateway setup](gateway-setup.md#payment-server-sources-acceptblue-and-authorizenet).

What the agent may do without a person is only what each server itself marks `auto`, and none of it moves money or
stores a card:

| Server | `auto` (no person; the same list in the park and gateway rules) | Everything else |
| --- | --- | --- |
| accept.blue (28 tools, names start `ablue_`) | `describe_lookup`, `read`, `search_lookups`, `create_customer`, `create_customer_from_transaction`, `update_customer`, `create_invoice` (a draft only: nothing is sent or charged) | 21 tools, **a person approves each**: charges, refunds, voids, captures, saved payment methods, sending, changing or cancelling invoices, recurring schedules, webhooks, deletes |
| Authorize.net (24 tools, names start `authnet_`) | `describe_lookup`, `read`, `search_lookups`, `save_customer_profile`, `save_shipping_address` | 19 tools, **a person approves each**: charges, refunds, voids, captures, subscriptions, payment forms, deleting records, webhooks, test mode |

### Settings (placeholders until access)

The tunnel will not start without `/etc/durable-park/acceptblue.env` and `/etc/durable-park/authnet.env`. Yaakov runs, in a
terminal on the VM (not screen-shared), one script for each:

```sh
sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/enter-acceptblue-secrets.sh
sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/enter-authnet-secrets.sh
```

(before the live clone has these scripts, run them from the reviewed worktree, see the switch-on order) and types the placeholders: for accept.blue `pending` for the source key, `pending` for the PIN and
`https://api.sandbox.accept.blue/api/v2` for the address (it must be an accept.blue address: the server refuses any
other, so a made-up `.invalid` address does not work here); for Authorize.net `pending` for the login id and `pending` for the
transaction key. The scripts print `saved 3 values` and `saved 2 values`. With these, the sandbox simply refuses every
call, a person is still asked first, and nothing leaves the VM except those refused calls to the two sandboxes. When the real
sandbox values arrive, run the script again (it asks before overwriting) and recreate the tunnel (no ledger row `parked`,
`approved` or `executing`, then from `deploy/tunnel`: `sudo docker compose up -d --no-build wh-tunnel`). Authorize.net stays on its
sandbox: `AUTHNET_ENV=sandbox` is fixed in `tunnel.yaml`, and going live is a separate reviewed change.

Never `cat` these files or run `docker compose config`, `docker inspect` or `docker exec ... env`: they print the values.

### accept.blue and Authorize.net switch-on (in this order)

Done in this order on 2026-10-25 ([the record](acceptance-2026-10-25-acceptblue-authnet.md)). Keep the steps for a rebuild or a new commit.

**Why the order matters.** The running tunnel container reads `tunnel.yaml` from the live clone. The moment the live
clone is fast-forwarded to the reviewed change, `tunnel.yaml` asks for two servers (`acceptblue-mcp-server`,
`authnet-mcp-server`) that the old image does not have. If the old container restarted in that gap, it could not start,
and QuickBooks and Maverick would be offline until the new image runs. So do everything that does not touch the live clone
first (steps 1 and 2), then do steps 3 to 6 back to back, without stopping in between.

1. **Before the live clone changes.** For each server, a read-only deploy key and root's checkout at the pinned commit
   ([gateway setup](gateway-setup.md#payment-server-sources-acceptblue-and-authorizenet)). Read the pinned commits from the
   reviewed branch (`git -C /home/hermes/tapdash-agents-wt/ab-an show origin/feat/acceptblue-authnet-tunnel:durable-park/deploy/tunnel/acceptblue-mcp.sha`,
   and `authorizenet-mcp.sha`), not from the live clone, which does not have them yet.
2. **Settings, still before the live clone changes.** Yaakov runs the two scripts from the reviewed worktree
   (`sudo bash /home/hermes/tapdash-agents-wt/ab-an/durable-park/deploy/tunnel/enter-acceptblue-secrets.sh`, then the same
   with `enter-authnet-secrets.sh`; the live clone does not have them yet; they only write `/etc/durable-park/acceptblue.env` and `authnet.env`, which the running tunnel
   does not read until it is recreated). Placeholders: see Settings above.
3. **Back to back from here** (no ledger row `parked`, `approved` or `executing`, [health check](#health-check)).
   Merge the reviewed change and fast-forward the live clone ([Deploy a change](#deploy-a-change)). Do not restart the
   gateway now: `hermes_mcp.sh` (step 8) does that.
4. From `deploy/tunnel`: `sudo bash build.sh` builds the image `durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-d6f43f9-ab-9bb8d72-an-4dd4278` (it refuses a checkout that is not root's,
   another commit or not clean). **Then it proves the approvals**: it runs `--list-approvals` for the Maverick, accept.blue and
   Authorize.net launchers in the new image and compares each with the committed `*-approvals.json` file (28 and 24 tools,
   7 + 21 and 5 + 19 for the two new ones). Any difference stops it with an error and a diff: do not run that image
   ([gateway setup](gateway-setup.md#the-acceptblue-and-authorizenet-servers-in-the-image)).
5. `sudo docker compose run --rm check` (it should list `acceptblue: ok, 28 tools` and `authnet: ok, 24 tools` next to the
   others).
6. `sudo docker compose up -d --no-build wh-tunnel` (`--no-build`: compose must never build a missing image on its own, that
   would skip the checks in `build.sh`). Watch the tunnel come back **online**. If a step from 4 to 6 fails, fix it and go on, or
   roll back (below); do not leave the live clone ahead of the image.
7. In withHuman: register gateway servers `acceptblue` and `authnet` through the tunnel (tunnel server names `acceptblue`
   and `authnet`) and grant both to agent `proto-agents` ([HUMAN STEP B2](gateway-setup.md#human-step-b2-register-the-acceptblue-and-authorizenet-servers)).
   Then save `deploy/withhuman/entry-pipeline.json` as a new entry revision (the file has 31 blocks; revision 15 had 25,
   and this became revision 16 on 2026-10-25) and activate it. Check with `preview_pipeline`: a `park:acceptblue` call to
   `mcp__wh__acceptblue__ablue_read` and a `park:authnet` call to `mcp__wh__authnet__authnet_read` approve, and a
   `park:acceptblue` call to `mcp__wh__acceptblue__ablue_refund_transaction` and a `park:authnet` call to
   `mcp__wh__authnet__authnet_refund_transaction` ask a person. Do this **before** step 8, so the agent never sees the
   servers without their rules.
8. `sudo bash deploy/hermes_mcp.sh`: it checks the three skills folders, restarts the gateway, checks the tool scope and policy,
   and only then checks that `mcp__wh__acceptblue__ablue_read` and `mcp__wh__authnet__authnet_read` registered. If one is not
   registered (step 7 not done), the script exits 1 with a message and the gateway **stays up**, without the two servers: the
   scope and policy checks have already passed by then, so that is safe. Do step 7 and run the script again.
9. Test in Teams: a lookup runs without asking; a refund asks a person (deny it).

Not wired on purpose: Authorize.net's webhook receiver (no `AUTHNET_WEBHOOK_LISTEN`, no signature key, no open port).

### Roll back the two servers

- **The pipeline rules misbehave**: `activate_pipeline_revision` to revision 15 (revision 16 has been live since 2026-10-25 16:35 UTC). The two servers then ask a person for every call
  (the money-server rule names both), which is safe and noisy.
- **The tunnel image misbehaves**: from `deploy/tunnel`, check out the previous `docker-compose.yml`, `tunnel.yaml` and `Dockerfile`
  (so the tag is `durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-d6f43f9` again), no ledger row `parked`, `approved` or `executing`,
  then `sudo docker compose up -d --no-build wh-tunnel`. That image is still on the VM. Then run `deploy/hermes_mcp.sh` from the matching
  checkout, or the agent keeps a list that names tools that are gone.

## Rotate secrets before production

Several secrets were pasted into chat during testing (approved for testing only). Before any real books:

- [ ] Revoke the withHuman provisioner token; re-provision the agent instance (new `instance_token` and receiver
      secret): [receiver-setup.md](receiver-setup.md#step-6--admin-provisioner-token-then-provision-human-step)
- [ ] Rotate the Intuit app client secret and refresh token; re-run OAuth; wipe the `durable-park-qbo-data`
      volume: [gateway-setup.md](gateway-setup.md#environment-variables)
- [ ] Issue a new withHuman tunnel token
- [ ] Replace the Maverick placeholders (or any Maverick token that was ever shared) with real values:
      [Maverick settings](#maverick-settings-placeholders-until-sandbox-access)
- [ ] Replace the accept.blue and Authorize.net placeholders (or any key of theirs that was ever shared) with real values:
      [accept.blue and Authorize.net settings](#settings-placeholders-until-access)
- [ ] Rotate the `durable-park-bridge` webhook endpoint secret and re-store `bridge_secret` (`root:root 0600`), then
      `sudo systemctl restart durable-park-bridge` ([how](#store-or-rotate-the-bridges-keys))
- [ ] Regenerate `wake_secret`
- [ ] Rotate the withHuman org key (`audit_key`; the bridge needs it, so it is always set): revoke it in withHuman
      and [store a new one](#store-or-rotate-the-bridges-keys) with `audit.read`, `request.read`, `request.read.unrouted`,
      then `sudo systemctl restart durable-park-bridge` (it copies the key at start)
- [ ] Rotate the Langfuse keys (`HERMES_LANGFUSE_PUBLIC_KEY`, `HERMES_LANGFUSE_SECRET_KEY`, if set): [Rotate the keys](#rotate-the-keys)
- [ ] Swap the Bitwarden access token (it was pasted in chat on 2026-10-22): [Swap the access token](#swap-the-access-token), then switch the machine account to read-only in Bitwarden
- [ ] Revoke any GitHub token shared in chat; keep only the VM's deploy key for this repo
- [ ] Then the rest of the [backlog's "Before real books" list](backlog.md#before-real-books)

## Rollback

- **A pipeline rule sends everything to a human, or approves too much (withHuman)**: `activate_pipeline_revision`
  to the previous revision (seconds). Never activate rev 8 while the ask-every-call code is live: rev 8 with that
  code auto-approves `hermes`/`mcp:` calls. The audit entry shows which block decided, so fix that rule.
- **Clean Setup Phase 1 (spec section 6, row 4.1; done 2026-10-22 02:22 UTC; these are the steps to undo it)**. Keep an open session for the whole change.
  - Connector unit: over `tailscale ssh ubuntu@proto-agents`, restore the previous unit file (`/root/hermes-shell-mcp.service.bak`),
    `sudo systemctl daemon-reload`, then `sudo systemctl restart hermes-shell-mcp`. The old OAuth store is not reusable
    across users: re-authorize the connector in Claude.
  - sshd (password login is off: `sshd -T` says `passwordauthentication no`): run `sudo sshd -t` before any reload; revert the one line in
    `/etc/ssh/sshd_config.d/50-cloud-init.conf`; keep the open session until `sudo sshd -T` shows the right value.
  - Temporary sudo for hermes (`/etc/sudoers.d/90-hermes` was removed): recreate it with `sudo install -m 440`, fix the step
    so it runs as ubuntu, then delete the file again.
- **The one-agent merge (2026-10-21)**: the configs, units and ledger from before the cutover are in
  `/home/hermes/merge-backup-20261021T021246Z/` (`hermes/` and `ubuntu/`). You cannot go back to the old `ubuntu`
  install: it was **deleted on 2026-10-22** (TAP2-112, [what happened](architecture.md#0-how-strong-is-it-today)), and it must
  not be rebuilt, because it ran an unguarded agent. What survives is a backup of its chat history, config files,
  skills and logs, `/home/ubuntu/old-hermes-backup-2026-10-22.tar.gz` (being moved to Google Drive; no secrets and no
  program files in it): read from it, never run it as a second agent. To undo only the `hermes` side of the merge:
  `systemctl --user disable --now hermes-dashboard`, copy `hermes/config.yaml` and `hermes/.env` from the merge backup
  over `~/.hermes/`, and restart `hermes-gateway` (locked-down tools again, no Teams). The config rollback does not
  undo the venv switch: the install's `facts.json` now points at venv `58c7a333` (the Teams packages) and the old venv
  `e74ec07b` still exists. Only with nothing `parked`, `approved` or `executing`. Plan:
  [docs/plans/2026-10-20-one-agent-merge.md](../../docs/plans/2026-10-20-one-agent-merge.md).
- **The patched Langfuse plugin (once deployed)**: go back to the unpatched catalog plugin with the rollback in
  [Switch to the patched plugin](#switch-to-the-patched-plugin-deploy): enable `langfuse`, disable `tapdash-langfuse`
  **with the gateway stopped** (never with it running, see the [warning](#never-change-plugins-while-the-gateway-runs)),
  and make `observer_hooks` match the plugin that is loaded (the old name goes back first, the new name comes off only
  after a restart), each stop/start with nothing `parked`, `approved` or `executing`.
- **Code**: `git revert <commit>` in the clone, push, then make it live as in [Deploy a change](#deploy-a-change).
  For the bridge and receiver that includes copying the reverted files to `/opt/durable-park` again: the root
  copies do not change by themselves.
- **The October 2026 move to the GitHub clone**: the previous standalone repo is kept at
  `/home/hermes/durable-park.pre-github`. To go back: `rm /home/hermes/durable-park` (the link), move
  `.venv` and `.superpowers` back from `tapdash-agents/durable-park/`, rename `durable-park.pre-github` to
  `durable-park`, restart the gateway. Delete the old copy once the clone has run cleanly for a while.
