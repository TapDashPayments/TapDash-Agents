# All tools, withHuman decides every call — Implementation Plan (spec §8 items 1–7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tell the truth in the docs about the re-applied lockdown, take root away from the agent's user (Clean Setup
Phase 1), make durable-park ask withHuman about every tool call (instant allow/refuse, park only when a human must
decide), give withHuman the matching pipeline, and make the Hermes tool policy one stored list that is checked at
every gateway start.

**Architecture:** Three ops tasks on `proto-agents` (docs, live clone, users/connector/SSH) and eleven code tasks in
`durable-park/` (sweep, ledger, AAP request building, circuit breaker, park hook, plugin wiring, pipeline file, tool
policy, start-time check), then one go-live task that reviews, deploys and proves spec §5 #6–#12. The hook keeps one
flow: ledger row first, one AAP request through a shared `Gate`, then `Allow` → row `allowed` and run, `Deny` → row
`refused` and block, `Pending` → park exactly as today.

**Tech Stack:** Python 3.14, `agentapprovalprotocol==0.3.3`, `httpx==0.28.1`, SQLite (WAL), pytest 9.1.1, Hermes Agent
v0.22.2 plugin API, systemd (system + user units), withHuman management API (MCP tools), bash.

**Spec:** `docs/specs/2026-10-21-all-tools-withhuman-design.md` (merged in PR #4). Research behind both:
`/home/hermes/tapdash-agents-wt/one-agent-merge/.superpowers/research/{aap-research,clean-setup-research,relock-report}.md`
on proto-agents. Repo rules: `AGENTS.md` (read it first).

## Deviations from the spec (found while planning; Task 1 writes them into the spec as §9)

Each line: what the spec says → what is true → what this plan does. **Yaakov approves these before Task 1.**

| # | Spec | Found | Plan |
|---|---|---|---|
| D1 | §8 items 1–2: fix PR #3, then merge it | PR #3 was merged at `1847dff` **without** the fix; main now says the full toolset is live. Live clone is 15 commits behind (`5dab848..1847dff`, docs + one comment only) | Task 1 fixes main directly; Task 2 is only the fast-forward |
| D2 | §4.1 step 7 (SSH) after the connector move | `ubuntu` has a password (`passwd -S` = `P`), ufw allows 22/tcp from anywhere, effective `passwordauthentication yes`, and `ubuntu` has NOPASSWD sudo: a guessed password is root from the internet | Task 3 does SSH **first**. The `ubuntu` password is kept (OVH console fallback); only SSH password login goes off |
| D3 | §4.1 steps 2–4 | The connector also keeps its OAuth state in `~hermes/.local/share/fastmcp/oauth-proxy/`; `server.py` names hermes on lines 149, 172, 178, 181, 241, 274, 305; `set-github-oauth.sh` says "run as hermes"; the venv has no requirements file | Task 3 copies the OAuth state, rebuilds the venv from `pip freeze`, and patches all seven strings plus `set-github-oauth.sh` |
| D4 | §4.3 row 9 (anything else → human) | Gateway calls of **non-money** QuickBooks tools arrive with server `quickbooks` and no row approves them, so every QuickBooks read would ask Yaakov | Task 10 adds row **2b**: server `quickbooks` and not money → approve |
| D5 | §4.3 row 3: server starts with `park:` → approve | Would auto-approve risky DocuSign (or any future gateway server) calls that `DEFAULT_RISKY_TOOLS` parks today | Row 3 is `server equals park:quickbooks`; other gateway servers fall to row 9 (human) until they get a rule |
| D6 | §4.3 rows 4–7 tool names | Hermes 0.22.2 names: `todo_list` (not `todo*`), `image_generate` (not `image_gen*`), `process_manage`, `cronjob_manage`; there is **no** `send_message` tool; `memory` is one tool. Hooks see the canonical name | Rows use the real names (legacy aliases added to the deny row). Row 5 is dropped. Row 6 (browser) is written when group (c) opens (item 8): until then browser calls hit row 9 (human) |
| D7 | §4.3 "rule 1 = today's rule 1, narrowed" | Today's rule 2 regex starts with `^(quickbooks__)?`, so it cannot match `mcp__wh__…` names | Row 1 gets its own regex `^mcp__wh__quickbooks__(create\|update\|delete)[_-](…12 types…)$` |
| D8 | §4.3 "`preview_pipeline` per row" | `preview_pipeline` cannot set `/request/server` (refused as an extra property); server-based rows never match in a preview | Task 10 proves every row offline with a small condition evaluator in the tests, previews the agent/tool half, and Task 13 reads the live audit trace (`request_approval_block.evaluated`) for each row it exercises |
| D9 | §4.2 "block if another `pre_tool_call` callback is registered **after** durable-park" | Hermes merges `modify` directives from **every** callback over the original args, whatever the order; shell hooks and outbound webhooks also land in the same list | The hook blocks every non-exempt call while **any** other `pre_tool_call` callback is registered, unless it is named in the new setting `observer_hooks` (default empty). Checked on every call |
| D10 | §4.2 step 5 `Allow` | The SDK returns `Allow` in async mode only when `create` already answers `approved`. Not yet proven for withHuman's pipeline approve | Task 13 step 2 probes it live before go-live and **stops** if `create` answers `pending` |
| D11 | §4.2 step 5 `Pending` / "already waiting" | A request id stored on a row that is then cancelled makes the sweep re-wake it every 5 min; a wake that lands before the hook's own transition would inject a stray APPROVED/DENIED | Instant decisions store the request id only in the final `parked → allowed|refused` transition; the "already waiting" cancel keeps the id in `note` only; `set_request_id` becomes a CAS. "One waiting park per chat" counts only rows that have a request id or are past `parked` |
| D12 | §4.2 `ledger_retention_days` "new setting" | The sweep is stdlib-only and cannot read plugin settings | Sweep flag `--retention-days` (default 30), passed by the wrapper `configure_hermes.sh` writes |
| D13 | §4.2 breaker "3 consecutive transport failures" | The SDK turns transport, credential and protocol failures all into `Deny(status="error")` | The breaker counts every `Deny` with status `error`; its state is a status file the sweep alerts on |
| D14 | §4.2 `context.platform` | Not in the ledger or the hook kwargs; session keys look like `agent:main:telegram:dm:<id>` | `platform` = third `:` field of `session_key` (`"unknown"` otherwise), so `build_call` stays a pure function of the row (the executor rebuilds the identical request) |
| D15 | §8 item 7 `apply --allow <toolset>...` | `configure_hermes.sh` and `hermes_mcp.sh` call `apply` with no flags; a flag would be silently dropped on their next run (re-lock) | The allowed list is a root-owned file `/etc/durable-park/allowed_toolsets`; `apply` and `check` read it. Toolsets that need Phase 2 are refused |
| D16 | §8 item 7 "`check` at every gateway start" | No gateway-start plugin hook exists; a plugin that raises in `register` is unloaded (fails **open**); a failing `ExecStartPre` takes Telegram/Teams and the sweep down | A user-unit drop-in runs the check as `ExecStartPre=-` and writes `tool_policy_status.json`; the plugin fails closed at load when that report is missing, failed or older than 15 min; the sweep alerts |
| D17 | §4.2 "parallel tool calls run the hook in parallel" | Hermes serialises the `pre_tool_call` stage of a batch behind one lock | No code change; latency adds up per call (the spec's per-call estimate stands) |
| D18 | §5 #6/#7 test with `web_search` and "a denied tool" | Under the current lockdown only `clarify`, `parked_action_execute` and `mcp__wh__quickbooks__*` are reachable | Instant approve is proven with a QuickBooks read; instant deny with a temporary pipeline revision that denies one QuickBooks read, then rolled back |

## Global Constraints

- VM `proto-agents` (Tailscale `100.99.171.75`) is the only place to experiment. **Never touch the production Hermes host.**
- Live clone `/home/hermes/tapdash-agents` is never edited in place; code work happens on a branch (in this container
  or a worktree under `/home/hermes/tapdash-agents-wt/`). Live changes arrive only by `git pull --ff-only` of merged `main`.
- **Never print, paste or commit a secret** (`/etc/durable-park/*`, `.env`, `config.yaml` secret values, tokens). Show
  key names only. Secrets are typed by an admin with `read -rs`.
- Every heredoc sent through the connector has a **unique** delimiter.
- Root steps are run by Yaakov (admin, user `ubuntu`); the plan gives the exact one-line command.
- Before any `hermes-gateway` or bridge restart: `sqlite3 -readonly /home/hermes/.hermes/plugin-data/durable-park/ledger.db "select count(*) from parks where status in ('parked','approved','executing')"` prints `0`, or stop.
- Tests: `cd durable-park && .venv/bin/pytest -q -W error` green; write the failing test first. Unit tests never reach the
  network or read real credentials (`tests/conftest.py`). Today: 412 collected, 409 pass, 3 skipped.
- Docs change in the same commit as the code they describe; before each push run
  `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD` and `--verify-map`. New tracked non-`.md`
  files get a rule in `.github/docs-map.json` (which itself requires a `CONTRIBUTING.md` touch).
- Exempt tools (spec Q1): exactly `parked_action_execute`, `clarify`. Final default for `proto-agents`: **human**.
- AAP request constants (unchanged): mode `async`, timeout `168h`, retry budget `8.0` s, HTTP timeout `10.0` s.
- Redacted argument keys: any key whose lower-cased name contains `password`, `token`, `secret`, `authorization` or
  `api_key`, at any depth → value `"[redacted]"`. Size cap: canonical JSON over **16384** bytes → every string value cut
  to **1024** characters and `context.args_truncated = true`.
- Breaker: **3** consecutive `error` verdicts open it for **30** s. Sweep thresholds: request-less `parked` rows older
  than **10 min** are cancelled; `allowed|refused|cancelled` rows older than **30 days** are deleted.
- Money tool regex (gateway form, rev 8, verbatim):
  `^(quickbooks__)?(create|update|delete)[_-](payment|bill_payment|transfer|deposit|journal_entry|refund_receipt|sales_receipt|credit_memo|vendor_credit|purchase|invoice|bill)$`
- Phase-2-only toolsets (spec A4, §4.4 e–g): `terminal`, `file`, `code_execution`, `delegation`, `cronjob`, `skills`,
  `computer_use`. They stay disabled in Hermes and denied in the pipeline.
- Commit trailers: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and the session's `Claude-Session:` line.
  No model names in commit messages, PR text or code.
- Branches: Task 1 `docs/relock-truth`; Task 3 docs `ops/clean-setup-phase1`; Tasks 4–10 `feat/ask-every-call`;
  Tasks 11–12 `feat/tool-policy-file`. One draft PR each; each is reviewed before merge.

## Review Focus

1. **A tool call outside a chat** (Hermes CLI turn, `tests/live/test_gateway_smoke.py::test_one_shot_cli_turn_returns_the_sandbox_company_name`,
   a delegated sub-agent): there is no session key, so every non-exempt tool is now refused, not only money tools.
   A person expects a clear refusal, not a hang → Task 8 test `test_no_session_blocks_every_non_exempt_tool` and
   Task 9 updates the live smoke test to expect the refusal text.
2. **A provider that reuses tool-call ids** within a session (ledger ids today look like `call_515c11f78…`, unique):
   with a row per call, a reused id with different args is `ID_REUSED` and the same args a second time is "already
   allowed" — never a second run → Task 8 test `test_reused_call_id_after_allow_is_not_run_again`.
3. **A deploy while a park waits:** `build_call` now adds `context`, so a park created by the old code would replay as
   a *new* request → Task 13 deploys only with zero active rows and Task 6 test `test_build_call_is_pure_function_of_row`
   pins that replay rebuilds the identical body.
4. **A wake for a call that was decided instantly** (withHuman sends `request.resolved` for every request with
   `resume`): the chat must get no APPROVED/DENIED message → Task 8 test `test_wake_for_allowed_or_refused_row_injects_nothing`.
5. **Arguments that are not JSON-clean or huge** (bytes, floats like NaN, a 2 MB file body): the SDK refuses NaN and
   non-JSON types; the call must be refused, the row `refused`/`cancelled`, never a crash that unloads the hook →
   Task 6 test `test_non_json_arguments_raise` and Task 8 test `test_unserialisable_arguments_block_and_cancel`.

---

### Task 1: Docs tell the truth about the re-lock (spec §8 item 1)

**Files:**
- Modify: `durable-park/deploy/configure_hermes.sh:2-4` (header comment)
- Modify: `README.md:64`; `durable-park/README.md:9-18,53,76`
- Modify: `durable-park/docs/architecture.md:27-55,298-309,327`
- Modify: `durable-park/docs/runbook.md:33-43,106,199-201`
- Modify: `durable-park/docs/receiver-setup.md:48-49,54-58,126-136` (heading at :126 and its only anchor link at :56-58 change together)
- Modify: `durable-park/docs/gateway-setup.md:3-5`
- Modify: `durable-park/docs/backlog.md` items 0, 6, 7 and "Later"
- Modify: `docs/specs/2026-10-21-all-tools-withhuman-design.md` (§2 row "Open PR #3", §8 items 1–2, new §9 = the Deviations table above)
- Modify: `docs/specs/2026-10-20-one-agent-merge-design.md:3`, `docs/plans/2026-10-20-one-agent-merge.md` (one "superseded" line under the title; no rewrite of the record)

**Interfaces:** Produces nothing code uses. Wording every replacement must carry (copy, adapt only grammar):

> Since 2026-10-21 03:16 UTC the v1 tool lockdown is re-applied on proto-agents as a stopgap: the agent can reach only
> `clarify`, `parked_action_execute` and `mcp__wh__quickbooks__*` (58 platform keys; `hermes_tool_policy.py check` ok).
> Hermes approvals stay off. The user `hermes` still has passwordless sudo until Clean Setup Phase 1
> ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1); the lockdown is a configuration guard,
> not a permission boundary. Tools open again only through that spec (§4.4). Re-running `configure_hermes.sh` or
> `hermes_tool_policy.py` re-applies the current state and restarts the gateway, so the "no parked/approved/executing
> row" rule applies.

- [ ] **Step 1:** `git switch -c docs/relock-truth origin/main`.
- [ ] **Step 2:** Rewrite each listed passage with the wording above. Backlog: item 0 → "decided: Clean Setup Phase 1
  (spec §4.1) then Phase 2 before local tools; docker backend and `approvals.deny` dropped"; item 6 → open again (no
  strikethrough), "planned as spec §8 item 7"; item 7 → "planned as spec §4.5 (§8 item 10), required before file,
  terminal or code tools open"; "Later: Widen `risky_tools`…" → "withHuman decides every call (spec 2026-10-21 §4.2)".
  `configure_hermes.sh` header → `# On proto-agents this script re-applies the current tool policy (v1 lockdown, re-applied 2026-10-21 03:16 UTC). It restarts the gateway: run it only when no ledger row is parked, approved or executing. See docs/specs/2026-10-21-all-tools-withhuman-design.md.`
- [ ] **Step 3:** Spec: §2 row "Open PR #3" → "PR #3 merged at `1847dff` without the item 1 fix (fixed by `docs/relock-truth`)"; §8 items 1–2 → "done (see §9 D1)"; append §9 "Amendments from planning (2026-10-21)" = the Deviations table of this plan.
- [ ] **Step 4: Verify** `grep -rn -E "lockdown (was )?lifted|full (Hermes )?toolset|DO NOT RE-RUN|not applied on proto-agents" --include=*.md --include=*.sh .` lists only the dated records (2026-10-20 spec/plan, acceptance doc). Run the docs check and `node` mermaid check as CI does (`tools/docs_check/validate_mermaid.mjs`) → exit 0. `cd durable-park && .venv/bin/pytest -q -W error tests/test_configure_script.py tests/test_tool_policy.py` → pass.
- [ ] **Step 5:** Commit `docs: the v1 lockdown is back on proto-agents (re-applied 2026-10-21 03:16 UTC); record planning amendments`, push, open draft PR, get Yaakov's review, merge.

### Task 2: Fast-forward the live clone (spec §8 item 2)

**Files:** none in the repo. VM: `/home/hermes/tapdash-agents`.

- [ ] **Step 1:** As hermes (connector, before Task 3): `git -C ~/tapdash-agents status -sb` → clean, on `main`.
- [ ] **Step 2:** `git -C ~/tapdash-agents pull --ff-only` → fast-forward to the Task 1 merge commit.
- [ ] **Step 3: Verify** `git -C ~/tapdash-agents log --oneline -1` equals `origin/main`; `git -C ~/tapdash-agents diff --stat ad4c35d..HEAD -- durable-park/plugin durable-park/bridge durable-park/receiver durable-park/scripts` prints nothing (docs and the one comment only, so no restart).

### Task 3: Clean Setup Phase 1 — SSH, connector to `ubuntu`, no sudo for `hermes` (spec §8 item 3, §4.1)

**Files:**
- VM (Yaakov, root): `/etc/ssh/sshd_config.d/50-cloud-init.conf`, `/etc/systemd/system/hermes-shell-mcp.service`, `/home/ubuntu/hermes-shell-mcp/`, `/home/ubuntu/.local/share/fastmcp/`, `/etc/sudoers.d/90-hermes` (deleted), `/home/ubuntu/.config/systemd/user/hermes-{gateway,dashboard}.service` (deleted)
- Repo (branch `ops/clean-setup-phase1`): `durable-park/docs/architecture.md` §6, `durable-park/docs/runbook.md` ("Deploy a change", "Who does what"), `AGENTS.md` rule 3, `durable-park/docs/backlog.md` item 0 → done

**Interfaces:** Produces: admin commands run as `ubuntu`; hermes files via `sudo -u hermes -H bash -lc '<cmd>'`; hermes user units via `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user <verb> <unit>`; git via `sudo -u hermes git -C /home/hermes/tapdash-agents …`. Every later task uses these forms.

- [ ] **Step 1 (Yaakov):** Open `tailscale ssh ubuntu@proto-agents` and keep it open until Step 9; know the OVH console as last resort.
- [ ] **Step 2 (Yaakov) — SSH passwords off first (D2):** `sudo sed -i 's/^PasswordAuthentication yes$/PasswordAuthentication no/' /etc/ssh/sshd_config.d/50-cloud-init.conf && sudo sshd -t && sudo systemctl reload ssh`
  **Verify:** `sudo sshd -T | grep -i '^passwordauthentication'` → `passwordauthentication no`; a new `tailscale ssh ubuntu@proto-agents` still logs in.
- [ ] **Step 3 (Yaakov) — rotate the exposed dashboard password:** the value of `dashboard.basic_auth.secret` in `~hermes/.hermes/config.yaml` was printed into a planning session on 2026-10-21. Set a new one typed with `read -rs` (Claude prepares the exact text-edit command that keeps comments; never round-trip the YAML), then `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user restart hermes-dashboard`. **Verify:** dashboard login works with the new value only.
- [ ] **Step 4 (Claude, as hermes, before the move):** write `/home/hermes/clean-setup/server.py.patch` changing the seven strings of D3 to: runs as Linux user `ubuntu` (admin, passwordless sudo), starts in `/home/ubuntu`, Hermes files and units belong to `hermes` and are reached with the forms in **Interfaces**; and `set-github-oauth.sh` to say "Run as user ubuntu" with path `~/hermes-shell-mcp/.env`. Show the diff to Yaakov.
- [ ] **Step 5 (Yaakov) — copy the connector:**
  `sudo install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/hermes-shell-mcp /home/ubuntu/.local /home/ubuntu/.local/share /home/ubuntu/.local/share/fastmcp`
  `sudo -u hermes /home/hermes/hermes-shell-mcp/.venv/bin/python -m pip freeze | sudo -u ubuntu tee /home/ubuntu/hermes-shell-mcp/requirements.lock >/dev/null`
  `sudo cp -a /home/hermes/hermes-shell-mcp/{server.py,set-github-oauth.sh,audit.log,bg,.env} /home/ubuntu/hermes-shell-mcp/ && sudo cp -a /home/hermes/.local/share/fastmcp/oauth-proxy /home/ubuntu/.local/share/fastmcp/ && sudo chown -R ubuntu:ubuntu /home/ubuntu/hermes-shell-mcp /home/ubuntu/.local && sudo chmod 600 /home/ubuntu/hermes-shell-mcp/.env`
  `sudo -u ubuntu python3 -m venv /home/ubuntu/hermes-shell-mcp/.venv && sudo -u ubuntu /home/ubuntu/hermes-shell-mcp/.venv/bin/pip install -q -r /home/ubuntu/hermes-shell-mcp/requirements.lock && sudo cat /home/hermes/clean-setup/server.py.patch | sudo -u ubuntu patch -d /home/ubuntu/hermes-shell-mcp -p1` (`/home/hermes` is 750, so `ubuntu` reads the patch through `sudo cat`)
  **Verify:** `sudo -u ubuntu /home/ubuntu/hermes-shell-mcp/.venv/bin/python -c 'import fastmcp; print(fastmcp.__version__)'` → `4.1.1`; `grep -c hermes /home/ubuntu/hermes-shell-mcp/server.py` shows only the documented `sudo -u hermes` forms.
- [ ] **Step 6 (Yaakov) — switch the unit:**
  `sudo cp /etc/systemd/system/hermes-shell-mcp.service /root/hermes-shell-mcp.service.bak && sudo sed -i -e 's#^User=hermes$#User=ubuntu#' -e 's#^Group=hermes$#Group=ubuntu#' -e 's#/home/hermes/hermes-shell-mcp#/home/ubuntu/hermes-shell-mcp#g' /etc/systemd/system/hermes-shell-mcp.service && sudo systemctl daemon-reload && sudo systemctl restart hermes-shell-mcp`
  **Verify:** `systemctl show hermes-shell-mcp -p User -p WorkingDirectory` → `User=ubuntu`, `/home/ubuntu/hermes-shell-mcp`; `ss -ltnp | grep 8792` listening; from Claude, `run_command id` → `uid=1000(ubuntu)` (if Claude lost auth: reconnect the Proto-Agents connector in Claude settings). Yaakov updates the connector description in Claude to the new server instructions text. **Rollback:** `sudo cp /root/hermes-shell-mcp.service.bak /etc/systemd/system/hermes-shell-mcp.service && sudo systemctl daemon-reload && sudo systemctl restart hermes-shell-mcp`. Do not continue until Verify passes.
- [ ] **Step 7 (Yaakov) — remove `hermes`'s sudo:** `sudo rm /etc/sudoers.d/90-hermes && sudo visudo -c`
  **Verify:** `sudo -u hermes sudo -n true` → "a password is required"; `ls /etc/sudoers.d/` has no `90-hermes`.
- [ ] **Step 8 (Yaakov, as ubuntu) — stale units:** `rm /home/ubuntu/.config/systemd/user/hermes-gateway.service /home/ubuntu/.config/systemd/user/hermes-dashboard.service && systemctl --user daemon-reload`
  **Verify:** `systemctl --user list-unit-files 'hermes*'` (as ubuntu) lists nothing.
- [ ] **Step 9 — prove nothing broke (spec §5 #1–#5):** active: `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user is-active hermes-gateway hermes-dashboard`, `systemctl is-active durable-park-bridge durable-park-receiver hermes-shell-mcp`, `systemctl --user is-active devtunnel-hermes` (as ubuntu); one Telegram and one Teams message answered; `sudo -u hermes -H bash -lc 'hermes cron list'` shows the sweep ran < 6 min ago; `sudo -u hermes git -C /home/hermes/tapdash-agents fetch` succeeds; `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user restart hermes-dashboard` succeeds. Record each output (no secrets) for the PR.
- [ ] **Step 10:** Yaakov restricts Tailscale SSH to `ubuntu` in the admin console (spec Q4) and ends the open `login -- hermes` session.
- [ ] **Step 11 — docs (branch `ops/clean-setup-phase1`):** architecture.md §6 gains the connector row (`hermes-shell-mcp`, system unit, user `ubuntu`, port 8792, Funnel `/hermes-shell`); runbook "Deploy a change" root steps run as `ubuntu` with the **Interfaces** forms; AGENTS.md rule 3 adds "Commands through the Proto-Agents connector run as `ubuntu`; reach hermes files with `sudo -u hermes`"; backlog item 0 → done (date). Docs check passes; commit `docs: admin connector runs as ubuntu; hermes has no sudo (Clean Setup Phase 1)`; draft PR; merge; Task 2 Step 2 again as `sudo -u hermes git -C /home/hermes/tapdash-agents pull --ff-only`.

### Task 4: The sweep cancels a request-less `parked` row after 10 minutes (spec §8 item 4, backlog #3)

**Files:**
- Modify: `durable-park/plugin/durable_park/ledger.py:223` (`set_request_id` → CAS)
- Modify: `durable-park/plugin/durable_park/park_hook.py:146-150` (use the CAS result)
- Modify: `durable-park/scripts/durable_park_sweep.py` (new step in `main`)
- Modify: `durable-park/docs/runbook.md` ("already waiting" section; alert list), `durable-park/docs/architecture.md` §5 known gaps (remove "a `parked` row without a request id blocks its chat until handled by hand"), `durable-park/docs/backlog.md` (#3 → Done)
- Test: `durable-park/tests/test_ledger.py`, `durable-park/tests/test_sweep.py`, `durable-park/tests/test_park_hook.py`

**Interfaces:**
- Produces: `Ledger.set_request_id(self, park_id: str, request_id: str) -> bool` — `UPDATE … WHERE park_id=? AND status='parked' AND request_id IS NULL`; True when one row changed.
- Produces: sweep constant `ORPHAN_PARK = timedelta(minutes=10)` and `_cancel_orphan_parks(ledger, now: datetime) -> None`.

- [ ] **Step 1: Failing tests.**
  `test_ledger.py::test_set_request_id_only_once_and_only_while_parked`: park → `set_request_id(pid,"r1") is True`; again `"r2"` → `False` and `get(pid).request_id == "r1"`; a row transitioned to `cancelled` → `set_request_id` → `False`.
  `test_sweep.py::test_orphan_parked_row_is_cancelled_after_ten_minutes`: `World.row("parked", request_id=None)` backdated 11 min → after `World.main`: status `cancelled`, note `"no approval request after 10 minutes (cancelled by the sweep)"`, stdout contains `"durable-park: {park_id} ({tool}) never got an approval request; cancelled after 10 minutes"`.
  `test_sweep.py::test_young_or_requested_parked_rows_are_left_alone`: request-less row 9 min old, and a row with `request_id` 60 min old → both still `parked`, stdout has no "never got".
  `test_park_hook.py::test_request_id_not_attached_to_a_row_the_sweep_cancelled`: FakeProvider auto=None; make `ledger.set_request_id` return False (cancel the row between park and verdict by monkeypatching `Ledger.set_request_id` to first transition the row to `cancelled`) → result `{"action":"block","message": COULD_NOT_OPEN}` and `provider.requests[<id>]` status `cancelled`.
- [ ] **Step 2:** Run `.venv/bin/pytest -q -W error tests/test_ledger.py tests/test_sweep.py tests/test_park_hook.py -k "request_id or orphan or young"` → the four fail.
- [ ] **Step 3:** Implement the CAS; in the hook's `Pending` branch, when `set_request_id` returns False: `gate.client.cancel(request_id)` (errors suppressed), block `COULD_NOT_OPEN_MESSAGE`. In the sweep, call `_cancel_orphan_parks` right after `_mark_stale_executing`; age from `created_at`.
- [ ] **Step 4:** Full suite `.venv/bin/pytest -q -W error` → all pass. Docs check passes.
- [ ] **Step 5:** Commit `feat(sweep): cancel a parked row that never got an approval request after 10 minutes` on `feat/ask-every-call`.

### Task 5: Ledger — `allowed`/`refused` statuses and retention (spec §4.2 "Ledger: rows for every call")

**Files:**
- Modify: `durable-park/plugin/durable_park/ledger.py:25-29` (`TRANSITIONS`), new method
- Modify: `durable-park/scripts/durable_park_sweep.py` (`--retention-days`, `_apply_retention`)
- Modify: `durable-park/deploy/configure_hermes.sh:263` (wrapper passes `--retention-days 30`)
- Modify: `durable-park/docs/architecture.md` §3 state diagram (two new terminal states), `durable-park/docs/receiver-setup.md:41` (wrapper line), `durable-park/docs/runbook.md` (retention, `VACUUM` note)
- Test: `durable-park/tests/test_ledger.py`, `durable-park/tests/test_sweep.py`, `durable-park/tests/test_configure_script.py`

**Interfaces:**
- Produces: `TRANSITIONS["parked"] == {"approved","denied","expired","cancelled","allowed","refused"}`; `ACTIVE_STATUSES` unchanged.
- Produces: `Ledger.delete_finished_before(self, cutoff: str) -> int` — deletes rows with `status IN ('allowed','refused','cancelled') AND updated_at < cutoff` (cutoff in the ledger's `%Y-%m-%dT%H:%M:%S.%fZ` format); returns the count.
- Produces: sweep `parse_args` flag `--retention-days INT` (default `30`, must be ≥ 1); `_apply_retention(ledger, now, days) -> None`, prints nothing.

- [ ] **Step 1: Failing tests.** `test_ledger.py::test_parked_can_finish_allowed_or_refused` (both edges succeed, store `request_id` and `note` in the same CAS; `allowed → executing` raises `ValueError`); `test_ledger.py::test_allowed_and_refused_are_not_active` (`active_for_session` excludes them); `test_ledger.py::test_delete_finished_before_keeps_parks_and_young_rows` (rows `allowed`, `refused`, `cancelled` 31 days old deleted; `executed`, `denied`, `unknown` 31 days old kept; `allowed` 29 days old kept; returns 3); `test_sweep.py::test_retention_days_flag` (`--retention-days 0` → exit 2; default deletes the 31-day `allowed` row); `test_configure_script.py::test_plan_values` gains the needle `--retention-days 30`.
- [ ] **Step 2:** Run them → fail.
- [ ] **Step 3:** Implement. `_apply_retention` runs after the wake step, inside `try/except` like `_formcheck_alert` (a retention error never stops the sweep; error to stderr).
- [ ] **Step 4:** Full suite → pass; docs check passes.
- [ ] **Step 5:** Commit `feat(ledger): allowed and refused rows with 30-day retention`.

### Task 6: The AAP request for every call — server label, redaction, context, shared Gate (spec §4.2 table)

**Files:**
- Modify: `durable-park/plugin/durable_park/gatecall.py` (new functions, `build_call`, `GateHolder`)
- Modify: `durable-park/plugin/durable_park/park_hook.py:51-62` (`gateway_slug` moves into `server_label`)
- Modify: `durable-park/docs/architecture.md` §1 table (gatecall row), §8 numbers (16 KiB / 1 KiB)
- Test: `durable-park/tests/test_gatecall.py`

**Interfaces:**
- Produces: `server_label(tool: str) -> str` — `mcp__wh__<slug>__<t>` → `"park:<slug>"`; any other `mcp__<alias>__<t>` → `"mcp:<alias>"` (alias = text up to the first `__` after `mcp__`); everything else → `"hermes"`.
- Produces: `platform_of(session_key: str) -> str` — third `:` field when the key starts with `agent:` and has ≥ 3 fields, else `"unknown"`.
- Produces: `redact_arguments(args: Mapping) -> tuple[dict, bool]` — returns a new dict (input untouched) with secret-named keys redacted at any depth (lists included) and the truncated flag per Global Constraints; size measured as UTF-8 bytes of `ledger.canonical_json`.
- Produces: `build_call(row: ParkRow) -> Call` — `key=row.call_key`, `tool=row.tool`, `server=row.server`, `arguments=` redacted `json.loads(row.args_json)`, `context={"args_sha256": row.args_digest, "platform": platform_of(row.session_key), "session_key": row.session_key}` plus `"args_truncated": True` only when cut, `resume` unchanged.
- Produces: `class GateHolder: __init__(self, base_url: str, token_file: str)`, `get(self) -> Gate` — thread-safe (one `threading.Lock`); builds `make_gate(base_url, token, client=None)` once and reuses it; rebuilds when the token file's `(st_mtime_ns, st_size)` changes; reads the token only when (re)building and never logs it; propagates `OSError` when the file is unreadable.
- Unchanged: `make_gate`, `bounded_http`, `AAP_TIMEOUT`, `RETRY_BUDGET_SECONDS`, `HTTP_TIMEOUT_SECONDS`.

- [ ] **Step 1: Failing tests** in `test_gatecall.py`:
  `test_server_label` parametrized: `("mcp__wh__quickbooks__create_payment","park:quickbooks")`, `("mcp__wh__docusign__createEnvelope","park:docusign")`, `("mcp__github__get_issue","mcp:github")`, `("mcp__my_srv__x_y","mcp:my_srv")`, `("web_search","hermes")`, `("connectors__slack__post","hermes")`.
  `test_platform_of`: `"agent:main:telegram:dm:8169087659"` → `"telegram"`; `""` → `"unknown"`; `"x:y"` → `"unknown"`.
  `test_redaction_any_depth_and_case`: `{"Password":"p","nested":{"api_key":"k","list":[{"access_token":"t"}]},"amount":5}` → those three values `"[redacted]"`, `amount` 5, input unchanged, flag False.
  `test_truncation_over_16_kib`: `{"body":"x"*20000,"n":1}` → `body` length 1024, flag True; `{"body":"x"*1000}` → unchanged, flag False.
  `test_build_call_context_and_resume`: exact `Call` fields for a stored row (replace today's `test_build_call_is_stable` assertions).
  `test_build_call_is_pure_function_of_row`: `build_call(row).submission(gate)` body equals for two calls on the same row (Review Focus 3).
  `test_non_json_arguments_raise`: `build_call` on a row whose `args_json` holds `NaN` (the ledger stores it; `json.dumps` allows it) raises `ValueError` before any HTTP — checked with `json.dumps(…, allow_nan=False)` in `build_call`, so it never reaches the SDK, whose catch-all would turn it into a `Deny("error")` and count against the breaker (Review Focus 5).
  `test_gate_holder_reuses_and_rebuilds_on_token_change`: holder over a tmp token file → `get() is get()`; rewrite file with a different token → new Gate; delete file → `OSError`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement. `server_label` replaces `"park:" + gateway_slug(tool)` at the hook's `ledger.park` call.
- [ ] **Step 4:** Full suite → pass (existing park tests still see `park:quickbooks`). Docs check.
- [ ] **Step 5:** Commit `feat(gatecall): request for every tool — server label, redaction, context, shared gate`.

### Task 7: Circuit breaker with a status file the sweep alerts on (spec §4.2 "Concurrency and latency")

**Files:**
- Create: `durable-park/plugin/durable_park/breaker.py`
- Modify: `durable-park/scripts/durable_park_sweep.py` (`_breaker_alert`)
- Modify: `.github/docs-map.json` (rule: `durable-park/plugin/durable_park/breaker.py` → architecture.md or runbook.md), `CONTRIBUTING.md` (one line: the breaker module is mapped), `durable-park/docs/architecture.md` (§1 table row "breaker"), `durable-park/docs/runbook.md` (alert texts)
- Test: `durable-park/tests/test_breaker.py`, `durable-park/tests/test_sweep.py`

**Interfaces:**
- Produces: `STATUS_FILENAME = "breaker_status.json"`; `class Breaker: __init__(self, status_path: str | None, *, threshold: int = 3, cooldown: float = 30.0, clock=time.monotonic, wallclock=<utc now ISO "%Y-%m-%dT%H:%M:%SZ">)`; `allow_attempt(self) -> bool` (False only while open and cooldown not elapsed; after the cooldown the next attempt goes through); `record_success(self) -> None` (failures 0; closes if open); `record_failure(self) -> None` (failures +1; at ≥ threshold opens or re-opens with a fresh cooldown). Thread-safe. Writes `{"open": bool, "opened_at": str|None, "closed_at": str|None}` atomically (temp + `os.replace`) **only on an open/close change**; a write error is logged, never raised.
- Produces: sweep `BREAKER_STATUS = "breaker_status.json"`, `BREAKER_STATE = "breaker_alert_state.json"`, `_breaker_alert(ledger_path, now) -> None`. Alert texts (exact): open → `durable-park ALERT: withHuman could not be reached (since {opened_at}); every tool call is being refused until it answers again.`; close → `durable-park: withHuman answers again (since {closed_at}); tool calls are decided normally.`

- [ ] **Step 1: Failing tests.** `test_breaker.py`: `test_opens_after_three_failures_and_blocks_for_cooldown` (fake clock: 2 failures → `allow_attempt` True; 3rd → False; clock +29 s False; +31 s True); `test_failure_after_cooldown_reopens_and_success_closes`; `test_status_file_written_only_on_change` (count writes; contents keys and `open` values); `test_unwritable_status_path_never_raises`. `test_sweep.py`: `test_breaker_alert_once_on_open_once_on_close` (status open → one ALERT line; second sweep silent; status closed → one recovery line; third sweep silent); `test_breaker_opened_and_closed_between_sweeps_prints_both`; `test_no_breaker_file_prints_nothing`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement (mirror `_formcheck_alert`'s state handling; the state file stores the `opened_at`/`closed_at` already announced). `_breaker_alert` is wrapped in `try/except` in `main` like the form-check alert.
- [ ] **Step 4:** Full suite and `check_docs.py --verify-map` → pass.
- [ ] **Step 5:** Commit `feat: circuit breaker for withHuman outages, alerted by the sweep`.

### Task 8: The park hook asks withHuman about every call (spec §4.2 decision flow)

**Files:**
- Modify: `durable-park/plugin/durable_park/park_hook.py` (flow, messages, rename)
- Modify: `durable-park/plugin/durable_park/formcheck.py` (parameter rename `risky` → `form_checked`, problem text "no form-checked tools are registered (is the gateway connected?)")
- Modify: `durable-park/docs/architecture.md` (§1 diagram + table, §3 states, §4 flowchart "Tool call", §5), `durable-park/docs/form-check.md` (`form_checked_tools`), `durable-park/docs/runbook.md` (new chat messages; "Add a new risky tool" → "Add a form-checked tool" + "risk is a pipeline rule now")
- Test: `durable-park/tests/test_park_hook.py`, `test_executor.py`, `test_wake.py`, `test_formcheck.py` (renamed imports)

**Interfaces:**
- Consumes: Task 4 `set_request_id -> bool`; Task 5 transitions; Task 6 `server_label`, `build_call`, `GateHolder.get`; Task 7 `Breaker`.
- Produces: `DEFAULT_FORM_CHECKED_TOOLS` (today's `DEFAULT_RISKY_TOOLS` tuple, renamed; same two regexes); `DEFAULT_EXEMPT_TOOLS = ("parked_action_execute", "clarify")`.
- Produces: `make_pre_tool_call(ledger, gate_factory, form_checked, *, exempt=DEFAULT_EXEMPT_TOOLS, breaker=None, foreign_hooks_fn=None, session_key_fn=None, form_checker=None)`; `foreign_hooks_fn() -> list[str]` returns names of non-allowed foreign callbacks (Task 9 supplies it; `None` = none).
- Produces: `fail_closed_hook(exempt: Iterable[str], message: str = COULD_NOT_OPEN_MESSAGE)` — blocks every tool not in `exempt`.
- Produces messages (exact):
  - `NO_SESSION_MESSAGE = "durable-park: no chat session; the tool call was refused"`
  - `NO_CALL_ID_MESSAGE = "durable-park: no tool call id; the tool call was refused"`
  - `REFUSED_MESSAGE = 'durable-park: "{tool}" was refused by withHuman policy{note}. It did not run. Do not retry it or work around it; tell the user.'` (`note` = `": " + reviewer note` when present, else `""`)
  - `UNREACHABLE_MESSAGE = 'durable-park: withHuman could not be reached; "{tool}" did not run. Try again later.'`
  - `FOREIGN_HOOK_MESSAGE = "durable-park: another plugin hook could change tool arguments after approval ({names}); every tool call is refused until an admin removes it or lists it in observer_hooks."`
  - `PARK_MESSAGE`, `ALREADY_WAITING_MESSAGE`, `ID_REUSED_MESSAGE`, `NOT_PARKED_MESSAGE`, `INVALID_ARGS_MESSAGE`, `CHECK_UNAVAILABLE_MESSAGE`, `COULD_NOT_OPEN_MESSAGE` unchanged.
- Flow (order is the contract; one code path):
  1. tool in `exempt` → `None`.
  2. `foreign_hooks_fn()` non-empty → block `FOREIGN_HOOK_MESSAGE` (log error with the names).
  3. no session key → `NO_SESSION_MESSAGE`; no `session_id`/`tool_call_id` → `NO_CALL_ID_MESSAGE`.
  4. Form check only when the tool fullmatches `form_checked` and `ledger.get_by_call(...)` is None (as today).
  5. `row = ledger.park(..., server=server_label(tool))`; then today's `ID_REUSED` / `NOT_PARKED` / repeat-`PARK_MESSAGE` checks. (`NOT_PARKED` now also covers `allowed`/`refused`: the call is not run again.)
  6. `breaker` given and `not breaker.allow_attempt()` → `transition(parked→refused, note="error: withHuman unreachable (breaker open)")` → `UNREACHABLE_MESSAGE`.
  7. `verdict = gate_factory().check(build_call(row), can_pause=True)`.
  8. `Allow` → `breaker.record_success()`; `transition(parked→allowed, request_id=verdict.request_id, note="withHuman: approved")`; CAS False → block `COULD_NOT_OPEN_MESSAGE` (the consumed approval is spent; nothing runs); else `None`.
  9. `Deny` → status `error`: `record_failure()`, else `record_success()`; `transition(parked→refused, request_id=verdict.request_id, note=f"withHuman {verdict.status}: {verdict.reason}")`; block `UNREACHABLE_MESSAGE` for `error`, else `REFUSED_MESSAGE` (note = the reviewer's text after `"Their note: "` when present).
  10. `Pending(rid)` → `record_success()`; `busy` = first other row of this chat that is `approved|executing`, or `parked` **with** a `request_id` (D11); busy → `gate.client.cancel(rid)` (errors suppressed), `transition(parked→cancelled, note=f"already waiting for {busy.park_id}; request {rid} cancelled")` (no `request_id` column), block `ALREADY_WAITING_MESSAGE`; else `set_request_id` (Task 4) → `PARK_MESSAGE`.
  11. Any exception → as today (log type, cancel the row when no request was opened, `COULD_NOT_OPEN_MESSAGE`).

- [ ] **Step 1: Rewrite the tests that encode the old behaviour** (they must fail against the old code): `test_non_risky_passes` → `test_exempt_tools_pass_without_request` (`clarify`, `parked_action_execute` → `None`, `provider.requests == {}`, no row); `test_allow_at_park_time_fails_closed` → `test_instant_allow_runs_and_records_allowed` (FakeProvider auto=`"approved"` → `None`; one row `allowed` with that request id, note `"withHuman: approved"`; request consumed once); `test_provider_error_blocks` / `test_transport_error_blocks_and_cancels` → `UNREACHABLE_MESSAGE`, row `refused`, note starts `"withHuman error"`; `test_second_risky_call_blocked_while_parked` and `test_concurrent_loser_is_cancelled` → the second call's request is created then cancelled (`provider.requests[rid]` status `cancelled`), row `cancelled`, its `request_id` column `None`, message `ALREADY_WAITING`; `test_non_risky_tools_are_not_checked` → `test_only_form_checked_tools_are_form_checked`.
- [ ] **Step 2: New failing tests:** `test_instant_deny_records_refused_with_note` (provider auto=None, `provider.decide`-on-create to `"denied"` with note `"too big"` → exact `REFUSED_MESSAGE` with `": too big"`); `test_every_tool_gets_a_request_with_server_label` (`web_search` → body `server == "hermes"`, `mcp__github__x` → `"mcp:github"`); `test_request_body_is_redacted_and_carries_context` (password key redacted in `provider.only().body["arguments"]`, `context.args_sha256 == row.args_digest`); `test_breaker_open_refuses_without_request` (3 transport errors then a 4th call → `provider.requests` count unchanged, row `refused`, note mentions breaker); `test_foreign_hook_blocks_every_non_exempt_call` (`foreign_hooks_fn=lambda: ["x.y"]` → `web_search` blocked with names, `clarify` → `None`); `test_no_session_blocks_every_non_exempt_tool` (Review Focus 1); `test_reused_call_id_after_allow_is_not_run_again` (same call twice → second is `NOT_PARKED` with status `allowed`, one request) (Review Focus 2); `test_unserialisable_arguments_block_and_cancel` (args with `float("nan")` → `COULD_NOT_OPEN`, row `cancelled`, `provider.requests == {}`, breaker failure count unchanged) (Review Focus 5); `test_can_pause_is_true` (spy on `Gate.check` kwargs); `test_wake.py::test_wake_for_allowed_or_refused_row_injects_nothing` (rows `allowed` and `refused` with request ids; `handle_wake` → no inject, `notified_at` unchanged) (Review Focus 4); `test_wake.py::test_wake_before_instant_finish_is_left_alone` (row `parked`, no request id → no transition, no inject).
- [ ] **Step 3:** Run `.venv/bin/pytest -q -W error tests/test_park_hook.py tests/test_wake.py` → the new and rewritten tests fail, nothing else.
- [ ] **Step 4:** Implement the flow; rename `risky` → `form_checked` in `park_hook.py`, `formcheck.py` and the three test files' imports (`RISKY = [re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS]`).
- [ ] **Step 5:** Full suite → pass; docs check passes (architecture.md diagrams re-read and still true; mermaid validates).
- [ ] **Step 6:** Commit `feat(park-hook): ask withHuman about every tool call; instant allow/refuse, park only for a human`.

### Task 9: Plugin wiring — settings, shared gate, breaker, foreign-hook guard, fail closed for every tool

**Files:**
- Modify: `durable-park/plugin/durable_park/__init__.py` (`register`)
- Modify: `durable-park/plugin/durable_park/compat.py` (`DEPENDS_ON` entry for `PluginManager.iter_hook_callbacks`, Hermes 0.22.2 `hermes_cli/plugins_dispatch.py:523`)
- Modify: `durable-park/tests/live/test_gateway_smoke.py::test_one_shot_cli_turn_returns_the_sandbox_company_name` (expects the `NO_SESSION_MESSAGE` refusal now — Review Focus 1)
- Modify: `durable-park/docs/architecture.md` (settings table), `durable-park/docs/runbook.md` (settings, `observer_hooks` procedure: read the callback's code, confirm it never returns `modify`, then list its `module.qualname`), `durable-park/README.md`
- Test: `durable-park/tests/test_park_hook.py` (register tests)

**Interfaces:**
- Consumes: Tasks 6–8.
- Produces settings (via `ctx.get_config`): `exempt_tools` (default `list(DEFAULT_EXEMPT_TOOLS)`), `form_checked_tools` (default `list(DEFAULT_FORM_CHECKED_TOOLS)`; invalid pattern → log + defaults appended, as `_compile_risky` today), `observer_hooks` (default `[]`), `aap_base_url`, `token_file`, `ledger_path` unchanged. A configured `risky_tools` logs a warning `"risky_tools is renamed form_checked_tools; ignoring it"`.
- Produces: `foreign_pre_tool_call_hooks(ctx, own: set[int], allowed: set[str]) -> list[str]` — names (`f"{cb.__module__}.{cb.__qualname__}"`) of callbacks from `ctx._manager.iter_hook_callbacks("pre_tool_call")` whose `id` is not in `own` and whose name is not in `allowed`; when the manager or method is missing or raises → `["<hook registry unavailable>"]` (fail closed).
- `register` builds one `GateHolder(base_url, token_file)` (`gate_factory = holder.get` for hook **and** executor), one `Breaker(<ledger dir>/breaker_status.json)`, and on ledger failure registers `fail_closed_hook(exempt)` + `drop_wake_hook()`.

- [ ] **Step 1: Failing tests:** rewrite `test_register_wires_pre_tool_call_with_default_settings` (asks `exempt_tools`, `form_checked_tools`, `observer_hooks`; a `web_search` call with FakeCtx and no network → blocked with `COULD_NOT_OPEN` or `UNREACHABLE`, never `None`), `test_register_fails_closed_when_ledger_cannot_open` (`web_search` blocked, `clarify` → `None`), `test_register_honours_settings…` (custom `exempt_tools`); new `test_foreign_hook_detected_through_manager` (FakeCtx with `_manager.iter_hook_callbacks` returning own + a foreign function → hook blocks; same with the foreign name in `observer_hooks` → proceeds to the request), `test_missing_hook_registry_fails_closed`, `test_hook_and_executor_share_one_gate` (spy `GateHolder.get` called by both), `test_risky_tools_setting_warns`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement; identify own callbacks by `id()` of the functions this `register` passed to `ctx.register_hook`.
- [ ] **Step 4:** Full suite → pass; docs check.
- [ ] **Step 5:** Commit `feat(plugin): wire ask-every-call — settings, shared gate, breaker, foreign-hook guard`.

### Task 10: withHuman entry pipeline revision as a reviewed file (spec §8 item 6, §4.3)

**Files:**
- Create: `durable-park/deploy/withhuman/entry-pipeline.json` — the complete `blocks` array and `default_escalation_path` for the next entry revision
- Create: `durable-park/tests/test_pipeline_policy.py` (+ a small condition evaluator inside it)
- Modify: `.github/docs-map.json` (rule `durable-park/deploy/withhuman/*.json` → `durable-park/docs/architecture.md`), `CONTRIBUTING.md` (one line), `durable-park/docs/architecture.md` (pipeline table :133-137 rewritten to the rows below; revision number filled in Task 13)

**Interfaces:**
- Produces the block list, in this order (ids key-shaped; every condition node id a fresh UUID v4, unique in the file; `timeout "50ms"`, `max_attempts 1` except the webhook; all conditions start with `/agent/slug equals proto-agents`):

| id | condition (besides agent) | block |
|---|---|---|
| `pa-park-money` | `/request/server equals park:quickbooks` and `/request/tool regex ^mcp__wh__quickbooks__(create\|update\|delete)[_-](payment\|bill_payment\|transfer\|deposit\|journal_entry\|refund_receipt\|sales_receipt\|credit_memo\|vendor_credit\|purchase\|invoice\|bill)$` | outcome `human`, path `notify-yaakov-yehuda`, reason `Parked durable-park action: approve to let the agent run it exactly once.` |
| `block-06ebcdd6` | rev 8 block copied verbatim (server `quickbooks` + gateway money regex → webhook `durable-park-bridge`) | unchanged |
| `pa-gateway-other` | `/request/server equals quickbooks` | outcome `approve`, reason `QuickBooks call that is not a money move; durable-park already asked about it as park:quickbooks.` (D4) |
| `pa-park-quickbooks-other` | `/request/server equals park:quickbooks` | outcome `approve`, reason `QuickBooks read or non-money write; the gateway decides the real call again.` (D5) |
| `pa-hermes-read` | `/request/server equals hermes` and `/request/tool in [web_search, web_extract, vision_analyze, memory, session_search, todo_list, kanban_list, kanban_show, kanban_attachments, skills_list, skill_view, image_generate]` | outcome `approve`, reason `No side effect outside the chat.` |
| `pa-hermes-local-deny` | `/request/server equals hermes` and `/request/tool in [terminal, execute_code, process_manage, process, write_file, patch, read_file, search_files, delegate_task, cronjob_manage, cronjob, skill_manage]` | outcome `deny`, reason `Local machine tools stay off until the ledger has its own owner (Clean Setup Phase 2).` |
| `pa-mcp-human` | `/request/server starts_with ["mcp:"]` | outcome `human`, path `notify-yaakov-yehuda`, reason `New MCP server: a person decides until it has a rule.` |
| `pa-default-human` | (agent only) | outcome `human`, path `notify-yaakov-yehuda`, reason `No rule for this proto-agents tool; a person decides.` |
| `hermes` | rev 8 block copied verbatim | unchanged |
| `approve-every-call` | rev 8 block copied verbatim | unchanged |

  `default_escalation_path`: `notify-yaakov-yehuda`. Rule-shaped blocks are `branch` blocks holding one `outcome` (no branch-level `reason`/`escalation_path`), as in withHuman's reference.

- [ ] **Step 1: Failing tests** in `test_pipeline_policy.py` (file absent → fail): `test_block_ids_are_unique_and_key_shaped` (`^[a-z0-9][a-z0-9._-]{0,62}$`); `test_condition_ids_are_unique_uuids`; `test_order_and_tail` (ids in the table's order; last two blocks deep-equal the rev 8 copies kept in the test as fixtures); `test_park_money_regex_matches_exactly_the_36_money_tools` (matches the 36 Hermes names `mcp__wh__quickbooks__{create,update,delete}_{12 types}` — Hermes turns the gateway's `create-bill` into `create_bill` — and the hyphen form `mcp__wh__quickbooks__create-bill` that `[_-]` keeps; non-matches `mcp__wh__quickbooks__create_payment_method`, `…create_purchase_order`, `…create_estimate`, `…update_company_info`); and table-driven `test_decisions` using an evaluator for `all/any/equals/in/starts_with/regex` over `(agent, server, tool) → (block id, outcome)` (regex leaves evaluated with Python `re.search` on the pattern as written — every pattern is anchored and uses only the RE2-compatible subset):
  `(proto-agents, park:quickbooks, mcp__wh__quickbooks__create_payment) → pa-park-money human`;
  `(proto-agents, park:quickbooks, mcp__wh__quickbooks__search_customers) → pa-park-quickbooks-other approve`;
  `(proto-agents, quickbooks, create_payment) → block-06ebcdd6 webhook`; `(proto-agents, quickbooks, create-bill) → block-06ebcdd6 webhook`;
  `(proto-agents, quickbooks, search_customers) → pa-gateway-other approve`;
  `(proto-agents, park:docusign, mcp__wh__docusign__createEnvelope) → pa-default-human human`;
  `(proto-agents, hermes, web_search) → pa-hermes-read approve`; `(proto-agents, hermes, terminal) → pa-hermes-local-deny deny`; `(proto-agents, hermes, process_manage) → pa-hermes-local-deny deny`;
  `(proto-agents, hermes, browser_navigate) → pa-default-human human`; `(proto-agents, mcp:github, mcp__github__x) → pa-mcp-human human`;
  `(proto-agents, None, mcp__wh__quickbooks__create_payment) → pa-default-human human`;
  `(hermes, None, terminal) → approve-every-call approve`; `(hermes, None, StripeRefund) → hermes human`.
- [ ] **Step 2:** Run → fail. **Step 3:** Write the JSON (copy the three rev 8 blocks verbatim from `get_pipeline_revision` rev 8, without `snapshot_id`/`config_version`/`content_hash`). **Step 4:** Tests + `--verify-map` + docs check pass.
- [ ] **Step 5 — withHuman, save but do not activate:** confirm every tool name in `pa-hermes-read`/`pa-hermes-local-deny` exists in Hermes 0.22.2 (`grep -rn "name=\"<tool>\"" ~hermes/.hermes/hermes-agent/tools` via `sudo -u hermes`); `read_reference` once; `update_pipeline` (pipeline `entry`, blocks and default path from the file, `activate: false`); note the new revision number N; `get_pipeline_revision_references` N → activatable. `preview_pipeline` with the file's blocks for samples `(proto-agents, mcp__wh__quickbooks__create_payment)` → human via `pa-default-human` (server missing in previews, D8) and `(hermes, terminal)` → approve. Then, for each rule-shaped `pa-*` block, preview a two-block copy — that block with its `/request/server` leaf removed, followed by `approve-every-call` — with one matching sample (`mcp__wh__quickbooks__create_payment`, `search_customers`, `mcp__wh__quickbooks__search_customers`, `web_search`, `terminal`, `mcp__github__x` respectively) → the block's outcome, and one non-matching sample (`browser_navigate`) → approve. Record N and every preview result in the PR.
- [ ] **Step 6:** Commit `feat(pipeline): entry pipeline for ask-every-call as a reviewed file (rev N saved, not active)`.

### Task 11: Tool policy from one stored list (spec §8 item 7, part 1)

**Files:**
- Modify: `durable-park/deploy/hermes_tool_policy.py` (argparse CLI, allow file, status file)
- Modify: `durable-park/deploy/configure_hermes.sh` (creates `/etc/durable-park/allowed_toolsets` if missing: `install -m 640 -o root -g hermes /dev/null`), `durable-park/deploy/hermes_mcp.sh` (no flags change; reads the same file through `apply`/`check`)
- Modify: `durable-park/docs/receiver-setup.md` ("Agent tool policy" section), `durable-park/docs/runbook.md` ("Open a toolset": edit the file as admin, `apply`, `check`, restart with no active rows), `durable-park/docs/backlog.md` (#6 progress)
- Test: `durable-park/tests/test_tool_policy.py`, `durable-park/tests/test_configure_script.py`

**Interfaces:**
- Produces: `ALLOW_FILE = "/etc/durable-park/allowed_toolsets"`; `PHASE2_TOOLSETS = frozenset({"terminal","file","code_execution","delegation","cronjob","skills","computer_use"})`.
- Produces: `read_allowed(path: str) -> list[str]` — missing file → `[]`; one toolset per line, `#` comments and blanks ignored, order kept, duplicates dropped; a name not in `FORBIDDEN_TOOLSETS` → `ValueError("unknown toolset: …")`; a name in `PHASE2_TOOLSETS` → `ValueError("<name> stays off until Clean Setup Phase 2")`.
- Produces: `desired_settings(allowed: list[str]) -> dict` — `agent.disabled_toolsets` = `FORBIDDEN_TOOLSETS` minus `allowed` (order kept); every platform list = `["durable_park", "clarify", *allowed, "wh" | "no_mcp"]`; other keys as today.
- Produces: `allowed_tool_names(allowed: list[str]) -> set[str]` (names from Hermes `get_tool_definitions(enabled_toolsets=allowed)`); `is_allowed(name: str, extra: set[str] = frozenset()) -> bool`.
- Produces CLI: `hermes_tool_policy.py {apply,check} [--allow-file PATH] [--status-file PATH]` (`--status-file` only with `check`). Exit 0 ok, 1 check failed, 2 usage error or invalid allow file (message on stderr, nothing written). `check --status-file` writes `{"ok": bool, "checked_at": "%Y-%m-%dT%H:%M:%SZ", "problems": [str], "allowed": [str]}` atomically, also when failing.

- [ ] **Step 1: Failing tests:** `test_read_allowed` (missing → `[]`; comments/dupes; unknown → ValueError; `terminal` → ValueError with "Phase 2"); `test_desired_settings_opens_only_the_allowed` (`["web","vision"]` → both absent from `disabled_toolsets`, present in every platform list, `terminal` still disabled); `test_desired_settings_empty_equals_todays_lockdown` (== today's output with monkeypatched sources); `test_cli_usage_errors_exit_2` (no command; `apply --status-file x`; invalid allow file); `test_check_writes_status_file_on_failure` (monkeypatch `check` internals to report one LEAK → exit 1, file `ok: false`, problems non-empty); existing `test_allowlist_is_exactly_the_plugin_tool_and_clarify` keeps passing with an empty allow file; `test_configure_script.py::test_plan_values` gains `allowed_toolsets`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Full suite + shellcheck tests + docs check pass.
- [ ] **Step 5:** Commit `feat(tool-policy): one root-owned list of allowed toolsets; Phase-2 toolsets refused` on `feat/tool-policy-file`.

### Task 12: Check the tool policy at every gateway start (spec §8 item 7, part 2; backlog #6)

**Files:**
- Create: `durable-park/deploy/tool_policy_check.sh` (wrapper: finds the Hermes python from `~/.hermes/installs/*/facts.json` the way `configure_hermes.sh` `hermes_python()` does; runs `check --status-file "$DURABLE_PARK_POLICY_STATUS"`; when no python is found writes `{"ok": false, "checked_at": …, "problems": ["hermes python not found"], "allowed": []}`; **always exits 0** so the gateway starts and fails closed inside)
- Create: `durable-park/deploy/hermes-gateway-policy.conf` (drop-in: `[Service]` `Environment=DURABLE_PARK_POLICY_STATUS=%h/.hermes/plugin-data/durable-park/tool_policy_status.json`, `ExecStartPre=-/bin/bash %h/durable-park/deploy/tool_policy_check.sh`)
- Modify: `durable-park/deploy/configure_hermes.sh` (installs the drop-in to `~hermes/.config/systemd/user/hermes-gateway.service.d/50-durable-park-policy.conf` as hermes, then user `daemon-reload`)
- Modify: `durable-park/plugin/durable_park/__init__.py` (`register` reads the status), `durable-park/scripts/durable_park_sweep.py` (`_policy_alert`)
- Modify: `.github/docs-map.json` (`durable-park/deploy/*.conf` → runbook.md or receiver-setup.md), `CONTRIBUTING.md`, `durable-park/docs/runbook.md`, `durable-park/docs/architecture.md` (start sequence), `durable-park/docs/backlog.md` (#6 → Done)
- Test: `durable-park/tests/test_park_hook.py` (register), `test_sweep.py`, `test_configure_script.py`

**Interfaces:**
- Consumes: Task 11 status file format; Task 8 `fail_closed_hook(exempt, message)`.
- Produces: plugin setting `tool_policy_status` (default `<ledger dir>/tool_policy_status.json`); `policy_problem(path: str, now: datetime, max_age: timedelta = timedelta(minutes=15)) -> str | None` in `__init__.py`; when it returns a problem, `register` logs it and registers `fail_closed_hook(exempt, POLICY_MESSAGE)` instead of the normal hook (the wake hook and executor still register). `POLICY_MESSAGE = "durable-park: the tool policy check did not pass at gateway start; tool calls are refused until an admin fixes it."`
- Produces: sweep `POLICY_STATUS = "tool_policy_status.json"`, `POLICY_STATE = "tool_policy_alert_state.json"`, `_policy_alert(ledger_path, now)`; texts: `durable-park ALERT: the tool policy check failed at gateway start — {problems}. Every tool call is refused until it passes.` and `durable-park: the tool policy check passes again.`

- [ ] **Step 1: Failing tests:** `test_policy_problem` (missing → "no tool policy report"; `ok:false` → its problems; 16 min old → "stale"; fresh ok → None); `test_register_fails_closed_on_policy_problem` (`web_search` → `POLICY_MESSAGE`, `clarify` → None, executor registered); `test_sweep.py::test_policy_alert_once_and_recovery`; `test_configure_script.py::test_drop_in_installed_and_reloaded` (needles `50-durable-park-policy.conf`, `daemon-reload`) and `test_policy_wrapper_syntax_and_shellcheck`.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Full suite, shellcheck, `--verify-map`, docs check pass.
- [ ] **Step 5:** Commit `feat: check the tool policy at every gateway start; fail closed and alert when it fails`.

### Task 13: Review, go-live of ask-every-call and the pipeline, acceptance (spec §8 items 5–6; §5 #6–#12)

**Files:**
- Create: `durable-park/docs/acceptance-2026-10-2X-ask-every-call.md` (evidence per spec §5 row, no secrets)
- Modify: `durable-park/docs/architecture.md` (active pipeline revision N), spec §5 (link the acceptance doc)

- [ ] **Step 1 — independent review (AGENTS.md rule 2):** use superpowers:requesting-code-review on `feat/ask-every-call` with the most capable model; the reviewer checks the three guarantees against Tasks 4–10 and the Review Focus list. Fix findings; merge only with the review clean.
- [ ] **Step 2 — probe the `Allow` path (D10), before activating rev N:** as hermes, run a script (unique heredoc delimiter) with the plugin venv python that builds `make_gate(base_url, <token read from file, never printed>)`, calls `gate.client.create({"tool":"durable-park-probe","arguments":{},"timeout":"168h","server":"hermes","context":{"call_id":"probe-<uuid>"}}, "probe-<uuid>")` and prints only `request.status`. Rev 8 approves it (`approve-every-call`). **Expected:** `approved`. If `pending`: STOP, report to Yaakov; the instant path needs a redesign.
- [ ] **Step 3 — activate rev N:** health check (0 active rows); `activate_pipeline_revision` N. With today's code this changes nothing (the hook only sends money parks; the gateway rows match today's outcomes). **Rollback:** activate rev 8.
- [ ] **Step 4 — deploy code:** health check; `sudo -u hermes git -C /home/hermes/tapdash-agents pull --ff-only`; Yaakov runs `sudo bash /home/hermes/tapdash-agents/durable-park/deploy/configure_hermes.sh` (regenerates the sweep wrapper with `--retention-days 30`, re-applies the current tool policy, restarts the gateway). **Verify** the wrapper: `sudo -u hermes grep -c -- '--retention-days 30' <wrapper path from receiver-setup.md>` → `1`. **Verify:** gateway log has `durable-park loaded` and no `another plugin hook` error. **Rollback:** `git -C … checkout <previous main>` (as hermes) and restart with 0 active rows, then rev 8.
- [ ] **Step 5 — acceptance (spec §5), record each in the acceptance doc:**
  #6 instant approve: in Telegram ask for a QuickBooks customer search (D18) → runs; withHuman audit shows one request created → approved by `pa-park-quickbooks-other` → consumed, and the gateway call approved by `pa-gateway-other`; ledger row `allowed` with that request id.
  #7 instant deny: save rev N+1 = rev N plus a first block denying `mcp__wh__quickbooks__get_company_info` (confirm the name with `list_tool_definitions`), activate, ask for the company info → blocked with `REFUSED_MESSAGE`, row `refused`, audit `denied`; activate rev N again.
  #8 park unchanged: `create_payment` (sandbox) parks; while parked run `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user restart hermes-gateway` (spec-required durability test); approve → wake → `parked_action_execute` → `executed`; one human ask in withHuman; bridge passed once (`gateway_passed_at` set).
  #9 fail closed: set `plugins.entries.durable-park.settings.aap_base_url` to `https://127.0.0.1:9/api/aap` (text edit, keep comments), restart (0 active rows) → a QuickBooks read is refused with `UNREACHABLE_MESSAGE`, `clarify` still answers, sweep prints the breaker ALERT once; restore the URL, restart, one recovery line.
  #10 latency: 10 QuickBooks reads → `select (julianday(updated_at)-julianday(created_at))*86400 from parks where status='allowed' order by created_at desc limit 10` median ≤ 0.3 s.
  #11 tests green on the merged commit; crash injection: `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user set-environment DURABLE_PARK_CRASH_AFTER_CONSUME=1`, restart (0 active rows), park + approve a sandbox payment → executor stops after consume → after 10 min the sweep marks `unknown` and alerts; never a second run; then `unset-environment` and restart.
  #12 policy as written: for each row exercised above, the audit event `request_approval_block.evaluated` names the expected block id; record revision N.
- [ ] **Step 6:** Commit the acceptance doc and architecture revision number; draft PR; merge.

### Task 14: Go-live of the tool policy list and start check (spec §8 item 7)

- [ ] **Step 1:** Independent review of `feat/tool-policy-file` (as Task 13 Step 1); merge.
- [ ] **Step 2:** Health check (0 active rows); pull as hermes; Yaakov runs `sudo bash /home/hermes/tapdash-agents/durable-park/deploy/configure_hermes.sh` (creates the empty allow file, installs the drop-in, applies, checks, restarts).
- [ ] **Step 3: Verify:** `/etc/durable-park/allowed_toolsets` is `root:hermes 640` and empty; `sudo -u hermes cat ~hermes/.hermes/plugin-data/durable-park/tool_policy_status.json` has `"ok": true` and a `checked_at` from this start; a QuickBooks read still works in Telegram.
- [ ] **Step 4 — fail-closed proof:** Yaakov writes `terminal` into the allow file → `configure_hermes.sh` refuses with exit 2 and changes nothing; Yaakov empties the file again. Then add a leak by hand: as hermes add `web` to one platform's `platform_toolsets` (text edit), restart → status `ok: false`, every non-exempt call refused with `POLICY_MESSAGE`, sweep ALERT once; revert, restart → recovery line. Record in the acceptance doc.

---

## Execution order and dependencies

Task 1 → Task 2 → Task 3 (ops, Yaakov present). Tasks 4–10 can be coded in parallel with Tasks 1–3 but go live
(Task 13) only after Task 3. Task 10's revision is saved inactive and activated in Task 13 Step 3. Tasks 11–12 are
coded after Task 8 (they use `fail_closed_hook(exempt, message)`) and go live in Task 14 after Task 13.
