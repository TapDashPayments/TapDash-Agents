# durable-park backlog

What is still open, in one place. **Everything under "Before real books" must be done before durable-park is
connected to a real QuickBooks company.** Details of each finding: [acceptance-2026-10-20.md](acceptance-2026-10-20.md).

## Before real books

| # | Item | Why it matters |
|---|---|---|
| 0 | **Contain the agent's tools:** Clean Setup Phase 1 ([spec](../../docs/specs/2026-10-21-all-tools-withhuman-design.md) §4.1) is **DONE (2026-10-22 02:22 UTC, 2026-10-21 10:22 PM ET)**: the connector `hermes-shell-mcp` runs as `ubuntu`, `/etc/sudoers.d/90-hermes` is removed (`hermes` has no sudo), SSH password login is off, the stale `ubuntu` gateway and dashboard units are removed, and all services passed the check. **Still open from Phase 1:** restrict Tailscale SSH to `ubuntu` in the Tailscale admin console (the owner's action; `COMMANDS.md` step 10), and optionally rotate the Hermes dashboard login (`COMMANDS.md` step 3). **Phase 2 is still open** (item 7); docker backend and `approvals.deny` dropped. Must be done before any real credentials. | `hermes` can no longer sudo, so an agent that got a terminal or file tool could not read `tunnel.env` (QuickBooks secret and refresh token, withHuman tunnel token) or change `/opt/durable-park`. It could still read the secrets `hermes` uses by design (`instance_token`, `audit_key`, `wake_secret`; `bridge_secret` is root-only since the bridge got its own user, 2026-10-22) and forge ledger rows, because the ledger is still owned by `hermes`; Hermes approvals are off. The v1 lockdown (re-applied 2026-10-21 03:16 UTC) withholds file and terminal tools and **must stay in place until Phase 2**. Accepted by Yaakov on 2026-10-20 for the sandbox exploration phase (that risk was wider before Phase 1). |
| 1 | Rotate every secret ([runbook](runbook.md#rotate-secrets-before-production)) | Some were pasted into chat during testing. |
| 2 | Retry a decision whose chat message Hermes accepted but dropped (acceptance Important #2) | Otherwise an approved action can sit unannounced and block the chat. The sweep already re-wakes an `approved` row that was never announced; the gap is only a message Hermes accepted then dropped. **Deferred by Yaakov 2026-10-21: not an issue now; revisit before real books.** |
| 4 | Decide which QuickBooks write tools need approval (Important #4) | Today only the 12 money types ask a person. The non-money writes (customers, vendors, items, accounts...) are approved by pipeline rules `pa-gateway-other` and `pa-park-quickbooks-other`. |
| 7 | A separate owner for the ledger ([design](receiver-setup.md#v2-separate-ledger-owner-design-only-not-built)); planned as spec §4.5 (§8 item 10), required before file, terminal or code tools open. **The bridge part is done** by the [bridge change](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md): the bridge no longer reads or writes the ledger, so a forged row cannot get a gateway call approved. What is left is the ledger as the park hook, wake handler, executor and sweep use it (see "Option 2" under Later for removing it) | The lockdown is the only thing keeping the agent from writing the ledger (it no longer buys the bridge's approval, but a forged row could still confuse the executor or the sweep). A separate owner is **not enough on its own** while the agent's user could sudo; that part of item 0 is done (Clean Setup Phase 1, 2026-10-22), so this is now the route to a real boundary around the ledger. **Still open.** Until it is done, `hermes` owns `ledger.db` and is in `dpwake`, so file, terminal and code tools must stay off. |
| 8 | Langfuse retention: the Hobby plan gives 30 days of data access and the retention cannot be changed, against the 365-day retention policy (TapDash Agents security decisions, 2026-10-15). Decide a paid plan or self-hosting ([Tracing](runbook.md#tracing-langfuse)) | After 30 days the traces can no longer be read. Fine for the test VM; not for real books. |
| 9 | Langfuse is another outside company that holds full conversations (capture mode `full`: chat text, model inputs and outputs, tool arguments and results). Decide whether that is acceptable for production, and which capture mode to use | With real books those would include real customer and money data. |
| 10 | **Critical (found 2026-10-22, not caused by tracing):** the Telegram bot token is written in plain text to the `hermes-gateway` journal (the `httpx` INFO request lines). Rotate the token in BotFather and update `TELEGRAM_BOT_TOKEN` in `~/.hermes/.env` (typed in with `read -rs`, never in chat), then set the `httpx` logger to WARNING for the gateway so it is not logged again | Anyone who can read the journal can control the bot. Until it is rotated, treat the old token as exposed. |

## Langfuse tracing gaps (from the 2026-10-22 audit)

Found by auditing the live traces ([acceptance-2026-10-22-langfuse.md](acceptance-2026-10-22-langfuse.md)). Tracing is
live and works. Items 1 to 4 need plugin or durable-park code, so each is a recommendation for Yaakov to decide; item 5
is only noted. No code was changed in the tracing work.

1. **No `userId` on any trace.** The Nous plugin never sets one. Recommend an upstream PR to pass the chat user id, or a
   small durable-park-side tag. Worth it once there is more than one user.
2. **Every trace is named `Hermes turn` and every model call `LLM call N`.** Recommend upstream: name by platform and
   purpose (for example `telegram-chat-turn`).
3. **Park, approval and resume are separate traces,** joined only by the park id in the text; the inner `create_payment`
   is not a child of `parked_action_execute`. Recommend a durable-park follow-up: add the park id and the withHuman
   request id as trace metadata or tags (for example `park:P-...`), and an event or span for the approval decision. It
   touches the park, wake and executor code, so it needs **its own plan, tests and independent review** (AGENTS.md Rule 2).
4. **The root `CHAIN` has a parent observation id that points at a span that is never exported.** Cosmetic; upstream.
5. **Noted, not a gap: the Langfuse public key is in every span's metadata** (`scope.attributes.public_key`, an SDK default). An
   identifier, not a secret; nothing to do unless the SDK adds an option to leave it out.

Also found on the way:

- The QuickBooks server answers "withhuman_context was IGNORED": the agent sends a parameter that is dropped.
  Investigate where it comes from and whether anything relied on it.
- `hermes plugins enable langfuse` created a new Hermes environment directory; the old ones remain. Clean up the old
  environments later, when disk space matters.

## Later

- **Option 2: remove the ledger; withHuman is the only state** (park context stored in the withHuman request, the list API for the sweep). The bridge already works this way ([spec](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md)); this extends it to the park hook, wake handler, executor and sweep, so there is no file the agent's user could forge. Replaces the dpledger service design in spec 2026-10-21 §4.5.
- Stop double-checking non-money QuickBooks calls: today a non-money write is asked once as `park:quickbooks` (pipeline rule `pa-park-quickbooks-other` approves it) and again at the gateway (`pa-gateway-other` approves it), so both ask withHuman for nothing. Park and replay such calls without the second pass, or skip the park for them.
- Alerts to Microsoft Teams instead of Telegram (Hermes's Teams platform is now set up in the same gateway, since
  2026-10-21; only the sweep's delivery target has to change).
- Widen `risky_tools` beyond QuickBooks money moves: withHuman decides every call (spec 2026-10-21 §4.2); today the
  agent has no terminal or file tools, so nothing else needs gating yet.
- More than one approver, or a backup approver (a withHuman escalation-path change; no code).
- Tool Search: on since 2026-10-21 19:59 UTC and verified ([record](acceptance-2026-10-21-tool-search.md)); what is left is `docs/tool-rollout.md` (spec [section 4.4](../../docs/specs/2026-10-21-all-tools-withhuman-design.md), section 8 item 9).
- Replace the GitHub login on the VM (`gh` as the admin user `ubuntu`) with a **fine-grained token limited to this repository**. Today it is a broad classic OAuth token (scopes `repo`, `workflow`, `read:org`, `gist`) that reaches every repository of that account ([runbook](runbook.md#open-a-pull-request-from-the-vm)).
- Optional: run the sweep from a systemd timer instead of Hermes cron, so it can alert when the gateway is down (today it runs inside the gateway, so a dead gateway also silences it). The cost is about 1 s of CPU per run, effectively negligible.
- File two upstream Hermes issues: the `${env:VAR}` warning is printed before `secrets.command` has run, and the plugin catalog rejects unknown tiers in older clients.
- Mem0 plugin files are left installed but disabled on `proto-agents` (`~/.hermes/plugins/mem0`, `~/.hermes/mem0.json`, and `MEM0_TELEMETRY` in `~/.hermes/.env`). Yaakov decides: keep or remove.
- Delete the stale `~/.hermes/plugin-data/durable-park/plugin_load.json.hidden` (left by a manual test on 2026-10-21 17:00 UTC; harmless, nothing reads it) when convenient.
- The bot sometimes sends malformed QuickBooks calls (for example `get_invoice_pdf` without `invoice_id`). Hermes rejects them before the park hook, so nothing runs, but 3 in a row pause the `wh` MCP server for about 60 s. Reported by the owner; look at the model's instructions and the tool descriptions.
- Dashboard pages: pending withHuman approvals, full withHuman view, live trace of tool calls (spec section 8 item 12).
- The archived `ubuntu` Hermes install still holds Teams secrets (spec section 8 item 16).
- Rev 8 rollback hazard: rev 8 auto-approves `hermes`/`mcp:` calls, so never activate it while the ask-every-call code runs ([runbook](runbook.md#rollback)).
- Name the human decider in the refusal note (withHuman management API lookup; in progress).
- The second agent (content) and one Hermes profile per person.
- `bridge_check.py` and bridge-unit minors, deferred from the bridge-own-user change ([plan](plans/2026-10-22-bridge-own-user.md)): check 4 (no other process runs `bridge.app`) is a heuristic that scans `/proc/*/cmdline` and can false-alarm (any process with `bridge.app` or `bridge/app.py` among its arguments, such as an editor, fails it); an edit to `durable-park-bridge.socket` takes effect only after `sudo systemctl restart durable-park-bridge.socket` (the deploy script does not restart a running socket); the check does not look at the permissions of the `/etc/durable-park` directory (it does check the running bridge's supplementary groups, since the final review).
- Bridge: also require a human decider for the park (audit `approval.decided` actor), so a future pipeline edit that auto-approves money parks cannot make the bridge approve without a person. Today the bridge checks that the park request is `approved` and consumed in withHuman, whoever or whatever decided it.
- Minor review findings listed in [acceptance-2026-10-20.md](acceptance-2026-10-20.md#minor-selected).

## Done since v1

- 2026-10-22: the gateway bridge runs as its own user `dpbridge` and systemd holds its port ([plan](plans/2026-10-22-bridge-own-user.md)): `durable-park-bridge.socket` keeps `127.0.0.1:8791` permanently (the service gets it as fd 3, `--systemd-socket`, `Restart=always`), `bridge_secret` is `root:root 0600`, `bridge_secret` and `audit_key` reach the bridge through `LoadCredential` (a rotation needs a bridge restart), `deploy/bridge_check.py` verifies the isolation at every deploy, and `configure_hermes.sh` refuses to run without both keys. Closes the gap that anything running as `hermes` could kill the bridge and answer "approve" on its port. Minors deferred: see Later.
- 2026-10-22: the bridge verifies approvals in withHuman instead of the ledger ([spec](../../docs/specs/2026-10-22-bridge-verifies-withhuman.md)): stateless, org key with `audit.read`, `request.read`, `request.read.unrouted`, 120 s window, single use, the unit has no ledger path and `ProtectHome=yes` ([runbook](runbook.md#the-gateway-bridge)). Settles the bridge part of item 7 and the old item 5 (stored answers: the bridge keeps none). The old `bridge_deliveries.db` can be deleted once the new bridge is live.
- 2026-10-21: Tool Search compatibility (PR #26) and Tool Search switched on and verified live, with one approved $1 payment run once through the bridge ([acceptance-2026-10-21-tool-search.md](acceptance-2026-10-21-tool-search.md)).
- 2026-10-21: stale policy log level fixed (PR #22); executor told how to find the park id (PR #23); load check waits the full timeout (PR #25); the sweep's subprocess test is hermetic (PR #27); secret references written bare so the CLI is quiet, and a true 5-minute sweep on a `*/5 * * * *` schedule (PR #28; both applied live).
- 2026-10-21: Hermes v0.22.3 is the tested version (`SUPPORTED_HERMES_VERSION`): live approve -> run-once re-test passed;
  record in [acceptance-2026-10-21-hermes-0.22.3.md](acceptance-2026-10-21-hermes-0.22.3.md).
- 2026-10-21: ask every call (PR #7): every tool call goes to withHuman for a decision, except the exempt tools
  (`parked_action_execute`, `clarify`); acceptance record in [acceptance-2026-10-21-ask-every-call.md](acceptance-2026-10-21-ask-every-call.md) (PR #12).
- 2026-10-21: tool policy from one list, with a start check (PRs #8, #9).
- 2026-10-21: bridge copies the whole plugin package, `_vendor` included, and gates on a live bridge (PR #10).
- 2026-10-21: the refusal note says whether a policy rule or a person decided (PR #11).
- 2026-10-21: one policy alert when the tool policy report fails (PR #13).
- 2026-10-21: load check: the gateway is stopped (`ExecStartPost`, `deploy/plugin_load_check.sh`) when the plugin left no
  load proof (`plugin_load.json`) for its pid; the sweep alerts as a backup; the drop-in limits restarts to 3 in 10 minutes.
- 2026-10-21: check the agent's tool policy at every gateway start (Important #6), built as spec §8 item 7 part 2: the `hermes-gateway` drop-in runs
  `deploy/tool_policy_check.sh` before each start; the plugin refuses every call when the report is missing, failed
  or older than 15 minutes, and the sweep alerts on failure and on recovery. It checks the v1 lockdown that is
  applied on proto-agents.
- Bridge matches hyphenated tool names (Important #1).
- `2.0` and `2` treated as the same amount (minor).
- Form check before parking.
- Docs, diagrams and the docs-update check (Important #7).
- A `parked` row that never got a request id is cancelled by the sweep after 10 minutes (Important #3).
