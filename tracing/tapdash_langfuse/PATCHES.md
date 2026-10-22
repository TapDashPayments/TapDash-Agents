# tapdash-langfuse: our copy of Nous's Langfuse plugin

This directory is a vendored copy of the official Hermes Langfuse plugin, kept so we can patch it (trace names,
user ids, tags from tool results) without waiting for upstream.

| | |
|---|---|
| Upstream | https://github.com/NousResearch/hermes-official-plugins, subdirectory `langfuse` |
| Upstream commit | `c2a95963c442f3f23e4bc6fe0da898e1dd1fb63d` (plugin v2.0.0) |
| Vendored on | 2026-10-22 |
| Our version | `2.0.0+tapdash.1` |
| License | MIT, Nous Research (`LICENSE`, unchanged) |

## Changes against upstream

Keep this list complete: every difference from upstream goes here, in the commit that makes it.

1. `plugin.yaml`: `name: tapdash-langfuse`, `label: Langfuse (TapDash patched copy)`, `version: 2.0.0+tapdash.1`,
   and a new `python_dependencies: ["langfuse==4.15.2"]` (the same key durable-park uses).
2. `pyproject.toml`: `name = "tapdash-langfuse"`, `version = "2.0.0+tapdash.1"`. The dependency is unchanged.
3. `tests/test_langfuse_plugin.py` line 39: the discovery test looks the plugin up under its new registry key,
   `tapdash-langfuse` (it was `langfuse`). Without this, that one test fails only because of the rename.

4. `__init__.py`: trace userId and per-platform trace names (spec D2, D3).
   - New helpers `_trace_name`, `_ascii_attr`, `_remember_sender`, `_pop_sender`, the dict `_TURN_SENDERS`
     (max 256 entries, oldest dropped) and its own lock `_SENDER_LOCK`. It is not `_STATE_LOCK`: the root trace
     is opened while `_STATE_LOCK` is held, and that lock is not re-entrant.
   - `on_pre_llm_call` takes `sender_id` (and `user_message`, see item 5) and, before its early return, remembers it with the chat user's name
     (`get_session_env("HERMES_SESSION_USER_NAME")`, imported inside the function so the plugin still loads
     without the gateway), keyed by `turn_id or task_id`. A turn with no sender drops any older entry under that
     key, so a stale sender is never applied to a later turn.
   - `_start_root_trace` pops that sender, names the root observation and the propagated `trace_name`
     `<platform>-turn` / `cron-run` / `hermes-turn` (it was always `Hermes turn`), adds `user_id` to the
     propagated attributes only when the sender is a US-ASCII string of 1-200 chars with no leading or trailing
     whitespace (an invalid value is left out, never altered; `_ascii_attr`), and adds `user_name` (cut to 200
     characters; non-ASCII is kept) to the **root observation's** metadata. It is exported as
     `langfuse.observation.metadata.user_name`, not as trace metadata, so a trace-metadata filter on `user_name` does
     not find it (amended after the strong review).
   - Edge case: the sender is popped by the first root a turn opens. If the same turn opens a second root (after a
     non-retryable `api_request_error` that is followed by a fallback request, or an API call after `_finish_trace`),
     that root gets no `userId` and none of the first root's late pattern tags, although both roots share one trace
     id (seed `session::turn_id`). Whether Langfuse then keeps the first root's tags depends on how it merges trace
     tags. Accepted and not fixed: it is rare and affects only attribution, never a tool call.
   - No upstream test asserted the old name `Hermes turn`, so no upstream test assertion changed.
   - New tests: `tests/test_tapdash_patches.py`.

5. `__init__.py`: `HERMES_LANGFUSE_TAG_PATTERNS` tags traces by pattern (spec D4-D6). Not in upstream.
   - Setting: `label=regex;label=regex`. A match in the turn's user message, a tool call's arguments or a tool's
     result adds the trace tag `label:value`; value = group 1 if the regex has a group, else the whole match.
     Label `[a-z0-9_-]{1,40}`. A regex cannot contain `;`. A bad entry (no `=`, bad label, regex that does not
     compile) logs one `logger.warning` per distinct raw setting value and is ignored; parsing is cached on the
     raw string.
   - Limits: at most 10 pattern tags per trace (`_MAX_PATTERN_TAGS`); a tag over 200 chars or not US-ASCII is
     skipped; only the first 200 000 chars of each scanned text (`_MAX_SCAN_CHARS`); non-string values are scanned
     as JSON (`json.dumps` semantics with `default=str`, but not ASCII-escaped, and stopped after 200 000 chars).
     Known limit: a JSON escape glued to an id hides it from `\b` (`{"a": "x\nP-0A9C15CE"}` gives no tag); documented
     in the README and pinned by a test.
   - `HERMES_LANGFUSE_CAPTURE=metadata`: no pattern tags (a tag would carry content). `userId` and `user_name` are
     still sent in that mode.
   - Privacy and safety: pattern tags are taken from the raw text and are not passed through secret redaction, even
     in `sanitized` mode, so only use patterns that match non-secret ids. Patterns run on up to 200 000 chars per
     text and must avoid catastrophic backtracking (`re` has no timeout; the root scan runs under `_STATE_LOCK`).
   - Where scanning happens: `_start_root_trace` scans the turn's ORIGINAL user message (the `user_message` that
     `pre_api_request` / `pre_llm_call` carry, raw, not the captured copy that `HERMES_LANGFUSE_MAX_CHARS`
     shortens; the API copy of the last user message carries injected memory, plugin context and gateway notes, so
     it is used only when `user_message` is absent) and puts the tags into the propagated tags before the root
     opens; `on_post_tool_call` scans `[args, result]` outside `_STATE_LOCK`, then adds the tags under it, if the
     trace is still the live one for its key. `on_pre_tool_call` does no scanning.
   - A late tag joins `state.propagated["tags"]` (children opened later carry it) and is also written to the root
     span with `root_span._otel_span.set_attribute("langfuse.trace.tags", ...)`, because Langfuse keeps tags per
     observation and a trace counts only tags its root carries (spec D5). That uses a private SDK attribute: it is
     wrapped in `_failsafe`, so a future SDK without it only loses late tags.
   - New: helpers `_tag_patterns`, `_parse_tag_patterns`, `_scan_text`, `_pattern_tags`, `_add_trace_tags`, the
     `TraceState.pattern_tags` field, and tests in `tests/test_tapdash_patches.py`, including one that runs the real
     Langfuse SDK with an in-memory span exporter (no network) and checks `user.id`, the trace name, the late tag on
     the root's `langfuse.trace.tags` and the root's `user_name` metadata, so an SDK change that breaks them fails
     the suite.
   - Value on proto-agents: `HERMES_LANGFUSE_TAG_PATTERNS=park=\bP-[0-9A-F]{8}\b` (durable-park park ids).
6. `README.md`: a TapDash banner at the top (patched copy, installed by symlink at the deploy, do not follow upstream's install,
   update or remove steps, never reload plugins with the gateway running); upstream's `hermes update` migration
   paragraph and `hermes plugins remove langfuse` are marked as not applying here; the stale "Hermes turn" text
   now names the per-platform trace names; the tuning section documents `HERMES_LANGFUSE_TAG_PATTERNS`, marked as a
   TapDash patch (with its JSON-escape limit).
7. `__init__.py`: every "LLM call N" generation records which tools were offered to the model (TAP2-125, design D9). Not in
   upstream.
   - `on_pre_llm_request` reads `request["body"]["tools"]` (only when `request` and its `body` are dicts) through the
     new helper `_tools_offered(body)`, called inside `_failsafe("tools offered")`, and adds two keys to the
     generation's metadata: `tools_offered` (sorted list of tool names) and `tools_offered_count` (int).
   - Shapes: OpenAI style `{"type": "function", "function": {"name": ...}}` and Anthropic / flat style
     `{"name": ...}`. Any other entry (not a dict, no usable name, name not a non-empty string) is skipped. If `tools`
     is missing, is not a list or yields no name, both keys are left out.
   - Limits: at most 200 names (`_MAX_TOOLS_OFFERED`), each cut to 64 characters (`_MAX_TOOL_NAME_CHARS`).
   - Order of steps (decision): (1) collect the distinct FULL names; (2) `tools_offered_count` = how many there are,
     before any cut or cap (250 distinct tools give 200 names and count 250); (3) cut each name to 64 characters and
     deduplicate again, so the list never holds a duplicate even when two long names share their first 64
     characters; (4) sort; (5) keep the first 200. So the count can be higher than the list for two reasons: the cap,
     and long names that collapse into one after the cut.
   - Names only, never descriptions or schemas, so it is recorded in every capture mode (including `metadata`).
   - `_start_child_observation`, `on_pre_tool_call` and `register` are unchanged.
   - New: `_tools_offered`, `_MAX_TOOLS_OFFERED`, `_MAX_TOOL_NAME_CHARS` and tests in `tests/test_tools_offered.py`.
   - Docs: README ("What each generation's metadata shows") and the design doc ("Tools offered on each LLM call (D9)").
8. `__init__.py`: `HERMES_LANGFUSE_SCORE_PATTERNS` scores a trace by pattern (design D10). Not in upstream.
   - Setting: `name=regex[=>literal];name=regex`. The name ends at the FIRST `=`; a literal starts at the LAST `=>`.
     Name `[a-z0-9_-]{1,40}` (the tag-label rule). A regex cannot contain `;`. A bad entry (no `=`, bad name, empty
     or non-compiling regex) logs one `logger.warning` naming the setting, once per distinct raw value (parsing is
     cached on the raw string), and is skipped.
   - Matching: `re.search` on the turn's ORIGINAL user message only (the same source and the same 200 000-char cap
     as the pattern tags; the request copy only when Hermes passes no original). Tool arguments and results are not
     scanned. The first matching entry per name wins, so a name is scored at most once per trace
     (`TraceState.scored_names`).
   - Value: the named group `value`, else the `=>literal`, else group 1, else the whole match; stripped and
     lower-cased. An entry whose value is empty or over 100 chars is skipped (a later entry with the same name can
     still win). Comment: every named group except `value`, in the order they appear in the regex, skipping empty
     ones; a group named `comment` is written bare, any other as `<group>: <text>`; joined with `; `, cut to 500
     chars; none when there is no such group.
   - Emission: `_start_root_trace`, after the root span exists, inside `_failsafe`: `client.create_score(name=...,
     value=..., trace_id=..., data_type="CATEGORICAL", comment=..., score_id=f"{trace_id}-{name}")` (signature
     checked against langfuse 4.15.2). A `create_score` that raises is swallowed per score.
   - `HERMES_LANGFUSE_CAPTURE=metadata`: no scores (the generic plugin cannot tell whether a value carries content,
     the same rule as pattern tags).
   - Privacy and safety: values and comments are taken from the raw text and are not passed through secret
     redaction, even in `sanitized` mode, so only use patterns that match non-secret text (a reviewer's note ends up
     in the comment). The regex runs on up to 200 000 chars under `_STATE_LOCK`: avoid catastrophic backtracking.
   - Idempotency limit: Langfuse replaces a re-sent score only when id, name AND UTC date all match. The
     deterministic `score_id` makes a same-day retry idempotent; a retry across UTC midnight would add a second score.
     Not an issue in practice: a score is sent once per trace, at trace start.
   - Sampling: `create_score` follows the trace's sampling (`add_score_task(force_sample=False)` for a valid 32-hex
     trace id): with `HERMES_LANGFUSE_SAMPLE_RATE` below 1 a sampled-out trace gets no score either.
   - New: helpers `_parse_score_patterns`, `_score_patterns`, `_pattern_scores`, the `TraceState.scored_names` field,
     and `tests/test_pattern_scores.py` (which also fails when durable-park's `wake.py` texts drift from the copies it
     holds as fixtures).
   - Value on proto-agents (durable-park's approval decision; see the README for the sample texts):
     `HERMES_LANGFUSE_SCORE_PATTERNS=approval_decision=\A\s*\[durable-park\] (?P<comment>P-[0-9A-F]{8}) (?:\("[^"]*"\) )?was (?P<value>APPROVED|DENIED|CANCELLED) in withHuman(?: by (?P<by>[^:]+?))?(?:: (?P<note>.+?))?\. (?:It did not run\.|Call parked_action_execute );approval_decision=\A\s*\[durable-park\] (?P<comment>P-[0-9A-F]{8}) \("[^"]*"\) got no decision before its deadline\.=>expired`
   - `README.md` documents the setting in the tuning list and under "Score traces by pattern (TapDash patch)".

`on_pre_tool_call` is unchanged from upstream.

Tool failures (addendum item 3, TAP2-125): **verified, not patched.** Upstream's `on_post_tool_call` already maps a
tool result with status `error` to level ERROR and `blocked`/`cancelled` to WARNING, with the error type as
`status_message`. `TestToolObservationKeying.test_failed_mcp_tool_call_ends_observation_at_error_level` drives a failed
MCP call (`mcp_quickbooks_create_bill`, `McpError`) through `on_pre_tool_call` and `on_post_tool_call` and pins that
result. It passes on the unpatched code, so no code change was made.

## Known upstream hints left unchanged

- `__init__.py` (the missing-credentials warning in `_build_client`, about line 281) logs "... or disable the plugin
  (`hermes plugins disable langfuse`) to silence this warning". On proto-agents the plugin is `tapdash-langfuse`, not
  `langfuse`, and a plugin must never be disabled with the gateway running: stop the gateway first, as in the runbook's
  [Turn it off](../../durable-park/docs/runbook.md#turn-it-off) (warning:
  [never change plugins while the gateway runs](../../durable-park/docs/runbook.md#never-change-plugins-while-the-gateway-runs)).
  The message is left as upstream wrote it so the copy stays easy to diff.

## Running the tests

```sh
bash tracing/run_tests.sh            # as the hermes user on the test VM
bash tracing/run_tests.sh -k attribution   # extra pytest arguments are added; the deselection below always stays
```

Expected today: `245 passed, 4 deselected`. The 4 deselected tests (`CrossesHookBoundary`) re-exec the process
through Hermes's dependency installer, which would run against the gateway's shared Hermes environment and drops the
test path, so they cannot run from a checkout. The script deselects them with `--deselect` by node id, so a `-k` or
`-m` of your own cannot bring them back. It runs under the Python of the Hermes environment the running gateway
uses (read from `/proc/<gateway pid>/maps`; it changes nothing) and, if that cannot be told, the first Hermes
environment that can `import langfuse`; it prints which one it used.

## Update procedure (taking a newer upstream)

1. Check out upstream at the new commit and diff its `langfuse/` against the previous upstream commit.
2. Copy the new files over this directory (no `__pycache__`, no `.hermes-catalog.json`).
3. Re-apply every change listed above, and re-apply our code patches to `__init__.py`.
4. Update the commit and date in the table, and bump the `+tapdash.N` suffix.
5. Run `bash tracing/run_tests.sh`; every test must pass.
6. Re-review `on_pre_tool_call`: it must stay byte-identical to upstream (it is the one hook that can block or
   observe a tool call; see the tracing section of [the runbook](../../durable-park/docs/runbook.md#tracing-langfuse)).
