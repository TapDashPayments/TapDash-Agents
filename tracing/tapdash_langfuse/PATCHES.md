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

Nothing under `__init__.py` is changed yet.

## Running the tests

```sh
bash tracing/run_tests.sh            # as the hermes user on the test VM
bash tracing/run_tests.sh -k attribution
```

Expected today: `139 passed, 4 deselected`. The 4 deselected tests (`CrossesHookBoundary`) re-exec the process
through Hermes's dependency installer, which drops the test path, so they cannot run from a checkout.

## Update procedure (taking a newer upstream)

1. Check out upstream at the new commit and diff its `langfuse/` against the previous upstream commit.
2. Copy the new files over this directory (no `__pycache__`, no `.hermes-catalog.json`).
3. Re-apply every change listed above, and re-apply our code patches to `__init__.py`.
4. Update the commit and date in the table, and bump the `+tapdash.N` suffix.
5. Run `bash tracing/run_tests.sh`; every test must pass.
6. Re-review `on_pre_tool_call`: it must stay byte-identical to upstream (it is the one hook that can block or
   observe a tool call; see the tracing section of [the runbook](../../durable-park/docs/runbook.md#tracing-langfuse)).
