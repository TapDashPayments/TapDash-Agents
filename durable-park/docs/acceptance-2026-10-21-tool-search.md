# durable-park: verification record, Tool Search on (2026-10-21)

**Result: Hermes Tool Search is ON on proto-agents since 2026-10-21 19:59 UTC, and the live check passed.** The
change is one key, `tools.tool_search.enabled: "on"`, set as the [runbook](runbook.md#turn-on-tool-search) says. The
compatibility code behind it was merged earlier (PR #26); how the hook sees the bridge tools:
[architecture section 4b](architecture.md#4b-hermes-tool-search-the-bridge-tools-in-the-hook-path).

- Environment: test VM `proto-agents` (QuickBooks sandbox), Hermes Agent v0.22.3, ask-every-call policy
  ([acceptance-2026-10-21-ask-every-call.md](acceptance-2026-10-21-ask-every-call.md)).
- Previous records: [acceptance-2026-10-21-hermes-0.22.3.md](acceptance-2026-10-21-hermes-0.22.3.md).
- All times are UTC, 2026-10-21. "Seen on the VM" means read from the VM at about 20:10 by the person writing this
  record; "reported" means the owner saw it in a chat or in the Hermes dashboard and it was not re-measured here.

## Checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Policy check with the bridge | PASS (reported; the report file is seen) | The gateway start ran `deploy/tool_policy_check.sh`: `ok` on 64 platform keys, with the bridge tools visible. `tool_policy_status.json` reads `"ok": true, "problems": []`, checked 19:59:15Z. |
| 2 | Tool Search activated | PASS (seen) | `agent.log`: `tool_search activated (tier 1): 1 core/visible tools kept, 143 deferred (~39170 tokens)` at 19:59:23, 19:59:40 and 20:00:42 (the Telegram chat `20261021_200036_fbbb1a4f`). |
| 3 | Plugin loaded in the running gateway | PASS (seen) | `plugin_load.json` pid 370372 is the gateway's MainPID, written 19:59:17Z, mode `asking`, hooks `pre_gateway_dispatch` and `pre_tool_call`. |
| 4 | A new chat's first call is cheap | PASS (reported) | About 6.4k input tokens for a new Telegram chat's first call, against 41-60k before. The per-session totals in `state.db` are sums over a whole chat, so this figure was not re-derived from them. |
| 5 | Reads go through the bridge, one row each | PASS (seen) | The ledger holds `allowed` rows for the read tools the model called in that chat (`search_invoices`, `read_invoice`), and no row for `tool_search`, `tool_describe` or `tool_call`. |
| 6 | A money move parks under its real name | PASS (seen) | Park P-33AD7978 (`mcp__wh__quickbooks__create_payment`, $1) was created at 20:01:42Z. |
| 7 | Approve, then run once | PASS (seen) | P-33AD7978 was approved in withHuman by the owner and the row is `executed` (executing 20:02:21Z, passed the bridge once, `gateway_passed_at` 20:02:22Z). Its stored result is QuickBooks (sandbox) payment **153**, `TotalAmt` 1. The ledger holds exactly one row for it. |
| 8 | No wrapper reached the hook | PASS (seen) | `errors.log` has no `reached the park hook still wrapped` line. It has two Hermes warnings of the model's own malformed calls (`tool_call to 'mcp__wh__quickbooks__search_invoices' is missing required ...`, `... read_invoice failed argument validation`): Hermes rejected them before the hook, so nothing ran ([backlog](backlog.md#later)). |
| 9 | Nothing left waiting | PASS (seen) | No ledger row is `parked`, `approved` or `executing` at 20:10. `hermes-gateway` and `hermes-dashboard` are active. |

## Other live changes made the same day

Not repo changes; the current values and the reasons are in the runbook's
[Live Hermes settings](runbook.md#live-hermes-settings-on-proto-agents):

- `platforms.api_server.enabled` and `platforms.ntfy.enabled` set to `false`. Hermes v0.22.3 made the API server
  require `API_SERVER_KEY`, ntfy has no adapter, nothing uses either, and the gateway had reported `DEGRADED`.
- `max_concurrent_sessions: 20` (the key was empty and Hermes warned about it).
- The sweep cron job `5982945c6071` on `*/5 * * * *`, and the secret references in `config.yaml` in the bare `${VAR}`
  form (both PR #28, applied live).
- `gh` 2.46.0 installed and signed in as `ubuntu` only ([runbook](runbook.md#open-a-pull-request-from-the-vm)).

## Not covered

- The reported 6.4k-token figure (check 4) and the 64-key count (check 1, from the start check's output) were not
  re-measured for this record.
- Teams was not exercised in this check; only Telegram.
- QuickBooks payment 153 was read from the ledger's stored result, not looked up in QuickBooks.
