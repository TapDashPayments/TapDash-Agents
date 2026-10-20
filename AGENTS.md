# Instructions for AI agents (and people)

Read this before changing anything in this repo.

## Know the system first

1. [`README.md`](README.md): what this repo is.
2. [`durable-park/docs/architecture.md`](durable-park/docs/architecture.md): how the pieces connect. Read it
   before changing durable-park.
3. [`durable-park/docs/runbook.md`](durable-park/docs/runbook.md): how to check, deploy and fix the live system.

## Rule 1: change the docs with the code

Every change updates the docs, diagrams, tables and maps it affects, **in the same commit**. This is not
optional, and the tooling enforces it (pre-push hook, CI). Before you finish:

- Run `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD` and fix what it lists.
- Re-read the diagram or table that describes what you changed, and confirm it is still true. The check only
  knows *which* doc to touch, not whether you got it right.
- A new file goes into [`.github/docs-map.json`](.github/docs-map.json).
- Use `Docs-Not-Needed: <reason>` only for real no-op changes, and say so in your summary to the user.

Details: [CONTRIBUTING.md](CONTRIBUTING.md).

## Rule 2: the safety guarantees are the product

durable-park exists so that nothing risky runs without exactly one human approval, nothing runs twice, and an
unclear outcome is never retried. Any change that touches the park hook, the ledger, the executor, the wake
handler or the bridge must keep all three. Prove it with tests, and get an independent review before deploying.
When in doubt, fail closed (block, or ask a person). Never auto-approve.

## Rule 3: operating the live system

- The test VM `proto-agents` is the only place to experiment. **Never change the production Hermes host** (the
  jump host the connector runs on).
- Through the connector, every heredoc gets a **unique** delimiter, never reused inside the script.
- **Never print, paste or commit a secret.** Secrets live in `/etc/durable-park/` and are typed in by Yaakov with
  `read -rs`. If a user pastes one into chat, don't use it; ask them to revoke it.
- Root steps (copying into `/opt`, restarting system units) are Yaakov's. Give him the exact one-line command.
- Before restarting the gateway or the bridge, make sure no row is `parked`, `approved` or `executing`
  ([runbook](durable-park/docs/runbook.md#health-check)).
- Work on a git worktree or branch, not in the live clone's working tree. The gateway and the 5-minute sweep load
  code straight from it.

## Rule 4: tests

- `cd durable-park && .venv/bin/pytest -q -W error` must pass. Write the failing test first.
- Unit tests never reach the network and never read real credentials (`tests/conftest.py` enforces both).
  Live checks go in `tests/live/` and run only on the VM with `DURABLE_PARK_LIVE=1`.
