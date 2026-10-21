# All tools, withHuman decides every call, clean setup — design

Date: 2026-10-21 · Status: Draft for Yaakov's review · Author: Claude (with Yaakov)

Research this spec rests on (read-only, 2026-10-21, kept outside git): `aap-research.md` (AAP, the stock
`aap hook hermes`, pipeline rev 8, latency), `clean-setup-research.md` (users, services, files, sudo), and
`relock-report.md` (the v1 lockdown re-applied at 03:16 UTC). Findings from them are cited as "(AAP research §n)"
and "(Clean Setup research §n)".

## 1. Intent (in Yaakov's words, written back)

Stated:

1. **One Hermes agent** on `proto-agents` (done 2026-10-21). Two agents later, one at a time.
2. Goal state: the agent may have **all Hermes tools**, and **withHuman decides every tool call** by rules set in
   withHuman's pipeline (auto-approve, block, or ask a human), per tool and per argument (for example amounts).
   withHuman is the single place for permissions and the audit record of every call.
3. Hard requirement (why durable-park exists): a call waiting for a human is **saved to disk and survives Hermes
   restarts**. No in-memory holds.
4. Approach: **extend durable-park** so it asks withHuman about every tool call, not only the 12 QuickBooks money
   types. Instant decision → run or block at once, no park, no chat round trip. Human needed → park exactly as today.
5. Clean Setup, Phase 1 now: the admin connector moves to the admin user `ubuntu`; `hermes` loses passwordless
   sudo; SSH password login goes off. Phase 2: the ledger moves to its own user (the v2 ledger service already
   designed in `receiver-setup.md`); the agent user loses read access to `bridge_secret` and anything it does not need.
6. Order: (1) Clean Setup Phase 1 + SSH; (2) durable-park "ask every call" + pipeline rules, then open tools one by
   one, local file/terminal/code tools last; (3) Clean Setup Phase 2, and only then file/terminal/code tools.
   Reason: today the agent's user owns `ledger.db` and is in `dpwake`, so with file or terminal tools it could forge a
   ledger row or read a secret even without sudo.
7. Test `tools.tool_search` later (off today on purpose: the wake message names `parked_action_execute`).
8. Dashboard pages (pending approvals, full withHuman view, live tool trace) stay wanted; not in this spec.

Assumed (not stated; Yaakov confirms or corrects):

- A1. The QuickBooks sandbox stays the only money system during this work; real books wait for the backlog's
  "Before real books" list.
- A2. "Every call" excludes two tools that are not actions: `parked_action_execute` (the replay of an already
  approved call) and `clarify` (a question to the user). See §4.2.
- A3. The final default for the `proto-agents` agent in the pipeline is **ask a human**, not approve: a tool nobody
  wrote a rule for asks a person. (The production `hermes` agent keeps its own blocks.)
- A4. Until Phase 2, the pipeline **denies** `terminal`, `execute_code`, `process`, `write_file`, `patch`,
  `delegate_task`, `cronjob` outright, and those toolsets stay disabled in Hermes as well (belt and braces).

## 2. Where we are (verified 2026-10-21 03:16–03:37 UTC, read-only)

| Fact | Evidence |
|---|---|
| One Hermes agent, user `hermes`, user units `hermes-gateway` and `hermes-dashboard`; Telegram and Teams connected | relock-report.md step 7; merge spec §10 |
| v1 tool lockdown is **back** as a stopgap: only `clarify`, `parked_action_execute`, `mcp__wh__quickbooks__*` on 58 platform keys | relock-report.md step 5 (`hermes_tool_policy.py check` ok); `~/.hermes/config.yaml:1060` `agent.disabled_toolsets` (35 toolsets), `:1905` `platform_toolsets` |
| Hermes built-in approvals off | `config.yaml:1896-1897` `approvals: mode: false` |
| `plugins.hook_callback_timeout: 60`; `tools.tool_search.enabled: "off"`; no `hooks:` block; no `aap` binary | `config.yaml:75`, `:1882-1883`; AAP research §3 |
| durable-park parks only `DEFAULT_RISKY_TOOLS` (12 QuickBooks money types + DocuSign names) via a `pre_tool_call` hook; non-risky calls return `None` at once | `park_hook.py:18-24`, `:84-87` |
| A park is row-first: ledger row, then AAP request (`Gate` mode `async`, 168 h, 8 s retry budget, 10 s HTTP); `Pending` → request id stored, block "PARKED"; anything else → row `cancelled`, block | `park_hook.py:125-156`; `gatecall.py:12-17`, `:57-63` |
| The AAP SDK in async mode returns `Allow` only after `consume` succeeds, `Deny` when the pipeline denies/errs, `Pending(request_id)` only while a human must decide | `agentapprovalprotocol/_gate.py:180-212` |
| Replay never re-enters the hook: `parked_action_execute` uses `ctx.dispatch_tool` | `__init__.py:140-141`; executor.py:6; architecture.md §2 note 16 |
| withHuman entry pipeline rev 8: (1) agent `proto-agents` and (server starts with `park:` or tool starts with `mcp__wh__`) → human; (2) server `quickbooks` + money tool regex → bridge webhook; (3) tool contains `Stripe` → human; (4) approve everything | AAP research §4 |
| Latency: proto-agents → withHuman median 0.133 s (min 0.113, max 0.381); real create→consume 105–153 ms | AAP research §5 |
| `hermes` has passwordless sudo via `/etc/sudoers.d/90-hermes` (30 bytes, Oct 19 23:29); `ubuntu` via `90-cloud-init-users`; neither is in `sudo`/`admin` groups | `sudo ls -l /etc/sudoers.d/`; Clean Setup research §1 |
| The admin connector `hermes-shell-mcp` runs as `User=hermes Group=hermes`, `WorkingDirectory=/home/hermes/hermes-shell-mcp`, port 8792 behind Funnel `/hermes-shell` | `systemctl show hermes-shell-mcp`; Clean Setup research §2 |
| sshd: `50-cloud-init.conf` says `PasswordAuthentication yes`, `60-cloudimg-settings.conf` says `no`; effective value **yes** | `sudo grep` of both files; `sudo sshd -T` → `passwordauthentication yes` |
| `hermes` is in `dpwake`; `ledger.db` is `hermes:hermes 644`; `bridge_secret`/`wake_secret` `root:dpwake 640`; `instance_token` `root:hermes 640` | `id hermes`; `stat` of the files |
| Live clone `/home/hermes/tapdash-agents` is at `ad4c35d`, 4 commits behind `origin/main` | `git status -sb` in the clone |
| Open PR #3 (branch `ops/one-agent-merge`) documents the **open** toolset as live (e.g. its architecture.md:299 "now the agent's user has passwordless..."); that is no longer true since the re-lock | worktree `one-agent-merge` at `b354c96` |

## 3. Approaches considered

| Approach | How it waits for a human | Double ask on QuickBooks | Cost per auto-decided call | Verdict |
|---|---|---|---|---|
| **A. Extend durable-park** to ask withHuman about every call in-process (chosen) | Park: ledger row on disk, webhook wake, resume in the same chat, run once. Survives restarts. 168 h window. | None: the money call's hook request *is* the park request (server `park:quickbooks`); the replay is matched by the bridge as today. | ~0.15–0.3 s (one kept-alive HTTPS client, no process spawn). | **Chosen.** Meets the hard requirement with code that is already accepted and tested. |
| B. Stock `aap hook hermes` shell hook for every tool | In-memory: the hook process long-polls up to 270 s, then expires and blocks. Does not survive a restart; a human who answers after 4.5 min answered nothing. | Yes without a `matcher`: hook request + park request + replay (AAP research §3). | ~0.3 s (spawn + TLS each call), 0.6–0.8 s when loaded. | Rejected: breaks the hard requirement. |
| C. Both: shell hook with a `matcher` that excludes the money tools and `parked_action_execute`, durable-park unchanged | Shell hook for everything else, in memory. | Avoided by the matcher. | As B, plus two credentials and two config surfaces to keep in step. | Rejected: still an in-memory hold for every non-QuickBooks human decision; two mechanisms to audit. |

## 4. Design

### 4.1 Clean Setup Phase 1 — users, connector, sudo, SSH

Goal: the agent's user `hermes` has no root path; `ubuntu` is the admin; nothing the agent runs changes.

| Step | What | Exact place |
|---|---|---|
| 1 | Keep an out-of-band session open for the whole change: `tailscale ssh ubuntu@proto-agents` (and the OVH console as last resort). | — |
| 2 | Copy the connector to the admin: `/home/ubuntu/hermes-shell-mcp/` (server.py, `.env` 0600, `bg/`, audit.log). Re-create `.venv` as `ubuntu` (the old venv carries hermes-owned absolute paths). | `/home/hermes/hermes-shell-mcp` → `/home/ubuntu/hermes-shell-mcp` |
| 3 | Edit the system unit: `User=ubuntu`, `Group=ubuntu`, `WorkingDirectory=`, `EnvironmentFile=`, `ExecStart=` to the new paths. Port 8792 and the Funnel path `/hermes-shell` stay. | `/etc/systemd/system/hermes-shell-mcp.service` |
| 4 | Fix the text that says the connector runs as hermes with sudo (`server.py:149,172`) and the Proto-Agents connector description in Claude. New text: runs as `ubuntu` (admin, passwordless sudo); Hermes files are reached with `sudo -u hermes`. | server.py; connector settings |
| 5 | `daemon-reload`, restart `hermes-shell-mcp`; from Claude run `id` → `uid=1000(ubuntu)`. Only then continue. | — |
| 6 | Delete `/etc/sudoers.d/90-hermes`. `hermes` is in no sudo group, so this single file is the whole grant. | `sudo rm /etc/sudoers.d/90-hermes` |
| 7 | SSH passwords off: set `PasswordAuthentication no` in `50-cloud-init.conf` (sshd keeps the first value it reads, and `50-` sorts before `60-`). `sudo sshd -t`, then `sudo systemctl reload ssh`. Key login for `ubuntu` is unaffected (`/home/ubuntu/.ssh/authorized_keys`, 1 key). | `/etc/ssh/sshd_config.d/50-cloud-init.conf` |
| 8 | Remove the stale inactive `hermes-gateway.service` and `hermes-dashboard.service` unit files under `/home/ubuntu/.config/systemd/user/` so a second gateway can never start under `ubuntu`. | Clean Setup research §2 |
| 9 | Docs: architecture.md §6 (add the connector row, user `ubuntu`), runbook "Deploy a change" (root steps run by `ubuntu`), AGENTS.md rule 3 (commands through the connector run as `ubuntu`). | repo |

How admin work reaches hermes-owned files afterwards:

- Files: `sudo -u hermes -H bash -lc '<command>'` (home is 750, `~/.hermes` is 700).
- User units: `sudo -u hermes XDG_RUNTIME_DIR=/run/user/1001 systemctl --user <verb> hermes-gateway` (or
  `systemctl --user -M hermes@ <verb> ...`).
- Git: `sudo -u hermes git -C /home/hermes/tapdash-agents pull --ff-only`; worktrees under
  `/home/hermes/tapdash-agents-wt/` are created the same way. The deploy key stays `hermes`'s.
- Deploy scripts (`configure_hermes.sh`, `hermes_mcp.sh`, `enter-secrets.sh`, `provision.py`) already expect root:
  `sudo bash ...` from `ubuntu`. No script change.

Not in this phase: the tailnet ACL still allows Tailscale SSH as `hermes` (a `login -- hermes` session was seen).
Restricting the `ssh` rule to `ubuntu` is done in the Tailscale admin console (decided 2026-10-21, Q4: yes; Yaakov does it).

### 4.2 durable-park "ask every call"

**Decision flow** for every `pre_tool_call` (one flow, no second code path):

1. Tool in the exempt list → return `None` (runs). Default exempt (decided 2026-10-21, Q1: yes): `parked_action_execute`, `clarify`.
   - `parked_action_execute` carries only a park id; the executor consumes the approval itself and replays through
     `ctx.dispatch_tool` (no hooks). Asking withHuman about it would be a second ask for one action.
   - `clarify` is a question to the user, not an action; it must keep working when withHuman is unreachable so the
     agent can say so. Setting: `exempt_tools` in the plugin settings; changing it is a reviewed policy change.
2. No chat session, no session id or no tool call id → block (as today, `park_hook.py:92-97`).
3. Form check, only for tools in `form_checked_tools` (today's `risky_tools` list, renamed; default = the 12 QuickBooks
   money types + DocuSign). Other tools are not form-checked: Hermes and the tool validate them.
4. Ledger row first (`ledger.park`, status `parked`, server label per the table below), then one AAP request through
   a **shared** `Gate` (mode `async`, 168 h, retry budget 8 s, HTTP 10 s, one kept-alive `httpx.Client` for the
   plugin; token still read lazily from `token_file`).
5. Verdict:
   - `Allow` (the SDK already consumed the approval) → row `parked → allowed` (new terminal status, request id and
     decision note stored) → return `None`. Hermes runs the tool with the same arguments.
   - `Deny` → row `parked → refused` (new terminal status; the SDK status `denied|expired|error` and note stored) →
     block. Chat sees: `durable-park: "<tool>" was refused by withHuman policy[: note]. It did not run. Do not retry it
     or work around it; tell the user.` A transport failure (withHuman unreachable, credential refused) is a `Deny`
     with status `error`: chat sees `durable-park: withHuman could not be reached; "<tool>" did not run. Try again
     later.`
   - `Pending(request_id)` → the "one waiting park per chat" rule is applied **here** (today it runs before the
     request, `park_hook.py:109`): if another row in this chat is `parked|approved|executing`, cancel the new request
     (`DELETE /v1/requests/{id}`), row → `cancelled`, block "already waiting" as today. Otherwise store the request
     id, block "PARKED P-XXXX" as today. Wake, approval, `parked_action_execute`, replay, bridge: unchanged.
6. Any exception → block, row `cancelled` when no request was opened (as today, `park_hook.py:157-164`).

**What goes in the AAP request** (`gatecall.build_call`):

| Field | Built-in and plugin tools (`terminal`, `web_search`, ...) | withHuman gateway tools (`mcp__wh__<slug>__<tool>`) | Other MCP servers (`mcp__<alias>__<tool>`) |
|---|---|---|---|
| `tool` | the Hermes tool name | the full Hermes name, as today | the full Hermes name |
| `server` | `hermes` | `park:<slug>` (unchanged, so pipeline rule 1 and the bridge keep working) | `mcp:<alias>` |
| `arguments` | the exact arguments, except: keys named like secrets (`password`, `token`, `secret`, `authorization`, `api_key`, case-insensitive) are replaced by `[redacted]`; if the canonical JSON exceeds 16 KiB, string values are cut to 1 KiB each and `context.args_truncated: true` is set | same | same |
| `context` | `args_sha256` (digest of the exact arguments, same function as the ledger's `args_digest`), `platform`, `session_key` | same | same |
| `resume` | `{framework: hermes, park_id, session_key}` as today | same | same |

The ledger always keeps the exact arguments; `args_sha256` ties the withHuman audit entry to them. The replay
(money tools) still uses the ledger's exact arguments, and the bridge still matches on their digest.

**QuickBooks, no double ask.** A money call's hook request is the park request (`park:quickbooks`, tool
`mcp__wh__quickbooks__create_payment`) → pipeline rule 1 → human → `Pending` → parked. Replay through the gateway →
rule 2 → bridge → approve. Exactly one human ask, as today. A non-money QuickBooks call (reads, customers, items)
gets two automatic withHuman evaluations (hook request `park:quickbooks` → approve; gateway call `quickbooks` → approve)
and zero human asks. Rule 1 must therefore be narrowed to the money tool names (§4.3); today it sends every `park:`
request to a human.

**Ledger: rows for every call, not only parks.** Decided 2026-10-21 (Q3): yes, a row for every call with 30-day retention; the rows also feed the dashboard trace. Why: (a) the `Pending` path needs the row before the
request so a request id never lives only in memory (crash safety; guarantee 3), (b) one flow is easier to test than
two, (c) the dashboard's "live trace" (work list) gets a local source. Cost: two SQLite writes per tool call (about
a millisecond each) and growth of a few thousand rows a day at most. The sweep deletes `allowed|refused|cancelled`
rows older than 30 days (`ledger_retention_days`, new setting). `allowed` and `refused` are terminal and not
"active", so they never block a chat. Backlog #3 (a `parked` row with no request id) becomes more likely because
every call opens that window for a few hundred milliseconds; the sweep must cancel such rows older than 10 minutes.
That is a prerequisite, listed in §8.

**Concurrency and latency.** Hermes fires the hook once per tool call; parallel tool calls run the hook in parallel
on its timeout workers. The shared `httpx.Client` is thread-safe; the ledger uses CAS transitions. Expected cost
per auto-decided call: ~0.15–0.3 s (create + consume, kept-alive connection); a 5–15-call turn is ~1–4.5 s slower.
`hook_callback_timeout` stays 60 s: the worst single call on an outage is 8 s retries + 10 s HTTP ≈ 18–28 s, under
the cap. Circuit breaker: after 3 consecutive transport failures the hook blocks instantly for 30 s without calling
withHuman, then tries once more. The outage still freezes the agent (fail closed, by design); the sweep raises one
alert when the breaker opens and one when it closes.

**Fail closed.** Every path except `Allow` blocks. Ledger unavailable at start → `fail_closed_hook` blocks every
non-exempt tool (today it blocks only risky ones). No `modify` hook may run after durable-park: the plugin logs an
error and blocks every call if another `pre_tool_call` callback is registered after it (the arguments approved
must be the arguments run).

**The three guarantees (AGENTS.md rule 2).** (1) One human approval: unchanged for parks; an instant `Allow` is a
pipeline decision consumed once before the run. (2) Never twice: `Allow` returns `None` once for one tool call id;
the replay path is unchanged. (3) Unknown never retried: unchanged (executor and sweep). Tests: existing suite plus
`allowed`/`refused` transitions, exempt list, redaction and truncation, the moved "already waiting" check with a
cancelled request, breaker, and `can_pause=True` on every call.

Docs touched by the code: architecture.md (§1 table and diagram, §3 states, §5, §8 numbers), form-check.md
(`form_checked_tools`), runbook (new chat messages, retention, breaker alert); `.github/docs-map.json` gets a rule
for any new module (for example the breaker).

### 4.3 withHuman pipeline — starting policy (a proposal Yaakov edits)

New entry-pipeline revision. Blocks for agent `proto-agents` go **before** the `hermes` Stripe block and the final
`approve-every-call` (so production is untouched). Conditions use `/agent/slug`, `/request/server`,
`/request/tool`, `/request/arguments/...` (AAP research §4). Numeric tests only on argument fields; a string where a
number is expected makes the block error and sends the request to a human, which is the safe side.

| # | When (agent `proto-agents` and ...) | Then | Why |
|---|---|---|---|
| 1 | server `park:quickbooks` and tool matches the 12 money types (`mcp__wh__quickbooks__(create|update|delete)[_-](payment|...|bill)`) | human (`notify-yaakov-yehuda`) | today's rule 1, narrowed |
| 2 | server `quickbooks` and tool matches the money regex | webhook `durable-park-bridge` (approve or human) | unchanged |
| 3 | server starts with `park:` (any other gateway tool) | approve | reads and non-money writes; the gateway evaluates the real call again |
| 4 | server `hermes` and tool in the read-only set: `web_search`, `web_extract`, `vision_analyze`, `memory_*`, `session_search`, `todo*`, `kanban_*` reads, `skills_list`/`skill_view`, `image_gen*` | approve | no side effect outside the chat |
| 5 | server `hermes` and tool in `send_message`-type tools (Telegram/Teams/Discord send, reactions) | approve on the test VM | outbound chat only; revisit before the content agent |
| 6 | server `hermes` and tool starts with `browser_` and arguments have no `password`/credential fields | approve; `browser_*` with a form submit or login → human | reading the web is low risk; logging in is not |
| 7 | server `hermes` and tool in `terminal`, `execute_code`, `process`, `write_file`, `patch`, `read_file`, `search_files`, `delegate_task`, `cronjob`, `skill_manage` | **deny** until Phase 2 (§4.5). After Phase 2: `terminal`/`execute_code` with `/request/arguments/command` matching `sudo|rm -rf|curl.*\| *(ba)?sh|/etc/durable-park|durable-park|\.hermes/` → deny; `write_file`/`patch` with `/request/arguments/path` under `/etc/`, `/opt/`, `~/.hermes/` → deny; everything else in this row → human | the local-machine tools; a regex is a guard, not a boundary (AAP research §3.4) |
| 8 | server starts with `mcp:` | human | new MCP servers get a rule when they are added |
| 9 | anything else from `proto-agents` | **human** (A3, confirmed by Q1 on 2026-10-21) | an unlisted tool asks a person |
| 10 | existing `hermes` Stripe block, then `approve-every-call` | unchanged | production agent |

Tool names in rows 4–7 are checked against the live registry (`hermes tools list` on the VM) before a condition is
written; a name that does not exist simply never matches, so a typo falls through to row 9 (human), never to approve.

Argument thresholds: decided 2026-10-21 (Q2): keep all 12 money types on "human" for now, with no amount threshold, so no `TotalAmt` rule is written. Each revision is checked with
`preview_pipeline` on a sample request for every row before activation; the previous revision stays available for
rollback (`activate_pipeline_revision`).

### 4.4 Opening tools one by one

Per toolset:

1. Write or confirm its pipeline rule (§4.3); `preview_pipeline` with one approve case, one deny case and, where a
   human rule exists, one human case.
2. Enable it in Hermes with `hermes_tool_policy.py apply --allow <toolset>...` (new flag; `check` then verifies that
   exactly the allowed set is reachable on all 58 keys, which also covers backlog #6 when run at every start).
   Restart the gateway only when no row is `parked|approved|executing`.
3. Test from Telegram: one call the pipeline approves (runs; audit shows `approval.created/decided/consumed`), one it
   denies (blocked; the refusal text in chat; audit shows `denied`), and for a human rule one park (parked → approve
   in withHuman → wake → `parked_action_execute` → ran once).
4. Record date, toolset, pipeline revision, request ids and results in `docs/tool-rollout.md` (new doc).

Order: (a) web (`web_search`, `web_extract`), vision, memory, todo/kanban, session_search; (b) messaging send tools;
(c) browser without logins; (d) other MCP servers as they are added; **after §4.5 only**: (e) file (`read_file`,
`search_files`, `write_file`, `patch`), (f) terminal, process, code_execution, (g) delegation, cronjob,
skill_manage. `read_file` is in the last group on purpose: it can read `instance_token` (`root:hermes 640`) and
`bridge_secret` (`dpwake`) today.

### 4.5 Clean Setup Phase 2 — the ledger gets its own owner

The design is `receiver-setup.md` "v2: separate ledger owner"; this section lists only what changes for this spec.

- New system user `dpledger`; ledger and bridge delivery DB in `/var/lib/durable-park` (0700); the ledger service on
  `/run/durable-park/ledger.sock` with `SO_PEERCRED` checks; `instance_token` readable only by `dpledger`.
- **Plugin:** `Ledger` is replaced by a `LedgerClient` with the same method surface; the hook's AAP open/consume for
  every call (§4.2 step 4) is done **by the service**, which holds the token. The plugin never reads `token_file`.
- **Bridge:** runs as its own user (`dpbridge`), reads through the socket; `bridge_secret` becomes `root:dpbridge
  640`. `hermes` leaves nothing but `dpwake` (it still needs `wake_secret` for the Hermes webhook route's HMAC; a
  wake carries only a park id, so this is harmless).
- **Sweep:** moves from Hermes cron (uid `hermes`) to a system timer as `dpledger`, so "only the sweep may use
  stale-executing → unknown" is a uid check; its alerts go out through a narrow path (the existing Telegram script,
  later Teams).
- Migration when no row is `parked|approved|executing`: checkpoint, copy, re-own, change `ledger_path` and the bridge
  unit, restart in order receiver, service, bridge, gateway.
- Gateway access (Q5, decided 2026-10-21): the gateway's `wh` MCP bearer is the same instance token, so `hermes` can call
  the gateway directly. Accepted: that token cannot approve, so the worst case is denial of service (§7, Q5). When new gateway servers are added, per-agent grants (`set_agent_tool_access`) should list only the servers the agent needs.

## 5. Verification and acceptance (evidence for each; no claim without it)

| Part | # | Claim | Evidence |
|---|---|---|---|
| 4.1 | 1 | Connector runs as ubuntu | `systemctl show hermes-shell-mcp -p User` = `ubuntu`; `id` through Claude = uid 1000 |
| 4.1 | 2 | hermes has no sudo | `sudo -u hermes sudo -n true` → "a password is required"; `/etc/sudoers.d/90-hermes` absent |
| 4.1 | 3 | Nothing the agent runs broke | gateway, dashboard, bridge, receiver, devtunnel active; a Telegram and a Teams message answered; the sweep ran within 6 min |
| 4.1 | 4 | SSH passwords off | `sudo sshd -T | grep -i passwordauthentication` = `no`; key login as ubuntu still works |
| 4.1 | 5 | Admin can still deploy | `sudo -u hermes git -C ~hermes/tapdash-agents pull --ff-only` and a user-unit restart via `XDG_RUNTIME_DIR` both succeed |
| 4.2 | 6 | Instant approve | a `web_search` call runs; withHuman audit shows created → approved by pipeline → consumed, one request; ledger row `allowed` with that request id |
| 4.2 | 7 | Instant deny | a denied tool is blocked; chat shows the refusal text; row `refused`; nothing ran |
| 4.2 | 8 | Park unchanged | `create_payment` (sandbox) parks, survives `systemctl --user restart hermes-gateway` while parked, approve → wake → ran once; one human ask in withHuman; bridge passed once |
| 4.2 | 9 | Fail closed | with `aap_base_url` pointed at an unreachable host: every non-exempt call blocked, `clarify` still works, breaker alert fired once |
| 4.2 | 10 | Latency | 10 auto-approved calls: median hook time ≤ 0.3 s (measured from `post_tool_call` hook timing or withHuman timestamps) |
| 4.2 | 11 | Tests | `.venv/bin/pytest -q -W error` green; a crash-injection run (`DURABLE_PARK_CRASH_AFTER_CONSUME`) leaves `unknown`, never a retry |
| 4.3 | 12 | Policy as written | `preview_pipeline` results for one sample per row match the table; revision number recorded |
| 4.4 | 13 | Per toolset | the three tests of §4.4 step 3 recorded in `docs/tool-rollout.md` |
| 4.5 | 14 | Boundary | as `hermes`: `sqlite3 /var/lib/durable-park/ledger.db` fails (permission denied); `cat /etc/durable-park/instance_token` fails; a forged `executing` row is impossible without the service |
| 4.5 | 15 | Service rules | each disallowed transition from the wrong uid is refused in a live test; the existing ledger tests pass against `LedgerClient` |

## 6. Failure modes and rollback

| Part | Failure | What happens | Rollback |
|---|---|---|---|
| 4.1 | Connector does not start as ubuntu | Claude loses the VM | Over `tailscale ssh ubuntu@proto-agents`: restore the old unit file, `daemon-reload`, restart. Do step 6 only after step 5 passed. |
| 4.1 | sshd config error | `sshd -t` fails before reload; nothing changes | revert the one line; keep the open session until `sshd -T` is right |
| 4.1 | Something needed hermes's sudo after all | a root step fails in a log | recreate `90-hermes` temporarily (`sudo install -m 440`), fix the step to run as ubuntu, delete it again |
| 4.2 | withHuman down or slow | every non-exempt call blocked; chat told; breaker limits the stall to ~30 s per window | none needed; service returns |
| 4.2 | Bug in the new hook | fails closed: block (the `except` path) | `git checkout` the previous plugin commit in the live clone, restart the gateway with no active rows |
| 4.2 | Ledger grows unexpectedly | disk | retention setting; `VACUUM` in the runbook |
| 4.3 | A rule sends everything to a human | agent stalls on parks | `activate_pipeline_revision` to the previous revision (seconds) |
| 4.3 | A rule approves too much | a call ran that should have asked | the audit entry shows which block decided; fix the rule; before real books the deny list is reviewed |
| 4.4 | A toolset misbehaves | `hermes_tool_policy.py apply` without it; restart with no active rows | same script |
| 4.5 | Service down | every call blocked; wakes dropped until the sweep re-posts | restart the service; nothing lost (rows on disk) |
| 4.5 | Migration half done | gateway cannot open the ledger → fails closed | restore the copied DB and `ledger_path`; re-run when no row is active |

## 7. Risks accepted and open questions

Accepted:

- Until Phase 2, guarantee 1 rests on the Hermes tool policy plus the pipeline's deny of local tools; it is not a
  permission boundary. Same as today; accepted by Yaakov for the sandbox.
- A withHuman outage freezes the agent except `clarify`. By design.
- Every tool call is sent to withHuman with its arguments (redacted keys, size cap). The arguments can contain user
  content from chat. withHuman is already the audit record; accepted.
- Around 0.15–0.3 s per tool call.

Open questions (who decides):

- Q1 (Yaakov): confirm the exempt list `parked_action_execute`, `clarify` (A2), and the final default "human" (A3). **Decided 2026-10-21: yes.** Exempt: `parked_action_execute` and `clarify`; the final default for `proto-agents` is "ask a human", not approve.
- Q2 (Yaakov): any amount threshold for sandbox money tools, and which field (`TotalAmt`) it tests. **Decided 2026-10-21:** keep all 12 QuickBooks money types on "human" for now; no amount threshold.
- Q3 (Yaakov): keep rows for every call in the ledger (§4.2) with 30-day retention, or parks only. **Decided 2026-10-21: yes.** A ledger row for every call (allowed or refused), 30-day retention; the rows feed the dashboard trace.
- Q4 (Yaakov, Tailscale admin console): restrict Tailscale SSH to `ubuntu` so no interactive login as `hermes` exists. **Decided 2026-10-21: yes.** Yaakov restricts Tailscale SSH to `ubuntu` in the admin console.
- Q5 (withHuman): **Decided 2026-10-21: no separate gateway-only credential and no relay.** The AAP instance credential can only create a request, read it, cancel it, consume an approval a human already gave, and report an outcome (`agentapprovalprotocol/_client.py` lines 155-170: POST /v1/requests, GET /v1/requests/{id}, DELETE /v1/requests/{id}, POST /v1/requests/{id}/consume, POST /v1/requests/{id}/outcome). There is no endpoint to approve; only a person in withHuman or a pipeline rule decides. withHuman limits the agent's gateway access per agent: `proto-agents` is granted only the `quickbooks` gateway server (mode all, scope default; `list_agent_tool_access` 2026-10-21), and every gateway call is decided by the entry pipeline; risky QuickBooks calls also go through the durable-park bridge, which checks the ledger. So a compromised agent holding the credential can at worst create or cancel requests or spoil a pending approval (denial of service), never run a risky call without a human. The asset to protect is the ledger, which Phase 2 (§4.5) moves to its own user.
- Q6 (Yaakov): whether the content agent (second agent) shares this pipeline with its own `/agent/slug` blocks or
  gets a delegated pipeline.
  **Decided 2026-10-21: defer.** Decide when the second agent is built.

## 8. Work list (ordered; each line: what — why — depends on)

1. Fix PR #3 docs: say the v1 lockdown was re-applied 2026-10-21 03:16 UTC and the full toolset is **not** live — the
   PR describes a state that no longer exists — depends on nothing.
2. Merge PR #3; fast-forward the live clone (`sudo -u hermes git -C ~hermes/tapdash-agents pull --ff-only` after
   4.1, or as hermes before) — the `configure_hermes.sh` warning comment and merged docs must be on the path admins
   run — depends on 1.
3. Clean Setup Phase 1 (§4.1 steps 1–9) — removes root from the agent's user before any tool is opened — depends on 2.
4. Backlog #3: sweep cancels a `parked` row with no request id after 10 min — "ask every call" opens that window on
   every tool call — depends on nothing; land before 5.
5. durable-park "ask every call" (§4.2) with tests, docs, independent review (AGENTS.md rule 2) — the core of this
   spec — depends on 3, 4.
6. withHuman entry pipeline revision (§4.3), `preview_pipeline` per row, record the revision number — the rules the
   hook relies on; rule 1 must be narrowed before 5 goes live or every QuickBooks read parks — depends on Q1–Q2 (decided 2026-10-21);
   activate together with 5.
7. `hermes_tool_policy.py --allow` and run `check` at every gateway start (backlog #6) — one place that says which
   toolsets are on — depends on 5.
8. Open tools one by one, groups (a)–(d) of §4.4, `docs/tool-rollout.md` — the goal state, safely — depends on 5–7.
9. Test `tools.tool_search: on` with a park (wake → `parked_action_execute` still found) — cuts the per-turn tool
   description cost; today off on purpose — depends on 8 (enough tools for it to matter).
10. Clean Setup Phase 2 (§4.5): v2 ledger service, `dpledger`, bridge user, sweep timer, migration — the real
    boundary — depends on 5.
11. Open groups (e)–(g) of §4.4 (file, terminal, code, delegation, cron) with the Phase-2 argument rules — last, by
    decision — depends on 10.
12. Dashboard pages: pending withHuman approvals; full withHuman view; live trace of tool calls (source: the ledger's
    rows for every call plus withHuman's audit API) — the original ask — depends on 5 (rows) and Q3 (decided 2026-10-21: a row for every call).
13. Backlog #1 rotate every secret — before real books — depends on 10 (rotate once, into the new owners).
14. Backlog #2 retry a dropped decision message; #4 decide which other QuickBooks writes need approval (now a
    pipeline rule, §4.3 row 3); #5 tie the bridge's stored answers to the request id — before real books — depend
    on 5 (for #4) and nothing (for #2, #5).
15. Sweep alerts to Teams instead of Telegram — Teams is where Yaakov works — depends on nothing.
16. Decide the archived `ubuntu` Hermes install (`/home/ubuntu/.hermes`): delete after 3 and 8 pass, or keep as a
    snapshot — housekeeping; it holds Teams secrets — Yaakov decides; depends on 3.
