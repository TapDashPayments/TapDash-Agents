# durable-park runbook

How to check, change and fix durable-park on the test VM. For how it works, see [architecture.md](architecture.md).

- [Access and ground rules](#access-and-ground-rules)
- [Health check](#health-check)
- [Deploy a change](#deploy-a-change)
- [Alerts and what to do](#alerts-and-what-to-do)
- [Troubleshooting](#troubleshooting)
- [Rotate secrets before production](#rotate-secrets-before-production)
- [Rollback](#rollback)

## Access and ground rules

| What | How |
|---|---|
| VM | `proto-agents` (OVH VPS, Ubuntu 26.04, Hermes v0.22.2). Yaakov: `tailscale ssh ubuntu@proto-agents`. |
| Claude | Through the Hermes Server connector, piping scripts into `/tmp/dp-tools/vm` (runs as `hermes`) or `/tmp/dp-tools/vmroot` (root) on the jump host. |
| Repo on the VM | `/home/hermes/tapdash-agents` (a clone of this repo). `/home/hermes/durable-park` links to its `durable-park/` folder. |
| withHuman | app.withhuman.ai: agent `proto-agents`, gateway server `quickbooks`, webhook endpoint `durable-park-bridge`, entry pipeline. |
| Telegram test bot | `@TapDashAgentsBot`, home channel = Yaakov's DM (where alerts go). |

Rules:

- **Never change the production Hermes host.** The jump host is production; only the VM is for testing.
- When piping scripts through the connector, give every heredoc a **unique** delimiter (e.g. `<<'XEOF_7'`) and
  never reuse it inside. A reused `EOF` once ran the tail of a script on the production host.
- **Never print a secret.** Secrets are typed in by Yaakov with `read -rs` (never pasted into chat).
- Root steps (copying into `/opt`, restarting system units) are done by Yaakov in his own terminal. Claude's
  safety check blocks them for Claude.
- QuickBooks is a **sandbox** company. Never connect real books before the [rotation](#rotate-secrets-before-production)
  and the open v2 items are done.

## Health check

As `hermes` on the VM:

```sh
systemctl --user is-active hermes-gateway                          # active
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

   ```sh
   cd /home/hermes/tapdash-agents
   git pull --ff-only
   # edit code AND the docs it affects (see CONTRIBUTING.md)
   cd durable-park && .venv/bin/pytest -q -W error && cd ..
   git add -A && git commit
   git push        # the pre-push hook refuses code changes without their docs
   ```

   `git push` uses the VM's deploy key for this repo (`~/.ssh/tapdash_agents_deploy`, set in the clone's
   `core.sshCommand`). One-time per clone: `git config core.hooksPath .githooks`.

2. **Make it live**, only when nothing is waiting or running (check [waiting rows](#health-check) first).

   | You changed | Make it live with |
   |---|---|
   | `plugin/` (park hook, form check, wake, executor, ledger) | `systemctl --user restart hermes-gateway` (as hermes) |
   | `plugin/durable_park/ledger.py` | also the bridge's copy (root, Yaakov): `sudo install -m 0644 -o root -g root /home/hermes/durable-park/plugin/durable_park/ledger.py /opt/durable-park/plugin/durable_park/ledger.py && sudo systemctl restart durable-park-bridge` |
   | `bridge/app.py` | root, Yaakov: `sudo install -m 0644 -o root -g root /home/hermes/durable-park/bridge/app.py /opt/durable-park/bridge/app.py && sudo systemctl restart durable-park-bridge` |
   | `receiver/*.py` | root, Yaakov: copy the changed file to `/opt/durable-park/receiver/` the same way, then `sudo systemctl restart durable-park-receiver` |
   | `scripts/durable_park_sweep.py` | nothing - the next cron run uses it |
   | `deploy/` (units, policy, Hermes config) | `sudo bash /home/hermes/durable-park/deploy/configure_hermes.sh` (idempotent; restarts the gateway) |
   | `deploy/tunnel/` | rebuild and restart the tunnel: see [gateway-setup.md](gateway-setup.md#build-check-run-task-2-step-3-after-the-human-step-below) |

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

### Form checker not working

`durable-park ALERT: the form checker is not working - <reason>. Risky actions will be refused until it is fixed.`

Risky calls are blocked (fail closed) until it is fixed. Read `formcheck_status.json` and the reason:

| Reason | Likely cause | Fix |
|---|---|---|
| no form-check report / last ran N minutes ago | Gateway down, or durable-park did not load | Health check; `systemctl --user restart hermes-gateway`; look for `durable-park loaded` in `~/.hermes/logs/agent.log` |
| no risky tools are registered | Gateway lost the `wh` MCP server (withHuman down, token problem) | `hermes mcp test wh`; check the withHuman gateway and `instance_token` |
| could not read the tool registry / Hermes type fixing is unavailable | A Hermes upgrade moved an internal | Read `plugin/durable_park/compat.py` and `formcheck.py`; adjust and redeploy |
| `<tool>: rules could not be used` | A tool's schema changed in a way the checker cannot compile | Look at that tool's schema (`withHuman` tool catalog); see [form-check.md](form-check.md) |

A second line `durable-park: the form checker is working again.` follows when it recovers.

## Troubleshooting

### Yaakov was asked twice for one action

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

- **Name differs** (e.g. `create-bill` vs `create_bill`): the bridge must build names the way Hermes does
  (`bridge/app.py: hermes_tool_name`). Was the bridge's `/opt` copy updated?
- **Digest differs**: the gateway rewrote the arguments (seen so far: added `withhuman_scope`, `2.0` sent as `2`).
  Extend `ledger.normalize_args` for the new rewrite, add a test, deploy to the plugin and the bridge copy.

### A chat says "already waiting" but nothing is pending in withHuman

Probably a `parked` row without a request id (the gateway died between saving and asking; known v2 gap). Check
[waiting rows](#health-check); if the row has no `request_id`, cancel it by hand (safe: such a row never reached withHuman):

```sh
python3 - <<'PY'
import sqlite3, datetime
c = sqlite3.connect("/home/hermes/.hermes/plugin-data/durable-park/ledger.db")
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
n = c.execute("update parks set status='cancelled', note='cancelled by hand: no request', updated_at=? "
              "where status='parked' and request_id is null", (now,)).rowcount
c.commit(); print(n, "row(s) cancelled")
PY
```

### The bot never got the decision

The sweep re-posts the wake every 5 minutes. If it still does not arrive: health check (gateway up? webhook route
`/health`?), then `journalctl -u durable-park-receiver -n 20` for wake errors.

## Rotate secrets before production

Several secrets were pasted into chat during testing (approved for testing only). Before any real books:

- [ ] Revoke the withHuman provisioner token; re-provision the agent instance (new `instance_token` and receiver
      secret): [receiver-setup.md](receiver-setup.md#step-6--yaakov-provisioner-token-then-provision-human-step)
- [ ] Rotate the Intuit app client secret and refresh token; re-run OAuth; wipe the `durable-park-qbo-data`
      volume: [gateway-setup.md](gateway-setup.md#environment-variables)
- [ ] Issue a new withHuman tunnel token
- [ ] Rotate the `durable-park-bridge` webhook endpoint secret and re-store `bridge_secret`
- [ ] Regenerate `wake_secret`
- [ ] Revoke any GitHub token shared in chat; keep only the VM's deploy key for this repo
- [ ] Fix the v2 Important items still open in [acceptance-2026-10-20.md](acceptance-2026-10-20.md) and decide the
      write-tool scope

## Rollback

- **Code**: `git revert <commit>` in the clone, push, then make it live as in [Deploy a change](#deploy-a-change).
- **The October 2026 move to the GitHub clone**: the previous standalone repo is kept at
  `/home/hermes/durable-park.pre-github`. To go back: `rm /home/hermes/durable-park` (the link), move
  `.venv` and `.superpowers` back from `tapdash-agents/durable-park/`, rename `durable-park.pre-github` to
  `durable-park`, restart the gateway. Delete the old copy once the clone has run cleanly for a while.
