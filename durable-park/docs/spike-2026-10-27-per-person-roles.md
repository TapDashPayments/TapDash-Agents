# Spike record: per-person roles (Task 1), 2026-10-27

**Result: STOPPED EARLY on purpose, then fully cleaned up.** Test VM `proto-agents`, QuickBooks sandbox only.
Linear [TAP2-27](https://linear.app/tapdash/issue/TAP2-27). Plan: [docs/plans/2026-10-26-per-person-roles.md](../../docs/plans/2026-10-26-per-person-roles.md), Task 1.
Spec: [docs/specs/2026-10-26-per-person-roles-design.md](../../docs/specs/2026-10-26-per-person-roles-design.md), section 8.

Times are US Eastern (ET) on the evening of 2026-10-26, with UTC (2026-10-27) in brackets.

## In plain words

We built a pretend "second person" for the Ops bot and sent Yaakov's own Teams messages to it for about 35 minutes.
His lookup was caught and sent to withHuman for approval, with the right test key on it. He approved it.
But the bot never got the answer back: the part that picks up approvals still uses the main key, and withHuman
said "no, that is not yours" (403). That is exactly the gap plan Task 4 is meant to fix. Yaakov chose to stop
there (option "B"), so the write test was not done. Everything was then removed. Nothing ran in QuickBooks.

So the test answered some of the plan's questions, not all. Two answers are still missing, and one of them is the
plan's own example of a possible blocker. The design is **not cleared yet** (see "Does any answer block the design?").

## What was set up (all removed again, see "Clean up")

| What | Detail |
|---|---|
| withHuman agent | `spike-a`, kind `service`, created 10:06 PM (02:06Z) |
| Two instances | `spike-a-1` (`e0f4f80a-...`) and `spike-a-2` (`c98a57c9-...`). The key of `spike-a-1` went straight into `/etc/durable-park/instances/spike.token` (root:hermes 640, never shown) |
| Temporary grant | `spike-a` -> QuickBooks sandbox, mode `selected`, tools `get_customer` and `create_customer` |
| Hermes profile | `spike`, made blank with `hermes profile create spike --no-alias --no-skills` (not a clone). Its `config.yaml` got `model.*`, `mcp_servers.wh`, the durable-park plugin block with `token_file: /etc/durable-park/instances/spike.token` and the shared ledger, all platforms `enabled: false`, and `secrets.command` (enabled, `override_existing: true`) pointing at a helper. `hermes_tool_policy.py apply` + `check` were run for it (check OK) |
| Temporary helper | `/opt/durable-park/libexec/spike-secrets` (root-owned): printed `DURABLE_PARK_WH_TOKEN=` from the spike key file, nothing else |
| Route | one `gateway.profile_routes` entry: `{name: spike, platform: teams, user_id: <Yaakov's Teams ID>, guild_id: <tenant ID>, profile: spike}`, added 10:08 PM (02:08Z) |
| Restart | gateway restarted 10:09 PM (02:09Z) with 0 active ledger rows; port check printed 1 |

Proof the spike profile used its own key: `tools/list` on the withHuman gateway with the spike key showed only
`quickbooks__create_customer` and `quickbooks__get_customer`, and the spike profile's log said
`MCP server 'wh' (HTTP): registered 2 tool(s)` (the default profile registers 311).

## What happened

| Time (ET) | What |
|---|---|
| 10:09:08 PM | Spike profile log: `durable-park loaded` (about 4 seconds after the restart, before any message) |
| 10:12:59 PM | Yaakov's "Look up QuickBooks customer 1" reached the **spike** profile (session `agent:spike:teams:dm:...`) |
| 10:13:12 PM | durable-park parked it: `P-2F1A8021`, ledger `profile=spike`. withHuman request `611043a2-...` shows agent `spike-a`, instance `spike-a-1` (`e0f4f80a-...`) |
| about 10:15 PM | Yaakov approved it in the withHuman phone app. withHuman: request status `approved` |
| 10:15:25 PM | The wake (it runs in the default profile) tried to read the decision and failed: `durable-park wake failed (CredentialRefused); the sweep will retry`. It failed the same way every 5 minutes after that |
| 10:15:29 PM | His "Create a QuickBooks sandbox customer ..." was refused on purpose by durable-park's one-at-a-time rule: `P-2F1A8021 is already waiting for approval in this chat; this call was NOT run`. Ledger `P-F4996BE6` = `cancelled`; its withHuman request `098c70f5-...` = `cancelled`. Nothing ran |
| 10:17:08 PM | Same approval read by hand: with the **spike** key -> `200`, status `approved`. With the **main proto-agents** key -> `403 Forbidden` |
| 10:22 PM | Yaakov chose **B**: cancel the stuck row by hand, clean up, test the rest in Task 4 |

Why the lookup got stuck: the row stays `parked` because the wake can never read its decision, and a `parked`
row blocks new calls in that chat and blocks restarts (ledger rule). So it needed one manual ledger write.

## Clean up (10:42 to 10:46 PM ET, 02:42 to 02:46Z)

| Step | Done | Check |
|---|---|---|
| Cancel `P-2F1A8021` | With durable-park's own `Ledger.transition("parked" -> "cancelled")` (the same call the sweep and park hook use), with a note saying why. `notified_at` was set in the same write, so the sweep does not try to announce it into the deleted profile's chat (Yaakov was told on WhatsApp) | 0 rows `parked`, `approved` or `executing` |
| Remove the route | `config.yaml` put back byte for byte from the copy taken just before the route was added | no `gateway.profile_routes`; the route matcher returns no route for Yaakov's Teams ID, so he goes to the default profile. Not checked with a real Teams message (we chose not to ask him to send one), so his next message is the last check of plan step 5's "the bot answers Yaakov as before" |
| Restart | 0 active rows, then restart at 10:43 PM (02:43Z) | `sudo ss -ltnp \| grep -c ':3978 '` = 1; Teams connected; no errors in `gateway.log` since |
| Delete the profile | `hermes profile delete -y spike` | gateway log: `[MULTIPLEX] Profile 'spike' unserved — 0 adapter(s) stopped and unrouted`; `hermes profile list` shows only `default`; the main gateway kept running (same PID, 0 restarts) |
| Delete files | key file (shredded), `/etc/durable-park/instances/`, the helper, `/root/.spike-ids.json`, `/home/hermes/.spike/` (held the tenant ID and the policy report), the config copy | all gone |
| withHuman | grant set to mode `none` with no tools (revision 2), both instances `disabled`, `spike-a` archived | `spike-a` status `archived`, 0 live instances |
| Pin | `deploy/hermes_pin.sh check` as `hermes` in a login shell | all OK (v0.22.3, update block on, passive check off) |

Left on purpose (not live): withHuman keeps archived agents and old requests (`611043a2` approved but never
run, `098c70f5` cancelled) in its history; the two cancelled ledger rows stay until the sweep's 30-day cleanup;
Hermes left its own 8-byte marker `~/.hermes/profiles/.deleted/spike`.

## Decisions for later tasks

| Key | Answer |
|---|---|
| `OPTION` | **Not decided yet: P by the plan's rule until the gateway half is checked.** The plan sets `R` only if *both* withHuman records carry the right instance, else `P`. The park request did; the gateway request was never reached, so spec 8 item 1 (the question that decides R or P) is still open. R is still the likely answer. If Task 3 is built for R before this is checked, it stays safe (a gateway record with no instance goes to a person and never runs: `test_gateway_record_missing_from_listing_goes_to_a_person`), but it may have to be redone. Yaakov decides: re-run first (recommended, see "How to get the missing answers") or build R and accept the rework risk |
| `INSTANCE_ON_BOTH_RECORDS` | **Park request: yes** (both spike requests, `611043a2` and `098c70f5`, are park requests and show `agent_instance_id` = `spike-a-1`). **Gateway request: not tested** (no call got past the park). No task before the live rollout (Task 8) tests it; Task 4 has no live step |
| `PLUGIN_LOADS_AT_BOOT` | **yes** for the routed profile: its own log shows `durable-park loaded` 4 seconds after the restart, minutes before the first message. (There is no separate per-profile `plugin_load.json` yet; Task 4 adds `status_dir`) |
| `SESSION_PROFILE` | **yes**: ledger rows have `profile=spike`; session key `agent:spike:teams:dm:...` |
| `INJECT_CROSS_PROFILE` | **not tested** (the wake failed before it could resume the call). Task 4 *uses* this answer to build the wake and has no live step, so it cannot find it out itself. The plan's own example of a blocker is `INJECT_CROSS_PROFILE = no` |
| `DEFAULT_KEY_READS_OTHER_INSTANCE` | **no**: the main proto-agents key got `403 Forbidden` on the spike request (logged `CredentialRefused` at 10:15:25 PM ET); the spike key read the same request with `200`, `approved` |
| `LLM_KEY_SOURCE` | The profile had **no** `auth.json` and an empty `.env`. Hermes used the **global** `~/.hermes/auth.json` (Nous login) as a read-only fallback (`hermes_cli/auth.py`: "profile first, then the global root"). The model answered (provider `nous`). The profile's secret set held only `DURABLE_PARK_WH_TOKEN`: **no** `TEAMS_*`, `TELEGRAM_*` or `BWS_*` keys |
| `TEAMS_USER_ID_SHAPE` | Entra object ID: a 36-character lowercase GUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`), same as `TEAMS_ALLOWED_USERS`. Route `guild_id` = tenant ID, also a 36-character GUID. The route with `user_id` + `guild_id` matched. (The chat ID is a different thing: `a:` plus a long text) |
| `SLUG_SEPARATOR` | not tested (only needed for option P) |

## Does any answer block the design?

**No answer we got blocks it, but the design is not cleared yet.** The plan says to stop if an answer blocks the
design. Here is where each answer stands:

- `DEFAULT_KEY_READS_OTHER_INSTANCE = no` does **not** block. It is the gap Task 4 was written for: Task 4 makes the
  wake read each decision with the key of the row's own profile (`token_file_for(row.profile)`). It also shows
  withHuman keeps instances apart, which the design wants.
- Two answers are **missing**. Missing is not the same as fine:
  - `INJECT_CROSS_PROFILE` is the plan's own example of a blocker. If it is "no", the default profile's wake cannot
    resume a routed person's call, and Task 4's wake design has to change.
  - The gateway request's instance (`INSTANCE_ON_BOTH_RECORDS`) decides `OPTION`, and so how Task 3 is built.

So Tasks 3 and 4 should not start on a guess about these two. Yaakov decides whether to re-run first.

## How to get the missing answers

One short re-run of Task 1 steps 2 to 5: the same throwaway setup and the same full cleanup, about 15 minutes, with
Yaakov's OK and a quiet window. One change: after Yaakov approves the lookup in withHuman, the stuck ledger row is
marked `approved` by hand, after reading his approval with the spike key (one manual ledger write; this is the
"option A" offered on Oct 26). The default profile's wake can then resume the call. That shows:

- whether the call resumes in the routed profile and its answer reaches his Teams chat (`INJECT_CROSS_PROFILE`;
  spec 8 items 3 and 5);
- whether the resumed call's gateway request in withHuman shows the same spike instance (`INSTANCE_ON_BOTH_RECORDS`,
  and then `OPTION`).

## Gaps found (for Task 4 and the runbook)

1. A key that withHuman **refuses** (403) leaves a row `parked` forever: the sweep re-wakes every 5 minutes, logs
   a warning, and nobody is alerted; the chat and restarts stay blocked. Task 4's check only covered a
   **missing** key file, so this PR adds a second check to plan Task 4: alert (and say what to do) when the wake
   keeps getting `CredentialRefused`, with a test.
2. `deploy/hermes_pin.sh check` run over plain `ssh` (not a login shell) falsely says
   `FAIL passive update check is on`, because `hermes` is not on the PATH there. Run it as `hermes` in a login
   shell from the durable-park folder: `sudo -u hermes -H bash -lc 'cd /home/hermes/durable-park && deploy/hermes_pin.sh check'`.
   (The runbook now says so.) The script itself should say "could not run hermes" instead (follow-up).
3. The park hook's one-at-a-time rule opens a withHuman request for the second call and then cancels it, but the
   cancelled ledger row does not save that request's ID (`098c70f5` is only findable in withHuman). Not harmful (nothing runs), just good to know when reading records.
