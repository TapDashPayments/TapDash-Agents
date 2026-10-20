# Contributing

## Setup (once per clone)

```sh
git config core.hooksPath .githooks      # turns on the pre-push docs check
cd durable-park && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

## Making a change

1. Branch (or, for operations work on the VM, commit on `main` in `/home/hermes/tapdash-agents`).
2. Change the code **and the docs it affects** (next section).
3. Run the tests: `cd durable-park && .venv/bin/pytest -q -W error`. New behaviour needs a test.
4. Push. Open a pull request for anything beyond operations fixes; the template has the checklist.
5. Make it live on the VM as the [runbook](durable-park/docs/runbook.md#deploy-a-change) says.

Commit messages: a short imperative subject (`fix(bridge): ...`, `feat: ...`, `docs: ...`) and a body that says
why. Commits written by an AI agent end with its `Co-Authored-By:` line.

## Keep the docs current

**Every change updates the docs, diagrams, tables and maps it affects, in the same commit or pull request.**
Out-of-date docs are worse than none: the next person (or AI agent) trusts them.

What counts as affected:

- A component, arrow, port, path, user, state, threshold or message changes -> the matching diagram or table in
  [`durable-park/docs/architecture.md`](durable-park/docs/architecture.md).
- How to deploy, check or fix something changes -> [`runbook.md`](durable-park/docs/runbook.md).
- A setup step changes -> the setup guide ([gateway](durable-park/docs/gateway-setup.md),
  [receiver](durable-park/docs/receiver-setup.md)).
- What the project is or how to start changes -> the READMEs.

### How it is enforced

[`.github/docs-map.json`](.github/docs-map.json) maps code to the docs that describe it. For every rule, a change
to its code must also change at least one of its docs.

| Where | What runs |
|---|---|
| `git push` | `.githooks/pre-push` refuses the push |
| CI on every push and pull request | the `Docs updated with code` job fails |
| CI | every tracked file must be covered by the map, so new files get mapped (`--verify-map`) |
| CI | every Mermaid diagram must parse (`tools/diagrams`), so a broken diagram cannot land |

See the rules: `python3 tools/docs_check/check_docs.py --explain`.

**Adding a file?** Add it to a rule in `docs-map.json` (or to `exempt` if it is a test or a doc). CI fails until
you do.

**Really no doc change needed** (a pure refactor, a typo)? Add a trailer line to a commit message with the reason:

```
Docs-Not-Needed: renamed a local variable, no behaviour change
```

Use it rarely: it shows up in the history, and reviewers will ask.

### Diagrams

Diagrams are [Mermaid](https://mermaid.js.org) blocks inside the markdown. GitHub draws them, and they are
edited as text in the same commit as the code. Rules:

- Label every arrow with what travels on it (`saves the exact call`, `approve or human`), not just a line.
- Show the mechanism (which part talks to which, through what), not a list of names.
- Check locally before pushing: `node tools/diagrams/validate_mermaid.mjs $(git ls-files '*.md')`, after
  `npm ci --prefix tools/diagrams` once.

## Tooling changes

The docs check, hooks and CI live in `tools/`, `.githooks/` and `.github/`. Changing them means updating this
file (that is their rule in the map). The checker has its own tests:
`python3 -m pytest -q tools/docs_check`.
