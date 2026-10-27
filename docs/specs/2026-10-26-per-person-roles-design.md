# Per-person roles for the Ops agent (design)

Date: 2026-10-26 (revised the same day after an independent review). Linear: TAP2-27. Status: design approved in
chat by Yaakov; written spec waiting for his review. Builder: tab.bot, after this spec and the implementation plan
are approved.

In plain English: today everyone who talks to the bot is treated the same, and withHuman sees only one "agent".
This design gives each person a **role** (Admin, Ops or Viewer). The role decides which tools the person can use.
withHuman keeps making every decision, and now it also knows **who** asked.

## Words used here

| Word | Meaning |
|---|---|
| Profile | A separate Hermes "home" folder with its own settings, keys and chat history. We make one per person. |
| Route (`profile_routes`) | A line in Hermes's settings: "messages from this Teams person go to that profile". |
| Teams user ID | The hidden ID Microsoft gives each Teams account (an Entra object ID, like `1b2c...`), not the email. |
| Tenant ID (`guild_id` in Hermes) | The ID of the company's Microsoft account (TapDash.co). |
| withHuman agent | withHuman's record for an AI agent. Its tool list and rules apply to it. Here: one per role (or per person, see section 4). |
| Instance | One copy of a withHuman agent, with its own key. |
| Provisioner token | A withHuman key that can only create new instances of one agent. |
| durable-park | Our Hermes plugin: asks withHuman about every tool call, holds ("parks") risky ones until a person approves, then runs the exact call once. |
| Bridge | Our small service that tells withHuman's gateway "this call was already approved" so the approved call is not asked about twice. |
| Ledger | durable-park's record of every parked call. |
| Sweep | durable-park's 5-minute safety check that re-sends missed messages and posts alerts to Teams. |

## 1. Decisions (Yaakov, 2026-10-26)

| # | Question | Decision |
|---|---|---|
| 1 | Who uses the Ops agent? | Several people with different rights. |
| 2 | How are rights set? | **By role.** Per-person changes on top of a role come later: GitHub #79 (Linear TAP2-179). |
| 3 | Which roles? | **Admin, Ops, Viewer** (section 2). |
| 4 | Where do roles and rights live? | **In withHuman** (tool lists and rules per role). Hermes sends each person to their own profile with their own key. |
| 5 | Who approves? | **Unchanged:** Yaakov approves everything, including his own requests, through the escalation path `notify-yaakov-yehuda`. "The asker must not approve their own request" stays an open item of TAP2-83. withHuman can already name different approvers per rule, so that needs no build later. |
| 6 | Which chat apps? | **Teams only.** Telegram stays Yaakov's own admin line (the default profile). WhatsApp later (TAP2-66). |
| 7 | Where does this design live? | Here, for the client now; written so the hermes-withhuman-kit can reuse it later. |
| 8 | Recurring tasks (TAP2-31) | Native Hermes scheduling, **left off for now**. Event triggers later: GitHub #80. |

## 2. Roles

| Role | Can do in the systems (withHuman) | Can do in the chat (Hermes) |
|---|---|---|
| **Admin** | Same as Ops. | Every chat command (`/update`, `/restart`, `/model` ...). Role changes are made with `people.sh` on the server (section 6.4), not by chat. |
| **Ops** | Everything the agent can do today, **with today's rules**: reads run; money moves and most changes ask Yaakov; the changes today's rules already let run on their own still do (Maverick drafts and notes or tickets that tell nobody, accept.blue customers and draft invoices, Authorize.net customer profiles; see `durable-park/docs/architecture.md`). | `/help`, `/whoami` only. |
| **Viewer** | Only the tools on the Viewer read list (section 6.2). Everything else is refused, never asked about. | `/help`, `/whoami` only. |

## 3. Project map (what does what)

```mermaid
flowchart LR
  S["Sarah in Teams<br/>(Ops)"] --> GW["Hermes gateway<br/>(one process)"]
  V["Victor in Teams<br/>(Viewer)"] --> GW
  X["Unknown person"] -->|"not on the allowlist"| NO["Refused"]
  GW -->|"route: Sarah's Teams ID"| PS["Profile sarah<br/>(her key)"]
  GW -->|"route: Victor's Teams ID"| PV["Profile victor<br/>(his key)"]
  PS --> DPS["durable-park<br/>(sarah's copy)"]
  PV --> DPV["durable-park<br/>(victor's copy)"]
  DPS -->|"as Sarah"| WH["withHuman<br/>(role tool lists + rules)"]
  DPV -->|"as Victor"| WH
  WH --> BR["Bridge<br/>(approved call? same person?)"]
  WH -->|"read: run<br/>change: ask Yaakov<br/>Viewer change: refuse"| SYS["Business systems"]
  DPS --> L[("Ledger<br/>(one, shared)")]
  DPV --> L
  SW["Sweep"] --> L
```

| Piece | Where | What it does |
|---|---|---|
| Roles | withHuman | Tool lists (grants per gateway server) and rules per role. |
| People | withHuman + Hermes | Each person has their own withHuman key and their own Hermes profile; withHuman's records name the person. |
| Routing | Hermes `gateway.profile_routes` in the default profile's `config.yaml` | Teams user ID + tenant ID → that person's profile. One gateway serves all profiles (`gateway.multiplex_profiles`, on by default). |
| Strangers | Hermes `TEAMS_ALLOWED_USERS` | Only people added with `people.sh` are on it; `people.sh` adds the route and the allowlist entry together. A catch-all route to an empty profile `not-set-up` is a second lock (section 6.3). |
| Admin commands | Hermes `platforms.teams.extra.allow_admin_from` (direct messages) **and** `group_allow_admin_from` (group chats and channels) | Both lists = the Admins' Teams IDs. The two lists are separate in Hermes (`gateway/slash_access.py`); an empty one means no gating in that kind of chat. |
| Approvals | withHuman, unchanged | Yaakov approves everything that asks a person. |

Facts these rows rely on (checked 2026-10-26 against Hermes v0.22.3 code and docs, and by the independent review):
- Teams messages carry `user_id` = the sender's Entra object ID and `guild_id` = the tenant ID
  (`plugins/platforms/teams/adapter.py:521-529`); a route with both matches. If Teams sends no tenant, Hermes fills
  in the configured one, so the tenant check is weak; the user ID alone is unique, so the risk is low.
- Routes are chosen by how specific they are, not by order (`gateway/profile_routing.py`). A route to a profile the
  gateway does not serve is rejected.
- Hermes: "Sender routing selects a profile; it is not deny-by-default authorization." So the allowlist stays on.
- Hermes keeps one plugin manager per profile and loads durable-park separately for each profile
  (`hermes_cli/plugins.py:1589-1592`). Each profile has its own MCP connections.
- withHuman rules can see only the tool, server, arguments, the agent's slug and name, and earlier blocks' results.
  They cannot see which chat user typed. That is why the role travels with the agent.
- **Keys are for knowing who asked, not for walls between people.** All profiles run in one process as the `hermes`
  user, so that process can read every key file. The agent itself has no file or terminal tools, which is what keeps
  a person from using another's key.

## 4. One withHuman agent per role, or per person? (decided by the spike)

The bridge must tell people apart: an approval for Sarah's call must never let Tom's identical call through.

- **Option R (role agents):** agents `ops-admin`, `ops-ops`, `ops-viewer`; one instance per person. Works only if
  withHuman's gateway webhook tells the bridge **which instance** made the call. The spike checks this.
- **Option P (person agents), fallback:** one agent per person whose name starts with the role, for example
  `ops-ops--sarah`, `ops-viewer--victor`. Rules match the role with a name prefix (`regex ^ops-viewer--`); the bridge
  tells people apart by agent name. `people.sh` copies the role's tool list to each new person agent.

Everything else in this design is the same for both. The plan picks one after the spike.

## 5. What happens when someone asks for something

1. Sarah (Ops) writes in Teams. Hermes matches her Teams user ID and tenant to her route and runs the turn in
   profile `sarah`.
2. Sarah's profile reaches withHuman with Sarah's key, so withHuman lists only the tools her role has.
3. Before each tool call, Sarah's copy of durable-park asks withHuman with Sarah's key. The rules decide: run,
   refuse, or ask Yaakov.
4. If Yaakov must decide, he approves in withHuman. The decision is read **with Sarah's key** (the key of the
   ledger row's profile), the parked call runs once in Sarah's profile, the bridge checks it is the same person's
   approved call, and the result is posted in Sarah's chat.

## 6. Changes

### 6.1 Bridge (`durable-park/bridge/`)

| Today | Change |
|---|---|
| Trusts one agent: `DEFAULT_EXPECTED_AGENT = "proto-agents"`; any other agent goes to a person (`app.py:54, 155-156`). | Trusts a list of agents (the role or person agents). |
| Looks up approved parks by agent only (`app.py:171`). | Also requires the park and the gateway call to come from **the same person** (same instance in option R, same agent in option P). |

### 6.2 withHuman

- Agents and keys as in section 4. Keys go into root-only files; nothing prints them.
- **Ops and Admin grants:** what `proto-agents` has today (several servers are granted as "all tools", which also
  covers future tools).
- **Viewer read list (new, written by hand per server):** today's rules have no clean read lists. QuickBooks reads
  are a name pattern; the Maverick, accept.blue and Authorize.net "auto" lists mix reads with changes (for example
  `create_customer`, `create_invoice`, `save_vault_customer`); the Hermes "read" rule includes `memory` and
  `image_generate`. So the plan lists the Viewer's exact read tools per server, and the Viewer gets only those
  (grant mode "selected"). The Hermes list for Viewers leaves out `memory` and `image_generate`.
- **Rules (new pipeline revision, saved by Yaakov):**
  1. **First block:** a Viewer call that is not on the Viewer read list is refused. It sits before every other rule,
     including "Money server: always a person".
  2. A Viewer call on the read list is approved.
  3. Every `slug equals proto-agents` condition also accepts the Ops and Admin agents. Ops therefore keeps today's
     auto-approved changes (section 2). `proto-agents` stays until the default profile moves, then a later
     revision removes it.

### 6.3 Hermes configuration

- Default profile `config.yaml`: `gateway.profile_routes` (one route per person: platform `teams`, `user_id`,
  `guild_id` = TapDash tenant ID; plus one Teams catch-all route to `not-set-up`), `TEAMS_ALLOWED_USERS`, and both
  `allow_admin_from` and `group_allow_admin_from` under `platforms.teams.extra`. Example route:

  ```yaml
  gateway:
    profile_routes:
      - name: sarah
        platform: teams
        user_id: "1b2c3d4e-0000-0000-0000-000000000000"
        guild_id: "<TapDash tenant ID>"
        profile: sarah
  ```

- Each person's profile is created **blank** from a template (never cloned: cloning copies keys). The template
  sets today's lockdown, the plugin set, and `plugins.entries.durable-park.settings.token_file` pointing at that
  person's own key file (an existing durable-park setting).
- `not-set-up`: same lockdown and plugin, no tools, and a short persona that answers "you're not set up yet, ask an
  admin". It still runs the model (a small cost) and its reply depends on the model following the persona; it can
  never do anything, which is what matters.

### 6.4 durable-park (`durable-park/`)

| Topic | Change |
|---|---|
| One copy per profile | Hermes loads durable-park once per profile. Each copy uses its own profile's `token_file`. |
| Files | **Shared:** the ledger (one fixed path for every profile). **Per profile:** the status files that sit next to the ledger today (load proof, tool policy load, breaker, lift status, form check), so one profile's copy never overwrites another's. The sweep reads every profile's status files. |
| Who asked | Already there: ledger rows store `profile` and `user_id` (`ledger.py:55-56`, filled from `HERMES_SESSION_PROFILE` in `park_hook.py:157-163`). Use them. |
| Decisions and wakes | A decision is read with the key of the row's profile, and the call resumes in that profile's session (also in the sweep's re-send path). |
| Start checks | `tool_policy_check.sh` checks every profile, `not-set-up` included, and refuses to start when a profile has no key file or two profiles share a key. |

### 6.5 New admin command `durable-park/deploy/people.sh` (run by an admin with `sudo`)

It runs as root, because it reads the provisioner tokens (root-only) and writes the key files, and it edits
Hermes's files as the `hermes` user.

| Command | What it does |
|---|---|
| `people.sh add <name> <teams-user-id> <role>` | Creates the person's withHuman key (written straight to a root-only file, never printed), the blank profile from the template, the route and the allowlist entry. Restarts the gateway only when no ledger row is `parked`, `approved` or `executing` (else waits and says so). |
| `people.sh move <name> <role>` | Refused while that person has a row `parked`, `approved` or `executing` (the old key must finish them). Then: new key under the new role, old key disabled. |
| `people.sh remove <name>` | Same refusal while rows are active. Then: key disabled, route and allowlist entry removed, chat history kept for the retention period. |
| `people.sh list` | Name, role, Teams ID, last seen. |

Every step is checked; on any failure it undoes what it did, says what failed, and exits with an error.

## 7. When things go wrong (never silent)

| Problem | Result |
|---|---|
| A profile has no key file, or two profiles share one | Gateway does not start; Teams alert names the profile. |
| A person's key cannot be read at run time | Their tool calls are refused; Teams alert. |
| An approved call shows up from a different person | The bridge sends it to a person; nothing runs on someone else's approval. |
| Not on the allowlist | Refused by Hermes. |
| Allowlisted, no route | Lands on `not-set-up`: no tools. |
| A non-admin sends `/restart` or `/update` (direct or group chat) | Refused. |
| withHuman down | Everything refused (existing breaker), as today. |
| `move` or `remove` with active rows | Refused with the list of rows to finish first. |
| `people.sh` fails midway | Undoes its steps, says what failed, exit code not 0. |

## 8. Tests

**Spike first (before any build):**
1. Does withHuman's gateway webhook tell the bridge which instance made the call? (Decides option R or P.)
2. Does each profile's copy of durable-park load, use its own `token_file`, and record its profile?
3. Can a decision be read with the row's profile key, and does resume reach a routed profile's session (including
   the sweep's re-send)?
4. What does a real Teams user ID look like, and does a route with `user_id` + `guild_id` match it?
5. Park → approve → run once in a routed profile, result in that person's chat.

**Acceptance (sandbox accounts only):**
- Viewer: a lookup runs without asking; a change is refused without paging Yaakov.
- Ops: a change asks Yaakov; after approval it runs once. A change on today's auto list runs on its own.
- **Approval used by a different person:** Tom sends the exact call Sarah got approved; it does not run on her
  approval.
- Two people at the same time: each shows up under their own name in withHuman; each result lands in the right chat.
- A stranger is refused; an allowlisted person with no route can do nothing.
- A non-admin's `/restart` is refused in a direct chat **and** in a group chat; an Admin's works.
- A gateway restart during a pending approval still finishes in the right person's chat.
- `people.sh move` is refused with an active row, and works after it ends.
- Results go in a new acceptance record in `durable-park/docs/`.

## 9. Order of work

1. Spike (section 8).
2. Viewer read lists per server (with Yaakov).
3. Bridge change, then durable-park changes (PRs, tests first).
4. withHuman: agents, grants, the new rules (old `proto-agents` rules kept).
5. `people.sh` (PR).
6. Yaakov as Admin, one test Ops and one test Viewer person (a second Teams account is needed); run acceptance.
7. Add the client's people (Yaakov gives each person's role).

## 10. Out of scope (tracked elsewhere)

- Per-person rights on top of a role: GitHub #79.
- Event-started tasks: GitHub #80. Turning on the scheduler: TAP2-31.
- "The asker must not approve their own request": TAP2-83.
- Teams Approve/Deny buttons: TAP2-29. Note for that work: Hermes's own Teams approval cards accept a click from
  anyone on `TEAMS_ALLOWED_USERS` (`adapter.py:661-671`), Viewers included; the buttons must check the role.
- WhatsApp: TAP2-66.

## 11. References

- Hermes multi-profile gateways: https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways
- Hermes profiles: https://hermes-agent.nousresearch.com/docs/user-guide/profiles
- Hermes code v0.22.3: `gateway/profile_routing.py`, `gateway/slash_access.py`, `plugins/platforms/teams/adapter.py`,
  `hermes_cli/plugins.py`
- durable-park: `bridge/app.py`, `plugin/durable_park/ledger.py`, `park_hook.py`, `wake.py`
- withHuman condition fields: the management MCP's `read_reference` topic `conditions`
