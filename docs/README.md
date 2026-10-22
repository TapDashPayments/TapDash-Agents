# Specs and plans

In plain English: before we build something, we write a **spec** (what and why) or a **plan** (the steps). This page
lists every one of them and says whether it is done. If a spec and the code disagree, the code and the
[architecture](../durable-park/docs/architecture.md) win. The records of what was tested live next to the runbook, in
[`durable-park/docs/`](../durable-park/docs/) (the files named `acceptance-...`).

| Document | What it is | Status |
|---|---|---|
| [specs/2026-10-20-one-agent-merge-design.md](specs/2026-10-20-one-agent-merge-design.md) | Merge the two Hermes installs on the test VM into one agent | Implemented 2026-10-21, then superseded the same day by the v1 lockdown |
| [plans/2026-10-20-one-agent-merge.md](plans/2026-10-20-one-agent-merge.md) | The steps for that merge | Run 2026-10-21; superseded the same day (the full toolset is no longer live) |
| [specs/2026-10-21-all-tools-withhuman-design.md](specs/2026-10-21-all-tools-withhuman-design.md) | Open the agent's tools one by one, with withHuman deciding every call, and clean up the VM (Clean Setup) | Accepted for the sandbox 2026-10-21. Phase 1 done 2026-10-22. Phase 2 (a separate owner for the ledger) is open |
| [specs/2026-10-22-bridge-verifies-withhuman.md](specs/2026-10-22-bridge-verifies-withhuman.md) | The bridge checks approvals in withHuman, not in the ledger | Implemented 2026-10-22 |
| [../durable-park/docs/plans/2026-10-22-bridge-own-user.md](../durable-park/docs/plans/2026-10-22-bridge-own-user.md) | The bridge runs as its own Linux user, and systemd holds its port | Implemented 2026-10-22 |
| [../durable-park/docs/plans/2026-10-22-withhuman-context-lift.md](../durable-park/docs/plans/2026-10-22-withhuman-context-lift.md) | Move the agent's reason note to where withHuman expects it | Implemented 2026-10-22 (PRs #35, #40, #43) |
| [plans/2026-10-22-langfuse-tracing.md](plans/2026-10-22-langfuse-tracing.md) | Turn on Langfuse tracing and audit the traces | Done 2026-10-22 |
| [specs/2026-10-22-langfuse-trace-gaps-design.md](specs/2026-10-22-langfuse-trace-gaps-design.md) | Fix the gaps the audit found: user id, trace names, park tags | Implemented 2026-10-22 |
| [plans/2026-10-22-langfuse-trace-gaps.md](plans/2026-10-22-langfuse-trace-gaps.md) | The steps for those fixes (the patched plugin) | Implemented 2026-10-22 |
| [../durable-park/docs/plans/2026-10-19-durable-park-prototype.md](../durable-park/docs/plans/2026-10-19-durable-park-prototype.md) | The original build plan for durable-park | Historical: details changed during the build |

When you add a spec or a plan, add a row here with its status, and change the status when the work is done.
