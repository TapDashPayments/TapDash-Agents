# Toolset Rollout Guide & Profile Scoping

In plain English: this page explains how we turn on Hermes tools one group at a time, and how each tool call carries the user and profile so withHuman can apply the right approval rule.

This document tracks the rollout of Hermes toolsets through `durable-park` and `withHuman` pipeline rules.

## Profile Scoping in AAP Context

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

This context allows `withHuman` pipeline rules to apply profile-specific escalation paths or rules (e.g., auto-approving read actions for `finance_admin` profiles while requiring human review for standard profiles).

## Toolset Rollout Order

1. **Group A (Read-only / Safe)**:
   - `web_search`, `web_extract`
   - `vision_analyze`
   - `memory`, `session_search`
   - `todo_list`, `kanban_list`, `kanban_show`
   - Status: Active in pipeline rules (§4.3 row 4).

2. **Group B (Messaging Outbound)**:
   - Chat messaging send tools (Telegram, Teams, Discord)
   - Status: Approved on test VM (§4.3 row 5).

3. **Group C (Browser automation)**:
   - `browser_*` without credential fields.
   - Status: Pending verification.

4. **Group D (Local Machine / Privileged Tools)**:
   - `read_file`, `search_files`, `write_file`, `patch`
   - `terminal`, `execute_code`, `process_manage`
   - `delegate_task`, `cronjob_manage`, `skill_manage`
   - Status: **Denied** by default in pipeline rules until Clean Setup Phase 2 (ledger process isolation) is completed.

With no user id or profile in the session (for example outside a chat) the two keys are left out.
