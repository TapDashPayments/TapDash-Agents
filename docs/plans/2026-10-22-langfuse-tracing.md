# Langfuse tracing on proto-agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Every message to and from the agent, every model call and every tool call on `proto-agents` is visible in Langfuse Cloud with full content, without weakening durable-park (one human approval, never twice, unclear never retried).

**Architecture:** Operations change plus docs. Nous's official `langfuse` Hermes plugin (hooks only, no tools) sends traces to Langfuse Cloud US. Its `pre_tool_call` hook is a pure observer, so it is listed in durable-park `observer_hooks` (otherwise durable-park refuses every call, by design). No durable-park code changes.

**Tech Stack:** Hermes v0.22.3 (`~hermes/.hermes/config.yaml`, `~hermes/.hermes/.env`), plugin catalog entry `langfuse` v2.0.0 (repo `https://github.com/NousResearch/hermes-official-plugins`, subdir `langfuse`, commit `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d`), SDK `langfuse==4.15.2`, Langfuse Cloud US Hobby plan.

**Spec:** Linear TAP2-125 (decisions) and the 2026-10-22 chat with Yaakov: region US, plan Hobby (free), capture `full`, everything on the test VM only.

## Global Constraints

- Test VM `proto-agents` only. Never touch the production Hermes host.
- Never print, log, paste or commit a secret. The Langfuse public/secret keys are typed in by Yaakov with `read`; never echo `.env` values (names only, or masked).
- Live clone `/home/hermes/tapdash-agents` is never edited; repo work happens in worktree `/home/hermes/tapdash-agents-wt/langfuse-tracing` (branch `ops/langfuse-tracing`).
- Before any `hermes-gateway` restart: the durable-park ledger (`/home/hermes/.hermes/plugin-data/durable-park/ledger.db`, table `parks`) has no row with status `parked`, `approved` or `executing`.
- Exact settings (non-secret, appended to `~hermes/.hermes/.env`):
  - `HERMES_LANGFUSE_BASE_URL=https://us.cloud.langfuse.com`
  - `HERMES_LANGFUSE_CAPTURE=full`
  - `HERMES_LANGFUSE_ENV=proto-agents`
- durable-park observer hook to add: `hermes_plugins.langfuse.on_pre_tool_call` (format `module.qualname`, durable_park/__init__.py `_callback_name`). Existing entry `hermes_plugins.platforms__raft.adapter._on_pre_tool_call` must stay.
- Install command: `hermes plugins install langfuse --yes-deps --no-enable` (run as `hermes`). Enabling is a separate step after the keys exist.
- Backups get the suffix `.bak-langfuse-<UTC yyyymmddTHHMMSSZ>`; never delete existing backups.
- Do not touch the Mem0 leftovers (`~/.hermes/plugins/mem0`, `~/.hermes/mem0.json`, `MEM0_TELEMETRY` in `.env`); they are Yaakov's decision.

## Task 1: VM prep (no restart, plugin left disabled)

- [ ] Back up `~/.hermes/.env` and `~/.hermes/config.yaml` (cp -p, suffix above).
- [ ] Append the three non-secret settings above to `.env` only if each name is not already present.
- [ ] Run the install command. Expected: plugin in `~/.hermes/plugins/langfuse`, disabled; dependency `langfuse==4.15.2` prepared. If it prompts for keys with no TTY it skips them; that is fine.
- [ ] Verify the installed code equals the reviewed commit: `diff -r` of `~/.hermes/plugins/langfuse` against `/home/ubuntu/review/hop/langfuse` (reviewed checkout at `c2a95963`), ignoring only install metadata files. Any difference in `.py`/`plugin.yaml`/`pyproject.toml` → STOP and report.
- [ ] Add `hermes_plugins.langfuse.on_pre_tool_call` to `plugins.entries.durable-park.settings.observer_hooks` keeping the raft entry; result must be a YAML list of two strings. Confirm by reading it back (`hermes config get` or yaml load).
- [ ] Confirm `hermes plugins list` shows `langfuse` installed and disabled, `durable-park` enabled; `hermes-gateway` NOT restarted; ledger has no active rows.
- [ ] Report: exact commands, outputs (secrets masked), backup file names.

## Task 2: Docs (worktree only)

- [ ] `README.md` "Built on" table: add rows for the Langfuse Hermes plugin (Nous Research, official catalog plugin, v2.0.0 @ `c2a9596`, MIT, link) and Langfuse Cloud (hosted, US region `us.cloud.langfuse.com`, Hobby plan; SDK `langfuse` 4.15.2, MIT, link). Say what each does here: one trace per message turn, model calls and tool calls, full content.
- [ ] `durable-park/docs/runbook.md` "Hooks reviewed so far": add a row for `hermes_plugins.langfuse.on_pre_tool_call` — source: Nous official `langfuse` plugin v2.0.0 @ `c2a9596`; what it does: starts a Langfuse tool observation from a copy of the args, returns `None` on every path, never mutates args; reviewed 2026-10-22; result: listed in `observer_hooks`.
- [ ] `durable-park/docs/runbook.md`: add a section "Tracing (Langfuse)": where traces go (Langfuse Cloud US, project `proto-agents`), settings (the three env names above + the two key names, keys typed in by an admin with `read`), capture mode `full` and what that means (all chat text, tool args/results leave the VM), how to check it works, how to turn it off (`hermes plugins disable langfuse` then restart after the ledger check), how to rotate keys, and that the hook must be re-reviewed when the plugin version changes.
- [ ] `durable-park/docs/architecture.md`: if a diagram or component list names the outside services the agent talks to, add Langfuse Cloud (one-way, traces out). Re-read the diagram after the edit.
- [ ] `durable-park/docs/backlog.md`: add open items — (a) Langfuse Hobby keeps 30 days and retention is not configurable vs the 365-day policy: decide paid plan or self-host before production; (b) Langfuse is a new outside company holding full conversations: decide for production; (c) Mem0 plugin files left installed but disabled (`~/.hermes/plugins/mem0`, `mem0.json`, `MEM0_TELEMETRY`): keep or remove.
- [ ] Run `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD`; commit on `ops/langfuse-tracing` with a conventional message. Do not push.

## Task 3: Enable and verify (after Yaakov's keys are in `.env`)

- [ ] Keys present (names only, prefixes `pk-lf-`/`sk-lf-` checked without printing values).
- [ ] Ledger check; `hermes plugins enable langfuse`; restart `hermes-gateway`.
- [ ] Logs: langfuse plugin loaded with no key warning; durable-park loaded with 2 observer hooks and no "foreign pre_tool_call hook" line; tool policy status ok.
- [ ] Live test with Yaakov: one chat message, one QuickBooks read, one small payment that parks → each appears in Langfuse with full content; the payment still asks a human in withHuman and runs at most once.
- [ ] Write `durable-park/docs/acceptance-2026-10-22-langfuse.md`, commit, push branch, open PR (Yaakov merges).
