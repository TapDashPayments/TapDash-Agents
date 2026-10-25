# durable-park: acceptance record, Maverick and three remote servers (2026-10-25)

**Result: PASSED.** The Hermes agent on the test VM `proto-agents` now reaches
five servers through the withHuman gateway: QuickBooks (sandbox), Maverick (placeholder settings, no sandbox yet),
Supabase `tapdash-crm-dev`, Monday and DocuSign (demo). Read-only calls ran without asking anyone. The one
money call (a Maverick refund) was parked and asked Yaakov; he denied it, and nothing ran.

- Environment: test VM `proto-agents`, tunnel image `-mav-e4a2b10` (maverick-mcp `e4a2b10`), TapDash-Agents `d2eb929`,
  withHuman entry pipeline revision 14, then revision 15 (from 02:54 UTC).
- How to set it up: [gateway-setup.md, Remote gateway servers](gateway-setup.md) and the Maverick section of the
  [runbook](runbook.md).
- Previous record: [acceptance-2026-10-22-langfuse.md](acceptance-2026-10-22-langfuse.md).

## What is connected

| Server | How | Tools the agent sees | What runs without asking |
|---|---|---|---|
| QuickBooks (sandbox) | tunnel, local MCP server | 142 | Reads (get, search, list, read, query, find) |
| Maverick | tunnel, TapDash `maverick-mcp` | 42 | Lookups and internal drafts; a note or ticket only when nobody outside is told |
| `supabase-crm-dev` | remote, `mcp.supabase.com` (read only) | 7 | 6 read tools; `execute_sql` always asks |
| `monday` | remote, `mcp.monday.com` | 33 | 33 read tools |
| `docusign-demo` | remote, `mcp-d.docusign.com` | 22 | 22 read tools |

Everything else asks Yaakov first. Money calls on any server always ask a person.

## Tests (all times UTC, 2026-10-25; sent from Yaakov's Teams account to the agent)

| # | Time | Asked | Park ID(s) | Result |
|---|---|---|---|---|
| 1 | 02:40 | Search Maverick lookups | P-89E305A6 | Ran without asking |
| 2 | 02:42 | List Supabase tables | P-525A9F56 (and 2 repeats) | Ran without asking (4111 characters back) |
| 3 | 02:43 | List Monday workspaces | P-17847329 | Ran without asking |
| 4 | 02:44 | DocuSign user, account, billing plans | P-9B5C2DA8, P-9799E1D4, P-F7550A3D | Ran without asking |
| 5 | 02:46 | Read a Maverick record | P-946B2479 | Allowed. The call then failed at once, as expected with the placeholder address (the next ledger row is 6 s later). The message said "Maverick did not answer in 30 s. Nothing changed.", which is the wrong reason (see Fixed afterwards) |
| 5b | 02:46 | (the agent, on its own) read and then tried to edit its Maverick skill | P-89326D56, P-68064834, P-CEC69529 | Reading (`skills_list`, `skill_view`) was allowed. Editing (`skill_manage`) was refused by withHuman, as designed |
| 6 | 02:47 | Refund Maverick transaction 1, $1.00 | P-4296295C | **Parked; asked Yaakov.** He denied it in withHuman (04:14 UTC); the ledger row is `denied`. Nothing ran |
| 7 | 02:54:37 (after revision 15 went live at 02:54:06) | Revision 15 check: Monday workspaces, Supabase tables, DocuSign user | P-D9A3A465, P-C22078C8, P-AA4FB8B7 | Ran without asking under revision 15 |

The Supabase project `tapdash-crm-dev` is a new, empty dev project (only Supabase's own `auth`, `storage`, `vault` and
`realtime` tables). It is not the real CRM.

## Revision 15

Revision 15 (active 02:54 UTC) changes only blocks 19 to 21 (`gw-supabase`, `gw-monday`, `gw-docusign`): each now
matches on the server name alone. Revision 14 also matched any tool name starting with `<server>__`, which a call on a
different server could have used (a reviewer's finding on pull request 54). Test 7 shows the reads still run. Rollback: revision 14.

## Fixed afterwards

- `maverick-mcp` said "did not answer in 30 s" for a call that could not connect at all. Its PR #4 (merged,
  `d6f43f9`) names a failed or broken connection separately from a timeout; the tunnel now runs image `-mav-d6f43f9`.
  Checked 2026-10-25 on both images with the same placeholder settings (`/etc/durable-park/maverick.env`), one
  lookup each (`GET /payment/1`) straight through the server's client in a throw-away container:
  `-mav-e4a2b10` said "Maverick did not answer in 30 s. Nothing changed."; `-mav-d6f43f9` said "Could not
  connect to Maverick (check its address and the network). Nothing changed." Both failed at once (0.0 s).

## Still open

- Maverick has no sandbox yet; it runs with placeholder settings, so every real Maverick call fails.
- Who may change rules: revision 13 was made in the withHuman app by the Grok browser agent's member account. Yaakov
  decides whether that account keeps permission to activate pipelines.
