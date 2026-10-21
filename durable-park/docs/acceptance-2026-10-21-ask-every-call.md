# durable-park — acceptance record: ask every call (2026-10-21)

**Result: ACCEPTED for "ask every call" on the sandbox (QuickBooks sandbox, test VM `proto-agents`).** Spec §5 rows 6–12 pass, and Task 14 (tool-policy fail-closed) passes both of its cases. Rows 1–5 (Clean Setup Phase 1) and 13–15 (per-toolset rollout and the Phase 2 boundary) were **not run**; the table says why.

- Spec: [`docs/specs/2026-10-21-all-tools-withhuman-design.md`](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §5.
- Previous record: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).
- Code: GitHub `TapDashPayments/TapDash-Agents` `main` at 543cddd (PRs #6 docs/relock-truth, #7 feat/ask-every-call, #8 feat/tool-policy-file, #9 fix/policy-load-verdict, #10 fix/bridge-vendor-copy, #11 fix/refusal-note-source). Live clone `/home/hermes/tapdash-agents` at 543cddd. Unit tests at HEAD: 604 passed, 2 skipped (`pytest -q -W error`).
- Environment: Hermes Agent v0.22.2. withHuman entry pipeline rev 9 active (rev 8 is the rollback for the old code only; rev 10 was a temporary test deny, now deactivated). Pipeline file: [`deploy/withhuman/entry-pipeline.json`](../deploy/withhuman/entry-pipeline.json).
- Policy under test: exempt tools are `parked_action_execute` and `clarify`. Every other tool call is asked. Allow → ledger row `allowed` and the tool runs. Deny → row `refused`, call blocked. Pending → parked (unchanged v1 flow).
- Gateway restarted clean at 15:20:11Z (PID 327562) after the last check; ledger has 0 active rows.

## Checks

All times are UTC, 2026-10-21.

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Connector runs as `ubuntu` (spec 4.1 #1) | NOT RUN | Yaakov's terminal step; lock-out risk. `/home/hermes/clean-setup/COMMANDS.md` steps 3, 5–10 remain. |
| 2 | `hermes` has no sudo (spec 4.1 #2) | NOT RUN | Same as 1. |
| 3 | Nothing the agent runs broke (spec 4.1 #3) | NOT RUN | Same as 1. |
| 4 | SSH passwords off (spec 4.1 #4) | NOT RUN | Same as 1. |
| 5 | Admin can still deploy (spec 4.1 #5) | NOT RUN | Same as 1. |
| 6 | Instant approve | PASS | P-CC2DA87B `mcp__wh__quickbooks__search_items` at 15:21:57: withHuman request f13553ba-7562-5396-bd76-bb0e1fdc50af, pipeline rev 9, outcome approve by block `pa-park-quickbooks-other-outcome` (5 blocks evaluated, completed 15:21:57.634Z). Ledger row `allowed`, note "withHuman: approved", request id recorded. The tool ran (21 active items returned in chat). The spec names `web_search` as the example; this run used a QuickBooks read instead, because web is off under the lockdown. |
| 7 | Instant deny | PASS | A temporary rule was added to rev 10: block `pa-test-deny-company-info-outcome` ("TEST: deny get_company_info", reason "Acceptance test #7: instant deny (temporary rule)."). P-4BE2DE32 `get_company_info` at 13:40:54: request 427a9a13-1ca5-5e52-89ab-2ec4a421169a, outcome deny, 2 blocks evaluated, completed 13:40:54.931Z. Ledger row `refused`; chat showed the refusal; nothing ran. Rev 9 was re-activated afterwards. P-0F7B772B (a Teams `create_invoice` that Yaakov denied by hand) is `denied`, a different case from `refused`. Note: the P-4BE2DE32 row still reads "withHuman denied: A reviewer at the AAP…", because it was written before PR #11. Since #11, a pipeline deny is noted "refused by withHuman policy[: note]". |
| 8 | Park unchanged; restart while parked | PASS | P-F25E5773 `create_payment` $6 on invoice 1016, parked 13:47:02 (request 24909c6f-ee90-5bb9-bb25-410997641e17, outcome human by `pa-park-money-outcome`, rev 9, escalation level 1 urgency interrupt at 13:47:03). `hermes-gateway` was restarted while parked. Yaakov approved. Wake → executor → gateway call → bridge webhook block `block-ad4daeb4` (block id 00bce371-5394-4631-af6f-f1d7b341359e, rev 9, position 3) returned approve "Matches approved park P-F25E5773" (HTTP 200, 327 ms) at 13:51:15.672Z. Row `executed` at 13:51:17. QuickBooks payment 151. One human ask; bridge passed once. |
| 9 | Fail closed (withHuman unreachable) | PASS | `aap_base_url` pointed at an unreachable host. P-795E976E (14:10:09), P-F09268DE (14:10:26) and P-6CB6467E (14:10:45) were all `refused` ("withHuman error: The approval adapter could not obtain a valid AAP decision…"). Breaker opened at 14:10:52. P-C1512E06 at 14:10:55 was `refused` at once ("withHuman unreachable (breaker open)"). `clarify` still worked. The sweep posted one ALERT. After the URL was restored, the sweep posted "withHuman answers again (since 14:31:09Z)". `breaker_status.json`: opened_at 14:10:52Z, closed_at 14:31:09Z. First allowed call after recovery: P-B86C3DA5 at 14:31:09. |
| 10 | Latency | PASS | Over the 35 `allowed` rows since 13:00Z, the hook round-trip (ledger `created_at` → `updated_at`, which brackets the withHuman call) had median 0.282 s, min 0.235 s, max 1.423 s. Spec limit: median ≤ 0.3 s. |
| 11 | Tests; crash → `unknown`, never retried | PASS | Tests: 604 passed, 2 skipped at 543cddd. Crash: P-BEFE1029 `create_payment` $7 on invoice 1016, parked 13:54:30 (request e834ef23-efc6-5c22-bd7d-ed06a9c8f080, human by `pa-park-money-outcome`). Crash injected with `DURABLE_PARK_CRASH_AFTER_CONSUME` after consume. The sweep marked the row `unknown` ("no result recorded after 10 minutes") at 14:06:32. No $7 payment in QuickBooks (invoice 1016 still $53). Zero bridge deliveries for it. The bot's own retry P-4DB5DBC8 at 13:57:27 was `cancelled` by the hook: "already waiting for P-BEFE1029" (no route-around). |
| 12 | Policy as written | PASS | Rev 9 blocks in order: `pa-park-money` (human), `block-06ebcdd6` (bridge webhook), `pa-gateway-other` (approve), `pa-park-quickbooks-other` (approve), `pa-hermes-read` (approve), `pa-hermes-local-deny` (deny), `pa-mcp-human` (human), `pa-default-human` (human), `hermes` (Stripe → human), `approve-every-call`. Live outcomes match: money move → human (`pa-park-money-outcome`); read → approve (`pa-park-quickbooks-other-outcome`); gateway money call → bridge (`block-ad4daeb4`). Revision 9 is the recorded revision. The `preview_pipeline` samples were run during Task 12 and are in `.superpowers/sdd/plan-all-tools/task-12-report.md`. |
| Task 14 | Tool-policy fail closed | PASS | (a) `terminal` added to `/etc/durable-park/allowed_toolsets` → `hermes_tool_policy.py apply` exit 2, nothing applied (Phase-2 toolsets refused). (b) Real leak: `web` removed from `agent.disabled_toolsets` and added to the platform list → `tool_policy_check.sh` wrote `tool_policy_status.json` not ok (`LEAK web_extract`). The plugin loaded with `tool_policy_load.json` not ok. Every non-exempt call was blocked with POLICY_MESSAGE ("the tool policy check did not pass at gateway start…"), shown in chat. Sweep posted ALERT. Config restored from backup; gateway restarted at 15:20:11Z: status ok 15:20:11Z, load ok 15:20:13Z. Sweep posted "the tool policy check passes again." and "the tool policy check passed when the plugin loaded." The next read (P-CC2DA87B) ran. |
| 13 | Per toolset (spec 4.4 step 3) | NOT RUN | Later phase (Phase 2 rollout). Not started. |
| 14 | Boundary: `hermes` cannot read the ledger or the instance token (spec 4.5) | NOT RUN | Phase 2 (Clean Setup, ledger owner). Not started. |
| 15 | Service rules: disallowed transitions refused from the wrong uid (spec 4.5) | NOT RUN | Phase 2 (Clean Setup, ledger owner). Not started. |

The crash-injection, fail-closed, instant-deny and park evidence is from the live VM on `proto-agents`, not from unit tests.

## Observations (not defects)

- The policy-check recovery in the sweep posts two lines (status and load). This is a minor duplicate; it is a follow-up below.
- The Raft plugin's `hermes_plugins.platforms__raft.adapter._on_pre_tool_call` is a pure observer and is listed in `observer_hooks`. A foreign `pre_tool_call` hook that is not on that list blocks every call (`FOREIGN_HOOK`). Ruling: Hermes merges `modify` from all callbacks regardless of order, so "ours lowest wins" is not a defence.
- The bridge root copy in `/opt/durable-park` had been missing `_vendor`. That caused a crash loop, and a double ask on P-346432D5 earlier in the day. It was fixed live and by PR #10 (recursive sync + 401 health gate).
- A false "stale" policy ALERT came from the cron sweep process judging the gateway's report by wall clock. PR #9 fixed it (staleness is measured against process start).

## Follow-ups (open)

- Sweep: collapse the duplicate policy recovery and alert lines.
- Refusal note: name the human decider (withHuman management API lookup). The AAP wire exposes only status and note.
- Runbook: document the `observer_hooks` procedure for the Raft adapter.
- Clean Setup Phase 1 (spec §4.1, rows 1–5): Yaakov at a terminal, `COMMANDS.md` steps 3, 5–10.
- Phase 2 (rows 13–15) not started.
- Rotate all secrets before production (see [acceptance-2026-10-20.md](acceptance-2026-10-20.md) "Before production").
- P-BEFE1029 stays `unknown` by design. The $7 on invoice 1016 is unposted; a human may issue a fresh payment.
