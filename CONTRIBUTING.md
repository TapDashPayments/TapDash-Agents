# Contributing

## Setup (once per clone)

```sh
git config core.hooksPath .githooks      # turns on the pre-push docs check
cd durable-park && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

## Making a change

1. Work on a branch. On the VM, use a git worktree under `/home/hermes/tapdash-agents-wt/<name>`, never the live
   clone's working tree: the gateway and the sweep load code from it ([runbook](durable-park/docs/runbook.md#deploy-a-change)).
2. Change the code **and the docs it affects** (next section).
3. Run the tests: `cd durable-park && .venv/bin/pytest -q -W error`. New behaviour needs a test.
   For a change under `tracing/` (the patched Langfuse plugin), run `bash tracing/run_tests.sh` as `hermes` on the test
   VM instead; it needs the Hermes environment's Python, so CI does not run it yet
   ([backlog](durable-park/docs/backlog.md#langfuse-tracing-gaps-from-the-2026-10-22-audit)).
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
- A third-party piece is added, removed or changes version -> the [Built on](README.md#built-on) table.

### How it is enforced

[`.github/docs-map.json`](.github/docs-map.json) maps code to the docs that describe it. For every rule, a change
to its code must also change at least one of its docs. That is the **minimum** the tooling can check. You still
update every doc that is affected, and you check that what it now says is true.

| Where | What runs |
|---|---|
| `git push` | `.githooks/pre-push` refuses the push |
| CI on every pull request and on every push to `main` | the `Docs updated with code` job fails |
| CI | every tracked file must be covered by the map, so new files get mapped (`--verify-map`) |
| CI | every Mermaid diagram must parse (`tools/diagrams`), so a broken diagram cannot land |
| Every new or reopened pull request | the `Docs Keeper` workflow asks Kilo's Docs Keeper agent to check the docs (see [Docs Keeper](#docs-keeper-automatic)) |

See the rules: `python3 tools/docs_check/check_docs.py --explain`.

**Adding a file?** Add it to a rule in `docs-map.json` (or to `exempt` if it is a test or a doc). CI fails until
you do. Example: `durable-park/plugin/durable_park/breaker.py` (the withHuman circuit breaker) is mapped to
`architecture.md` in the "Plugin wiring, wake, executor, approval calls" rule.
The gateway-start tool policy check (`durable-park/deploy/tool_policy_check.sh` and its systemd drop-in
`durable-park/deploy/hermes-gateway-policy.conf`) is mapped to `runbook.md` in the "Deploy scripts and service units"
rule: change either, update the runbook's "Tool policy check failed" and "Open a toolset" sections.
The gateway-start load check (`durable-park/deploy/plugin_load_check.sh`, run by the same drop-in as `ExecStartPost`)
is covered by the same rule (`deploy/*.sh`): change it, update the runbook's "Gateway will not start: the plugin did
not load" section.
The vendored Langfuse plugin (`tracing/tapdash_langfuse/` and `tracing/run_tests.sh`) is mapped to the runbook and to
`tracing/tapdash_langfuse/PATCHES.md` in the "Tracing plugin (tapdash-langfuse)" rule: every difference from upstream
goes into `PATCHES.md` in the same commit. Its tests run with `bash tracing/run_tests.sh`, not the durable-park pytest.
The gateway bridge's own user and port (`durable-park/deploy/durable-park-bridge.service`, `durable-park-bridge.socket`
and `bridge_check.py`) are mapped to `architecture.md` and `gateway-setup.md` in the "Gateway bridge isolation" rule (and,
like the other deploy files, to the runbook and the two setup guides in "Deploy scripts and service units"): change one,
update the bridge's tables and the "Own user and own port" section in the same commit.
The withHuman entry pipeline kept as a reviewed file (`durable-park/deploy/withhuman/entry-pipeline.json`) is mapped to
`architecture.md`: change the file, change the pipeline table there in the same commit.
The backup-approver example (`durable-park/deploy/approvers.example.json`, TAP2-121) is mapped with the sweep in the
"Sweep (cron)" rule: change it, update the runbook's "Backup approver and unanswered approvals" section.

**Really no doc change needed** (no change in behaviour or interface: a typo, a comment, a local rename)? Add a
trailer line to a commit message with the reason:

```
Docs-Not-Needed: renamed a local variable, no behaviour change
```

Use it rarely: it shows up in the history, and reviewers will ask.

### Docs Keeper (automatic)

A helper checks the docs of your pull request for you. Its name is Docs Keeper, and it is an agent that lives in
Kilo (a service we use).

- **When it runs:** when you open a pull request, reopen one, or take one out of draft. It does not run for
  drafts, for pull requests from forks, or for branches that start with `docs/`.
- **What happens:** GitHub sends the pull request number to Kilo. Docs Keeper looks at the pull request and
  checks that the docs match the code. If they do not, it may push a docs fix to your pull request branch. It
  always leaves a comment that starts with `Docs Keeper:` to say what it did.
- **What it never does:** it never changes code and it never merges anything.
- **What it needs:** two repository secrets (Settings > Secrets and variables > Actions):
  `KILO_DOCS_KEEPER_WEBHOOK_URL` (the web address Kilo gives us) and `KILO_DOCS_KEEPER_SECRET` (the shared
  password for that address). If either one is missing, the workflow skips quietly and nothing happens.
- **To turn it off:** delete those two secrets, or disable the `Docs Keeper` workflow in the Actions tab.

The workflow is `.github/workflows/docs-keeper.yml`. It does not replace your own docs check: you still follow
the rules above.

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
