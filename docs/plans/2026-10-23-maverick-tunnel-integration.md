# Maverick Tunnel Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. Use superpowers:test-driven-development for every code change. Read `AGENTS.md` first.

**Goal:** Run the Maverick MCP server next to QuickBooks in the withHuman tunnel, and wire it into withHuman approvals, durable-park and Hermes, so the agent can use the 42 Maverick tools with the approval split from the spec.

**Architecture:** The tunnel image gains a Python venv with the `maverick-mcp` package, built from a checkout of the private repo pinned by SHA. `tunnel.yaml` gets a `maverick` server. durable-park allows and form-checks `mcp__wh__maverick__*` tools. The withHuman entry pipeline gets narrow name-list rules for the "Auto" tools; every other Maverick call takes the QuickBooks path (park, a person, then the durable-park bridge). Hermes reads the Maverick skills from the checkout.

**Tech Stack:** Docker Compose (BuildKit `additional_contexts`), Python 3.14 (`pytest -q -W error`), Bash (`shellcheck`), the withHuman management MCP (`update_pipeline`, `activate_pipeline_revision`, `preview_pipeline`, `create_gateway_server`).

**Spec:** `TapDashPayments/maverick-mcp`: `docs/specs/2026-10-23-maverick-mcp-design.md` (sections 6, 8, 10 and 13). The server's own plan is `docs/plans/2026-10-23-maverick-mcp-server.md` in that repo.

**Prerequisite:** the server plan is merged to `maverick-mcp` `main`. Its `python -m maverick_mcp --list-approvals` output is the source for the approval lists below.

## Global Constraints

- Work in the worktree `/home/hermes/tapdash-agents-wt/maverick-mcp`, branch `feat/maverick-tunnel`. Never edit the live clone. Git as `hermes`.
- Every change updates its docs in the same commit (`python3 tools/docs_check/check_docs.py --base origin/main --head HEAD`); every Mermaid block parses; every arrow is labeled.
- `cd durable-park && .venv/bin/pytest -q -W error` passes after every task; failing test first.
- Never print or commit secrets. `tunnel.env` and the new `maverick.env` are written only by their `enter-*.sh` scripts, run by Yaakov.
- Before restarting `hermes-gateway`, check that no ledger row is `parked`, `approved` or `executing`.
- Gateway server slug `maverick`; Hermes names `mcp__wh__maverick__<tool>`; park server name `park:maverick` (already produced by `gatecall.server_label`).
- The auto set (13): `maverick_search_lookups`, `maverick_describe_lookup`, `maverick_read`, `maverick_create_boarding_application`, `maverick_update_boarding_application`, `maverick_save_boarding_principal`, `maverick_update_boarding_fees`, `maverick_update_boarding_equipment`, `maverick_save_vault_customer`, `maverick_save_ach_customer`, `maverick_upload_dispute_evidence`, `maverick_create_dispute_draft`, `maverick_update_ticket`.
- The auto-unless-notify set (3): `maverick_add_boarding_note` (auto only when `notify_staff` is false), `maverick_create_ticket` and `maverick_reply_to_ticket` (auto only when `notify_merchant` and `attention_merchant` are both false).
- Every other Maverick tool goes to a person. Pipeline regexes use no `(?` and no lookaround.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_016zV8NgHrcm5Pgpt7k62NJV`; author `Yaakov Yehuda <220278432+Yaakovyitzchak1231@users.noreply.github.com>`.

## Review Focus

1. **A notify flag that is missing, `null` or the string `"false"`** must go to a person, not be approved (pipeline uses `is_false`; preview cases in Task 6).
2. **A Maverick tool name that only starts like an auto tool** (`maverick_read_all`, `maverick_readx`) must not be approved (anchored regexes; preview cases in Task 6).
3. **The checkout on the VM not matching the pinned SHA, or having local edits**, must stop the image build (Task 2 test of `build.sh`).
4. **The tunnel starting without `maverick.env`** must fail loudly, not run QuickBooks-only silently (Task 3: compose lists the file, the tunnel refuses unset `${VAR}`s; runbook says so).
5. **`skill_manage` reaching anything but a deny** (Task 5 test pins it in `pa-hermes-local-deny`).

---

### Task 1: Read-only access to the private repo, and the pinned checkout

**Files:**
- Create: `durable-park/deploy/tunnel/maverick-mcp.sha` (one line: the 40-character commit on `maverick-mcp` `main`)
- Modify: `durable-park/docs/gateway-setup.md` (new section "Maverick server source")

- [x] **Step 1:** As `hermes`, create `~/.ssh/maverick_mcp_deploy` (`ssh-keygen -t ed25519 -N '' -C hermes@proto-agents-maverick-mcp`). As `ubuntu`, add the public key read-only: `gh repo deploy-key add ~hermes/.ssh/maverick_mcp_deploy.pub -R TapDashPayments/maverick-mcp -t proto-agents-read`.
- [x] **Step 2:** Add to `~hermes/.ssh/config`: `Host github-maverick-mcp` / `HostName github.com` / `User git` / `IdentityFile ~/.ssh/maverick_mcp_deploy` / `IdentitiesOnly yes`.
- [x] **Step 3:** Clone `git@github-maverick-mcp:TapDashPayments/maverick-mcp.git` to `/home/hermes/maverick-mcp`, check out the commit in `maverick-mcp.sha`. Expected: `git -C /home/hermes/maverick-mcp rev-parse HEAD` equals the file.
- [x] **Step 4:** Document the three steps and "how to move to a new commit" in `gateway-setup.md`. Commit `docs(tunnel): pinned read-only checkout of maverick-mcp`.

---

### Task 2: The tunnel image carries the Maverick server

**Files:**
- Modify: `durable-park/deploy/tunnel/Dockerfile`, `durable-park/deploy/tunnel/docker-compose.yml`
- Create: `durable-park/deploy/tunnel/build.sh`
- Test: `durable-park/tests/test_tunnel_files.py`
- Docs: `README.md` (Built on table: a `maverick-mcp` row with the SHA), `durable-park/docs/gateway-setup.md`, `durable-park/docs/architecture.md` (image tag, system map shows both servers)

**Interfaces:**
- Produces: launcher `/usr/local/bin/maverick-mcp-server` (`exec /opt/maverick-mcp/bin/python -m maverick_mcp "$@"`); image tag `durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-<first 7 of maverick-mcp.sha>` in **both** compose services; named volume `durable-park-maverick-inbox` mounted at `/data/maverick-inbox` (owner `withhuman`, mode 0700) in the `wh-tunnel` service.

- [x] **Step 1: Write the failing tests** (plain text checks, no YAML package): `test_both_services_use_the_same_tag_with_maverick_sha`; `test_compose_has_maverick_build_context` (`additional_contexts` maps `maverick-src` to `${MAVERICK_MCP_SRC:-/home/hermes/maverick-mcp}`); `test_dockerfile_installs_with_require_hashes` (`--require-hashes -r requirements.lock` and `--no-deps`); `test_dockerignore_still_excludes_env`; `test_build_script_checks_sha_and_clean_tree` (`bash -n`, `shellcheck`, and the script text compares `git rev-parse HEAD` to `maverick-mcp.sha` and runs `git status --porcelain`).
- [x] **Step 2: Run** (FAIL).
- [x] **Step 3: Dockerfile:** new stage `FROM ${TUNNEL_IMAGE} AS maverick`, `USER root`, `COPY --from=maverick-src requirements.lock pyproject.toml /src/maverick/`, `COPY --from=maverick-src src /src/maverick/src`, `python3 -m venv /opt/maverick-mcp`, `/opt/maverick-mcp/bin/pip install --no-cache-dir --require-hashes -r /src/maverick/requirements.lock`, then `--no-deps /src/maverick`. Runtime stage: `COPY --from=maverick /opt/maverick-mcp /opt/maverick-mcp`, the launcher, `mkdir -p /data/maverick-inbox && chown withhuman:withhuman /data/maverick-inbox && chmod 0700 /data/maverick-inbox`, still `USER withhuman` last.
- [x] **Step 4: `build.sh`:** stops with a message if the checkout's HEAD is not the pinned SHA or the tree is dirty, then `docker compose build`.
- [x] **Step 5: Run** the tests (PASS). Build: `sudo bash build.sh`. Expected: the image builds; `docker run --rm --entrypoint maverick-mcp-server <tag> --list-approvals` prints 42 entries.
- [x] **Step 6:** Update the docs listed above. Commit `feat(tunnel): add the Maverick MCP server to the tunnel image`.

---

### Task 3: Maverick settings file and the tunnel entry

**Files:**
- Create: `durable-park/deploy/tunnel/enter-maverick-secrets.sh`
- Modify: `durable-park/deploy/tunnel/tunnel.yaml`, `durable-park/deploy/tunnel/docker-compose.yml`
- Test: `durable-park/tests/test_tunnel_files.py`
- Docs: `durable-park/docs/gateway-setup.md`, `durable-park/docs/runbook.md`, `durable-park/docs/architecture.md` (secrets table row for `maverick.env`)

**Interfaces:**
- Produces: `/etc/durable-park/maverick.env` (root, 0600) with exactly `MAVERICK_TOKEN`, `MAVERICK_GATEWAY_URL`, `MAVERICK_DASHBOARD_URL`; `tunnel.yaml` entry `maverick: {command: ["maverick-mcp-server"], env: {MAVERICK_TOKEN: ${MAVERICK_TOKEN}, MAVERICK_GATEWAY_URL: ${MAVERICK_GATEWAY_URL}, MAVERICK_DASHBOARD_URL: ${MAVERICK_DASHBOARD_URL}, MAVERICK_INBOX_DIR: /data/maverick-inbox}}`; both compose services list `env_file: [${TUNNEL_ENV_FILE:-/etc/durable-park/tunnel.env}, ${MAVERICK_ENV_FILE:-/etc/durable-park/maverick.env}]`.

- [x] **Step 1: Write the failing tests**: `test_tunnel_yaml_has_maverick_server`; `test_both_services_load_maverick_env`; `test_enter_maverick_secrets_script` (`bash -n`, `shellcheck`; `names=(MAVERICK_TOKEN MAVERICK_GATEWAY_URL MAVERICK_DASHBOARD_URL)`; values read with `read -rs`; the same character rules as `enter-secrets.sh`; URLs must start `https://`; writes via `mktemp` + `mv -f`, mode 0600, owner root; asks before overwriting).
- [x] **Step 2–4:** fail, implement, pass.
- [x] **Step 5:** Document in the runbook: until Maverick sandbox access arrives, Yaakov enters `MAVERICK_TOKEN=pending`, `https://maverick.invalid` for both URLs; every Maverick call then fails with a clear "did not answer" error, never silently. Commit `feat(tunnel): Maverick settings file and tunnel entry`.

---

### Task 4: durable-park allows and form-checks Maverick tools

**Files:**
- Modify: `durable-park/deploy/hermes_tool_policy.py:79` and the printed text at `:402-403`, `durable-park/deploy/hermes_mcp.sh:35-37` and `:131-138`, `durable-park/plugin/durable_park/park_hook.py:51-62`
- Test: `durable-park/tests/test_tool_policy.py`, `durable-park/tests/test_configure_script.py`, `durable-park/tests/test_park_hook.py`
- Docs: `durable-park/docs/architecture.md`, `durable-park/docs/form-check.md`, `durable-park/docs/runbook.md`

**Interfaces:**
- Produces: `ALLOWED_TOOL_PREFIXES = ("mcp__wh__quickbooks__", "mcp__wh__maverick__")`; `hermes_mcp.sh`: `TOOL_INCLUDE='["quickbooks__*", "maverick__*"]'`, `ALLOWED_PREFIX_RE='^mcp__wh__(quickbooks|maverick)__'` used with `grep -vE`, a second expected tool `EXPECTED_TOOL_MAVERICK=mcp__wh__maverick__maverick_read`, and `set_cfg skills.external_dirs '["/home/hermes/maverick-mcp/skills"]'`; `DEFAULT_FORM_CHECKED_TOOLS` gains a **third, last** entry `r"mcp__wh__maverick__maverick_[a-z0-9_]+"` (index 0 stays the QuickBooks regex, which `test_pipeline_policy.py:258-261` pins).

- [x] **Step 1: Write the failing tests**: in `test_is_allowed`, `mcp__wh__maverick__maverick_read` True and `mcp__wh__maverickX__a` False; in `test_hermes_mcp_checks_before_and_after_and_scopes_wh_tools` and `test_hermes_mcp_script_values_and_guards`, the new literals above; `test_maverick_tools_are_form_checked` (`mcp__wh__maverick__maverick_refund_transaction` matches, `mcp__wh__maverick__x` does not).
- [x] **Step 2–4:** fail, implement, pass (the whole durable-park suite).
- [x] **Step 5:** Docs. Commit `feat: allow and form-check Maverick tools`.

---

### Task 5: Skill reading under the lockdown

**Files:**
- Modify: `durable-park/deploy/hermes_tool_policy.py:106` (`PHASE2_TOOLSETS` loses `"skills"`), and the `check` logic so `skills` is accepted only while the pipeline file denies `skill_manage`
- Test: `durable-park/tests/test_tool_policy.py`, `durable-park/tests/test_pipeline_policy.py`
- Docs: `durable-park/docs/runbook.md` (how to open `skills` in `/etc/durable-park/allowed_toolsets`), `durable-park/docs/architecture.md`

- [x] **Step 1: Write the failing tests**: `test_skills_toolset_can_be_opened` (`read_allowed` accepts `skills`); `test_skill_manage_is_always_denied` (`pa-hermes-local-deny` lists `skill_manage`, and it sits before any approve rule that could match it); `test_check_refuses_skills_without_the_deny` (policy `check` fails when given a pipeline without that deny); the other six Phase-2 toolsets stay refused.
- [x] **Step 2–4:** fail, implement, pass.
- [x] **Step 5:** Yaakov adds `skills` to `/etc/durable-park/allowed_toolsets` (admin step in the runbook); run `hermes_tool_policy.py apply` and `check` (PASS). Commit `feat(policy): allow skill reading; skill_manage stays denied`.

---

### Task 6: withHuman approval rules for Maverick

**Files:**
- Modify: `durable-park/deploy/withhuman/entry-pipeline.json`
- Test: `durable-park/tests/test_pipeline_policy.py`
- Docs: `durable-park/docs/architecture.md` (block table, live revision), `durable-park/docs/gateway-setup.md` (block count and table), `durable-park/docs/tool-rollout.md`

**Interfaces:**
- Produces 7 new blocks, inserted **after `pa-hermes-local-deny` and before `money-server-human`**, each `branch`, 50 ms, first condition `/agent/slug equals proto-agents`:
  1. `pa-park-maverick-auto`: server equals `park:maverick` AND tool regex `^mcp__wh__maverick__(maverick_search_lookups|…the 13 auto names…)$` → approve.
  2. `pa-park-maverick-note`: server `park:maverick` AND tool equals `mcp__wh__maverick__maverick_add_boarding_note` AND `/request/arguments/notify_staff` `is_false` → approve.
  3. `pa-park-maverick-ticket`: server `park:maverick` AND tool regex `^mcp__wh__maverick__maverick_(create_ticket|reply_to_ticket)$` AND `notify_merchant` `is_false` AND `attention_merchant` `is_false` → approve.
  4. `pa-gateway-maverick-auto`, 5. `pa-gateway-maverick-note`, 6. `pa-gateway-maverick-ticket`: the same three for server equals `maverick`, tool regex with `^(maverick__)?maverick_…$`.
  7. `mav-gateway-write-bridge`: server equals `maverick` → webhook block `mav-gateway-write-bridge-webhook`, `endpoint_key: durable-park-bridge`, `allowed_outcomes: ["human", "approve"]`, `include` all false, 5 s, 3 attempts, escalation `notify-yaakov-yehuda` (same settings as `qb-gateway-write-bridge`).
  Everything else on `park:maverick` falls to `money-server-human` (already matches `maverick`) → a person.

- [x] **Step 1: Write the failing tests**: update `EXPECTED_ORDER`; add `park:maverick` to `MONEY_SERVERS`; new `DECISIONS` cases (proto-agents): park `maverick_read` → approve; park `maverick_refund_transaction` → human; park `maverick_create_ticket` with both flags false → approve; with `notify_merchant: true` → human; with the flags **missing** → human; with `"false"` as a string → human; park `maverick_read_all` → human; gateway `maverick_read` → approve; gateway `maverick_refund_transaction` → bridge webhook; tapdash-ops (not proto-agents) `maverick_read` → human.
- [x] **Step 2–4:** fail, edit the JSON (new UUIDs for every condition id), pass.
- [x] **Step 5: Apply** with the withHuman MCP: `update_pipeline` (key `entry`, the file's blocks, `activate: false`), `preview_pipeline` for each DECISIONS case above (expected outcomes match), then `activate_pipeline_revision`. Record the revision number in `architecture.md`.
- [x] **Step 6:** Commit `feat(pipeline): Maverick approval rules`.

---

### Task 7: Register the gateway server and switch it on

**Files:**
- Docs: `durable-park/docs/gateway-setup.md`, `durable-park/docs/runbook.md`, `durable-park/docs/acceptance-2026-10-23-maverick.md` (new record of what was run and seen)

- [ ] **Step 1:** Yaakov runs `sudo bash durable-park/deploy/tunnel/enter-maverick-secrets.sh` (placeholders until sandbox access).
- [ ] **Step 2:** Check the ledger has no `parked`, `approved` or `executing` row; `sudo docker compose run --rm check` lists 42 `maverick_` tools; `sudo docker compose up -d wh-tunnel`.
- [ ] **Step 3:** `create_gateway_server` in withHuman: slug `maverick`, through the existing tunnel (`53902108-628e-44f0-8339-860d00b6fa95`), tunnel entry `maverick`, one scope `default` (auth none, organization). `list_gateway_server_tools` shows 42 tools.
- [ ] **Step 4:** Run `durable-park/deploy/hermes_mcp.sh` (it restarts `hermes-gateway` and checks the registered tools). Expected: the registration log line lists `mcp__wh__maverick__maverick_read`; `hermes_tool_policy.py check` passes.
- [ ] **Step 5:** In Teams, ask the agent "Search Maverick lookups for chargebacks". Expected: `maverick_search_lookups` runs without an approval. Ask it to refund transaction 1: expected an approval request in withHuman (deny it). Record both in the acceptance file.
- [ ] **Step 6:** Commit the docs; push `feat/maverick-tunnel`; open the PR.
