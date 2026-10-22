# Langfuse trace gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every trace on proto-agents carries the sender's chat user id, a name that says where it came from, and a `park:P-…` tag that ties a parked payment, its approval turn and its run together.

**Architecture:** Vendor Nous's `langfuse` Hermes plugin (v2.0.0 @ `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d`) into `tracing/tapdash_langfuse/` as plugin `tapdash-langfuse`, patch three things in its `__init__.py`, test on the VM with the Hermes environment's Python, then swap it in for the catalog plugin on the VM. durable-park is not changed.

**Tech Stack:** Python 3.14 (Hermes environment), Langfuse Python SDK 4.15.2 (`propagate_attributes`, OTel span attributes), Hermes v0.22.3 plugin hooks, pytest 9.1.1.

**Spec:** `docs/specs/2026-10-22-langfuse-trace-gaps-design.md` (decisions D1–D8).

## Global Constraints

- Upstream source: `/home/ubuntu/review/hop/langfuse` (checkout of `https://github.com/NousResearch/hermes-official-plugins` at `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d`, subdir `langfuse`). Read it as `ubuntu`; copy with `sudo cp`, then `chown hermes:hermes`.
- Plugin name `tapdash-langfuse`; import namespace `hermes_plugins.tapdash_langfuse`; observer name `hermes_plugins.tapdash_langfuse.on_pre_tool_call`.
- Settings keep their upstream names (`HERMES_LANGFUSE_*`). New: `HERMES_LANGFUSE_TAG_PATTERNS`. Value on proto-agents: `park=\bP-[0-9A-F]{8}\b`.
- Trace names: `<platform>-turn`, `cron-run`, `hermes-turn` (spec D3). Tag format `label:value`; label `[a-z0-9_-]{1,40}`; ≤10 pattern tags per trace; tag ≤200 chars, US-ASCII; scan first 200 000 chars per text; no pattern tags when capture mode is `metadata` (spec D6).
- `userId` = `sender_id` from turn-scoped `pre_llm_call`, only if US-ASCII and 1–200 chars; `user_name` (from `get_session_env("HERMES_SESSION_USER_NAME")`, cut to 200 chars) in root metadata (spec D2).
- `on_pre_tool_call` stays byte-identical to upstream (spec D7).
- Tests: `bash tracing/run_tests.sh` (as hermes) — upstream suite minus `-k "not CrossesHookBoundary"`, with `-W error`.
- Repo rules: AGENTS.md (docs with code in the same commit, `python3 tools/docs_check/check_docs.py --base origin/main --head HEAD`, never a secret, worktree `/home/hermes/tapdash-agents-wt/langfuse-trace-gaps` branch `ops/langfuse-trace-gaps`, never the live clone). Commit as hermes with `-c user.name="Claude (AI agent)" -c user.email=noreply@anthropic.com` and the trailers `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01GyTXUxU9trosdm9HXxBw2d`.

## Review Focus

1. A turn with no sender (cron job, CLI `chat -q`) → no `userId`, no error, trace still named. Test in Task 2.
2. A sender id that is not US-ASCII or is over 200 chars → `userId` left out, not mangled. Test in Task 2.
3. A broken `HERMES_LANGFUSE_TAG_PATTERNS` (no `=`, bad label, bad regex) → one warning per bad entry, valid entries still work, tracing never raises into the agent. Test in Task 3.
4. A huge tool result (MBs) → only the first 200 000 chars scanned; the turn is not slowed by a full scan. Test in Task 3.
5. Two chats tracing at once → a tag found in one chat's tool result never lands on the other chat's trace. Test in Task 3.

---

### Task 1: Vendor the plugin and the test runner

**Files:**
- Create: `tracing/tapdash_langfuse/` (copy of upstream `langfuse/`: `__init__.py`, `plugin.yaml`, `pyproject.toml`, `LICENSE`, `README.md`, `pytest.ini`, `tests/`)
- Create: `tracing/tapdash_langfuse/PATCHES.md`, `tracing/run_tests.sh`
- Modify: `.github/docs-map.json` (new rule)

**Interfaces:**
- Produces: plugin dir `tracing/tapdash_langfuse/`; `bash tracing/run_tests.sh [pytest args...]` runs its tests.

- [ ] **Step 1:** Copy upstream `langfuse/` to `tracing/tapdash_langfuse/` (exclude `__pycache__`). `diff -r` against upstream: identical.
- [ ] **Step 2:** `plugin.yaml`: `name: tapdash-langfuse`, `label: Langfuse (TapDash patched copy)`, `version: 2.0.0+tapdash.1`, add `python_dependencies: ["langfuse==4.15.2"]` (same key durable-park uses); keep hooks/requires_env/optional_env. `pyproject.toml`: `name = "tapdash-langfuse"`, `version = "2.0.0+tapdash.1"`, dependency unchanged. `LICENSE` unchanged (MIT, Nous Research).
- [ ] **Step 3:** `tracing/run_tests.sh` (bash, `set -euo pipefail`, run as hermes): find the Hermes environment Python = the first `/home/hermes/.hermes/installs/*/environments/*/venv/bin/python` that can `import langfuse`; install `pytest==9.1.1` with `UV_NO_CONFIG=1 /home/hermes/.hermes/tools/uv-*/uv pip install --target "${XDG_CACHE_HOME:-$HOME/.cache}/tapdash-langfuse-pytest"` if not present; `cd tracing/tapdash_langfuse`; run `TERM=dumb HERMES_AGENT_REPO=/home/hermes/.hermes/hermes-agent PYTHONPATH=<target>:/home/hermes/.hermes/hermes-agent $PY -m pytest -p no:cacheprovider -q -W error -k "not CrossesHookBoundary" "$@"`. A comment says why the 4 tests are deselected (spec D8).
- [ ] **Step 4:** Run `bash tracing/run_tests.sh`. Expected: `139 passed, 4 deselected`.
- [ ] **Step 5:** `PATCHES.md`: upstream repo, commit, date vendored, "changes against upstream" list (only the manifest/pyproject renames so far), and "update procedure" (re-vendor, re-apply, re-run tests, re-review the hook per the runbook).
- [ ] **Step 6:** `.github/docs-map.json` rule `"Tracing plugin (tapdash-langfuse)"`: code `tracing/**` ; docs `durable-park/docs/runbook.md`, `tracing/tapdash_langfuse/PATCHES.md`. Run the docs check: ok.
- [ ] **Step 7:** Commit `feat(tracing): vendor Nous langfuse plugin as tapdash-langfuse (TAP2-125)`.

### Task 2: userId and trace names

**Files:**
- Modify: `tracing/tapdash_langfuse/__init__.py` (`on_pre_llm_call`, `_start_root_trace`, new helpers)
- Create: `tracing/tapdash_langfuse/tests/test_tapdash_patches.py`
- Modify: `tracing/tapdash_langfuse/PATCHES.md`

**Interfaces:**
- Produces: `_trace_name(platform: str) -> str`; `_ascii_attr(value: Any) -> Optional[str]` (the value unchanged if a US-ASCII string of 1–200 chars with no leading/trailing whitespace, else None; amended after final review); `_remember_sender(turn_key: str, sender_id: str, user_name: str) -> None`; `_pop_sender(turn_key: str) -> tuple[str, str]` (`("", "")` when unknown); module dict `_TURN_SENDERS` (insertion-ordered, max 256 entries, oldest dropped).
- Consumes: Hermes `pre_llm_call` kwargs `turn_id`, `task_id`, `sender_id`; `gateway.session_context.get_session_env`.

- [ ] **Step 1: Write failing tests** in `test_tapdash_patches.py`, using the fake `propagate_attributes` / `_Client` / `_Span` pattern of upstream `TestChildObservationsInheritTraceAttributes` (fake client returns spans; record `start_observation` kwargs and propagate kwargs):
  - `test_trace_name_per_platform`: `telegram`→`telegram-turn`, `Teams`→`teams-turn`, `cron`→`cron-run`, `""`→`hermes-turn`, `"we!rd plat"`→`weirdplat-turn`, `"!!!"`→`hermes-turn`.
  - `test_root_gets_sender_as_user_id_and_name`: monkeypatch `get_session_env` to return `"Yaakov"` for `HERMES_SESSION_USER_NAME`; call `on_pre_llm_call(turn_id="t1", task_id="k", session_id="s", platform="telegram", sender_id="123456789")` then `on_pre_llm_request(turn_id="t1", task_id="k", session_id="s", platform="telegram", api_call_count=1, request_messages=[{"role":"user","content":"hi"}])` → root propagate kwargs have `user_id="123456789"`, `trace_name="telegram-turn"`; root `start_observation` name `telegram-turn`, metadata `user_name == "Yaakov"`; `_TURN_SENDERS` no longer holds `t1`.
  - `test_no_sender_no_user_id`: `platform="cron"`, no `pre_llm_call` → no `user_id` key in propagate kwargs, trace name `cron-run`.
  - `test_bad_sender_left_out`: sender `"Ünïcode"` → no `user_id`; sender `"x"*201` → no `user_id`.
  - `test_sender_map_is_bounded`: 300 `_remember_sender` calls → `len(_TURN_SENDERS) == 256` and the first 44 keys are gone.
- [ ] **Step 2:** `bash tracing/run_tests.sh tests/test_tapdash_patches.py` → the new tests FAIL (missing names / wrong values).
- [ ] **Step 3: Implement.** `on_pre_llm_call` gains `sender_id: str = ""` and calls `_remember_sender(turn_id or task_id, sender_id, user_name)` BEFORE its early return (user name read with `get_session_env` inside `_failsafe`). `_start_root_trace` takes the sender with `_pop_sender(turn_id or task_id)`, names the root and `trace_name` with `_trace_name(platform)`, adds `user_id` to `propagated` only when `_ascii_attr(sender)` is not None, and adds `user_name` (cut to 200) to the root metadata when non-empty. Guard `_TURN_SENDERS` with `_STATE_LOCK`.
- [ ] **Step 4:** `bash tracing/run_tests.sh` → all pass (`144 passed, 4 deselected` or more), output clean. Upstream tests that asserted the literal name `"Hermes turn"` must now be updated to the new name — list each changed upstream assertion in PATCHES.md.
- [ ] **Step 5:** PATCHES.md: add the two changes. Commit `feat(tracing): userId from chat sender and per-platform trace names (TAP2-125)`.

### Task 3: Pattern tags (park linking)

**Files:**
- Modify: `tracing/tapdash_langfuse/__init__.py` (new helpers, `_start_root_trace`, `on_post_tool_call`)
- Modify: `tracing/tapdash_langfuse/tests/test_tapdash_patches.py`, `PATCHES.md`

**Interfaces:**
- Consumes: Task 2's `_start_root_trace` shape; `TraceState.propagated`, `TraceState.root_span`.
- Produces: `_tag_patterns() -> list[tuple[str, re.Pattern]]` (parsed from `HERMES_LANGFUSE_TAG_PATTERNS`, cached on the raw string, one warning per bad entry per distinct raw value); `_pattern_tags(values: Iterable[Any]) -> list[str]` (strings scanned as-is, other values as `json.dumps(value, default=str)`; first 200 000 chars each; empty list in `metadata` capture mode); `_add_trace_tags(state: TraceState, tags: list[str]) -> None` (merge into `state.propagated["tags"]` keeping order and uniqueness, at most 10 pattern tags, then `state.root_span._otel_span.set_attribute("langfuse.trace.tags", list(state.propagated["tags"]))` inside `_failsafe`); constants `_MAX_PATTERN_TAGS = 10`, `_MAX_SCAN_CHARS = 200_000`.

- [ ] **Step 1: Write failing tests:**
  - `test_patterns_parse_and_skip_bad_entries`: raw `"park=\bP-[0-9A-F]{8}\b;nolabel;Bad Label=x;broken=(;inv=invoice (\d+)"` → labels `["park", "inv"]`, three warnings logged (caplog), second call with the same raw logs nothing new.
  - `test_root_tagged_from_user_message`: patterns `park=\bP-[0-9A-F]{8}\b`; root request whose last user message is `"[durable-park] P-420E1D2C was APPROVED in withHuman."` → root propagate `tags` contain `"park:P-420E1D2C"` along with upstream's platform tags.
  - `test_late_tag_from_tool_result_reaches_root_and_later_children`: root opened without a match; `on_pre_tool_call` + `on_post_tool_call(result='PARKED P-0A9C15CE: ...')` → the fake root's `_otel_span.set_attribute` was called with `("langfuse.trace.tags", [..., "park:P-0A9C15CE"])`, and a generation started afterwards is opened inside propagate kwargs whose `tags` include it.
  - `test_tool_args_are_scanned`: `parked_action_execute` args `{"park_id": "P-420E1D2C"}` with result `{"status": "executed", "park_id": "P-420E1D2C"}` → exactly one `park:P-420E1D2C` tag.
  - `test_group_capture`: pattern `inv=invoice (\d+)` on `"pay invoice 1016"` → `inv:1016`.
  - `test_tag_limits`: 15 distinct matches → 10 tags; a 250-char value or a non-ASCII value is skipped.
  - `test_scan_limit`: match placed at character 250 000 of a 300 000-char result → no tag; at 1 000 → tag.
  - `test_metadata_capture_adds_no_tags`: `HERMES_LANGFUSE_CAPTURE=metadata` → no pattern tags anywhere.
  - `test_two_chats_do_not_share_tags`: two roots (different `task_id`/`session_id`), tool result with a park id only in chat A → chat B's root never receives a `set_attribute` call and its propagated tags are unchanged.
  - `test_root_without_otel_span_is_fine`: fake root span lacking `_otel_span` → no exception, propagated tags still updated.
- [ ] **Step 2:** Run the new tests → FAIL.
- [ ] **Step 3: Implement** the helpers; call `_pattern_tags([trace_input content])` in `_start_root_trace` and put the result into the initial `propagated["tags"]` (upstream tags first); in `on_post_tool_call`, after the state lookup, compute `_pattern_tags([args, result])` and call `_add_trace_tags(state, ...)` under `_STATE_LOCK`. Every new code path is wrapped so a failure only logs at debug (upstream's `_failsafe`). Do not touch `on_pre_tool_call`.
- [ ] **Step 4:** `bash tracing/run_tests.sh` → all pass, output clean. Confirm `on_pre_tool_call` is unchanged: `diff <(sed -n '/^def on_pre_tool_call/,/^def /p' upstream/__init__.py) <(sed -n '/^def on_pre_tool_call/,/^def /p' tracing/tapdash_langfuse/__init__.py)` → no output.
- [ ] **Step 5:** PATCHES.md: the setting, its format, limits and the proto-agents value. Commit `feat(tracing): HERMES_LANGFUSE_TAG_PATTERNS tags traces by pattern (TAP2-125)`.

### Task 4: Docs and hook re-review record

**Files:**
- Modify: `durable-park/docs/runbook.md` ("Tracing (Langfuse)": plugin is now `tapdash-langfuse` from `tracing/tapdash_langfuse`, symlinked; settings table gains `HERMES_LANGFUSE_TAG_PATTERNS`; "Find everything about one approval" = filter tag `park:P-…`; userId = chat sender id, `user_name` in metadata; trace names; observer name; "Hooks reviewed so far" gets a row for `hermes_plugins.tapdash_langfuse.on_pre_tool_call` (byte-identical to the reviewed upstream function, reviewed 2026-10-22) and the old row is marked retired; "Turn it on/off" and "When the plugin changes" use the new names and `tracing/run_tests.sh`)
- Modify: `README.md` (Built on: the Langfuse plugin row says "patched copy in `tracing/tapdash_langfuse`, see PATCHES.md"), `durable-park/docs/architecture.md` (plugin name in the map/notes), `durable-park/docs/backlog.md` (gaps 1–3 → "fixed by the patched plugin, live once deployed"; add "run tracing tests in CI"), `CONTRIBUTING.md` only if it lists test commands.
- Create: none.

- [ ] **Step 1:** Make the edits. Re-read every changed diagram/table.
- [ ] **Step 2:** Docs check ok; Mermaid check (same command as earlier docs commits) 0 failed.
- [ ] **Step 3:** Commit `docs(tracing): patched tapdash-langfuse plugin, pattern tags, user ids (TAP2-125)`.

### Task 5: Deploy on proto-agents and verify (after Yaakov merges the PR)

- [ ] **Step 1:** Push branch, open PR (base `main`), Yaakov merges (he also merges #31 first).
- [ ] **Step 2:** (amended after final review: zero-window order) As hermes: `git -C /home/hermes/tapdash-agents pull --ff-only`; back up `.env` and `config.yaml` (suffix `.bak-langfuse-gaps-<UTC>`); append `HERMES_LANGFUSE_TAG_PATTERNS=park=\bP-[0-9A-F]{8}\b` once; add `hermes_plugins.tapdash_langfuse.on_pre_tool_call` to `observer_hooks` NEXT TO the existing entries (three: Raft, `hermes_plugins.langfuse.on_pre_tool_call`, the new one; an entry whose plugin is not loaded is harmless), read back (list of 3 strings); `ln -s /home/hermes/tapdash-agents/tracing/tapdash_langfuse ~/.hermes/plugins/tapdash-langfuse`; `hermes plugins disable langfuse`; `hermes plugins enable tapdash-langfuse`.
- [ ] **Step 3:** (amended after final review) Ledger check (no `parked`/`approved`/`executing`) → restart gateway → verify: active, `durable-park loaded … 3 observer hook(s)`, no `foreign pre_tool_call`, tool policy ok, `hermes plugins list` shows `tapdash-langfuse` enabled and `langfuse` disabled. Then remove `hermes_plugins.langfuse.on_pre_tool_call` from `observer_hooks` (the catalog plugin is no longer loaded), read back (2 strings), ledger check, restart, verify `2 observer hook(s)`. Rollback on any failure: re-add the old name (if removed) before `hermes plugins enable langfuse` and `hermes plugins disable tapdash-langfuse`, ledger check, restart; remove the new name only after it is disabled and a restart happened (ledger check, restart again).
- [ ] **Step 4:** Yaakov sends a Telegram message and a $1 payment that parks; approves it. Query v2 observations with `fields=core,basic,trace_context` and a `traceTags any of ["park:P-…"]` filter → parking turn, approval turn (and the run, same trace as the approval turn) all returned; `userId` = his Telegram id; trace names `telegram-turn`.
- [ ] **Step 5:** Write `durable-park/docs/acceptance-2026-10-22-langfuse-gaps.md`, update backlog gaps to "done", commit, PR.
