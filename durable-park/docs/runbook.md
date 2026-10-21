# durable-park runbook

How to check, change and fix durable-park on the test VM. For how it works, see [architecture.md](architecture.md).

- [Access and ground rules](#access-and-ground-rules)
- [Health check](#health-check)
- [Deploy a change](#deploy-a-change)
- [Alerts and what to do](#alerts-and-what-to-do)
- [Troubleshooting](#troubleshooting)
- [Plugin settings](#plugin-settings)
- [Add a form-checked tool](#add-a-form-checked-tool)
- [Rotate secrets before production](#rotate-secrets-before-production)
- [Rollback](#rollback)

## Access and ground rules

| What | How |
|---|---|
| VM | `proto-agents` (OVH VPS, Ubuntu 26.04, Hermes v0.22.2). Admins: `tailscale ssh ubuntu@proto-agents` (passwordless sudo). |
| AI agents (e.g. Claude) | Current route: the Proto-Agents connector (see the last row), which runs as `hermes` with passwordless sudo. Older route (jump host, production; not for this VM): through the Hermes Server connector, piping scripts into `/tmp/dp-tools/vm` (runs as `hermes`) or `/tmp/dp-tools/vmroot` (root) on the jump host. |
| Repo on the VM | `/home/hermes/tapdash-agents` (a clone of this repo). `/home/hermes/durable-park` links to its `durable-park/` folder. |
| withHuman | app.withhuman.ai: agent `proto-agents`, gateway server `quickbooks`, webhook endpoint `durable-park-bridge`, entry pipeline. |
| Telegram test bot | `@TapDashAgentsBot`, home channel = the admin's DM with the bot (where alerts go). |
| Teams bot | "TapDash Agent", served by the same `hermes` gateway on `127.0.0.1:3978`; the devtunnel `proto-agents-bot` (user `ubuntu`, unit `devtunnel-hermes`) forwards to it. |
| Web dashboard | <http://proto-agents.taila95862.ts.net:9119/> (same as `http://100.99.171.75:9119/`; Tailscale only, not public), user `TapDash`; systemd user unit `hermes-dashboard` of user `hermes`. |
| Claude connector "Proto-Agents" | system service `hermes-shell-mcp` (`/home/hermes/hermes-shell-mcp`, `127.0.0.1:8792`, Tailscale Funnel path `/hermes-shell`, GitHub OAuth for one allowed account). Stopping it cuts Claude's access to the VM. |

Rules:

- **Never change the production Hermes host.** The jump host is production; only the VM is for testing.
- When piping scripts through the connector, give every heredoc a **unique** delimiter (e.g. `<<'XEOF_7'`) and
  never reuse it inside. A reused `EOF` once ran the tail of a script on the production host.
- **Never print a secret.** Secrets are typed in by an admin with `read -rs` (never pasted into chat).
- Root steps (copying into `/opt`, restarting system units) should be done by an admin in their own terminal. That is
  a convention, not a block: the agent has no terminal tool today, so only admins, and Claude through the
  Proto-Agents connector (which runs as `hermes` with passwordless sudo), do root steps.
- **One agent, v1 lockdown re-applied (since 2026-10-21 03:16 UTC).** The `hermes` install is the only agent on the VM.
  Since 2026-10-21 03:16 UTC the v1 tool lockdown is re-applied on proto-agents as a stopgap: the agent can reach only
  `clarify`, `parked_action_execute` and `mcp__wh__quickbooks__*` (58 platform keys; `hermes_tool_policy.py check` ok).
  Hermes approvals stay off. The user `hermes` still has passwordless sudo until Clean Setup Phase 1
  ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1); the lockdown is a configuration guard,
  not a permission boundary. Tools open again only through that spec (§4.4). Re-running `configure_hermes.sh` or
  `hermes_tool_policy.py` re-applies the current state and restarts the gateway, so the "no parked/approved/executing
  row" rule applies.
  durable-park asks withHuman about every tool call except `parked_action_execute` and `clarify` (withHuman's
  pipeline decides which need a person); today that means the QuickBooks tools, since the agent has no terminal or
  file tools. **This is not a security boundary today:** with passwordless sudo, an agent that got
  such a tool could read `/etc/durable-park/*` (QuickBooks secret and refresh token, withHuman tunnel token), change
  `/opt/durable-park`, forge ledger rows and call QuickBooks directly, bypassing withHuman. Accepted by Yaakov on
  2026-10-20 for the exploration phase on a QuickBooks sandbox; containing the agent's tools comes before real
  credentials ([backlog](backlog.md#before-real-books)). The old `ubuntu` user's
  Hermes install (gateway and dashboard units) is stopped and kept as an archive; do not start it next to this one.
- **Restarting `hermes-gateway` also restarts the Teams bot** (and briefly drops Telegram). The dashboard is its own unit
  and keeps running. The "no `parked`, `approved` or `executing` row" rule below still applies before any restart.
- QuickBooks is a **sandbox** company. Never connect real books before the [rotation](#rotate-secrets-before-production)
  and the open v2 items are done.

## Health check

As `hermes` on the VM:

```sh
systemctl --user is-active hermes-gateway hermes-dashboard         # active active
systemctl is-active hermes-shell-mcp                               # active
systemctl is-active durable-park-receiver durable-park-bridge      # active active
sudo docker ps --filter name=wh-tunnel --format '{{.Status}}'      # Up ...
curl -s http://127.0.0.1:8644/health                               # {"status": "ok", ...}
cat ~/.hermes/plugin-data/durable-park/formcheck_status.json       # "ok": true, recent checked_at
hermes cron list | grep -A3 durable-park-sweep                     # last run ok
~/.hermes/scripts/durable_park_sweep.sh                            # prints nothing when all is well
```

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
   git worktree add ../wt-myfix -b myfix && cd ../wt-myfix
   # edit code AND the docs it affects (see CONTRIBUTING.md); tests use the live clone's venv:
   (cd durable-park && /home/hermes/tapdash-agents/durable-park/.venv/bin/pytest -q -W error)
   git add -A && git commit
   cd /home/hermes/tapdash-agents && git merge --ff-only myfix && git push   # the hook checks the docs
   git worktree remove ../wt-myfix && git branch -d myfix
   ```

   `git push` uses the VM's deploy key for this repo (`~/.ssh/tapdash_agents_deploy`, set in the clone's
   `core.sshCommand`). One-time per clone: `git config core.hooksPath .githooks`. Bigger changes go through a
   pull request and an independent review instead (`CONTRIBUTING.md`).

2. **Make it live**, only when nothing is waiting or running (check [waiting rows](#health-check) first).

   | You changed | Make it live with |
   |---|---|
   | `plugin/` (park hook, form check, wake, executor, ledger) | `systemctl --user restart hermes-gateway` (as hermes) |
   | `plugin/durable_park/` (any file, incl. `_vendor/`) or `bridge/` | The bridge's `/opt/durable-park` copy is the **whole package**, `plugin/durable_park/` including `_vendor/` (the bridge imports `durable_park.ledger`, which runs `__init__` -> `formcheck` -> `_vendor`), not single files; a missing `_vendor` crash-loops it (`ModuleNotFoundError`). Root, admin: `sudo bash deploy/configure_hermes.sh` (step 4b copies both trees recursively, removes files the repo no longer has, restarts the bridge, and **stops with the last 20 journal lines unless the bridge answers `401` to an unsigned `POST` within 10 s**, so it never goes on to restart the gateway on a dead bridge). By hand, copy the tree the same way, then check `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8791/wh/bridge` prints `401`. |
   | any file in `receiver/` | root, admin: copy each changed file to `/opt/durable-park/receiver/` the same way, then `sudo systemctl restart durable-park-receiver` |
   | `scripts/durable_park_sweep.py` | nothing - the next cron run uses it |
   | `deploy/` (units, policy, Hermes config) | On proto-agents `configure_hermes.sh` re-applies the current tool policy (the v1 lockdown, re-applied 2026-10-21 03:16 UTC) and restarts the gateway: run it only when no ledger row is `parked`, `approved` or `executing`. Changing which tools are open goes through the [spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) section 4.4, not a hand edit of `~/.hermes/config.yaml`. |
   | `deploy/tunnel/` | rebuild and restart the tunnel: see [gateway-setup.md](gateway-setup.md#build-check-run-task-2-step-3-after-the-human-step-below) |
   | `deploy/withhuman/entry-pipeline.json` | in withHuman, save it as a new entry revision and ACTIVATE it before the gateway restart that brings this code live (with today's code it changes nothing: park requests still reach a human, gateway rows match rev 8) |

   Rollback: revert the code first (git checkout the previous `main`, restart the gateway with 0 active rows); never
   re-activate rev 8 while this code runs - rev 8 auto-approves every `hermes`/`mcp:` request.

3. **Verify**: run the [health check](#health-check). After a gateway restart the form self-check writes a fresh
   `formcheck_status.json` within about a minute.

## Alerts and what to do

Alerts come from the sweep and arrive in the Telegram DM with the bot (the cron job's delivery target).

### Outcome UNKNOWN

`durable-park: P-XXXXXXXX outcome UNKNOWN - check <tool> in the sandbox before retrying`

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

Form-checked calls (QuickBooks money moves, DocuSign writes) are blocked (fail closed) until it is fixed; other
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
older than 15 minutes makes it refuse every tool call except `parked_action_execute` and `clarify` (the log says
`durable-park tool policy check failed (<reason>)`); wakes are dropped and `parked_action_execute` answers
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
| `no tool policy report` | The drop-in is not installed (re-run `deploy/configure_hermes.sh`), or the status directory is not writable by `hermes` | `ls ~/.config/systemd/user/hermes-gateway.service.d/`; `systemctl --user daemon-reload`; check the directory's owner |
| `hermes python not found` | Not exactly one `~/.hermes/installs/*/facts.json`, or its Python is missing | Check that directory; the same lookup is `hermes_python()` in `deploy/configure_hermes.sh` |
| `the tool policy check did not write a report` | The check was killed (the 60 s timeout), crashed, or could not write its report; the wrapper then wrote this failing report itself | `journalctl --user -u hermes-gateway` for `tool_policy_check:` lines; run `deploy/hermes_tool_policy.py check --status-file /tmp/x.json` by hand as `hermes` in Hermes's Python and read the error |
| `invalid allow file: <reason>` | `/etc/durable-park/allowed_toolsets` has a Phase 2 or unknown name, or `hermes` cannot read it | Fix the file as in [Open a toolset](#open-a-toolset) (step 2), then restart the gateway |
| a toolset or tool problem | `hermes_tool_policy.py check` found the config drifted (a Hermes upgrade, a manual change) | Run `apply` then `check` as in [Open a toolset](#open-a-toolset) |
| `stale` | The report was written more than 15 minutes **before the gateway process started** (the gateway started without the drop-in, so no fresh report). The gateway's uptime no longer matters, and a plugin reload inside the running gateway stays fine. A process started more than 15 minutes after the last gateway start (for example a manual `hermes` run) is refused, which is correct: the start check did not run for it (it fails closed but does not write the verdict) | Install the drop-in if missing; otherwise restart the gateway (the start writes a fresh report). Visible through the `tool_policy_load.json` alert above |

Before go-live, time the check once on the VM (`time bash deploy/tool_policy_check.sh` as `hermes`): it must
finish well under 60 s, because `ExecStartPre` counts toward `TimeoutStartSec`.

Fix it, then `systemctl --user restart hermes-gateway` (only when nothing is waiting or running): the check runs
again at the start. If the report directory is not writable the script cannot replace an old report: it logs
`durable-park: tool policy report could not be refreshed (directory not writable?)` (journal) and the plugin's
15-minute staleness check refuses calls in any process started after the old report aged out. `systemctl --user show hermes-gateway -p ExecStartPre` shows the drop-in is active.

### withHuman could not be reached

`durable-park ALERT: withHuman could not be reached (since <time>); every tool call is being refused until it answers again.`

The park hook's circuit breaker opened after 3 consecutive errors from withHuman, so tool calls are refused
(fail closed) without asking. Every 30 seconds it lets calls through again until one gets an outcome. Read `breaker_status.json` next to the
ledger (`open`, `opened_at`, `closed_at`); check the withHuman status page, the `instance_token`, and the VM's
network (`hermes mcp test wh`). Nothing needs restarting: it closes by itself on the first answer. A gateway
restart while it is open keeps it open but lets the first call through at once, so the close is still recorded.

`durable-park: withHuman answers again (since <time>); tool calls are decided normally.` follows when it closes. If
the outage was shorter than 5 minutes, both lines may arrive together.

## Troubleshooting

### An approver was asked twice for one action

The park was approved, but the bridge did not match the gateway call, so withHuman asked a person again. Find
what differed:

```sh
python3 - <<'PY'
import sqlite3
L = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro", uri=True)
D = sqlite3.connect("file:/home/hermes/.hermes/plugin-data/durable-park/bridge_deliveries.db?mode=ro", uri=True)
print(L.execute("select park_id, tool, substr(args_digest,1,16) from parks order by created_at desc limit 3").fetchall())
print(D.execute("select outcome, park_id, name, substr(args_digest,1,16) from deliveries order by rowid desc limit 3").fetchall())
PY
```

The bridge's own log says why it answered `human`: `journalctl -u durable-park-bridge -n 20` (root), tag
`why=`: `no-park` (no matching executing park), `replay` (repeat of an earlier answer), `replay-mismatch` (same
delivery key, different call), `agent`, `preview`, `type` (not a real block delivery). Argument values are never
logged.

- **Name differs** (e.g. `create-bill` vs `create_bill`): the bridge must build names the way Hermes does
  (`bridge/app.py: hermes_tool_name`). Was the bridge's `/opt` copy updated?
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
| `durable-park: no chat session; the tool call was refused` | The call came from outside a chat (e.g. `hermes chat -q`). | Use a chat |
| `durable-park: no tool call id; the tool call was refused` | Hermes passed no session id or call id. | Check the Hermes version (`compat.py`) |
| `durable-park: could not open an approval request` | An error in the hook (token file unreadable, arguments that are not strict JSON, ledger error; logged as `park hook failed for tool=...`), or durable-park could not load and refuses every call (a setting of the wrong type, logged as `durable-park setting <name> must be a list of strings`, or `durable-park failed to load`). | Read `~/.hermes/logs/agent.log` |

### A chat says "already waiting" but nothing is pending in withHuman

Another row of the chat is `approved` (the agent has not called `parked_action_execute` yet), `executing`, or
`parked` with a request id whose decision has not reached the chat yet (the sweep re-wakes it every 5 minutes).
Look at [waiting rows](#health-check). A `parked` row **without** a request id (the gateway died between saving and
asking) does not block the chat; the sweep cancels it once it is 10 minutes old and prints
`durable-park: P-XXXXXXXX (<tool>) never got an approval request; cancelled after 10 minutes`.

### The bot never got the decision

The sweep re-posts the wake every 5 minutes. If it still does not arrive: health check (gateway up? webhook route
`/health`?), then `journalctl -u durable-park-receiver -n 20` for wake errors.

## Plugin settings

Settings live under `plugins.entries.durable-park.settings` in `~/.hermes/config.yaml` (user `hermes`). Edit the
file by hand (keep comments), then restart the gateway (only when nothing is waiting or running, see the
[health check](#health-check)). What each one does and its default: [architecture](architecture.md#plugin-settings).

```yaml
plugins:
  entries:
    durable-park:
      settings:
        exempt_tools: [parked_action_execute, clarify]   # keep parked_action_execute
        form_checked_tools: ['mcp__wh__quickbooks__(create|update|delete)[_-](payment|bill)']  # optional
        observer_hooks: []                                # module.qualname of reviewed hooks; live values: "Hooks reviewed so far"
        # tool_policy_status: /path/to/tool_policy_status.json   # default: next to the ledger; leave unset
```

- `exempt_tools`, `form_checked_tools` and `observer_hooks` must be lists of strings. A wrong type (a number,
  `true`, a mapping, an empty `observer_hooks:` that YAML reads as null) makes durable-park refuse every call
  except `parked_action_execute` and `clarify`; the log says `durable-park setting <name> must be a list of
  strings`. Fix it and restart.
- `exempt_tools`: anything listed here runs with no approval and no ledger row. Keep it to
  `parked_action_execute` and `clarify`. Any other name logs an ERROR at every gateway start; an MCP tool
  (`mcp__...`) or a form-checked tool is refused (`durable-park setting exempt_tools must not list MCP or
  form-checked tool '<name>'`) and every call is refused until it is removed.
- `risky_tools` is retired: it was renamed `form_checked_tools`. If it is still set the gateway log says
  `risky_tools is renamed form_checked_tools; ignoring it`; move the value to `form_checked_tools` and delete it.
- After the restart, `~/.hermes/logs/agent.log` shows `durable-park loaded: ... exempt tool(s), ... form-checked
  pattern(s), ... observer hook(s)`.

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
4. Add its exact name to `observer_hooks`, restart the gateway, and check one call is asked about normally again.
5. Re-do this review whenever that plugin, script or command changes.

Registration order does not make a foreign hook safe: Hermes merges `modify` from every `pre_tool_call` callback, so
a hook that runs after durable-park, or sits lower in `config.yaml`, can still change the arguments. Only a reviewed
observer may be listed.

### Hooks reviewed so far

| Name | Source | What it does | Reviewed (date) | Result |
|---|---|---|---|---|
| `platforms__raft.adapter._on_pre_tool_call` (full name `hermes_plugins.platforms__raft.adapter._on_pre_tool_call`) | Hermes Raft platform plugin, on `proto-agents` | Records telemetry about each call; always returns `None`, on every path including errors. Never returns `modify`, `block` or any other directive. | 2026-10-21 | Listed in `observer_hooks`; gateway restarted; calls asked about normally again |

`<hook registry unavailable>` is not a hook name: it means durable-park could not read Hermes's hook or middleware
registry (e.g. after a Hermes upgrade). It cannot be allowed; check the Hermes version against `compat.py`.

## Add a form-checked tool

**Risk is a pipeline rule now.** Whether a call needs a person is decided by withHuman's entry pipeline for every
tool call; the plugin no longer has a list of tools that need approval. To make a tool need a person, add a rule
to the pipeline. The plugin's list only says which tools get the [form check](form-check.md) before withHuman is
asked (and which ones the form self-check watches).

Example: put a new gateway server's write tool (say DocuSign `createEnvelope`) through the full path. Every place
that has to agree:

1. **withHuman**: register the server (e.g. slug `docusign`) and grant it to agent `proto-agents`
   ([gateway-setup.md](gateway-setup.md#human-step-b-task-2-step-4-register-the-server)). Add a pipeline rule that
   sends its `park:docusign` request to a person, and add it to pipeline **rule 2**, so the bridge decides its
   replays ([rule 2](gateway-setup.md#human-step-c-task-7-step-5-endpoint-secret-pipeline)).
2. **Hermes MCP filter**: `deploy/hermes_mcp.sh` only lets `quickbooks__*` tools in (`tools.include`), and fails if any
   other `mcp__wh__` tool registers. Allow the new server there.
3. **Tool lockdown**: `deploy/hermes_tool_policy.py` only allows `mcp__wh__quickbooks__*` (plus `clarify` and
   `parked_action_execute`); tools open only through the [spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) section 4.4 (the allowed
   list is `/etc/durable-park/allowed_toolsets`, spec §9 D15), not a code edit.
4. **Form-checked list**: the plugin form-checks tools matching the form-checked patterns (setting
   `plugins.entries.durable-park.settings.form_checked_tools`, see [Plugin settings](#plugin-settings); default
   `DEFAULT_FORM_CHECKED_TOOLS` in `plugin/durable_park/park_hook.py`). DocuSign's six write tools are already in
   the default. Leaving a tool off this list does **not** let it through: it is still asked about.
5. **Docs**: the pipeline table and system map in [architecture.md](architecture.md), this runbook, the setup guide.
6. **Test**: unit tests for the new pattern, then one live park, approve and run.

The form check needs nothing: it reads each tool's rules from Hermes automatically.

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
   `terminal`, `file`, `code_execution`, `delegation`, `cronjob`, `skills` and `computer_use` are refused until
   Clean Setup Phase 2; a typo is refused too (exit 2, message on stderr, nothing changed).
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

## Rotate secrets before production

Several secrets were pasted into chat during testing (approved for testing only). Before any real books:

- [ ] Revoke the withHuman provisioner token; re-provision the agent instance (new `instance_token` and receiver
      secret): [receiver-setup.md](receiver-setup.md#step-6--admin-provisioner-token-then-provision-human-step)
- [ ] Rotate the Intuit app client secret and refresh token; re-run OAuth; wipe the `durable-park-qbo-data`
      volume: [gateway-setup.md](gateway-setup.md#environment-variables)
- [ ] Issue a new withHuman tunnel token
- [ ] Rotate the `durable-park-bridge` webhook endpoint secret and re-store `bridge_secret`
- [ ] Regenerate `wake_secret`
- [ ] Revoke any GitHub token shared in chat; keep only the VM's deploy key for this repo
- [ ] Then the rest of the [backlog's "Before real books" list](backlog.md#before-real-books)

## Rollback

- **The one-agent merge (2026-10-21)**: the configs, units and ledger from before the cutover are in
  `/home/hermes/merge-backup-20261021T021246Z/` (`hermes/` and `ubuntu/`). To go back: `systemctl --user disable --now hermes-dashboard`, copy
  `hermes/config.yaml` and `hermes/.env` over `~/.hermes/`, restart `hermes-gateway` (locked-down tools again, no
  Teams), then start the `ubuntu` user's `hermes-gateway` and `hermes-dashboard` units (they are disabled and stopped:
  `enable --now` them) and check Telegram and the dashboard login. They were disabled on 2026-10-21 at 02:37:47 UTC, so
  nothing restarts them at boot until you do this. The config rollback does not undo the venv switch: the install's
  `facts.json` now points at venv `58c7a333` (the Teams packages) and the old venv `e74ec07b` still exists. Only with nothing `parked`, `approved` or
  `executing`. Plan: [docs/plans/2026-10-20-one-agent-merge.md](../../docs/plans/2026-10-20-one-agent-merge.md).
- **Code**: `git revert <commit>` in the clone, push, then make it live as in [Deploy a change](#deploy-a-change).
  For the bridge and receiver that includes copying the reverted files to `/opt/durable-park` again: the root
  copies do not change by themselves.
- **The October 2026 move to the GitHub clone**: the previous standalone repo is kept at
  `/home/hermes/durable-park.pre-github`. To go back: `rm /home/hermes/durable-park` (the link), move
  `.venv` and `.superpowers` back from `tapdash-agents/durable-park/`, rename `durable-park.pre-github` to
  `durable-park`, restart the gateway. Delete the old copy once the clone has run cleanly for a while.
