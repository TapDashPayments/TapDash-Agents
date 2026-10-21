# One agent on proto-agents — merge design

> **Superseded 2026-10-21:** the v1 lockdown was re-applied on 2026-10-21 at 03:16 UTC, so the full toolset is no longer live. See [the all-tools spec](2026-10-21-all-tools-withhuman-design.md).

Date: 2026-10-20 · Status: Implemented 2026-10-21 · Author: Claude (with Yaakov)

## 1. Intent (in Yaakov's words, written back)

- Two agents are still the plan, built one at a time. Right now: **one agent** on proto-agents.
- That agent gets **every tool** Hermes offers (terminal, files, browser, Teams, Telegram, QuickBooks via withHuman, ...).
- **withHuman is the only gate.** Hermes's own built-in approval prompts are off. (as a policy; not yet a security boundary — see §8)
- **First phase: allow everything** on the Hermes side so Yaakov can see what the agent actually uses, then lock down as he chooses.
- **withHuman side is not touched.** The existing rule (12 QuickBooks money-move tool types are parked for approval) stays as is. No step backwards.
- Per-person profiles with different permissions come later. Not in this spec.

## 2. Where we are (verified 2026-10-20, read-only)

Two Hermes 0.22.2 installs on the VPS `proto-agents` (100.99.171.75), built by two sessions that could not see each other:

| | `hermes` user install | `ubuntu` user install |
|---|---|---|
| Created | Oct 19, 7:29pm ET | Oct 20, 2:45pm ET |
| What it is | durable-park / withHuman approvals prototype (v1 accepted Oct 20) | Teams bot "TapDash Agent" + web dashboard |
| Channels | Telegram, webhook (durable-park wake) | Teams, via devtunnel `proto-agents-bot` → port 3978 |
| Tools | **locked down**: `platform_toolsets` = `[durable_park, clarify, wh]` on every platform; `agent.disabled_toolsets` adds 35 toolsets (file, terminal, browser, skills, vision, web, memory, … — full list in `durable-park/deploy/hermes_tool_policy.py` FORBIDDEN_TOOLSETS) (before 2026-10-21; see §4.1 and §10) | stock defaults |
| Hermes approvals | not set (= `smart`) | `mode: off` |
| Dashboard | none | `hermes dashboard --host 100.99.171.75 --port 9119`, basic-auth user `TapDash` |
| Model | `meituan/longcat-2.5-preview:free` | `upstage/solar-mini4:free` |
| Cron | `durable-park-sweep` every 5m | none |
| Sessions | 25 (20 oneshot tests, 4 Telegram, 1 API) | 1 (Teams) |

Other facts the design relies on:

- durable-park registers a generic `pre_tool_call` hook; the parked set is `risky_tools` (regex list) in the plugin settings, default = the 12 QuickBooks types. Only QuickBooks has ever been parked/replayed in tests.
- durable-park bridge (127.0.0.1:8791) and receiver run as system services; the withHuman MCP server `wh` is configured in the `hermes` config.
- `deploy/configure_hermes.sh` in `/home/hermes/durable-park` writes the lockdown; re-running it would re-lock the agent. Backlog item 6 (re-check lockdown at every start) is NOT implemented, so nothing re-locks automatically.
- Hermes `approvals.mode` accepts `smart | manual | off`; `off` skips all terminal-command checks except the built-in hardline blocklist and any user `approvals.deny` patterns.
- Teams adapter reads `TEAMS_CLIENT_ID/SECRET/TENANT_ID/ALLOWED_USERS` (present in the `ubuntu` .env), `TEAMS_PORT` (default 3978) and `TEAMS_HOST` (default: all interfaces). The Teams SDK is a separate install: `python -c "import pm; pm.sync_venv(['teams'], explicit=True)"`.
- Hermes dashboard scans `~/.hermes/plugins/<name>/dashboard/manifest.json` for custom tabs (needed for the next spec).

## 3. Decision

Keep the **`hermes` install** as the one agent (it holds the tested approvals stack). Open it up, add Teams and the dashboard to it, retire the `ubuntu` install (archive, don't delete).

Rejected: (a) building on `ubuntu` and re-deploying durable-park there — re-does accepted work; (b) keeping two installs and a dashboard that reads both — fragile cross-user file access, two agents when one is wanted.

## 4. Changes

### 4.1 Hermes side: allow everything (`/home/hermes/.hermes/config.yaml`)

| Lockdown setting | First draft of §4.1 said | Done (2026-10-21) |
|---|---|---|
| `platform_toolsets.<58 platforms>` | delete block | delete block (same) |
| `known_plugin_toolsets.<40 platforms>` | not mentioned | delete block (stock has none; with it present, plugin toolsets such as `durable_park` are OFF on every platform once `platform_toolsets` is gone) |
| `agent.disabled_toolsets` = 35 "forbidden" toolsets | remove 6 entries | set to [] — Hermes's stock default (hermes_cli/config_defaults.py:279); the 31-entry list on the ubuntu install is the "Blank Slate" setup preset, not stock |
| `tools.tool_search.enabled: off` | not mentioned | **keep off.** durable-park's wake message tells the model to call `parked_action_execute` by name; with tool_search on, Hermes may hide plugin/MCP tools behind a search bridge. Untested; restricts nothing. |
| `gateway.strict: true`, `gateway.trust_recent_files: false` | leave | leave (same) |

Found while planning on 2026-10-20: the lockdown is `hermes_tool_policy.py::desired_settings()`, which writes five settings.

1. Under the existing `approvals:` block add `mode: off` (Hermes built-in approvals off; withHuman stays the only gate).
2. Leave `strict: true`, `trust_recent_files: false`, `max_turns: 500` as they are (not part of the lockdown; unrelated to tool availability).
3. Add a comment at the top of `/home/hermes/durable-park/deploy/configure_hermes.sh`: as committed: "DO NOT RE-RUN on proto-agents. On 2026-10-20 Yaakov decided the agent gets every Hermes tool (withHuman/durable-park remains the only gate). This script re-applies the v1 tool lockdown via hermes_tool_policy.py. See docs/specs/2026-10-20-one-agent-merge-design.md §4.1." No code change.

### 4.2 withHuman side: no change

`plugins.entries.durable-park.settings` untouched (no `risky_tools` override → default 12 QuickBooks types). withHuman pipeline, escalation path, bridge, receiver: untouched.

### 4.3 Teams moves to the `hermes` install

1. Pre-check: install the Teams SDK into the `hermes` install (`pm.sync_venv(['teams'], explicit=True)` run with the install's Hermes python). Verify by import before cutover.
2. Append to `/home/hermes/.hermes/.env`, copied by script from `/home/ubuntu/.hermes/.env` (values never printed): `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET`, `TEAMS_TENANT_ID`, `TEAMS_ALLOWED_USERS`. Add `TEAMS_HOST=127.0.0.1` (devtunnel connects locally; today 3978 listens on all interfaces).
3. `devtunnel-hermes.service` (user `ubuntu`) stays exactly as it is — it only forwards to 127.0.0.1:3978 and does not care who answers.

### 4.4 Dashboard moves to the `hermes` install

1. Copy the `dashboard.basic_auth` block (username, password_hash, secret) from the `ubuntu` config into the `hermes` config, by script.
2. New user unit `~/.config/systemd/user/hermes-dashboard.service` for user `hermes`, same ExecStart as today: `hermes dashboard --host 100.99.171.75 --port 9119 --no-open`. Enabled, so it survives reboot (linger is already on for `hermes` — its gateway runs as a user unit).
3. Bookmark `http://100.99.171.75:9119/`, login `TapDash`, keep working.

### 4.5 Retire the `ubuntu` install

`systemctl --user stop` + `disable` for `hermes-gateway.service` and `hermes-dashboard.service` under `ubuntu`. Nothing deleted; `/home/ubuntu/.hermes` stays as an archive. The single Teams session is not migrated. `devtunnel-hermes.service` stays enabled.

### 4.6 Model

Keep `meituan/longcat-2.5-preview:free` (the one durable-park was accepted on). Change later if wanted; not part of this merge.

## 5. Cutover (expected downtime: under a minute)

0. Backups (timestamped): both `config.yaml`, both `.env`, all four user units, `/home/hermes/.hermes/plugin-data/durable-park/ledger.db`.
1. 4.3.1 (SDK) and all config/env edits for `hermes` — **no restart yet**.
2. Dry-run: `hermes` python loads config without error; `TEAMS_*` resolve in env.
3. Stop `ubuntu` gateway + dashboard (frees 3978 and 9119).
4. Restart `hermes-gateway.service` (user `hermes`).
5. Start + enable `hermes-dashboard.service` (user `hermes`).
6. Run the verification list (§6). On any failure that is not fixable in a few minutes → §7 rollback.
7. Disable the `ubuntu` units (only after §6 passes).

## 6. Verification (evidence required for each; no claim without it)

| # | Claim | Evidence |
|---|---|---|
| 1 | Gateway up with Telegram + Teams | `systemctl --user status hermes-gateway` active; gateway.log shows both adapters started; `ss -ltnp` shows 127.0.0.1:3978 owned by the `hermes` gateway |
| 2 | Teams works | Yaakov sends a Teams message to TapDash Agent → reply arrives |
| 3 | Telegram works | Yaakov sends a Telegram message → reply arrives |
| 4 | Tools are open | Agent runs `terminal` (e.g. `uname -a`) and `read_file` on request, no approval prompt |
| 5 | Hermes approvals off | config shows `approvals.mode: off`; a flagged-but-harmless command (e.g. `rm -f /tmp/x`) runs without prompt |
| 6 | withHuman still gates | Telegram: "record $1 payment ... (sandbox)" → durable-park parks it; row `parked` in ledger; request visible in withHuman; deny it → denial delivered in chat |
| 7 | Dashboard | `http://100.99.171.75:9119/` loads, `TapDash` login works, Sessions tab shows the `hermes` sessions (25+) |
| 8 | Cron intact | `durable-park-sweep` still listed and runs within 6 min |
| 9 | Nothing left listening from `ubuntu` | `ss -ltnp` shows no `ubuntu`-owned 3978/9119; `devtunnel-hermes` still active |

## 7. Rollback (about a minute)

Restore the `hermes` config/.env from backup, restart `hermes-gateway`, stop `hermes-dashboard`, re-enable + start the `ubuntu` gateway and dashboard units.

## 8. Known risks / accepted

- With tools open and Hermes approvals off, **only QuickBooks money moves are gated**. Terminal and file tools run freely. Accepted for the setup phase on a test VM with a sandbox QuickBooks.
- **This is not a security boundary today.** The agent's terminal and file tools run on the VM (`terminal.backend: local`) as `hermes`, which has passwordless sudo. The agent can read `/etc/durable-park/*` (the QuickBooks client secret and refresh token and the withHuman tunnel token in `tunnel.env`, `wake_secret`, `bridge_secret`, `instance_token`), change `/opt/durable-park`, forge ledger rows, stop services, edit the Proto-Agents connector (`/home/hermes/hermes-shell-mcp`, also exposed through Tailscale Funnel) and call QuickBooks directly, bypassing withHuman. So withHuman gates QuickBooks money moves only when the agent does not deliberately go around it. Accepted by Yaakov on 2026-10-20 for the exploration phase on a QuickBooks sandbox test VM. A fix is pending his choice between (a) sandboxing the agent's terminal/file tools in Hermes's docker backend (recommended), (b) `approvals.deny` patterns as a partial backstop, (c) accepting it until before real books. Tracked as item 0 of the durable-park backlog's "Before real books".
- durable-park was accepted under the lockdown. Parking QuickBooks calls with the full toolset present is expected to behave the same (the hook is tool-name based) but is re-verified in §6.6 (result: §10).
- Free model quality is a known limitation (see acceptance notes); unchanged by this merge.
- Secrets rotation before real books (durable-park backlog #1) is still owed; unchanged by this merge.

## 9. Out of scope (next specs)

- Dashboard pages: pending withHuman approvals, full withHuman view, live agent/tool trace.
- Widening `risky_tools` (the later lock-down phase); `approvals.deny` backstop.
- Per-person profiles; the second (content) agent.

## 10. Result (2026-10-21)

Cutover at 02:20 UTC on 2026-10-21 (gateway on venv `58c7a333`, Telegram and Teams connected, no rollback). Verification of §6, with Yaakov:

| # | Result | Evidence |
|---|---|---|
| 1 | Pass | Gateway active; Telegram and Teams connected; `127.0.0.1:3978` owned by the `hermes` gateway |
| 2 | Pass | Teams message received 02:31:35 UTC; Yaakov confirmed the reply |
| 3 | Pass | Telegram works |
| 4 | Pass | "run uname -a" at 02:32:20; the reply had `Linux vps-c204f492` (Yaakov) |
| 5 | Pass | `/tmp/x` created and removed at 02:33:08 with no prompt; `/tmp/x` absent afterwards (`approvals` loads as `False`, which Hermes normalises to "off") |
| 6 | Pass | P-6CFC909B `create_payment` parked 02:33:45, denied 02:34:30, denial delivered in chat; 0 open rows |
| 7 | Pass | Dashboard login `TapDash` works (Yaakov) |
| 8 | Pass | `durable-park-sweep` present |
| 9 | Pass | `ubuntu` `hermes-gateway` and `hermes-dashboard` disabled and inactive (02:37:47 UTC); `devtunnel-hermes` enabled and active |

After the merge, fast-forward the live clone `/home/hermes/tapdash-agents` to main so the warning comment in `configure_hermes.sh` lands on the path admins run (docs and a comment only; no restart needed).
