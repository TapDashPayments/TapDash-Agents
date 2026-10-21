# durable-park: acceptance record, Hermes v0.22.3 (2026-10-21)

**Result: PASSED. Hermes Agent v0.22.3 is now the tested version** (`SUPPORTED_HERMES_VERSION` in
[`compat.py`](../plugin/durable_park/compat.py)). The plugin was built against v0.22.2; the source-line references in
code comments and in [architecture.md](architecture.md) still describe the v0.22.2 source and were not re-read.

- Environment: test VM `proto-agents` (QuickBooks sandbox), Hermes Agent v0.22.3, ask-every-call policy
  ([acceptance-2026-10-21-ask-every-call.md](acceptance-2026-10-21-ask-every-call.md)).
- Previous records: [acceptance-2026-10-20.md](acceptance-2026-10-20.md) (v1, Hermes v0.22.2).

## Checks

All times are UTC, 2026-10-21.

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Park and deny | PASS | P-9ED07456 parked and the deny was delivered correctly on v0.22.3. |
| 2 | Approve, then run once | PASS | Live re-test at 18:39Z. P-0AAB04FF (`create_payment`, $1 on invoice 1016) parked; approved in withHuman; the wake was injected in the same Telegram DM; it executed once through `parked_action_execute`; the bridge passed it once (`gateway_passed_at` set); QuickBooks payment 152 was created; invoice 1016 went from $53 to $52. No duplicate payment. |

## What changed in the repo

- `SUPPORTED_HERMES_VERSION` is `"0.22.3"`: on v0.22.3 the plugin logs no version notice; on any other version
  (v0.22.2 included) it logs the usual notice (a WARNING in the gateway process, an INFO elsewhere).
- The version stated in the root README "Built on" table, the runbook's access table and the deploy script headers is
  v0.22.3. Earlier acceptance records, specs and plans keep their v0.22.2 text: they record what was true then.
