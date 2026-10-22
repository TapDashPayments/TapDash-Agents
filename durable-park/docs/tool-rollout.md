# Toolset Rollout Guide & Profile Scoping

In plain English: this page explains how we turn on Hermes tools one group at a time, and how each tool call carries the user and profile so withHuman can apply the right approval rule.

This document tracks the rollout of Hermes toolsets through `durable-park` and `withHuman` pipeline rules.

Where things stand: the v1 tool lockdown is still applied ([architecture section 0](architecture.md#0-how-strong-is-it-today)),
so the agent reaches none of the tool groups below today. This page is about which groups have a pipeline rule
(`deploy/withhuman/entry-pipeline.json`, revision 9) and which do not.

## Profile Scoping in AAP Context

**Planned, not live (the profile-based rules).** What is live: when the session has a user id or a profile,
`gatecall.py` sends them in the request's `context`, as in the example below (its values are only examples). What is
planned and **not** live: pipeline rules that use them. The pipeline file has no profile condition, and
`finance_admin` is only an example name.

When tool calls are processed by `gatecall.py`, `user_id` and `profile` context are read from the Hermes session context (`HERMES_SESSION_USER_ID`, `HERMES_SESSION_PROFILE`) and sent in the Agent Approval Protocol (`Call.context`) payload:

```json
{
  "context": {
    "args_sha256": "...",
    "platform": "telegram",
    "session_key": "agent:main:telegram:dm:42",
    "user_id": "12345",
    "profile": "finance_admin"
  }
}
```

**Planned, not live:** this context would allow `withHuman` pipeline rules to apply profile-specific escalation paths or rules (e.g., auto-approving read actions for `finance_admin` profiles while requiring human review for standard profiles).

## Toolset Rollout Order

1. **Group A (Read-only / Safe)**:
   - `web_search`, `web_extract`
   - `vision_analyze`
   - `memory`, `session_search`
   - `todo_list`, `kanban_list`, `kanban_show`
   - Status: Active in pipeline rules (§4.3 row 4).

2. **Group B (Messaging Outbound)**:
   - Chat messaging send tools (Telegram, Teams, Discord)
   - Status: **no rule yet.** The spec proposed one (§4.3 row 5, approve on the test VM), but it was never written:
     there is no messaging rule in `entry-pipeline.json`. A messaging call is a `hermes` call that no earlier block
     matches, so it hits the pipeline's catch-all rule (block 8, `pa-default-human`) and asks an approver every time.

3. **Group C (Browser automation)**:
   - `browser_*` without credential fields.
   - Status: Pending verification (no browser rule exists in `entry-pipeline.json` either; such calls would also hit the catch-all rule).

4. **Group D (Local Machine / Privileged Tools)**:
   - `read_file`, `search_files`, `write_file`, `patch`
   - `terminal`, `execute_code`, `process_manage`
   - `delegate_task`, `cronjob_manage`, `skill_manage`
   - Status: **Denied** by default in pipeline rules until Clean Setup Phase 2 (ledger process isolation) is completed.

With no user id or profile in the session (for example outside a chat) the two keys are left out.
