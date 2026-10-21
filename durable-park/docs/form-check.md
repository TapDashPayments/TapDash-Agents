# Form check before asking (v1.1)

**What it does.** Before a form-checked call is recorded and withHuman is asked about it, the plugin checks its
arguments against the tool's own input rules (the JSON Schema Hermes registered for it, i.e. what the model was
shown). If the call breaks them, it is refused with the first problem, e.g.

    durable-park: "mcp__wh__quickbooks__create_bill" was NOT run and no approval was requested: its
    arguments break the tool's rules (arguments.params.bill.Line[0] must contain ['DetailType']
    properties). Fix the arguments and call "mcp__wh__quickbooks__create_bill" again.

The model fixes the call and tries again; the human is only asked about a call QuickBooks will accept.
It works for any tool and service: the rules are read from Hermes on every call, nothing is hand-written.

**Which tools.** Only the form-checked tools: names that fully match one of the form-checked patterns
(`form_checked_tools`; default `DEFAULT_FORM_CHECKED_TOOLS` in `plugin/durable_park/park_hook.py`: the
QuickBooks create/update/delete of the 12 money types, and the six DocuSign writes), set in
`plugins.entries.durable-park.settings.form_checked_tools` ([settings](architecture.md#plugin-settings); the old
name `risky_tools` is ignored with a warning). Every other tool call is not form-checked but is still asked about (see [architecture](architecture.md#4-tool-call-form-check-then-one-question-to-withhuman)).
Being on this list does not make a call need a person: that is a withHuman pipeline rule.

**Never stricter than the server.** Hermes's own type fixing runs first (as dispatch would); top-level
`withhuman_*` fields are ignored (the gateway strips them); `nullable` fields accept null; `format`,
`multipleOf`, `pattern`, `propertyNames` and `patternProperties` are left to the server. A call that was
already recorded is never re-checked on replay.

**Fails closed.** No rules, unusable rules, a lookup error, or a rule pointing outside itself (never
fetched) -> the call is refused ("could not be checked") and no approval is opened.

**Checker.** `fastjsonschema` 2.22.2 vendored in `plugin/durable_park/_vendor/` (pinned, sha256 in its
README) so it never depends on Hermes's Python environment.

## Self-check and alert

- In each Hermes process that has the form-checked tools (the gateway), a background thread checks every
  30 minutes that the checker works, the registry is readable, every form-checked tool's rules load, and Hermes
  type fixing is available. It writes `formcheck_status.json` next to the ledger.
- The 5-minute sweep alerts **once** when that report is missing, older than 75 minutes, or failing on
  two sweeps in a row, and once when it recovers:

      durable-park ALERT: the form checker is not working — <reason>. Risky actions will be refused until it is fixed.

- The alert goes wherever the `durable-park-sweep` cron job delivers (today: Telegram). To change it,
  change that job's delivery target (Hermes cron `--deliver`; Hermes has email and Teams platforms, which
  must be configured first).

## Known limits

- Hermes type fixing (`tools.arg_coercion`) is not a documented plugin API; if an upgrade moves it, the
  check still runs on the raw arguments and the self-check alerts.
- One status file is shared by all Hermes processes that have the form-checked tools (last writer wins).
- No per-chat cap on retries after an invalid call (Hermes's own turn limits apply).
