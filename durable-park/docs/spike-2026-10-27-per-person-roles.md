# Spike record: per-person roles (Task 1), 2026-10-27

**Result: first run stopped early on purpose; one re-run (11:12 to 11:25 PM ET) got the two missing answers. Both fully cleaned up.** Test VM `proto-agents`, QuickBooks sandbox only.

**Verdict after the re-run: `OPTION = R`, but `INJECT_CROSS_PROFILE = no`, which is the plan's own example of a blocker. By the plan's STOP rule, stop before Tasks 3 and 4 until the wake design is changed. See "Re-run" and "Does any answer block the design?".**

**Task 1b (Oct 27, 9:00 to 9:17 AM ET): `WAKE_ROUTE_PER_PROFILE = yes`. Sending each wake to the person's own profile fixes that blocker. See "Task 1b: per-profile wake" at the end.**

Linear [TAP2-27](https://linear.app/tapdash/issue/TAP2-27). Plan: [docs/plans/2026-10-26-per-person-roles.md](../../docs/plans/2026-10-26-per-person-roles.md), Task 1.
Spec: [docs/specs/2026-10-26-per-person-roles-design.md](../../docs/specs/2026-10-26-per-person-roles-design.md), section 8.

Times are US Eastern (ET) on the evening of 2026-10-26, with UTC (2026-10-27) in brackets.

## In plain words

We built a pretend "second person" for the Ops bot and sent Yaakov's own Teams messages to it for about 35 minutes.
His lookup was caught and sent to withHuman for approval, with the right test key on it. He approved it.
But the bot never got the answer back: the part that picks up approvals still uses the main key, and withHuman
said "no, that is not yours" (403). That is exactly the gap plan Task 4 is meant to fix. Yaakov chose to stop
there (option "B"), so the write test was not done. Everything was then removed. Nothing ran in QuickBooks.

So the first run answered some of the plan's questions, not all. Yaakov approved one re-run ("A", 11:08 PM) to get
the two missing answers. It got them:

1. The withHuman request made when the tool really runs (the "gateway" request) **does** carry the test instance.
   So each person's own key shows up on both records. That means **option R** (one withHuman agent per role).
2. The main profile's wake **cannot** post a result back into another profile's chat. Hermes refuses it on purpose:
   `session ... belongs to profile spike, not the plugin's profile default`. This is the plan's own example of a
   blocker, so we **stop** here until the wake design is changed (Yaakov decides how).

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

## Re-run (11:12 to 11:25 PM ET, 03:12 to 03:25Z)

Yaakov said "A" at 11:08 PM: re-run once, with one manual ledger write (mark his approved lookup `approved` by
hand, after reading his approval with the test key).

Setup was the same as the first run, with one change: `spike-a` was archived and withHuman will not bring an
archived agent back (`PATCH /agents/spike-a {status: active}` -> `409 agent_archived: Agent is archived.`), so a
fresh agent **`spike-b`** was made, with instances `spike-b-1` (`2c1d5a90-...`, its key in the spike key file) and
`spike-b-2` (`ff028d99-...`), and the same two-tool grant. Same blank profile, same config, same helper, same route,
`hermes_tool_policy.py apply` + `check` OK. Restart at 11:12 PM with 0 active rows; port check printed 1. Spike
profile log: `durable-park loaded` and `registered 2 tool(s)` before any message.

| Time (ET) | What |
|---|---|
| 11:14:06 PM | His "Look up QuickBooks customer 1" reached the spike profile (session `agent:spike:teams:dm:...`) |
| 11:14:16 PM | Parked: `P-9360E171`, `profile=spike`. withHuman request `d25be236-...`: agent `spike-b`, instance `spike-b-1` (`2c1d5a90-...`) |
| 11:15:15 PM | He approved it in the withHuman app. Read with the **spike** key: `approved` (decision expires 11:20:15) |
| 11:15:38 PM | The one manual ledger write: `Ledger.transition("parked" -> "approved")` on `P-9360E171` (durable-park's own call), `decided_by` noting it was marked by hand |
| 11:15:44 PM | The default profile's wake read the row (now `approved`) and tried to post into his chat. **Hermes refused:** `Plugin message injection refused: plugin=durable-park session=agent:spike:teams:dm:... belongs to profile spike, not the plugin's profile default`. The plugin logged `gateway did not accept the message for P-9360E171; left unnotified` |
| 11:17:16 PM | To still reach the gateway step, he sent the bot "It's approved. Run parked action P-9360E171 now." The spike profile ran `parked_action_execute` in its own session |
| 11:17:21 PM | The approval was consumed (`POST .../requests/d25be236-.../consume` -> 200) and the gateway call started. withHuman made the **gateway request `f5698ed7-...`** (tool `get_customer`): agent `spike-b`, instance **`spike-b-1` (`2c1d5a90-...`)**, the same instance as the park request. It asked for a second approval with a 5-minute deadline |
| 11:22:21 PM | Nobody tapped the second approval in time. withHuman marked `f5698ed7` `expired` (by `system`). Hermes's MCP call hit its own 300 s timeout at the same second, so the row ended **`unknown`** with note `MCP call timed out after 300.0s`. Per the rule, the call was **not retried**. Nothing ran in QuickBooks (the gateway request expired before any approval) |
| 11:22:24 PM | The bot then asked again on its own: a new park `P-D57A9783` (its withHuman request `92ea5433-...` was left to expire). It was cancelled by hand as part of cleanup (`parked -> cancelled`, note: nothing ran) |

Clean up (11:23 to 11:25 PM ET): config put back byte for byte from the copy taken before the route; 0 active rows;
restart; port check 1; Teams connected; `hermes profile delete -y spike` (gateway log: `Profile 'spike' unserved`,
same PID); key file shredded, `/etc/durable-park/instances/`, helper, `/root/.spike-ids.json`, `/home/hermes/.spike/`
and the config copy deleted; grant set to mode `none` (revision 2), both `spike-b` instances `disabled`, `spike-b`
archived (0 live instances); `hermes_pin.sh check` as `hermes` in a login shell: all OK (v0.22.3); no `parked`,
`approved` or `executing` rows. Not checked with a real Teams message after cleanup. Ledger rows `P-9360E171`
(`unknown`) and `P-D57A9783` (`cancelled`) stay until the 30-day cleanup.

## Decisions for later tasks

| Key | Answer |
|---|---|
| `OPTION` | **R** (one withHuman agent per role). Spec 8 item 1, the question that decides R or P, is answered yes by the re-run: the gateway request `f5698ed7` carried the same instance (`spike-b-1`) as the park request `d25be236`. `INJECT_CROSS_PROFILE = no` does not change R vs P: Hermes refuses the inject because of the *profile* boundary, which is the same under P. But see the STOP verdict below before building Task 3 |
| `INSTANCE_ON_BOTH_RECORDS` | **yes**. Park requests: `611043a2` and `098c70f5` (first run, `spike-a-1`), `d25be236` (re-run, `spike-b-1`). Gateway request: `f5698ed7` (re-run, tool `get_customer`) shows agent `spike-b`, `agent_instance_id` = `spike-b-1` (`2c1d5a90-...`), from withHuman's request listing |
| `PLUGIN_LOADS_AT_BOOT` | **yes** for the routed profile: its own log shows `durable-park loaded` 4 seconds after the restart, minutes before the first message. (There is no separate per-profile `plugin_load.json` yet; Task 4 adds `status_dir`) |
| `SESSION_PROFILE` | **yes**: ledger rows have `profile=spike`; session key `agent:spike:teams:dm:...` |
| `INJECT_CROSS_PROFILE` | **no**. With the row `approved`, the default profile's wake tried to post into the spike session and Hermes v0.22.3 refused: `belongs to profile spike, not the plugin's profile default` (`gateway/run_plugin_injection.py`, the profile check for plugin injection under multiplexing). The result did not reach his chat. It only went on because he typed a new message asking the bot to run it |
| `DEFAULT_KEY_READS_OTHER_INSTANCE` | **no**: the main proto-agents key got `403 Forbidden` on the spike request (logged `CredentialRefused` at 10:15:25 PM ET); the spike key read the same request with `200`, `approved` |
| `LLM_KEY_SOURCE` | The profile had **no** `auth.json` and an empty `.env`. Hermes used the **global** `~/.hermes/auth.json` (Nous login) as a read-only fallback (`hermes_cli/auth.py`: "profile first, then the global root"). The model answered (provider `nous`). The profile's secret set held only `DURABLE_PARK_WH_TOKEN`: **no** `TEAMS_*`, `TELEGRAM_*` or `BWS_*` keys |
| `TEAMS_USER_ID_SHAPE` | Entra object ID: a 36-character lowercase GUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`), same as `TEAMS_ALLOWED_USERS`. Route `guild_id` = tenant ID, also a 36-character GUID. The route with `user_id` + `guild_id` matched. (The chat ID is a different thing: `a:` plus a long text) |
| `SLUG_SEPARATOR` | not tested (only needed for option P) |
| `WAKE_ROUTE_PER_PROFILE` | **yes** (Task 1b). A wake posted to `/p/spike/webhooks/durable-park-wake-spike` (route copied from `durable-park-wake` plus `profile: spike`) returned `202`, reached the spike profile's copy, which read the decision with the spike key (no `CredentialRefused`) and posted the result in Yaakov's Teams chat with no typing from him. Park `P-B80D1348`; withHuman requests `593a5e20` (park) and `51686024` (gateway). Needs the `is_wake_event` change (Task 4) |

## Does any answer block the design?

**Update after Task 1b: the blocker below is cleared. A per-person wake route works (`WAKE_ROUTE_PER_PROFILE = yes`), see "Task 1b: per-profile wake".**

**Yes. After the re-run, `INJECT_CROSS_PROFILE = no`, and the plan names exactly that as a blocker. STOP before
Tasks 3 and 4 until the wake design is changed.** `OPTION = R` is settled. The text below the line is the first
run's verdict, kept for the record.

What the blocker means: under multiplexing, a plugin may only post into sessions of its own profile. The default
profile's wake (the one webhook route and the one sweep) can therefore never tell a routed person's session that
an approval came in, whatever key it reads with. Task 4 planned to fix only the key (`token_file_for(row.profile)`);
that is still needed (`DEFAULT_KEY_READS_OTHER_INSTANCE = no`) but is not enough. Some ways out, for Yaakov to pick
(none tested): run the wake inside each routed profile (its own webhook route or sweep, so the plugin that posts is
that profile's own); or have the wake ask the routed profile's own plugin instance to post; or, as a stopgap, tell
the person to send any message so their own session runs `parked_action_execute` (what the re-run did by hand).

---

First run's verdict: **No answer we got blocks it, but the design is not cleared yet.** The plan says to stop if an answer blocks the
design. Here is where each answer stands:

- `DEFAULT_KEY_READS_OTHER_INSTANCE = no` does **not** block. It is the gap Task 4 was written for: Task 4 makes the
  wake read each decision with the key of the row's own profile (`token_file_for(row.profile)`). It also shows
  withHuman keeps instances apart, which the design wants.
- Two answers are **missing**. Missing is not the same as fine:
  - `INJECT_CROSS_PROFILE` is the plan's own example of a blocker. If it is "no", the default profile's wake cannot
    resume a routed person's call, and Task 4's wake design has to change.
  - The gateway request's instance (`INSTANCE_ON_BOTH_RECORDS`) decides `OPTION`, and so how Task 3 is built.

So Tasks 3 and 4 should not start on a guess about these two. Yaakov decides whether to re-run first.

## How to get the missing answers (first run's plan; done in the re-run above)

One short re-run of Task 1 steps 2 to 5: the same throwaway setup and the same full cleanup, about 15 minutes, with
Yaakov's OK and a quiet window. One change: after Yaakov approves the lookup in withHuman, the stuck ledger row is
marked `approved` by hand, after reading his approval with the spike key (one manual ledger write; this is the
"option A" offered on Oct 26). The default profile's wake can then announce the approval in the original chat (the wake only posts a message; the agent then runs the call with `parked_action_execute`). That shows:

- whether the approval is announced in the routed profile's chat, so the agent there runs the call and its answer reaches his Teams chat (`INJECT_CROSS_PROFILE`;
  spec 8 items 3 and 5);
- whether the resumed call's gateway request in withHuman shows the same spike instance (`INSTANCE_ON_BOTH_RECORDS`,
  and then `OPTION`).

## Gaps found (for Task 4 and the runbook)

New from the re-run:

4. The gateway's second approval has a 5-minute deadline, and Hermes's MCP call timeout is also 300 s. If the person
   does not tap in time, the row ends `unknown` ("may have run") even though withHuman shows the request
   `expired` and nothing ran. The executor could read the gateway request's state and record `expired` instead.
5. A read (`get_customer`) still asks twice (park + gateway), as the plan said for writes, because the gateway's
   rules only trust `proto-agents`. Two taps inside 5 minutes is easy to miss on a phone.
6. After the `unknown` result the bot parked the same lookup again on its own (`P-D57A9783`). Worth a line in the
   timeout reply telling the model not to re-ask by itself.

From the first run:

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

## Task 1b: per-profile wake (Oct 27, 9:00 to 9:17 AM ET, 13:00 to 13:17Z)

**Result: `WAKE_ROUTE_PER_PROFILE = yes`.** A wake posted to the person's own route reached that person's profile.
That profile's copy of durable-park read the decision with its own key, posted into its own chat, and the result
reached Yaakov's Teams chat with no typing from him. This clears the `INJECT_CROSS_PROFILE = no` blocker above.
Fully cleaned up. Plan: Task 1b. Spec: section 6.4 and section 8, item 9.

Times below are US Eastern (ET) on the morning of 2026-10-27, with UTC in brackets.

### What was set up (all removed again)

- withHuman agent `spike-c` (kind `service`), instances `spike-c-1` (`4830593c-...`) and `spike-c-2` (`eb839f2d-...`).
  The `spike-c-1` key went straight to `/etc/durable-park/instances/spike.token` (root:hermes 640, never printed).
- Grant: `spike-c` on QuickBooks **sandbox**, `get_customer` and `create_customer` only.
- Blank profile `spike` (same settings as Task 1: model, `mcp_servers.wh`, durable-park with
  `token_file: /etc/durable-park/instances/spike.token` and the shared ledger, no own bots, secrets helper that prints
  only `DURABLE_PARK_WH_TOKEN`). `hermes_tool_policy.py apply` and `check` passed for the profile.
- The profile's `plugins/durable-park` pointed at a **throwaway worktree** of `main` (`141f0a0`), never the live
  clone. In that copy only, `is_wake_event` (`wake.py:61-67`) also accepted `webhook/durable-park-wake-spike`.
  This code change was not committed; the real change belongs in Task 4 (spec 6.4).
- In the default profile's `config.yaml`:
  - the Teams route `{name: spike, platform: teams, user_id: <Yaakov>, guild_id: <tenant>, profile: spike}`;
  - the wake route below. `gateway.multiplex_profiles: true` was already set (it is needed for `/p/<profile>/`).
- One restart at 9:00:22 AM (13:00:22Z): 0 active ledger rows before, `:3978` count 1 after. Log:
  `[webhook] Listening on 127.0.0.1:8644 — routes: durable-park-wake, durable-park-wake-spike`.

**Route settings that worked** (a copy of `durable-park-wake` plus `profile`):

```yaml
platforms:
  webhook:
    extra:
      routes:
        durable-park-wake-spike:
          secret: ${DURABLE_PARK_WAKE_SECRET}   # same secret reference as durable-park-wake
          events: [wake]
          deliver: log
          prompt: "durable-park wake event. The durable-park plugin handles it; if you see this, reply only: ignored."
          profile: spike
```

Wake address: `http://127.0.0.1:8644/p/spike/webhooks/durable-park-wake-spike`.

### What happened

1. 9:06 AM (13:06:33Z): Yaakov sent "Look up customer 1" in Teams. It ran in the spike profile
   (session `agent:spike:teams:dm:...`) and parked: `parked P-B80D1348 tool=mcp__wh__quickbooks__get_customer`.
   Ledger row `profile=spike`. withHuman park request `593a5e20-fa79-5390-9361-ec8ae37ac86a`.
2. Yaakov approved it in withHuman. The spike key read it: `200`, `approved`. The ledger row stayed `parked`:
   no wake reached any route on its own (the receiver still posts only to the old address, as the plan expected).
3. 9:09:03 AM (13:09:03Z): wake posted by hand, as `hermes`, with `post_wake("P-B80D1348", <secret read from
   /etc/durable-park/wake_secret, never printed>, "http://127.0.0.1:8644/p/spike/webhooks/durable-park-wake-spike")`.
   **HTTP status `202`.**
4. The wake landed in the **spike** profile's log, not the default's (default log: only the access line
   `"POST /p/spike/webhooks/durable-park-wake-spike HTTP/1.1" 202`):

   ```text
   13:09:04,011 [webhook] POST event=wake route=durable-park-wake-spike prompt_len=98
   13:09:04,174 GET .../api/aap/v1/requests/593a5e20-... "HTTP/1.1 200 OK"        (spike key)
   13:09:04,333 pre_gateway_dispatch skip: reason=durable-park wake platform=webhook
   13:09:04,341 Plugin message injection admitted: plugin=durable-park session=agent:spike:teams:dm:...
   13:09:04,351 inbound message: ... msg='[durable-park] P-B80D1348 was APPROVED in withHuman by Yaakov Y Trout (web) ...'
   ```

   No `CredentialRefused` anywhere in the spike profile's log (count 0).
5. The bot called `parked_action_execute`. The gateway step asked once more (withHuman request
   `51686024-4aec-5a09-b970-3e533c1ce78a`, tool `get_customer`); Yaakov approved it.
6. 9:09:52 AM (13:09:52Z): `executed P-B80D1348`, then
   `plugin_message plugin=durable-park op=post platform=teams ... ok=True` (Teams `201 Created`), and the bot's
   own answer at 13:10:03 (`201 Created`). Ledger row `executed`, `outcome_notified_at` set. Yaakov typed nothing
   after his first message.

Both withHuman requests show agent `spike-c` and the same instance `spike-c-1` (`4830593c-...`), so
`INSTANCE_ON_BOTH_RECORDS = yes` again.

### Clean up (9:15 to 9:17 AM ET, 13:15 to 13:17Z)

- `config.yaml` restored byte-for-byte from its backup (Teams route and wake route gone); 0 active ledger rows;
  restart at 13:15:05Z; `:3978` count 1; log: `routes: durable-park-wake` only.
- `hermes profile delete -y spike` (the gateway kept running; it logged `Profile 'spike' unserved`). Hermes leaves
  its own marker `~/.hermes/profiles/.deleted/spike`.
- Throwaway worktree removed (`git worktree remove --force`, `git worktree prune`); live clone untouched and clean.
- `spike.token` shredded and the empty `instances` folder removed; secrets helper, the ID file, the temporary
  `~/.spike` folder and the config backup deleted.
- withHuman: grant set to `none` (revision 2), both instances `disabled`, `spike-c` archived; no pending
  `spike-c` requests.
- `hermes_pin.sh check`: OK, Hermes on the pinned v0.22.3 (`5b147ab`), update block on, passive update check off.
- No ledger rows were written by hand. `P-B80D1348` (`executed`) stays until the 30-day cleanup.

### What this means for later tasks

- Task 4 can use the per-profile wake as specified in 6.4. Its file list must include the `is_wake_event` change
  (the default copy accepts `webhook/durable-park-wake`; a person's copy accepts only
  `webhook/durable-park-wake-<its own profile>`). Without it the wake is ignored.
- The receiver and the sweep must post to `/p/<profile>/webhooks/durable-park-wake-<profile>` (here it was done by
  hand).
- A lookup still asks twice (park, then gateway), as in Task 1.
