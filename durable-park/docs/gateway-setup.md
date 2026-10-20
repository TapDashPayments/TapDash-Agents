# QuickBooks behind the withHuman gateway (tunnel)

The agent never holds QuickBooks credentials. A withHuman **tunnel** container
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
| Docker on VM | `docker.io` 29.1.3 + `docker-compose-v2` 2.40.3 + `docker-buildx` (Ubuntu 26.04 packages); service enabled; `hermes` is NOT in the `docker` group, run docker with `sudo` |

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

   (MagicDNS name `proto-agents.taila95862.ts.net`, Tailscale IP `100.99.171.75`.)
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

4. Type `exit` to leave. Tell Claude; Claude runs `check` and starts the
   tunnel. The tunnel's page in withHuman should then show it **online**.

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
