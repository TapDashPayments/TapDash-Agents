# Durable Park (Hermes × withHuman) Prototype — Implementation Plan (v2: real QuickBooks + DocuSign)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove on a throwaway VM that Hermes can park a risky **QuickBooks or DocuSign** tool call to disk, wait hours/days for one withHuman decision, survive restarts, and then run the exact saved call at most once in the original Telegram chat — with the real services reached only through withHuman's gateway, so Hermes never holds their credentials.

**Architecture:** QuickBooks (official Intuit MCP server, stdio, sandbox company) runs in a withHuman **gateway tunnel** container on the VM; DocuSign uses its official hosted MCP (`https://mcp-d.docusign.com/mcp`, developer account) registered in the withHuman gateway with OAuth. Hermes reaches both only through the gateway MCP server. A Hermes plugin (`durable-park`) blocks risky gateway tools in `pre_tool_call`, records them in a SQLite ledger and opens an AAP request (SDK `Gate`, async mode) — this is the one human decision. A receiver service gets withHuman's signed `request.resolved` webhook and forwards a signed wake to Hermes's webhook route; the plugin's `pre_gateway_dispatch` hook re-reads the decision and `ctx.inject_message`s the original chat. The model calls `parked_action_execute`, which consumes the approval and runs the frozen call via `ctx.dispatch_tool`; the gateway's pipeline then asks our **gateway bridge** (a withHuman webhook block) whether this exact call has a consumed park, and approves it once — otherwise it goes to a human. A no-agent cron sweep re-wakes missed decisions and marks crashed executions `unknown`.

**Tech Stack:** Hermes Agent v0.22.2 (commit d923a5eb) plugin API; Python 3.14 (Hermes runtime); Python ≥3.10 venv for services; `agentapprovalprotocol==0.3.3` (httpx); uvicorn + starlette; SQLite; Docker (withHuman tunnel image); Intuit `quickbooks-online-mcp-server` (Node ≥18, stdio); DocuSign hosted MCP (Streamable HTTP, OAuth auth-code + PKCE, pre-registered Integration Key); Tailscale Funnel; withHuman hosted AAP `https://app.withhuman.ai/api/aap`.

**Spec:** Chat-approved design (2026-10-19): "park → approve → resume", durable to disk, one human approval per action, approval consumed once, crash mid-run ⇒ `unknown` (no auto-retry), resume lands in the original chat, services reachable only via the gateway. Acceptance = the five checks (Task 9).

## Global Constraints

- **Sandboxes only:** QuickBooks sandbox company (`QUICKBOOKS_ENVIRONMENT=sandbox`); DocuSign developer/demo account (`mcp-d.docusign.com`, `account-d.docusign.com`). DocuSign recipients are Yaakov's own email addresses only (demo emails are real but not legally binding).
- Test VM only: `ubuntu@148.113.241.158` (vps-c204f492), reached from the Hermes host with `ssh -i ~/.ssh/proto_vm_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=~/.ssh/known_hosts_proto_vm ubuntu@148.113.241.158`. NEVER modify the production Hermes host's `~/.hermes` or services.
- Hermes runs as user `hermes` (`/home/hermes/.hermes`), gateway = systemd **user** unit `hermes-gateway`.
- Repo `/home/hermes/durable-park` (git, author "Claude <noreply@anthropic.com>"); plugin symlink `~/.hermes/plugins/durable-park -> /home/hermes/durable-park/plugin/durable_park`.
- Hermes MCP server entry for the gateway is named **`wh`** (short, so names stay under Hermes's 64-char limit). Gateway slugs: **`quickbooks`**, **`docusign`**. Hermes tool names therefore look like `mcp__wh__quickbooks__create_payment`.
- Risky-tool patterns (plugin setting `risky_tools`, regex fullmatch on the Hermes tool name; editable):
  - `mcp__wh__quickbooks__(create|update|delete)[_-](payment|bill_payment|transfer|deposit|journal_entry|refund_receipt|sales_receipt|credit_memo|vendor_credit|purchase|invoice|bill)`
  - `mcp__wh__docusign__(createEnvelope|updateEnvelope|triggerWorkflow|cancelWorkflowInstance|pauseNewWorkflowInstances|resumeWorkflow)`
  (QuickBooks bill/vendor tools use hyphens: `create-bill` — the `[_-]` handles it.)
- Everything else (reads, master data) passes through untouched.
- AAP: base URL `https://app.withhuman.ai/api/aap`; request timeout `"168h"`; Gate `mode="async"`, `report_outcomes=False`; resume handle `{"framework":"hermes","park_id":...,"session_key":...}`; park request `server` = `"park:" + <gateway slug>` (so pipeline rules can tell park requests from gateway calls).
- Ledger states: `parked → approved | denied | expired | cancelled`; `approved → executing → executed | failed | unknown`. `executing` only leaves to `executed|failed|unknown`. `unknown` is never retried automatically.
- Secrets never printed to logs, chat or tool output. Files: `/etc/durable-park/instance_token` (0640 root:hermes), `/etc/durable-park/receiver.json` (0600 aaprecv), `/etc/durable-park/wake_secret` (0640 root:dpwake; members hermes, aaprecv), `/etc/durable-park/bridge_secret` (0640 root:dpwake), `/etc/durable-park/tunnel.env` (0600 root; QuickBooks client id/secret/refresh token/realm id + tunnel token). Yaakov enters every external secret himself with `read -rs` one-liners.
- Park message (exact): `PARKED {park_id}: "{tool}" needs human approval in withHuman and has NOT run. Do not retry it or try another way to do it. Tell the user it is waiting for approval; you will be told here when it is decided.`
- Ports: Hermes webhook `127.0.0.1:8644` route `durable-park-wake`; receiver `127.0.0.1:8790` path `/aap/events`; gateway bridge `127.0.0.1:8791` path `/wh/bridge`. Public: `https://proto-agents.taila95862.ts.net/aap/events` and `/wh/bridge` via `tailscale funnel --bg --set-path=...`.
- `plugins.hook_callback_timeout: 60`.

## Review Focus

- Model ignores PARKED and retries or reaches the same service another way (e.g. a different risky QuickBooks tool) in the same session → blocked, message names the existing `park_id` (Task 4 `test_second_risky_call_blocked_while_parked`).
- Gateway call arrives without a matching consumed park (agent bypassing the plugin, or args changed) → bridge answers `human`, never `approve` (Task 7 `test_bridge_unmatched_goes_to_human`, `test_bridge_args_mismatch_goes_to_human`).
- `inject_message` returns False / gateway down when the decision arrives → row stays unnotified; sweep re-wakes (Task 6 `test_sweep_rewakes_unnotified_approved`).
- Hook runs without a chat session key (CLI/cron) → fail closed, no request opened (Task 4 `test_no_session_key_blocks`).
- Approval reaches Hermes after the 30-minute start window → executor refuses, `expired`, model told to ask again (Task 5 `test_executor_refuses_expired_approval`).

---

### Task 1: Repo, ledger, Gate wiring

**Files:** Create `plugin/durable_park/ledger.py`, `plugin/durable_park/gatecall.py`, `tests/test_ledger.py`, `tests/test_gatecall.py`, `pyproject.toml` (pytest config), `.gitignore`. Test venv `/home/hermes/durable-park/.venv` (`agentapprovalprotocol==0.3.3 pytest`).

**Interfaces (produces):**
- `Ledger(path: str)`: `park(*, session_key, session_id, tool_call_id, tool, args: dict, call_key, server: str) -> ParkRow` (idempotent on `(session_id, tool_call_id)`), `get(park_id) -> ParkRow|None`, `active_for_session(session_key) -> list[ParkRow]` (parked|approved|executing), `set_request_id(park_id, request_id)`, `transition(park_id, from_status, to_status, **fields) -> bool` (single `UPDATE … WHERE status=?`), `mark_notified(park_id)`, `list_by_status(*statuses) -> list[ParkRow]`, `find_executing_match(tool: str, args_digest: str) -> ParkRow|None`, `mark_gateway_passed(park_id) -> bool` (CAS on `gateway_passed_at IS NULL`).
- `ParkRow` fields: `park_id` (`"P-"+8 uppercase hex`), `session_key, session_id, tool_call_id, tool, server, args_json, args_digest, call_key, request_id, status, consume_key, result_json, note, created_at, updated_at, executing_at, notified_at, gateway_passed_at`.
- `args_digest = sha256(canonical_json(args))`, canonical = `json.dumps(sort_keys=True, separators=(",",":"))`. `call_key = sha256(f"{session_id}:{tool_call_id}")`.
- `gatecall.build_call(row) -> Call` (key=`call_key`, tool, arguments, server=`row.server`, resume handle per Global Constraints); `gatecall.make_gate(base_url, token) -> Gate`.

- [ ] **Step 1:** Repo, venv, `.gitignore` (`.venv/`, `__pycache__/`, `*.db`), git init.
- [ ] **Step 2: Failing tests:** `test_park_is_idempotent`, `test_transition_cas`, `test_active_for_session`, `test_find_executing_match_and_gateway_pass_once` (second `mark_gateway_passed` False), `test_build_call_is_stable`, `test_gate_pending_then_allow_then_consumed` (FakeProvider: `Pending` → decide approved → `Allow` → `Deny(status="consumed")`).
- [ ] **Step 3:** Run `.venv/bin/pytest -q` → FAIL. **Step 4:** Implement (sqlite3, WAL, `busy_timeout=5000`, UNIQUE(session_id, tool_call_id)). **Step 5:** PASS. **Step 6:** Commit `feat: ledger and AAP gate wiring`.

### Task 2: QuickBooks behind the withHuman gateway (tunnel)

**Files:** Create `deploy/tunnel/tunnel.yaml`, `deploy/tunnel/Dockerfile` (FROM withHuman tunnel image; add the Intuit server built from `https://github.com/intuit/quickbooks-online-mcp-server` at a pinned commit), `deploy/tunnel/docker-compose.yml`, `docs/gateway-setup.md`.

**Interfaces (produces):** a withHuman gateway server with slug `quickbooks`, reachable "through a tunnel", granted to agent `proto-agents` with **All tools**; tunnel container `wh-tunnel` (restart unless-stopped) with `QUICKBOOKS_ENVIRONMENT=sandbox` and `QUICKBOOKS_TOKEN_STORE_PATH=/data/qbo-token.env` on a named volume.

- [ ] **Step 1:** Install Docker on the VM; read the withHuman tunnel docs/`check` command and the tunnel image name; record exact names in `docs/gateway-setup.md`.
- [ ] **Step 2: HUMAN STEP (Yaakov):** Intuit developer account → app (Accounting scope) → sandbox company → OAuth 2.0 Playground → copy Client ID, Client Secret, Refresh token, Realm ID; withHuman → Gateway → Tunnels → Set up a tunnel → copy token. Enter all five into `/etc/durable-park/tunnel.env` with the `read -rs` one-liner provided.
- [ ] **Step 3:** Build and run the tunnel; run its `check` → expect the `quickbooks` server listed with its tool count (≈142).
- [ ] **Step 4: HUMAN STEP:** withHuman → Gateway → Register server `quickbooks` → Through a tunnel → agent `proto-agents` → All tools.
- [ ] **Step 5:** Commit `feat: quickbooks via gateway tunnel`.

### Task 3: DocuSign in the gateway + Hermes connected to the gateway

**Files:** Modify `docs/gateway-setup.md`; Create `deploy/hermes_mcp.sh` (writes Hermes `mcp_servers.wh` entry), `tests/live/test_gateway_smoke.py` (run manually).

**Interfaces (produces):** gateway slug `docusign` (remote URL `https://mcp-d.docusign.com/mcp`, OAuth scope with Integration Key client id/secret; if withHuman cannot do OAuth with a pre-registered client, fall back to `luthersystems/mcp-server-docusign` in the tunnel with JWT grant — record which path was used); Hermes MCP server `wh` connected to the gateway with the `proto-agents` instance credential (method chosen in Step 1).

- [ ] **Step 1:** Determine how a non-CLI agent instance connects to the gateway (gateway URL + bearer instance token as Streamable-HTTP MCP, or `withhuman mcp` proxy with a credentials file) from withHuman docs/CLI source; document it.
- [ ] **Step 2: HUMAN STEP:** DocuSign developer account → Apps and Keys → Integration Key + Secret Key → add withHuman's OAuth callback URL to Redirect URIs; withHuman → Register server `docusign` (remote, OAuth, client id/secret, scope `signature`) → Connect OAuth (DocuSign login → Allow Access) → agent `proto-agents`.
- [ ] **Step 3:** Run `deploy/hermes_mcp.sh`, restart gateway; `test_gateway_smoke.py` asserts Hermes lists `mcp__wh__quickbooks__get_company_info` and a `mcp__wh__docusign__getEnvelopes` tool, and a CLI one-shot `hermes chat -q "use get_company_info"` returns the sandbox company name.
- [ ] **Step 4 (critical unknown):** From a throwaway plugin snippet, `ctx.dispatch_tool("mcp__wh__quickbooks__get_company_info", {})` returns the company info → record result. If it fails, STOP and report (executor design depends on it).
- [ ] **Step 5:** Commit `feat: docusign + hermes gateway connection`.

### Task 4: Plugin registration + `pre_tool_call` park hook

**Files:** Create `plugin/durable_park/plugin.yaml`, `plugin/durable_park/__init__.py`, `plugin/durable_park/park_hook.py`, `tests/test_park_hook.py`

**Interfaces:**
- Consumes Task 1.
- Produces `make_pre_tool_call(ledger, gate_factory, risky: list[re.Pattern], session_key_fn) -> callable(**kw) -> dict|None` (None for non-risky; otherwise `{"action":"block","message":...}`); gateway slug derived from tool name (`mcp__wh__<slug>__...`).
- `session_key_fn` default: `from gateway.session_context import get_session_env; get_session_env("HERMES_SESSION_KEY","")`.
- `register(ctx)`: tool `parked_action_execute` (toolset `durable_park`), hooks `pre_tool_call`, `pre_gateway_dispatch`; settings via `ctx.get_config`: `risky_tools`, `aap_base_url`, `token_file`, `ledger_path` (default `<ctx.state.data_dir>/ledger.db`).
- `plugin.yaml`: `name: durable-park`, `version: "0.2.0"`, `description: Durable park/resume of risky tool calls via withHuman (prototype)`, `provides_tools: [parked_action_execute]`, `provides_hooks: [pre_tool_call, pre_gateway_dispatch]`.

- [ ] **Step 1: Failing tests** (FakeProvider, fake session_key_fn): `test_non_risky_passes` (e.g. `mcp__wh__quickbooks__search_customers` → None), `test_risky_call_parks_and_blocks` (`mcp__wh__quickbooks__create_payment` → exact Park message; row `parked`, `server=="park:quickbooks"`, request_id set; provider request `context.resume.park_id` matches), `test_hyphenated_bill_tool_is_risky` (`mcp__wh__quickbooks__create-bill`), `test_docusign_create_envelope_is_risky`, `test_same_tool_call_id_is_idempotent`, `test_second_risky_call_blocked_while_parked` (message contains first park_id and `already waiting`), `test_no_session_key_blocks` (message `durable-park: no chat session; risky tool refused`), `test_provider_error_blocks` (message `durable-park: could not open an approval request; the call was not run`, row `cancelled`).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: park hook and plugin registration`.

### Task 5: `parked_action_execute` executor

**Files:** Create `plugin/durable_park/executor.py`, `tests/test_executor.py`

**Interfaces:**
- Produces `EXECUTE_SCHEMA` (name `parked_action_execute`, param `park_id: string` required, description "Run a previously parked action after you were told it was approved.") and `make_executor(ledger, gate_factory, dispatch, session_key_fn, crash_after_consume=False) -> handler(args, **kw) -> str`.
- Flow: load row → reject missing / other session (`not your session`) / not `approved` → `transition(approved→executing, executing_at=now)` (False ⇒ `already running or done`) → `gate.check(build_call(row))`: `Allow` ⇒ (`crash_after_consume` → `os._exit(70)`) → `dispatch(row.tool, args)` → `transition(executing→executed, result_json)` → return `{"status":"executed","park_id","result"}`; `Deny(expired)` ⇒ `executing→expired` + message `The approval expired before it could run. Ask the user whether to request approval again.`; other `Deny` ⇒ `executing→failed`; dispatch exception or tool error JSON ⇒ `executing→failed` with note.
- `crash_after_consume` from env `DURABLE_PARK_CRASH_AFTER_CONSUME=1`.

- [ ] **Step 1: Failing tests** (stub dispatch counting calls): `test_executes_exactly_once`, `test_executor_rejects_pending`, `test_executor_rejects_other_session`, `test_executor_refuses_expired_approval`, `test_consumed_elsewhere_marks_failed`, `test_tool_error_marks_failed`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: parked action executor`.

### Task 6: Wake handling, receiver, provisioning, sweep, deploy

**Files:** Create `plugin/durable_park/wake.py`, `receiver/app.py`, `receiver/provision.py`, `receiver/wake_post.py`, `scripts/durable_park_sweep.py`, `deploy/durable-park-receiver.service`, `deploy/configure_hermes.sh`, `tests/test_wake.py`, `tests/test_wake_post.py`, `tests/test_sweep.py`

**Interfaces:**
- `async handle_wake(event, *, ledger, async_client_factory, inject) -> dict|None`: only for `event.source.chat_name == "webhook/durable-park-wake"`; payload `{"type":"wake","park_id"}`; `AsyncClient.get` → `parked→approved|denied|expired|cancelled` (note from decision); if approved-or-terminal and `notified_at` NULL → `inject(text, session_key=row.session_key)`, on True `mark_notified`; return `{"action":"skip","reason":"durable-park wake"}`.
- Inject texts (exact): approved `[durable-park] {park_id} was APPROVED in withHuman. Call parked_action_execute with park_id "{park_id}" now to run it exactly as saved. Do not call "{tool}" directly.`; denied/cancelled `[durable-park] {park_id} ("{tool}") was {STATUS} in withHuman{: note}. It did not run. Tell the user and continue without it.`; expired `[durable-park] {park_id} ("{tool}") got no decision before its deadline. It did not run. Ask the user if they want to request approval again.`
- `post_wake(park_id, secret, url="http://127.0.0.1:8644/webhooks/durable-park-wake") -> int` (header `X-Hub-Signature-256: sha256=<hex>`).
- Receiver: `Receiver(resumers={"hermes": resumer}, ...)`, resumer → `post_wake`; starlette `add_route("/aap/events", receiver.asgi(), methods=["POST"])`; uvicorn `127.0.0.1:8790`; systemd unit `durable-park-receiver` (User=aaprecv).
- `provision.py --provisioner-token-file F --url https://proto-agents.taila95862.ts.net/aap/events --name proto-agents-1` → writes `receiver.json` + `instance_token`.
- Sweep (`~/.hermes/scripts/durable_park_sweep.py`, cron `every 5m`, `--no-agent --deliver telegram`): `parked` → `post_wake`; `approved` & unnotified → `post_wake`; `executing` older than 10 min → `unknown` + print `durable-park: {park_id} outcome UNKNOWN — check {tool} in the sandbox before retrying`; else silent.
- `configure_hermes.sh` (idempotent): users/groups/files, receiver venv `/opt/durable-park` (`agentapprovalprotocol==0.3.3 uvicorn starlette`), Hermes config (`plugins.enabled += durable-park`, `plugins.entries.durable-park.allow_gateway_injection: true`, settings, `plugins.hook_callback_timeout: 60`, `platforms.webhook` local route `durable-park-wake` with wake secret, `deliver: log`), plugin symlink, cron, Funnel paths, gateway restart; confirm the `hermes-gateway` unit has `Restart=` set (needed for check 4).

- [ ] **Step 1: Failing tests:** `test_non_wake_event_passthrough`, `test_approved_wake_injects_once`, `test_denied_wake_injects_denial_text`, `test_inject_false_leaves_unnotified`, `test_pending_wake_noop`, `test_post_wake_signature_accepted`, `test_sweep_rewakes_unnotified_approved`, `test_sweep_marks_stale_executing_unknown`, `test_sweep_silent_when_nothing_to_do`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Run `configure_hermes.sh`; verify `/health` on 8644, plugin loaded log line, Funnel paths listed.
- [ ] **Step 6: HUMAN STEP:** withHuman → service agent `proto-agents` → Issue provisioner token → paste into `/etc/durable-park/provisioner_token` via `read -rs`. Run `provision.py` (prints instance_id only); `systemctl start durable-park-receiver`.
- [ ] **Step 7:** Commit `feat: wake, receiver, provisioning, sweep, deploy`.

### Task 7: Gateway bridge (single approval)

**Files:** Create `bridge/app.py`, `deploy/durable-park-bridge.service`, `tests/test_bridge.py`, Modify `docs/gateway-setup.md`

**Interfaces:**
- `bridge/app.py`: POST `/wh/bridge` (uvicorn `127.0.0.1:8791`, User=hermes so it can read the ledger). Verify `WithHuman-Signature` (`t=<unix>,v1=<hex HMAC-SHA256>` per withHuman webhook-endpoint docs; confirm exact signed string from docs in Step 1) with `bridge_secret`, ±300 s. Body gives `tool`, `server`, `arguments`, agent identity. Decision: gateway tool name → Hermes name `mcp__wh__{server}__{tool}`; `row = ledger.find_executing_match(tool_name, args_digest(arguments))`; if row and `mark_gateway_passed(row.park_id)` → `{"outcome":"approve","reason":"Matches approved park {park_id}"}`; else `{"outcome":"human","reason":"No approved park for this exact call"}`. Any error → `human`.
- withHuman Entry pipeline (HUMAN STEP, documented exactly): (1) server starts with `park:` → Ask a human (Yaakov); (2) MCP server is one of `quickbooks, docusign` AND tool matches the risky list → Webhook block `durable-park-bridge` (attempt timeout 5 s, 2 attempts) — outcomes allowed approve|human; (3) everything else → Approve.

- [ ] **Step 1: Failing tests:** `test_bridge_approves_matching_executing_once` (second identical call → human), `test_bridge_unmatched_goes_to_human`, `test_bridge_args_mismatch_goes_to_human`, `test_bridge_bad_signature_401`, `test_bridge_stale_timestamp_401`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Deploy service + Funnel path; HUMAN STEP: withHuman → Webhook endpoints → add `https://proto-agents.taila95862.ts.net/wh/bridge`, copy signing secret into `/etc/durable-park/bridge_secret` via `read -rs`; add pipeline rules above. Send withHuman's test delivery → bridge answers `human` (no park).
- [ ] **Step 6:** Commit `feat: gateway bridge for single approval`.

### Task 8: End-to-end smoke (no restarts yet)

**Files:** Create `docs/acceptance-2026-10-19.md`

- [ ] Yaakov asks the bot: "In QuickBooks, record a $5 payment from customer <sandbox customer> against their oldest open invoice." Expect PARKED reply; one pending request in withHuman (server `park:quickbooks`); sandbox shows no new payment. Approve → resume message in same chat → `parked_action_execute` → bridge approves → payment visible in sandbox; ledger `executed`, `gateway_passed_at` set. Record everything.

### Task 9: Acceptance — the five checks (live, with Yaakov)

- [ ] **Check 1 — park before approval:** DocuSign: "Send the NDA template to <Yaakov's email> for signature." PARKED; withHuman pending; no envelope in DocuSign demo, no email received.
- [ ] **Check 2 — restart then resume exact call:** restart `hermes-gateway` while parked; approve; resume in the same DM; envelope created with exactly the saved recipient/template; ledger `executed`; one envelope only.
- [ ] **Check 3 — duplicates can't double-run:** replay `post_wake` twice, ask the bot to run `parked_action_execute` again, and re-send the withHuman bridge delivery for the same args → no new injects, executor `already running or done`, bridge `human`, still one envelope/payment.
- [ ] **Check 4 — crash mid-run ⇒ unknown:** set `DURABLE_PARK_CRASH_AFTER_CONSUME=1`, park + approve a QuickBooks `create_payment`; gateway dies after consume and restarts; sweep posts the UNKNOWN line to Telegram within 15 min; ledger `unknown`; no payment created and none after two more sweeps. Remove the env var.
- [ ] **Check 5 — right chat, no bypass:** resume targeted `agent:main:telegram:dm:<chat_id>`; while a park is pending, ask the bot to use a different risky QuickBooks tool → blocked `already waiting`; `parked_action_execute` on a still-`parked` id → refused; a direct gateway call for a risky tool with no park (via `hermes chat -q` from the CLI, no session key → hook blocks; and via the gateway tool from a fresh chat with a crafted call) lands in withHuman as **Ask a human**, not auto-approved.
- [ ] Commit results doc; report pass/fail per check.
