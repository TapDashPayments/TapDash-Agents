# Form check before parking (v1.1)

**What it does.** Before a risky call is parked and a human is asked, the plugin checks its arguments
against the tool's own input rules (the JSON Schema Hermes registered for it, i.e. what the model was
shown). If the call breaks them, it is refused with the first problem, e.g.

    durable-park: "mcp__wh__quickbooks__create_bill" was NOT run and no approval was requested: its
    arguments break the tool's rules (arguments.params.bill.Line[0] must contain ['DetailType']
    properties). Fix the arguments and call "mcp__wh__quickbooks__create_bill" again.

The model fixes the call and tries again; the human is only asked about a call QuickBooks will accept.
It works for every tool and service: the rules are read from Hermes on every call, nothing is hand-written.

**Never stricter than the server.** Hermes's own type fixing runs first (as dispatch would); top-level
`withhuman_*` fields are ignored (the gateway strips them); `nullable` fields accept null; `format`,
`multipleOf`, `pattern`, `propertyNames` and `patternProperties` are left to the server. A call that was
already parked is never re-checked on replay.

**Fails closed.** No rules, unusable rules, a lookup error, or a rule pointing outside itself (never
fetched) -> the call is refused ("could not be checked") and no approval is opened.

**Checker.** `fastjsonschema` 2.22.2 vendored in `plugin/durable_park/_vendor/` (pinned, sha256 in its
README) so it never depends on Hermes's Python environment.

## Self-check and alert

- In each Hermes process that has the risky tools (the gateway), a background thread checks every
  30 minutes that the checker works, the registry is readable, every risky tool's rules load, and Hermes
  type fixing is available. It writes `formcheck_status.json` next to the ledger.
- The 5-minute sweep alerts **once** when that report is missing, older than 75 minutes, or failing on
  two sweeps in a row, and once when it recovers:

      durable-park ALERT: the form checker is not working - <reason>. Risky actions will be refused until it is fixed.

- The alert goes wherever the `durable-park-sweep` cron job delivers (today: Telegram). To change it,
  change that job's delivery target (Hermes cron `--deliver`; Hermes has email and Teams platforms, which
  must be configured first).

## Known limits

- Hermes type fixing (`tools.arg_coercion`) is not a documented plugin API; if an upgrade moves it, the
  check still runs on the raw arguments and the self-check alerts.
- One status file is shared by all Hermes processes that have the risky tools (last writer wins).
- No per-chat cap on retries after an invalid call (Hermes's own turn limits apply).
