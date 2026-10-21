# One Agent on proto-agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the `hermes` Hermes install on proto-agents into the one agent: every Hermes tool enabled, Hermes built-in approvals off, Teams + dashboard moved in from the `ubuntu` install, `ubuntu` install retired, withHuman/durable-park untouched.

**Architecture:** This is an operations change, not code. Every task edits config or systemd units on the VM over SSH (`ssh hermes@100.99.171.75`, reached from the Hermes_Server connector), verifies with a command whose expected output is written in the step, and records what changed in the repo. The only repo changes are the spec amendment, this plan, one comment in `durable-park/deploy/configure_hermes.sh`, and the docs that describe the agent's tool policy.

**Tech Stack:** Hermes Agent v0.22.2 (`~/.hermes/config.yaml`, `~/.hermes/.env`), systemd user units, bash/awk/python3 on Ubuntu, git worktree in `/home/hermes/tapdash-agents-wt/`.

**Spec:** `docs/specs/2026-10-20-one-agent-merge-design.md` (merged in PR #1). §4.1 is amended by Task 1 of this plan; see "Deviations from the spec" below.

## Deviations from the spec (found while planning; Task 1 writes them into the spec)

The lockdown is defined by `durable-park/deploy/hermes_tool_policy.py::desired_settings()`. It writes five things, not two:

| Lockdown setting | Spec §4.1 said | This plan does |
|---|---|---|
| `platform_toolsets.<58 platforms>` | delete block | delete block (same) |
| `known_plugin_toolsets.<40 platforms>` | not mentioned | delete block (stock has none; with it present, plugin toolsets such as `durable_park` are OFF on every platform once `platform_toolsets` is gone) |
| `agent.disabled_toolsets` = 35 "forbidden" toolsets | remove 6 entries | set to [] — Hermes's stock default (hermes_cli/config_defaults.py:279); the 31-entry list on the ubuntu install is the "Blank Slate" setup preset, not stock |
| `tools.tool_search.enabled: off` | not mentioned | **keep off.** durable-park's wake message tells the model to call `parked_action_execute` by name; with tool_search on, Hermes may hide plugin/MCP tools behind a search bridge. Untested; restricts nothing. |
| `gateway.strict: true`, `gateway.trust_recent_files: false` | leave | leave (same) |

Also: Hermes installs Teams dependencies itself at gateway start (`ubuntu` gateway.log 20:31:08 "dependencies missing — attempting install", done in 6 s). Task 3 still pre-installs so the first `hermes` restart is not waiting on pip.

## Global Constraints

- VM: `proto-agents` = `100.99.171.75`, Linux user `hermes` (passwordless sudo), reached via `ssh -o BatchMode=yes hermes@100.99.171.75` from the Hermes_Server connector. **Never touch the production Hermes host (ovh-hermes).**
- Repo work in a worktree, never in `/home/hermes/tapdash-agents` (live clone; the gateway and sweep load from it). Branch for this plan: `ops/one-agent-merge` from `origin/main`.
- **Never print, paste or commit a secret.** `.env` values, `password_hash`, `secret`, tokens: copy by script, show only key names.
- Every heredoc through the connector gets a unique delimiter.
- Before restarting the gateway: ledger must have no row in `parked`, `approved` or `executing` (runbook health check).
- withHuman side (plugin settings `risky_tools`, `mcp_servers.wh`, bridge, receiver, pipeline) is **not changed by any task**.
- Config edits are text edits that keep comments; never round-trip `config.yaml` through a YAML dumper.
- Commit trailer for repo commits: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01FxcYWEdzmD5mZef95rh1wx`. Code-only comment changes use `Docs-Not-Needed: <reason>`.
- `HPY` = the Hermes venv python. Read it from the install's facts file, the way `durable-park/deploy/configure_hermes.sh` `hermes_python()` does: `HPY=$(python3 -I -c 'import glob,json,os; f=glob.glob(os.path.expanduser("~/.hermes/installs/*/facts.json")); assert len(f)==1, f; print(json.load(open(f[0]))["packages"]["venv"]["environment"]+"/bin/python")')` (`packages.venv.environment` + `/bin/python`). The venv path is not fixed (it can change after `hermes pm repair`), so every script that needs the install's python defines `HPY` first (each connector call is a fresh shell) and never hard-codes a path. Check it once with `"$HPY" -c 'import hermes_cli; print(1)'`; if it fails, use the interpreter of the running gateway: `tr '\0' '\n' < /proc/$(systemctl --user show -p MainPID --value hermes-gateway)/cmdline | head -1`.
- Sub-agent models: implementer tasks on **sonnet**; pure-check steps (Task 0.1, Task 6 reads) may run on **haiku**; the final whole-branch/whole-change review on the most capable model.

## Review Focus

1. A gateway restart while a durable-park row is `parked`/`approved`/`executing` can lose or double-run an approval → Task 0 step 1 and Task 5 step 1 check the ledger immediately before any restart and stop if not clean.
2. Deleting `platform_toolsets` but leaving `known_plugin_toolsets` would silently switch off `durable_park` (`parked_action_execute`) and `wh` on every platform → Task 2 deletes both blocks and Task 6 #6 proves a park → deny round-trip still works.
3. A YAML edit that breaks the file (bad indentation after block deletion) would stop the gateway on restart → Task 2 step 6 parses the file with Hermes's own loader before anything restarts.
4. Port 9119 or 3978 still held by the `ubuntu` processes when the `hermes` services start → Task 5 step 2 stops `ubuntu` units first and step 3 checks `ss -ltnp` before starting `hermes` services.
5. `ubuntu` units coming back on reboot and fighting for the ports → Task 6 step 9 disables them and checks `is-enabled` = `disabled`.

---

### Task 0: Pre-flight and backups

**Files:**
- Create (VM): `/home/hermes/merge-backup-<UTC stamp>/` holding copies of both `config.yaml`, both `.env`, the four user units, `ledger.db`.
- Create (repo worktree): `/home/hermes/tapdash-agents-wt/one-agent-merge` on branch `ops/one-agent-merge`.

**Interfaces:**
- Produces: `BK=/home/hermes/merge-backup-<stamp>` (the backup dir path, echoed; later tasks and rollback use it) and `WT=/home/hermes/tapdash-agents-wt/one-agent-merge`.

- [ ] **Step 1: Ledger must be clean (haiku ok)**

Run on VM: `python3 -c "import sqlite3;c=sqlite3.connect('file:/home/hermes/.hermes/plugin-data/durable-park/ledger.db?mode=ro',uri=True);print(c.execute(\"select count(*) from parks where status in ('parked','approved','executing')\").fetchone()[0])"`
Expected: `0`. If not 0: STOP, report the rows (`select park_id,status,tool,created_at ...`) to Yaakov, do not continue.

- [ ] **Step 2: Take backups**

Run on VM (one script): `BK=/home/hermes/merge-backup-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p $BK/ubuntu $BK/hermes; cp ~/.hermes/config.yaml ~/.hermes/.env ~/.config/systemd/user/hermes-gateway.service $BK/hermes/; cp ~/.hermes/plugin-data/durable-park/ledger.db $BK/hermes/; sudo -n cp /home/ubuntu/.hermes/config.yaml /home/ubuntu/.hermes/.env /home/ubuntu/.config/systemd/user/hermes-gateway.service /home/ubuntu/.config/systemd/user/hermes-dashboard.service /home/ubuntu/.config/systemd/user/devtunnel-hermes.service $BK/ubuntu/; sudo -n chown -R hermes:hermes $BK; chmod -R go-rwx $BK; ls -la $BK/hermes $BK/ubuntu; echo BK=$BK`
Expected: 4 files under `hermes/` (config.yaml, .env, hermes-gateway.service, ledger.db) and 5 under `ubuntu/`; last line `BK=/home/hermes/merge-backup-...`. Record `BK`.

- [ ] **Step 3: Repo worktree**

Run on VM: `cd /home/hermes/tapdash-agents && git fetch -q origin && git worktree add -q -b ops/one-agent-merge /home/hermes/tapdash-agents-wt/one-agent-merge origin/main && git -C /home/hermes/tapdash-agents-wt/one-agent-merge log --oneline -1`
Expected: one line, the merge commit of PR #1 (spec present at `docs/specs/2026-10-20-one-agent-merge-design.md`).

---

### Task 1: Record the deviations (spec §4.1 amendment, plan, deploy-script warning)

**Files:**
- Modify: `docs/specs/2026-10-20-one-agent-merge-design.md` §4.1 (lines 49–54)
- Create: `docs/plans/2026-10-20-one-agent-merge.md` (this file)
- Modify: `durable-park/deploy/configure_hermes.sh` (add a comment after the shebang)

- [ ] **Step 1: Replace §4.1 items 1–3 in the spec with the five-row table from "Deviations from the spec" above** (keep items 4–5; renumber). Add one sentence under the table: "Found while planning on 2026-10-20: the lockdown is `hermes_tool_policy.py::desired_settings()`, which writes five settings."

- [ ] **Step 2: Add this plan at `docs/plans/2026-10-20-one-agent-merge.md`.**

- [ ] **Step 3: Add to `durable-park/deploy/configure_hermes.sh`, directly after the shebang line:**

```sh
# DO NOT RE-RUN on proto-agents. On 2026-10-20 Yaakov decided the agent gets every Hermes tool
# (withHuman/durable-park remains the only gate). This script re-applies the v1 tool lockdown
# via hermes_tool_policy.py. See docs/specs/2026-10-20-one-agent-merge-design.md §4.1.
```

- [ ] **Step 4: Docs check and commit**

Run in `$WT`: `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD` after staging; Expected: `docs check: ok`.
Commit message subject: `docs: amend merge spec §4.1 with the full lockdown; add implementation plan; warn in configure_hermes.sh`. Body explains the five settings. Trailer `Docs-Not-Needed: comment-only change to configure_hermes.sh; spec and plan are docs` plus the standard trailers.

---

### Task 2: Open up Hermes (config.yaml, no restart yet)

**Files:**
- Modify (VM): `/home/hermes/.hermes/config.yaml` — delete block at `^platform_toolsets:` (line 1168 → line before `^stt:`), delete block at `^known_plugin_toolsets:` (line 2311 → line before `^tools:`), replace the list under `agent.disabled_toolsets` (lines 1061–1095), add `  mode: off` under `^approvals:` (line 2616). Line numbers are from 2026-10-20; re-derive with grep, never hard-code.

**Interfaces:**
- Produces: a config that Hermes's loader accepts; `STOCK_DISABLED` = `[]`, the stock default `agent.disabled_toolsets` (step 2), reused by Task 6 #4.

- [ ] **Step 1: Confirm block boundaries fresh**

Run: `grep -n -E "^platform_toolsets:|^stt:|^known_plugin_toolsets:|^tools:|^approvals:|^  disabled_toolsets:" ~/.hermes/config.yaml`
Expected: exactly six lines, in the order `disabled_toolsets`, `platform_toolsets`, `stt`, `known_plugin_toolsets`, `tools`, `approvals`. If `tools:` is not the first top-level key after `known_plugin_toolsets:`, find the real next top-level key (`awk 'NR>L && /^[a-z_]+:/{print NR": "$0; exit}'`) and use that.

- [ ] **Step 2: Confirm the stock default disabled list**

Run with the install's python: `cd ~/.hermes/hermes-agent && $HPY -c "from hermes_cli import config_defaults as d; import re,inspect; src=inspect.getsource(d); print(src[src.find('disabled_toolsets'):src.find('disabled_toolsets')+1200])"`. Expected: the stock default is `disabled_toolsets: []` at `hermes_cli/config_defaults.py:279`, so there is no list to extract. Do not cross-check against the `ubuntu` install: its 31-entry list (with `clarify` and `stt`) is the "Blank Slate" setup preset, not stock. Set `STOCK_DISABLED = []`.

- [ ] **Step 3: Apply the edits with one python3 text script** (read file, operate on lines, write back; no YAML library):
  1. Delete lines from `^platform_toolsets:` up to (not including) the next line matching `^[a-z_]+:`.
  2. Delete lines from `^known_plugin_toolsets:` up to (not including) the next `^[a-z_]+:`.
  3. Replace the `^  disabled_toolsets:` line and the `^    - ` lines that follow it with the single line `  disabled_toolsets: []` (`STOCK_DISABLED` is empty, so there are no items).
  4. After the line `^approvals:` insert `  mode: off` as the next line.
  Write to `~/.hermes/config.yaml.new`, then `mv` over the original (owner `hermes`, mode unchanged).

- [ ] **Step 4: Verify the text**

Run: `grep -n -c -E "^platform_toolsets:|^known_plugin_toolsets:" ~/.hermes/config.yaml; grep -n -A1 "^approvals:" ~/.hermes/config.yaml; awk 'BEGIN{n=0} /^  disabled_toolsets:/{f=1;next} f&&/^    - /{n++;next} f{print n; exit}' ~/.hermes/config.yaml; grep -c -E "^    - (clarify|terminal|file|skills|vision|browser-cdp|browser-use)$" ~/.hermes/config.yaml`
Expected: `0`; `approvals:` followed by `  mode: off`; `0`; `0`.

- [ ] **Step 5: withHuman settings untouched**

Run: `diff <(sed -n '/^plugins:/,/^model:/p' $BK/hermes/config.yaml) <(sed -n '/^plugins:/,/^model:/p' ~/.hermes/config.yaml) && echo PLUGINS_SAME; diff <(sed -n '/^mcp_servers:/,/^approvals:/p' $BK/hermes/config.yaml) <(sed -n '/^mcp_servers:/,/^approvals:/p' ~/.hermes/config.yaml) && echo MCP_SAME`
Expected: `PLUGINS_SAME` and `MCP_SAME`.

- [ ] **Step 6: Hermes's own loader accepts the file**

Run: `cd ~/.hermes/hermes-agent && HERMES_HOME=/home/hermes/.hermes $HPY -c "from hermes_cli.config import load_config; c=load_config(); print('approvals', c.get('approvals',{}).get('mode')); print('platform_toolsets', c.get('platform_toolsets')); print('known', c.get('known_plugin_toolsets')); print('disabled', len(c['agent']['disabled_toolsets'])); print('tool_search', c.get('tools',{}).get('tool_search',{}).get('enabled'))"`
Expected: `approvals False` (the bare `off` in YAML is the boolean false; Hermes normalises it to the string "off" in `tools/approval_context.py` `_normalize_approval_mode`), `platform_toolsets None` (or `{}`), `known None` (or `{}`), `disabled 0`, `tool_search off`.

- [ ] **Step 7: The old lockdown check now reports the tools are open** (inverse evidence)

Run: `cd ~/.hermes/hermes-agent && HERMES_HOME=/home/hermes/.hermes $HPY -I /home/hermes/durable-park/deploy/hermes_tool_policy.py check 2>&1 | tail -5; echo exit=$?`
Expected: exit `1` and the output names forbidden tools (e.g. `terminal`, `read_file`) as present for `telegram`. That is the desired state now.

---

### Task 3: Teams into the `hermes` install (no restart yet)

**Files:**
- Modify (VM): `/home/hermes/.hermes/.env` — append 5 lines.

- [ ] **Step 1: Teams SDK present in the `hermes` install**

Run: `cd ~/.hermes/hermes-agent && $HPY -c "import microsoft_teams.apps; print('teams sdk ok')" 2>&1 | tail -1`
If `teams sdk ok`: skip step 2. Otherwise step 2.

- [ ] **Step 2: Install it the documented way**

Run: `cd ~/.hermes/hermes-agent && $HPY -c "import pm; pm.sync_venv(['teams'], explicit=True)" 2>&1 | tail -3` then re-run step 1.
Expected: `teams sdk ok`. If the import still fails, STOP and report the error text.

- [ ] **Step 3: Copy the four TEAMS_* keys and add TEAMS_HOST, by script, values never printed**

Run: `python3 - <<'ENVCOPY_4b9e'
import re,subprocess,pathlib
src=subprocess.run(['sudo','-n','cat','/home/ubuntu/.hermes/.env'],capture_output=True,text=True,check=True).stdout.splitlines()
want=['TEAMS_CLIENT_ID','TEAMS_CLIENT_SECRET','TEAMS_TENANT_ID','TEAMS_ALLOWED_USERS']
lines=[l for l in src if l.split('=',1)[0] in want]
assert [l.split('=',1)[0] for l in lines]==want, [l.split('=',1)[0] for l in lines]
dst=pathlib.Path('/home/hermes/.hermes/.env'); txt=dst.read_text()
assert 'TEAMS_' not in txt
dst.write_text(txt.rstrip('\n')+'\n\n# Teams bot "TapDash Agent" (moved from the ubuntu install, 2026-10-20)\n'+'\n'.join(lines)+'\nTEAMS_HOST=127.0.0.1\n')
print('appended', [l.split('=',1)[0] for l in lines]+['TEAMS_HOST'])
ENVCOPY_4b9e`
Expected: `appended ['TEAMS_CLIENT_ID', 'TEAMS_CLIENT_SECRET', 'TEAMS_TENANT_ID', 'TEAMS_ALLOWED_USERS', 'TEAMS_HOST']`.

- [ ] **Step 4: Verify keys only**

Run: `grep -c -E "^TEAMS_(CLIENT_ID|CLIENT_SECRET|TENANT_ID|ALLOWED_USERS|HOST)=" ~/.hermes/.env; grep -E "^TEAMS_HOST=" ~/.hermes/.env; stat -c "%a %U" ~/.hermes/.env`
Expected: `5`; `TEAMS_HOST=127.0.0.1`; `600 hermes` (if mode is wider than 600, `chmod 600`).

---

### Task 4: Dashboard into the `hermes` install (no start yet)

**Files:**
- Modify (VM): `/home/hermes/.hermes/config.yaml` — append `dashboard:` block (4 lines).
- Create (VM): `/home/hermes/.config/systemd/user/hermes-dashboard.service`.

- [ ] **Step 1: Copy `dashboard.basic_auth` by script, values never printed**

Run: `python3 - <<'DASHCOPY_7c1d'
import subprocess,pathlib,re
src=subprocess.run(['sudo','-n','cat','/home/ubuntu/.hermes/config.yaml'],capture_output=True,text=True,check=True).stdout.splitlines()
i=src.index('dashboard:'); block=[src[i]]
for l in src[i+1:]:
    if re.match(r'^[a-z_]+:',l): break
    block.append(l)
keys=[l.strip().split(':')[0] for l in block[1:] if l.strip()]
assert keys==['basic_auth','username','password_hash','secret'], keys
dst=pathlib.Path('/home/hermes/.hermes/config.yaml'); txt=dst.read_text()
assert '\ndashboard:' not in txt
dst.write_text(txt.rstrip('\n')+'\n# Web dashboard login (moved from the ubuntu install, 2026-10-20)\n'+'\n'.join(block)+'\n')
print('appended dashboard block with keys', keys)
DASHCOPY_7c1d`
Expected: `appended dashboard block with keys ['basic_auth', 'username', 'password_hash', 'secret']`.

- [ ] **Step 2: Loader accepts it**

Run: `cd ~/.hermes/hermes-agent && HERMES_HOME=/home/hermes/.hermes $HPY -c "from hermes_cli.config import load_config; d=load_config()['dashboard']['basic_auth']; print(sorted(d.keys()), d['username'])"`
Expected: `['password_hash', 'secret', 'username'] TapDash`.

- [ ] **Step 3: Write the unit** `/home/hermes/.config/systemd/user/hermes-dashboard.service`:

```ini
[Unit]
Description=Hermes Dashboard over Tailscale (hermes install)
After=network-online.target hermes-gateway.service
Wants=network-online.target

[Service]
ExecStart=/home/hermes/.hermes/hermes-agent/.hermes/bin/hermes dashboard --host 100.99.171.75 --port 9119 --no-open
WorkingDirectory=/home/hermes/.hermes
Environment="HERMES_HOME=/home/hermes/.hermes"
Environment="PATH=/home/hermes/.hermes/hermes-agent/node_modules/.bin:/home/hermes/.hermes/tools/node-26.7.0-linux-x64/bin:/home/hermes/.hermes/tools/npm-12.0.2-linux-x64/bin:/home/hermes/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Then: `systemctl --user daemon-reload && systemctl --user cat hermes-dashboard.service | head -3`
Expected: the unit is listed (not started).

---

### Task 5: Cutover

- [ ] **Step 1: Ledger clean (again, immediately before restart)** — same command as Task 0 step 1. Expected `0`; else STOP.

- [ ] **Step 2: Stop the `ubuntu` gateway and dashboard**

Run: `sudo -n -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop hermes-gateway.service hermes-dashboard.service; sleep 3; sudo -n -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active hermes-gateway.service hermes-dashboard.service devtunnel-hermes.service`
Expected: `inactive`, `inactive`, `active`.

- [ ] **Step 3: Ports are free**

Run: `sudo -n ss -ltnp | grep -E ":(3978|9119) " || echo PORTS_FREE`
Expected: `PORTS_FREE`.

- [ ] **Step 4: Restart the `hermes` gateway**

Run: `systemctl --user restart hermes-gateway.service; sleep 25; systemctl --user is-active hermes-gateway.service; tail -40 ~/.hermes/logs/gateway.log | grep -E "teams|telegram|durable-park|ERROR|Traceback" | cut -c1-180`
Expected: `active`; lines containing `✓ telegram connected`, `[teams] Webhook server listening on 127.0.0.1:3978`, `✓ teams connected`, `durable-park loaded: ... risky tool pattern(s)`; no `Traceback`. If Teams deps install runs first, allow up to 60 s more and re-tail.

- [ ] **Step 5: Start the `hermes` dashboard**

Run: `systemctl --user enable --now hermes-dashboard.service; sleep 8; systemctl --user is-active hermes-dashboard.service; curl -s -o /dev/null -w "%{http_code}\n" http://100.99.171.75:9119/`
Expected: `active`; HTTP `200` or `401` (login page / auth challenge), not `000`.

- [ ] **Step 6: Listeners**

Run: `sudo -n ss -ltnp | grep -E ":(3978|9119|8644|8791) " | sed -E 's/ +/ /g'`
Expected: `127.0.0.1:3978` and `100.99.171.75:9119` owned by `hermes`-user processes; `127.0.0.1:8644` (webhook) and `127.0.0.1:8791` (bridge) still present.

---

### Task 6: Verification per spec §6 (then retire `ubuntu`)

Each row needs its evidence pasted into the task report. Rows 2, 3 and 6 need Yaakov at Teams/Telegram/withHuman; ask him in one message with the three things to do, then wait.

- [ ] **#1 Gateway up with both channels** — evidence from Task 5 step 4 and 6.
- [ ] **#2 Teams** — Yaakov messages TapDash Agent in Teams → reply. Evidence: `grep -c "platform=teams" ~/.hermes/logs/gateway.log` increased, and Yaakov confirms.
- [ ] **#3 Telegram** — Yaakov messages the bot → reply. Evidence: same with `platform=telegram`, Yaakov confirms.
- [ ] **#4 Tools open** — Yaakov (Telegram): "run `uname -a` and tell me the output" → the reply contains `Linux vps-c204f492`. Evidence: `grep -c '"tool":"terminal"' ~/.hermes/logs/agent.log` (or the equivalent tool-call line) increased; reply text.
- [ ] **#5 Hermes approvals off** — config evidence from Task 2 step 6 (`approvals False`, which Hermes reads as "off"); Yaakov: "create /tmp/x then run `rm -f /tmp/x`" → done with no approval prompt in chat.
- [ ] **#6 withHuman still gates** — Yaakov (Telegram): "record a $1 payment from Kookies by Kathy against invoice 1016 (sandbox)". Expected: bot says it is waiting for approval; `select park_id,status from parks order by created_at desc limit 1` = `parked`; request visible in withHuman; Yaakov **denies** it; chat receives the denial; ledger row = `denied`. Evidence: the SQL output before and after, and Yaakov's confirmation.
- [ ] **#7 Dashboard** — Yaakov opens `http://100.99.171.75:9119/`, logs in as `TapDash`, Sessions tab lists the Telegram sessions. Evidence: Yaakov confirms; `grep -c "GET /api/sessions" ~/.hermes/logs/*.log` > 0 (or dashboard journal: `journalctl --user -u hermes-dashboard -n 20`).
- [ ] **#8 Cron intact** — `~/.hermes/hermes-agent/.hermes/bin/hermes cron list 2>/dev/null | grep durable-park-sweep` shows the job; within 6 min `grep -c durable-park ~/.hermes/logs/gateway.log` increases or `ls -t ~/.hermes/cron/output | head -1` is newer than the restart.
- [ ] **#9 Retire `ubuntu` units** — only after #1–#8 pass: `sudo -n -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user disable hermes-gateway.service hermes-dashboard.service; sudo -n -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-enabled hermes-gateway.service hermes-dashboard.service devtunnel-hermes.service`
  Expected: `disabled`, `disabled`, `enabled`. `sudo -n ss -ltnp | grep -E ":(3978|9119) "` shows only `hermes`-owned sockets.

**Rollback (any failure not fixed within a few minutes):** `cp $BK/hermes/config.yaml ~/.hermes/config.yaml; cp $BK/hermes/.env ~/.hermes/.env; systemctl --user stop hermes-dashboard.service; systemctl --user restart hermes-gateway.service; sudo -n -u ubuntu XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start hermes-gateway.service hermes-dashboard.service` then verify Telegram and the 9119 login page, and report.

---

### Task 7: Docs follow the change (repo "one rule")

**Files:**
- Modify: `durable-park/docs/architecture.md` — the paragraph at lines ~27–34 that says the agent has no file or terminal tools (the tool lockdown), and the "Test VM proto-agents" diagram if it lists the toolset.
- Modify: `durable-park/docs/runbook.md` — "Access and ground rules" (restart now also affects Teams + dashboard; the dashboard unit name), "Rollback".
- Modify: `durable-park/docs/backlog.md` — item 6 ("check the tool lockdown at every gateway start") and item 7: mark as superseded by the 2026-10-20 decision; add "Later: widen `risky_tools` beyond QuickBooks (the lock-down phase)".
- Modify: `durable-park/README.md` — status line: still v1 accepted, add "since 2026-10-20 the proto-agents agent runs with the full Hermes toolset; durable-park gates QuickBooks money moves only".

- [ ] **Step 1: Make the four edits.** Each must state the new truth in one or two sentences; do not delete the history of the lockdown, say when and why it was lifted and link the spec.
- [ ] **Step 2: Docs check** — `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD`; Expected `docs check: ok`.
- [ ] **Step 3: Commit** — subject `docs: proto-agents agent now runs the full Hermes toolset (merge done 2026-10-20)`; body lists the evidence rows from Task 6 (numbers, not secrets).
- [ ] **Step 4: Push the branch and open a PR** `ops/one-agent-merge` → `main` titled "One agent on proto-agents: spec amendment, plan, docs after merge" with the Task 6 evidence table in the body. PR body ends with the standard attribution lines.
- [ ] **Step 5: Remove the worktree after merge** — `git -C /home/hermes/tapdash-agents worktree remove /home/hermes/tapdash-agents-wt/one-agent-merge` (only after Yaakov merges).
