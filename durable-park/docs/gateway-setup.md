# QuickBooks behind the withHuman gateway (tunnel)

By design the agent does not hold QuickBooks credentials (the v1 tool lockdown, re-applied 2026-10-21 03:16 UTC, enforces it as a
configuration guard; since Clean Setup Phase 1 (2026-10-22) `hermes` has no sudo and cannot read the root-only
`/etc/durable-park/tunnel.env`, but the ledger is still `hermes`-owned until Phase 2, so it is not yet a full permission boundary; see [architecture](architecture.md#the-three-guarantees)). A withHuman **tunnel** container
(`wh-tunnel`) runs on the VM, makes one outbound HTTPS connection to
`app.withhuman.ai`, and starts the Intuit QuickBooks MCP server as a child
process. Every call goes through the withHuman approval pipeline first.

## Pinned versions (verified 2026-10-19)

| Thing | Value |
| --- | --- |
| Tunnel image | `quay.io/withhuman/tunnel:0.0.369` |
| Tunnel digest | `sha256:dad0c8b52d653b4969161a8ec589cf37048da6eec5747e098969e6a281f0c3e0` (this is also what `:latest` pointed to when pinned; published 2026-10-16) |
| Tunnel binary | `withhuman-tunnel 0.0.369 (protocol 1)`, entrypoint `withhuman-tunnel`, default cmd `run`, user `withhuman` (uid 100), Alpine, Node 24.22.0, `uv` 0.9.30 |
| Intuit server | `https://github.com/intuit/quickbooks-online-mcp-server` at commit `31a1dd5c3e170e368dbff651abbaf61aabde6ed9` (2026-09-15, "fix: make tests and CI cross-platform (#136)") |
| Local image built | `durable-park/wh-tunnel:0.0.369-qbo-31a1dd5` |
| Docker on VM | `docker.io` 29.1.3 + `docker-compose-v2` 2.40.3 + `docker-buildx` (Ubuntu 26.04 packages); service enabled; `hermes` is NOT in the `docker` group, run docker with `sudo` (as the admin `ubuntu`; `hermes` has had no sudo since 2026-10-22) |

The tunnel tells you to upgrade when its protocol is outdated ("the tunnel stops
asking you to upgrade"). If that happens: look up the newest tag at
https://quay.io/repository/withhuman/tunnel?tab=tags, change `TUNNEL_IMAGE` in
`Dockerfile` and `image:` in `docker-compose.yml`, rebuild.

## Files

| File | Purpose |
| --- | --- |
| `deploy/tunnel/Dockerfile` | `FROM` the pinned tunnel image; builds the Intuit server (`npm ci` -> `tsc`) at the pinned SHA, installs it at `/opt/qbo-mcp`, symlinks `/usr/local/bin/qbo-mcp-server`, creates `/data` owned by `withhuman` |
| `deploy/tunnel/tunnel.yaml` | one server `quickbooks`: `command: ["qbo-mcp-server"]` + env |
| `deploy/tunnel/docker-compose.yml` | service/container `wh-tunnel` (`restart: unless-stopped`), named volume `durable-park-qbo-data` at `/data`, `tunnel.yaml` read-only at `/etc/withhuman/tunnel.yaml`, `env_file: /etc/durable-park/tunnel.env`; profile service `check` |
| `deploy/tunnel/enter-secrets.sh` | interactive script that writes `/etc/durable-park/tunnel.env` |

## Environment variables

Stored in `/etc/durable-park/tunnel.env` (0600 root:root, never committed):

| Variable | Meaning |
| --- | --- |
| `QUICKBOOKS_CLIENT_ID` | Intuit app Client ID (sandbox/development key) |
| `QUICKBOOKS_CLIENT_SECRET` | Intuit app Client Secret |
| `QUICKBOOKS_REFRESH_TOKEN` | OAuth 2.0 refresh token from the Playground |
| `QUICKBOOKS_REALM_ID` | company ID of the sandbox company |
| `WITHHUMAN_TUNNEL_TOKEN` | tunnel token from withHuman (read by the tunnel only; the QuickBooks child process never sees it) |

Fixed in `tunnel.yaml` (not secrets): `QUICKBOOKS_ENVIRONMENT=sandbox`,
`QUICKBOOKS_TOKEN_STORE_PATH=/data/qbo-token.env`.

Token store (verified in the server source at the pinned commit,
`src/clients/quickbooks-client.ts`): `QUICKBOOKS_TOKEN_STORE_PATH` is supported.
It must be absolute and set in the process env (the tunnel.yaml `env` block does
that). When set, the server loads that file with dotenv `override: true`, and
writes the rotated `QUICKBOOKS_REFRESH_TOKEN` (and realm id) back to it
atomically (tmp file + rename, mode 0600) on every refresh. If the file does not
exist yet, dotenv ignores it and the env values are used; the first refresh
creates the file. The package's own `.env` is not read.

**Stale-token trap:** once `/data/qbo-token.env` exists, its refresh token beats
`QUICKBOOKS_REFRESH_TOKEN` from the env. Intuit invalidates the old refresh
token on each rotation. If you ever enter a NEW refresh token (new Playground
run), also wipe the volume: `sudo docker compose down && sudo docker volume rm durable-park-qbo-data`.
Refresh tokens expire after roughly 100 days of no use (Intuit; unverified for
sandbox).

## Build, check, run (Task 2 Step 3, after the human step below)

All commands from `/home/hermes/durable-park/deploy/tunnel`, with `sudo`:

```bash
sudo docker compose build                  # build the image (works without any secrets)
sudo docker compose run --rm check         # starts the server once, lists tools; no tunnel token needed
sudo docker compose up -d wh-tunnel        # long-running tunnel
sudo docker logs -f wh-tunnel              # watch it connect
sudo docker compose ps
```

`check` runs `withhuman-tunnel check` with the same image and `tunnel.yaml`. It
does not connect to withHuman, so `WITHHUMAN_TUNNEL_TOKEN` is not required, but
the four `QUICKBOOKS_*` variables are (the tunnel refuses to start when a
`${VAR}` in the config is unset). It does not call QuickBooks: tool listing
needs no network, so it passes even with dummy credentials. A real call proves
credentials.

Verified result (dummy credentials, 2026-10-19): `quickbooks: ok, 142 tools`
(the server's README claims 145; `tools/list` over MCP returns 142).

## Never run these (they print the secrets)

`wh-tunnel` gets all five values from `/etc/durable-park/tunnel.env`, and these
commands print them in plain text. Do not run them, and do not paste their output
anywhere:

- `docker compose config` (prints the resolved `env_file` values)
- `docker inspect wh-tunnel` / `docker inspect <image>` on a running container
  (the container's `Env` lists every value)
- `docker exec wh-tunnel env` / `printenv` / `cat /proc/1/environ`
- `docker compose run ... env`, `docker run --env-file ... env`
- `sudo cat /etc/durable-park/tunnel.env`
- `docker logs wh-tunnel` is probably safe, but this is **unverified** (the
  tunnel's `run` mode has not been exercised with real values yet): skim it
  yourself before sharing it.

Note that `QUICKBOOKS_ENVIRONMENT` and `QUICKBOOKS_TOKEN_STORE_PATH` are set in
`tunnel.yaml` (the QuickBooks child process env), not in the `wh-tunnel`
container env, so checking them via `docker exec wh-tunnel env` will not show
them (and must not be done). To check them, read `tunnel.yaml`; to prove the
child works, use `docker compose run --rm check`.

## HUMAN STEP A (Task 2 Step 2): get the five values

### Intuit (about 10 minutes)

All UI paths below are from memory of Intuit's portal and are **unverified**;
names may differ slightly.

1. Go to https://developer.intuit.com and sign in (or create a developer account).
2. Dashboard -> **Create an app** (or **My Apps** -> **Create an app**). Choose
   the **QuickBooks Online and Payments** platform, name it e.g. `proto-agents`,
   select scope **Accounting** (`com.intuit.quickbooks.accounting`), create it.
3. Open the app -> **Keys & credentials** -> **Development** tab (sandbox keys).
   Copy **Client ID** and **Client secret**.
4. On that same page add this Redirect URI and save (the Playground needs it):
   `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl`
5. Sandbox company: a sandbox company is normally created automatically with a
   developer account. Check Dashboard -> **Sandbox** (or the profile menu). If
   none exists, click **Add a sandbox company** (QuickBooks Online, US).
6. Open the **OAuth 2.0 Playground**: https://developer.intuit.com/app/developer/playground
   (also under Dashboard -> **Playground**).
7. Select your app, tick scope **Accounting**, click **Get authorization code**.
   Sign in, pick the **sandbox company**, click **Connect/Authorize**. You land
   back in the Playground with an authorization code and a **Realm ID**.
8. Click **Get tokens**. Copy the **Refresh token** (long string starting `RT1-...`)
   and the **Realm ID** (a number; shown in step 7 and also next to the tokens).
   Do not worry about the access token (1 hour).

### withHuman tunnel token

1. https://app.withhuman.ai -> **Gateway** -> **Tunnels** -> **Set up a tunnel**.
2. Name it e.g. `proto-vm`. The token is shown **once**: keep the tab open and
   copy it when the script asks.

### Enter all five into the VM

The script needs an interactive terminal (it refuses to run without a tty), so
open one on the VM over Tailscale SSH. Use a terminal that is not being
screen-shared or recorded.

1. From any device on your tailnet run:

   ```bash
   tailscale ssh ubuntu@proto-agents
   ```

   (MagicDNS name `proto-agents.taila95862.ts.net`.)
   Tailscale SSH is enabled on the VM (`tailscale set --ssh`). Your tailnet policy
   may open a browser to re-authenticate ("check" mode): follow it, then the
   session continues. `ubuntu` has passwordless sudo.

   If you get "permission denied" or the connection is refused by policy, your
   tailnet ACL has no SSH rule allowing you to reach this device as `ubuntu`.
   Add one in the Tailscale admin console: **Access controls** -> edit the policy
   file -> add under `"ssh"`:

   ```json
   {
     "action": "check",
     "src":    ["autogroup:member"],
     "dst":    ["proto-agents"],
     "users":  ["ubuntu"]
   }
   ```

   (`dst` may also be `autogroup:self` if the VM is registered to your own
   user, or a tag such as `tag:proto` if the VM is tagged.) Save, then retry.
   The exact policy syntax is **unverified** against your tailnet.

2. In that session run (nothing you type is echoed):

   ```bash
   sudo bash /home/hermes/durable-park/deploy/tunnel/enter-secrets.sh
   ```

3. Paste, in order: QuickBooks Client ID, Client Secret, Refresh token, Realm ID,
   withHuman tunnel token (Enter after each; you will not see the characters).
   Empty input, or a value containing whitespace, quotes, `$`, `#`, a backslash,
   a backtick or a control character, is refused and you are asked again. It
   prints `saved 5 values` and writes `/etc/durable-park/tunnel.env` (0600 root).

   If the script aborts or you make a mistake, just run it again. Keep the
   withHuman tab with the tunnel token open until the script has saved: the
   token stays on that page until you close it (withHuman's docs say it is shown
   once, so if you already closed it, create a new token for the tunnel instead).

4. Type `exit` to leave. Tell whoever is running the deploy (an admin or an AI agent); they run `check` and
   start the tunnel. The tunnel's page in withHuman should then show it **online**.

## HUMAN STEP B (Task 2 Step 4): register the server

Only after the tunnel shows online. Labels are taken from the withHuman docs;
the wizard layout is **unverified** on the live tenant.

1. https://app.withhuman.ai -> **Gateway** -> **Register server**.
2. Server: name it `quickbooks` (the slug is permanent and appears in tool names
   and approval rules). Choose **Through a tunnel** and select your tunnel.
3. Scopes: one scope; for the **tunnel server name** enter exactly `quickbooks`
   (the key under `servers:` in `tunnel.yaml`). No credential is stored in withHuman.
4. Agents: grant the server to agent **`proto-agents`** and choose **All tools,
   including future tools**. Click **Activate and continue**.
5. Approval step: leave for now (pipeline rules are set up later); nothing is
   saved on this step. If the **Tools** list is empty, click **Reload tools**.

## Unverified / open

- Intuit portal click paths above (from memory).
- Whether the tunnel's `run` mode connects cleanly with the real token (no token
  yet; only `check` was exercised).
- Where tunnel logs live beyond `docker logs wh-tunnel` (docs do not say).
- `check` output format beyond `quickbooks: ok, 142 tools` (seen once, with a
  dummy-credential run).
- Real QuickBooks calls (needs real sandbox credentials).

## Gateway bridge + approval pipeline (Task 7)

One human approval per action. The model's first call to a risky tool is parked (a withHuman AAP request on
the pseudo-server `park:<slug>` that an approver approves). When the model then runs the approved park with
`parked_action_execute`, the real call goes through the gateway, and the pipeline's **webhook block** asks the
**gateway bridge** whether this exact call is an approved park whose approval was just consumed. The bridge
checks that in withHuman itself (it keeps no state and does not use the ledger), answers `approve` exactly once
for a matching call and `human` for everything else, so nothing that was not parked is ever auto-approved, and
an approved park is not asked about a second time.

### What the bridge does

| Item | Value |
| --- | --- |
| Code | `bridge/app.py` (Starlette + uvicorn), POST `/wh/bridge` on `127.0.0.1:8791`; `bridge/verify.py` (the decision rule), `bridge/whapi.py` (the withHuman request list) |
| Unit | `deploy/durable-park-bridge.service` (system unit, `User=dpbridge`, `Restart=always`, strict sandbox, `ProtectHome=yes`, `ProtectProc=invisible`, no writable path; see the comments at its top) and `deploy/durable-park-bridge.socket` (socket unit: systemd holds `127.0.0.1:8791` permanently and passes it to the service as fd 3, `--systemd-socket`) |
| Own user | `dpbridge`: system user, no login, no home, no group but its own (not in `dpwake`, not in `hermes`). See [Own user and own port](#own-user-and-own-port) |
| Public URL | `https://proto-agents.taila95862.ts.net/wh/bridge` (Tailscale Funnel path, set up at deploy) |
| Secret | `/etc/durable-park/bridge_secret` (0600 root:root: the bot cannot read it). systemd reads it as root when the service starts and hands the bridge a private copy (`LoadCredential`, `--secret-file %d/bridge_secret`) |
| Org key | `/etc/durable-park/audit_key` (`--key-file %d/audit_key`, env `DURABLE_PARK_WH_KEY_FILE`; the file is 0640 root:hermes because the plugin reads it too, the bridge reads systemd's private copy made when it starts, so a rotation needs `sudo systemctl restart durable-park-bridge`; the bridge re-reads that copy on every call), never logged. Permissions `audit.read`, `request.read`, `request.read.unrouted` and nothing else: it can read requests and cannot approve, deny or read rules or members. Missing or too weak: every delivery gets `human` ([runbook](runbook.md#the-gateway-bridge)). API base `--api-base` (env `DURABLE_PARK_WH_API_BASE`, default `https://app.withhuman.ai/api/v1`) |
| State | None. No ledger (the old `--ledger` option, `DURABLE_PARK_LEDGER` and `bridge_deliveries.db` are gone; the old file can be deleted once the new bridge is live) |
| Other options | `--secret-file`, `--systemd-socket` (serve on the listening socket systemd passes as fd 3; it exits at once when systemd passes none; it cannot be combined with `--host` or `--port`), `--host 127.0.0.1` and `--port 8791` (only when run by hand, without `--systemd-socket`), `--expected-agent proto-agents` (env `DURABLE_PARK_BRIDGE_SECRET_FILE`, `DURABLE_PARK_EXPECTED_AGENT`). Budget: 3.5 s for the two withHuman lists (withHuman's webhook block gives up at 5 s) |
| Health check | `deploy/bridge_check.py` (root-owned copy in `/opt/durable-park/deploy/`): `sudo python3 -I /opt/durable-park/deploy/bridge_check.py`; `configure_hermes.sh` runs it after the 401 gate and stops if it fails |

Signature (confirmed against withHuman's open-source repo `internal/webhookdelivery`, and its webhook-endpoints
docs): header `WithHuman-Signature: t=<unix>,v1=<hex>`; `v1` = HMAC-SHA256 over `"<t>." + raw body`, keyed with
the **whole** `whsec_...` string as text (not base64-decoded); 300 s tolerance either side; constant-time compare.
A missing, malformed, stale or wrong signature gets `401` with an empty body, which withHuman treats as a failed
delivery (so the block falls back to a human).

Decision, for a delivery with a valid signature (the rule itself is `bridge/verify.py`, a pure function; the full
rule is in [architecture](architecture.md#the-bridges-decision-rule)):

1. The body must be `type: request_approval_block.webhook`, not `preview: true`, `agent.slug` must equal the
   expected agent (`proto-agents`), and `request.id` (the gateway request's own id) must be a non-empty string;
   otherwise `human`.
2. The Hermes name is built the way Hermes registers MCP tools (`hermes_tool_name`, now in `bridge/verify.py`):
   `mcp__wh__<server>__<tool>` with every character outside `[A-Za-z0-9_]` turned into `_` (so `create-bill`
   becomes `create_bill`), and names over 64 characters shortened with an 8-hex sha256 suffix. The arguments are
   digested after `ledger.normalize_args` (top-level `withhuman_context` dropped, `withhuman_scope` defaulted to
   `"default"`, whole-number floats such as `2.0` treated as `2`), exactly as at park time.
3. The bridge asks withHuman (`GET /api/v1/requests?agent_slug=proto-agents&q=<name>&limit=...`, newest first)
   for the park requests of that Hermes name. It approves only if exactly one is `approved`, on server
   `park:<slug>`, **consumed** (`consumption.consumed_at`) within the last 120 s (withHuman refuses to consume
   an expired approval, so the bridge does not read the approval's expiry),
   not truncated, with the same argument digest. It then lists the gateway requests of that tool: any other
   request for the same call created at or after the consumption means the approval is already used, so `human`.
   The answer is `{"outcome":"approve","reason":"Matches approved park request <id>"}`.
4. Anything else, or any error (no match, two matches, withHuman slow or down, key missing or too weak, bad answer):
   `{"outcome":"human","reason":"No approved park for this exact call"}`. The bridge keeps no record of its
   answers; withHuman retries a delivery and gets the same answer because the gateway request never counts
   against itself.

Logs (`journalctl -u durable-park-bridge`): one line per delivery with the delivery id, server, tool, the sorted
argument **key names**, the arguments digest and the outcome, plus a short `why` tag. Argument values, the
signing secret and the org key are never logged.

### Own user and own port

withHuman does not sign the bridge's answers: whoever answers on `127.0.0.1:8791` decides. If the bridge ran as
`hermes` (the bot's user), anything the bot runs could kill it and, until systemd restarted it, bind the port and
answer "approve" to every gateway call. So, since 2026-10-22:

- The bridge runs as `dpbridge`: a system user with no login, no home and no group but its own (not in `dpwake`, not
  in `hermes`). The bot cannot signal it or read its memory. (`ProtectProc=invisible` hides other users' processes
  from the bridge, not the other way round; the separate user is what protects it.)
- `durable-park-bridge.socket` holds `127.0.0.1:8791` permanently and passes it to the service as fd 3
  (`--systemd-socket`). While the bridge restarts (`Restart=always`, `RestartSec=1`) connections wait in the socket's
  backlog and nobody else can bind the port, for as long as the socket unit is neither stopped nor failed. systemd
  fails a socket unit (and frees the port) when the service it activates trips its start limit, or when the socket
  trips its trigger limit, so the service has `StartLimitIntervalSec=0` (it keeps being retried every second even if
  every start fails) and the socket has `TriggerLimitIntervalSec=0`. Started without a socket from systemd, the
  bridge exits at once.
- `bridge_secret` is `root:root 0600` and `audit_key` stays `root:hermes 0640`. systemd reads both as root when the
  service starts (`LoadCredential`) and gives the bridge private copies, so the bot can no longer read the signing
  secret. Rotating either key needs `sudo systemctl restart durable-park-bridge`.
- `deploy/bridge_check.py` checks all of this, read-only, and prints `ok:` or `FAIL:` per check (exit 0 only if every
  check passes): (1) the socket unit is active and listens on exactly `127.0.0.1:8791`; (2) the service is active and
  its main process runs as `dpbridge` with no supplementary group but `dpbridge`'s own (the `Groups:` line of
  `/proc/<pid>/status`); (3) only systemd and that process hold the port; (4) no other process runs
  `bridge.app` (a scan of `/proc/*/cmdline`: a heuristic, so a process that merely has `bridge.app` or
  `bridge/app.py` among its arguments, such as an editor, also fails it); (5) no user-level `durable-park-bridge*` unit
  exists for any user; (6) `dpbridge` has the `nologin` shell and is not in `dpwake` or `hermes`; (7) `bridge_secret`
  is `root:root 0600` (stat only, the contents are never read). `configure_hermes.sh` runs it after the bridge answers
  `401` to an unsigned request and stops if it fails; run it by hand any time ([runbook](runbook.md#health-check)).
- Not checked yet ([backlog](backlog.md#later)): the running bridge's supplementary groups and the permissions of the
  `/etc/durable-park` directory. An edit to the socket unit takes effect only after
  `sudo systemctl restart durable-park-bridge.socket`.

### HUMAN STEP C (Task 7 Step 5): endpoint, secret, pipeline

C1 and C2 come first on a fresh VM, because `configure_hermes.sh` refuses to start unless `bridge_secret` and
`audit_key` exist and are not empty (the bridge unit loads both as credentials and cannot start without them); C3 and
C4 need the bridge service and the Funnel path deployed and started. Labels come from the
withHuman docs (`webhook-endpoints`, `webhook-blocks`, `conditions`, `escalation-paths`); the exact screen layout
is **unverified** on the live tenant.

**C1. Add the webhook endpoint**

1. https://app.withhuman.ai -> **Webhook endpoints** (look under Settings or the left menu if it is not at top
   level) -> **Add endpoint** / **New endpoint**.
2. Name `durable-park-bridge`. If it asks for a key, use `durable-park-bridge` (lowercase letters, digits, `.`,
   `_`, `-`).
3. URL: `https://proto-agents.taila95862.ts.net/wh/bridge`. Save and activate it.
4. Copy the **signing secret** (`whsec_...`). It is shown **once**. Keep the tab open until step C2 is done.
   (Rotating the secret later takes effect immediately in withHuman: repeat C2 right after.)

**C2. Put the secret on the VM (no echo, no shell history, not in a command line)**

1. From a device on your tailnet: `tailscale ssh ubuntu@proto-agents` (use a terminal that is not being
   screen-shared).
2. Run this one-liner, paste the secret when it prompts (nothing is shown), press Enter:

   ```bash
   sudo bash -c 'set -eu; f=/etc/durable-park/bridge_secret; mkdir -p /etc/durable-park; install -m 0600 -o root -g root /dev/null "$f"; read -rs -p "Paste withHuman signing secret, then Enter: " S; echo; printf "%s" "$S" > "$f"; unset S; echo "saved $(wc -c < "$f") bytes to $f"'
   ```

   It creates the file empty with mode 0600 root:root first (only root can read it; systemd gives the bridge its own copy), then writes the secret into it, so the value is
   never in a wider-permission file. A `whsec_...` secret is about 49 bytes; a very different number means a bad paste,
   run it again.
3. systemd copies the secret to the bridge only when the service starts, so restart it (root):
   `sudo systemctl restart durable-park-bridge`. (The socket unit keeps the port during the restart. On a fresh VM
   there is no bridge yet: store the secret and the org key first, then run `configure_hermes.sh`. Before you run `configure_hermes.sh`: check the ledger (no `parked`, `approved` or `executing` rows), then stop `hermes-gateway` ([Before you run the configure script](runbook.md#before-you-run-the-configure-script)). The script starts it again when it finishes.)
4. Close the withHuman secret tab. Type `exit` to leave the VM.

**C3. Pipeline rules (Approval pipelines -> the Entry pipeline), in this order**

1. **Rule 1: parked calls ask an approver.** Condition, exactly as live (revision 8): **All** of
   (**Agent slug** *equals* `proto-agents`, **Any** of (**MCP server** *starts with* `park:`, **Tool name**
   *starts with* `mcp__wh__`)). A park request's tool is the Hermes name (`mcp__wh__quickbooks__...`), so the
   tool condition catches it even if the server condition does not match. Do **not** use the server condition
   alone: if it does not match, park requests fall through to rule 3 and are auto-approved. Action: **Ask a
   human**, with an escalation path whose **Notify** step names the approvers (create the path under Escalation paths
   if none exists).
2. **Rule 2: risky QuickBooks calls go to the bridge.** Add a **Branch** block with one **All** group. Tool
   names are the gateway's own, without the `mcp__wh__<slug>__` prefix; use the anchored regex:
   - **MCP server** *equals* `quickbooks`, and
   - **Tool name** *matches regex*
     `^(create|update|delete)[_-](payment|bill_payment|transfer|deposit|journal_entry|refund_receipt|sales_receipt|credit_memo|vendor_credit|purchase|invoice|bill)$`

   (v1 is QuickBooks only. Later, if DocuSign is added, add a second **All** group inside an **Any**: server
   `docusign` and tool regex `^(createEnvelope|updateEnvelope|triggerWorkflow|cancelWorkflowInstance|pauseNewWorkflowInstances|resumeWorkflow)$`.
   Nothing about DocuSign is needed now.) Inside the branch add a **Webhook** block:
   - Name `durable-park-bridge`; endpoint `durable-park-bridge`.
   - Allowed outcomes: **Approve** and **Ask a human** only (untick Continue and Deny).
   - Attempt timeout `5s`; max attempts `3` (as in `deploy/withhuman/entry-pipeline.json`).
   - Include: leave agent reasoning, context and previous metadata **off** (not needed; less data leaves).
   - Escalation path: the same approvers' path as rule 1, so a `human` answer asks an approver. Leave "agent's owner" off.
   - Reason text, for example: `Approve only a call that matches an approved durable-park park`.
3. **Rule 3: everything else is approved.** A final rule/default: **Approve**. Make sure the order is 1, 2, 3
   (nothing above rule 1; rule 3 last).

If the bridge is down, answers `401` or times out, the webhook block fails and the call goes to a human
(withHuman: "no accepted answer after the attempts -> human review"), so an outage never auto-approves. The same
holds when withHuman's own API is slow or down while the bridge asks it: the bridge answers `human`.

**C4. Test**

1. On the Webhook endpoints page, open `durable-park-bridge` and send the **test delivery**. The bridge answers
   `human` (a test delivery is not a block delivery and there is no park); the log line shows `why=type`.
   `journalctl -u durable-park-bridge -n 5` on the VM. Expect **no** `401` warning; a `401` means the secret
   file does not match the endpoint secret (redo C2, restart).
2. A real end-to-end check (a parked call, then `parked_action_execute`) happens in Task 8. After a change to the
   bridge, run the [live checks](runbook.md#the-gateway-bridge) (the lister test, one payment that passes once, a
   replay that gets a human).

### Known risks / unverified

- Settled live: argument rewrites by the gateway (`withhuman_scope` added, `2.0` sent as `2`) are absorbed by
  `ledger.normalize_args`; park requests are matched by rule 1 as written above (revision 8).
- The bridge relies on withHuman's request list (`GET /api/v1/requests`: `q` is a substring of the tool name, newest
  first, `limit` up to 200) and on the consumption record (`consumption.consumed_at`); both were read in withHuman's
  docs and source and verified live on 2026-10-22 ([spec](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md)).
- UI names (Webhook endpoints menu location, Branch vs Delegate block, "Entry pipeline") are from the docs, not
  seen on the live tenant. Whether the endpoint **test** accepts a `human` answer for a non-block delivery type.
- Deploy: `deploy/configure_hermes.sh` creates the user `dpbridge` (once), makes an existing `bridge_secret`
  `root:root 0600`, installs the bridge (Python `/opt/durable-park/bin/python`, the receiver's venv; code copied to
  `/opt/durable-park/bridge` and `/opt/durable-park/plugin`), installs, enables and starts the socket unit, restarts
  the service unit, waits for the 401 gate and runs `bridge_check.py` (a FAIL stops the script before the gateway is
  touched), then adds the Funnel path `/wh/bridge`. It refuses to run at all, changing nothing, when
  `/etc/durable-park/bridge_secret` or `/etc/durable-park/audit_key` is missing or empty (the unit's `LoadCredential`
  could not start without them), or when `deploy/bridge_check.py` is missing from the checkout. (Before the bridge
  had its own user it started without the keys and answered `401` or `human`; it no longer starts.)

## Hermes → gateway connection (MCP server `wh`, Task 3)

Hermes talks to the gateway directly over Streamable HTTP. It does not use the `withhuman mcp` stdio relay
that production uses. Hermes's `mcp_servers` accepts `url` + `headers` (reference:
`website/docs/reference/mcp-config-reference.md`), and `${VAR}` in `url`/`headers` is resolved when the
server connects (`_interpolate_env_vars`, `tools/mcp_tool_config.py:554-581`). An unresolved reference fails
closed instead of sending the literal text (`_require_rendered_remote`, same file, `:585-599`). The reference is
written bare, not as `${env:VAR}`: the two resolve alike, but the `env:` form makes every Hermes process log a
"Config ref ... is not set" warning, because config loads before the `command` secret source runs (why, and how
a missing secret is still noticed: [receiver-setup.md](receiver-setup.md#files-and-owners)).

`deploy/hermes_mcp.sh` (as root or hermes, after provisioning; it refuses while
`/etc/durable-park/instance_token` is missing) writes this entry with `hermes config set`. Check the ledger first (no `parked`, `approved` or `executing` rows): the script restarts `hermes-gateway` ([health check](runbook.md#health-check)):

```yaml
mcp_servers:
  wh:
    url: https://app.withhuman.ai/gateway/mcp
    headers:
      Authorization: Bearer ${DURABLE_PARK_WH_TOKEN}
    trust: full                # untrusted blocks the plugin's dispatch_tool replay (dispatch-probe-report)
    tools: {include: ["quickbooks__*"], resources: false, prompts: false}   # v1: QuickBooks only
    enabled: true
```

The token never goes into `config.yaml` or `.env`. `/opt/durable-park/libexec/hermes-secrets` (Hermes's
`command` secret source) prints `DURABLE_PARK_WH_TOKEN` from `instance_token` at each Hermes start. The script
then restarts `hermes-gateway` and waits until `mcp__wh__quickbooks__get_company_info` is registered.

The `include` filter also keeps the gateway's built-in `withhuman__*` management tools away from the model.
Hermes tool names are `mcp__wh__<slug>__<tool>`, so they match the plugin's risky patterns and the bridge's
`mcp__wh__<server>__<tool>`. The agent tool policy only admits `mcp__wh__quickbooks__*`, and `hermes_mcp.sh`
fails (stopping the gateway) if any other `mcp__wh__` tool registers.

Live check (manual): `DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_gateway_smoke.py`. The pipeline
must let the read-only `get_company_info` call through.

Known risk: Hermes's docs say that when a Streamable-HTTP session expires while idle, the first call to an
unannotated (write-capable) tool returns `outcome_uncertain` instead of being retried. The executor treats that
as `unknown`. So an approved replay after a long idle period can come back as UNKNOWN. Check whether the gateway
expires idle sessions during Task 8.
