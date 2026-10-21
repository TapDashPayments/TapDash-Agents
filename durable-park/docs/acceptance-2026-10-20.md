# durable-park v1 — acceptance results (2026-10-20)

**Result: ACCEPTED for v1 (QuickBooks sandbox, test VM `proto-agents`).** All five acceptance checks passed live. The final whole-branch review (4 parallel reviewers) found no Critical issues and no way for a risky call to run without exactly one human approval or to run twice. Important findings are recorded below and **deferred by Yaakov's decision** (no fixes in v1).

- Code: `/home/hermes/durable-park`, branch `main`, HEAD `c69aa7e`. Unit tests: 326 passed, 2 skipped (`pytest -q -W error`; the 2 skips are the opt-in live smoke tests).
- Hermes v0.22.2, model `meituan/longcat-2.5-preview:free`. withHuman entry pipeline rev 8.
- Risky set (parked): create/update/delete of payment, bill_payment, transfer, deposit, journal_entry, refund_receipt, sales_receipt, credit_memo, vendor_credit, purchase, invoice, bill.

## Checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Park → one approval → resume in same chat → runs once | PASS | Task 8: P-20C0C8CA ($5 vs invoice 1016) → QBO payment 145, invoice $75 → $70. One approval. |
| 2 | Restart while parked | PASS | P-9C59714A ($6) parked 16:52:56Z; `hermes-gateway` restarted 16:56:08Z (PID 166336 → 170280); row stayed `parked`; approved → wake injected 16:59:03Z in same DM → executor 16:59:08Z → bridge `approve` ("Matches approved park P-9C59714A") → `executed` 16:59:13Z; QBO payment 146, invoice $70 → $64. withHuman: park request → "Ask Yaakov" (human); gateway request → webhook block approve. |
| 3 | Duplicates cannot double-run | PASS | (a) Wake for P-9C59714A posted twice → both 202, both skipped, no inject, `notified_at` unchanged. (b) Bot refused twice to re-run a spent park (never called the tool); executor run directly on the live row with counting stubs → "is executed, not approved; it cannot run", other session → "not your session", gate/dispatch called 0×, row unchanged. (c) Signed local bridge deliveries: new key + same args → `human` (no-park); exact retry of that → `human` (replay); exact retry of the real approved delivery (request 84667a16) → `approve` (replay, by design — same withHuman request, which runs at most once); real key + amount changed to $7 → `human` (replay-mismatch). Ledger row untouched. (d) Invoice 1016 still $64 with only payments 145, 146. |
| 4 | Crash mid-run → `unknown`, never retried | PASS | `DURABLE_PARK_CRASH_AFTER_CONSUME=1` via user-unit drop-in. P-48274855 ($7) approved → executor consumed approval → gateway exit 70 at 17:42:19Z → systemd restart 17:42:25Z. Bot then called `parked_action_execute` itself → refused (row not `approved`). Sweep marked `unknown` ("no result recorded after 10 minutes") at 17:55:44Z and posted "outcome UNKNOWN" to Telegram (~13.5 min after crash). Zero bridge deliveries, zero `create_payment` calls; stayed `unknown` over 4 more sweeps; no $7 payment in QBO. Drop-in removed; gateway restarted clean (PID 175794, no crash var). |
| 5 | Right chat / no bypass | PASS | P-151AAC7D ($8) parked; bot then called `create_invoice` → hook blocked: "P-151AAC7D is already waiting for approval in this chat; this call was not run". Executor on the parked id (stubs) → "is parked, not approved; it cannot run", gate/dispatch 0×. Yaakov denied P-151AAC7D and the follow-up P-59F91FCA (`create_invoice`) → both `denied`, both denials delivered in the same DM, nothing created. Direct-gateway bypass (no park → bridge `human` → held for Yaakov) passed earlier. |

Final ledger: 2 executed, 2 denied, 1 unknown. QuickBooks sandbox: invoice 1016 $64, payments 145 and 146 only.

## Observations (not defects)

- "durable-park loaded" every ~6 min = Hermes cron loading plugins for each sweep run (harmless). Cron fires ~1 min after schedule, so the effective sweep period is ~6 min; `unknown` alerts arrive 10–16 min after a crash.
- Free model limits: it refused to re-run a spent park even when asked as a test (good judgment, but it means system guards had to be tested directly); once sent lookups with a missing required `params` field → Hermes paused the `wh` server ~54 s after 3 rejected calls; after a restart it starts a fresh session without earlier IDs.
- Receiver logs "no resume handle this receiver can use" for gateway (non-park) requests — expected.

## Final review findings — all DEFERRED (v2 backlog)

No Critical findings. Reviewer verdicts: plugin core, decision delivery, bridge, deploy+tests — all "acceptable with fixes".

### Important
1. **Hyphenated tool names never match in the bridge** (`bridge/app.py:262`). withHuman names `create-bill`/`update-bill`/`delete-bill` (and vendor tools) with hyphens; Hermes registers them as `create_bill` etc. The bridge builds `mcp__wh__quickbooks__create-bill` → no match → `human`. Effect: every bill park is asked twice (safe direction, resume broken for bills). Fix: sanitize `server`/`tool` like Hermes (`[^A-Za-z0-9_]` → `_`) before building the name; add a `create-bill` test.
2. **Lost approval if inject is scheduled but not delivered** (`wake.py:133-134`). `inject_message` True means scheduled; the gateway can drop it (draining, session not found, adapter missing). Row is marked notified, the sweep never re-wakes it, the chat stays blocked. Fix: sweep re-wakes `approved` rows older than ~15 min; optionally check the session store before injecting.
3. **`parked` row without `request_id` wedges the chat forever** (`wake.py:87-89`, sweep only wakes rows with a request_id). Arises if the gateway dies between `park()` and `set_request_id`. Fix: sweep cancels such rows after ~2 min and prints an alert.
4. **Scope: 32 QuickBooks write tools are auto-approved** (pipeline Rule 3 "everything else → Approve"): create/update (and delete where the server offers it) for account, attachable, class, customer, department, employee, item, payment_method, purchase_order, term, time_activity, vendor, plus `update_company_info` (from the gateway's 142-tool listing). **Decision (Yaakov, 2026-10-20): keep the 12 money types for v1; accepted limit.** Revisit for production (option: park every create/update/delete).
5. **Bridge replay store not bound to the withHuman request** (`bridge/app.py:151-174`). A stored `approve` replays for the same idempotency key forever, relying on withHuman's one-key-per-request, run-at-most-once semantics. Fix: also store/compare `request.id`; prune old rows; document the assumption.
6. **Tool lockdown only checked at deploy time.** A Hermes upgrade, `hermes tools`/`setup`, or a newly enabled plugin toolset could re-open terminal/file tools for the user that owns the ledger. Fix: `ExecStartPre` running `deploy/hermes_tool_policy.py check` on `hermes-gateway`, or a check in the sweep.
7. **Docs stale / missing.** `docs/gateway-setup.md` C3 Rule 1 matches only `server starts_with park:` (live rev 8 uses `All[slug=proto-agents, Any[server starts_with park:, tool starts_with mcp__wh__]]`) — following the doc could auto-approve park requests; digest paragraph pre-R22; "Known risks" still lists R9 as open. No "rotate before production" checklist (see below).

### Minor (selected)
- `mark_gateway_passed` UPDATE not guarded by `status='executing'` (`ledger.py:241-248`).
- Executor finishing after the sweep marked `unknown`: CAS fails, chat says "executed", ledger says `unknown` (no double run). Keep STALE_EXECUTING above MCP timeout + gate budget, or record the result on CAS failure.
- `risky_tools: []` silently disables parking; stale `approved` rows block the chat until the model runs the executor.
- Bridge name collision possible if `server`/`tool` contain `__` (not today); add an expected-server check.
- Integral floats (`5.0`) may over-escalate (double ask).
- Digest logged in full in the bridge journal; truncate.
- `unknown` alert is at-most-once; persistent wake failures only on stderr; AAP credential `expires_at` never checked.
- Receiver unit hardening thinner than bridge; bridge can read `~/.hermes/.env` and `instance_token`.
- `configure_hermes.sh` has no host guard (should refuse unless host is `proto-agents`); restarts the gateway even if a row is `executing`; `bridge_secret` mode not checked; Funnel check is path-string only.
- `.gitignore` lacks secret patterns (nothing sensitive is tracked); `pyproject.toml` lists no dependencies.
- Docs: receiver-setup Files table incomplete; tailnet name/IP in gateway-setup; no single end-to-end setup order.
- Untested: late executor vs sweep, wake routing across two chats, bridge after the row leaves `executing`, real tool-policy enforcement, `enter-secrets.sh`, configure re-run idempotency.

## Before production (required)

Several secrets were pasted into chat during testing (approved for testing only). Rotate all of them:
- Revoke the withHuman provisioner token; re-provision the agent instance (new `instance_token`, receiver secret).
- Rotate the Intuit app client secret and refresh token (re-run OAuth; wipe the `durable-park-qbo-data` volume).
- Issue a new withHuman tunnel token.
- Rotate the `durable-park-bridge` webhook endpoint secret and re-store `bridge_secret`.
- Also regenerate `wake_secret` (not pasted in chat; good hygiene before production).
- Fix deferred Important items 1, 2, 3, 6, 7 and decide item 4 (write scope) before any real books are connected.

## Since acceptance (2026-10-20, evening)

The record above is kept as it was. Changes since then:

- **Important #1 fixed**: the bridge builds tool names the way Hermes does (`create-bill` -> `create_bill`);
  proved live with one approval for bill #147.
- **Minor "integral floats" fixed**: `2.0` and `2` digest the same (`ledger.normalize_args`); a live $2 bill had
  been approved twice before the fix.
- **New: form check before parking** ([form-check.md](form-check.md)): a call that breaks the tool's rules is sent
  back to the model and nobody is asked; proved live (a bill without `DetailType` was blocked, the fixed one
  parked once).
- **Docs** (Important #7): architecture, runbook and READMEs written; stale setup steps corrected (pipeline rule 1,
  bridge matching).
- Code now lives in GitHub `TapDashPayments/TapDash-Agents` (`durable-park/`); the VM works in a clone of it.

Still open: Important #2, #3, #5, #6; the write-scope decision (#4); secret rotation.

The ask-every-call acceptance (2026-10-21) is recorded in acceptance-2026-10-21-ask-every-call.md.
