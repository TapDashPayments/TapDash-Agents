> **TapDash patched copy.** This is not the catalog plugin. It is a copy of Nous Research's `langfuse` plugin
> (v2.0.0 @ `c2a9596`) with TapDash's changes, listed in [`PATCHES.md`](PATCHES.md). At the deploy it is installed on the
> test VM as a symlink, `~/.hermes/plugins/tapdash-langfuse`, under the plugin name `tapdash-langfuse` (until then the
> VM runs the catalog `langfuse` plugin and there is no such link). The rest of this file is
> upstream's text with TapDash notes; **do not follow its install, update or remove steps** (`hermes plugins install
> langfuse`, `hermes update`, `hermes plugins remove langfuse`). Deploy, turn on, turn off and change it only with
> the [runbook](../../durable-park/docs/runbook.md#tracing-langfuse). **Never enable, disable or reload any plugin
> while the gateway is running**: Hermes reloads every plugin live, and durable-park refuses every tool call until
> the gateway is restarted. Stop the gateway first, as the runbook says.

<p align="center"><img src="../assets/langfuse.jpg" alt="Langfuse: official Hermes Agent plugin" width="100%"></p>

# Langfuse for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly bundled with Hermes core as `observability/langfuse`.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that traces Hermes turns,
LLM calls and tool invocations to [Langfuse](https://langfuse.com), the open-source LLM observability
platform. One span per turn, one generation per API call, one observation per tool call and subagent.
Usage totals, per-type token counts and cost estimates come from Hermes' own pricing, so Langfuse sees
the same breakdown (input / output / cache read / cache write / reasoning tokens) as `hermes logs`.

The plugin fails open: no SDK, no credentials or a transient Langfuse error all turn into a no-op in
the hook (a missing SDK or missing/placeholder keys log one warning). The agent loop is never affected.

**TapDash:** upstream's text here says that `hermes update` migrates existing Langfuse users to the catalog
plugin and that they need to do nothing. That is about the catalog plugin and does not apply to this copy. Nothing
installs, enables or updates `tapdash-langfuse` for you; its code changes only through the repository and the
runbook, and its keys and `HERMES_LANGFUSE_*` settings live in `~/.hermes/.env` on the VM.

## Install

From the Hermes plugin catalog (upstream; **not how TapDash installs it**, see the banner at the top):

```bash
hermes plugins install langfuse
```

Install asks for your project keys and saves them to the profile's `.env`. For a self-hosted Langfuse,
also set the server URL:

```bash
HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...
HERMES_LANGFUSE_SECRET_KEY=sk-lf-...
HERMES_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Keys pasted straight from a template (`pk-lf-...`, `your-key`, ...) are refused with a warning. The
`pk-lf-` / `sk-lf-` prefix is enforced only for Langfuse Cloud (`*.langfuse.com`); on any other
host a key without it logs a one-time warning and is still used, since a custom
Langfuse-compatible endpoint may issue its own keys and authenticates them itself.

Restart Hermes and the next turn ships a trace. Plugins are installed per profile: use
`hermes -p <profile> plugins install langfuse` for another profile. The only Python dependency is the
`langfuse` SDK, which install prepares for you.

## Verify

```bash
hermes plugins list                 # langfuse should show "enabled" (TapDash: "tapdash-langfuse")
hermes chat -q "hello"              # then check Langfuse for a "hermes-turn" trace (TapDash: "<platform>-turn")
```

Generation observations include the Hermes system prompt when the provider uses a separate `system`
param (Anthropic Messages API). Open an **LLM call** child span to inspect `role: system` (truncated
via `HERMES_LANGFUSE_MAX_CHARS`).

Root traces carry `platform:<name>` and `trigger:cron|interactive` tags (cron runs also
`cron-job:<job-id>`), and their metadata names the Hermes `profile` the turn ran for, so scheduled
runs and profiles sharing one project can be filtered apart. Kanban worker turns also carry
`kanban_task_id` / `kanban_run_id`, so a trace joins back to its board card.

## How it works

| Hook | Behaviour |
|---|---|
| `pre_api_request` / `pre_llm_call` | Open (or reuse) a per-turn root span named `<platform>-turn` (`telegram-turn`, `cron-run` for cron, `hermes-turn` when the platform is unknown; TapDash patch). Start a `generation` child observation for this API call with serialized recent messages as input. |
| `post_api_request` / `post_llm_call` | Close the generation, attach `usage_details`, `cost_details`, `finish_reason`, assistant output + tool calls. If no tool calls and non-empty content, close the turn. |
| `api_request_error` | Close the failed generation with `level=ERROR`, status code, retry counters and a capture-mode-scrubbed error message. Non-retryable failures also finish the turn. |
| `pre_tool_call` | Start a `tool` child observation with sanitized `args`. |
| `post_tool_call` | Close the tool observation with sanitized `result`. `read_file` payloads get summarized (head + tail + omitted-line count) so a huge file read stays under `HERMES_LANGFUSE_MAX_CHARS`. |
| `subagent_start` / `subagent_stop` | One observation per delegated subagent under the parent turn. |
| `on_session_end` / `on_session_finalize` | Close any still-open traces for the session, so interrupted or tool-only turns don't dangle. Only process exit flushes. |

Ended observations export through the SDK's bounded background queue: turn completion, API errors
and session rotation (`/new`, `/reset`, expiry) never wait for `flush()`, so a slow or unavailable
Langfuse backend cannot hold up a finished turn (or a cron job past its watchdog). Process exit
still flushes and can wait for the SDK. Traces may appear after the SDK's next batch; a full queue,
failed export or abrupt kill can lose telemetry (there is no offline spool).

Session grouping keys off the Hermes session ID (or task ID for subagents) via
`langfuse.propagate_attributes`, so everything in one `hermes chat` session lives under one Langfuse
session. Under a multiplexed gateway each profile traces with its own keys.
Each turn is its own trace, so `HERMES_LANGFUSE_SAMPLE_RATE` samples individual turns, not whole
conversations.

## Optional tuning

```bash
HERMES_LANGFUSE_ENV=production       # environment tag
HERMES_LANGFUSE_RELEASE=v1.0.0       # release tag
HERMES_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
HERMES_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
HERMES_LANGFUSE_MAX_DEPTH=4          # max payload depth (default: 4)
HERMES_LANGFUSE_CAPTURE=sanitized    # content capture mode (see below)
HERMES_LANGFUSE_DEBUG=true           # verbose plugin logging to agent.log
OTEL_SERVICE_NAME=my-agent           # OTel service.name on traces (default: hermes-agent)
HERMES_LANGFUSE_TAG_PATTERNS='park=\bP-[0-9A-F]{8}\b'   # TapDash patch: tag traces by pattern (see below)
```

The standard SDK names (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`) are
accepted too; the `HERMES_`-prefixed ones win when both are set.

`HERMES_LANGFUSE_MAX_DEPTH` controls nested payload capture in both `sanitized` and `full` modes,
including tool arguments and JSON tool results. The root is depth 0; each dictionary value or array
element adds one level. Values beyond the limit become `<max-depth>`, including scalars. For deeper MCP
responses, set it to a higher non-negative integer (for example, `10`). Unset or blank values default
to `4`; invalid or negative values log a warning and fall back to `4`. `0` keeps only the root level.
Increasing the depth exports more content and may produce larger traces; secret redaction,
string-length limits and the 50-item collection limit remain unchanged. `metadata` mode still omits
content.

### Tag traces by pattern (TapDash patch)

`HERMES_LANGFUSE_TAG_PATTERNS` is not in upstream. Its format is `label=regex;label=regex`. Whenever a regex
matches the turn's original user message (as the person sent it, without the memory, plugin context and
gateway notes that Hermes adds to the model's copy; the model's copy is used only when Hermes passes no original),
a tool call's arguments or a tool's result, the trace gets the tag
`label:value`. The value is capture group 1 if the regex has a group, else the whole match. For example
`inv=invoice (\d+)` on "pay invoice 1016" adds `inv:1016`.

- A label is 1-40 characters of `a-z 0-9 _ -`. A regex cannot contain `;`.
- An entry with no `=`, a bad label or a regex that does not compile logs one warning and is ignored; the
  other entries still work.
- At most 10 pattern tags per trace. A tag over 200 characters or not US-ASCII is skipped.
- Only the first 200 000 characters of each scanned text are scanned.
- With `HERMES_LANGFUSE_CAPTURE=metadata` no pattern tags are added (a tag would carry content). The trace
  `userId` (chat sender id) and the root observation's metadata `user_name` are still sent in that mode.
- Pattern tags are taken from the raw text and are **not** passed through secret redaction, even in `sanitized`
  mode (a tag carries the raw match). Only use patterns that match non-secret ids.
- A pattern runs on up to 200 000 characters of each text, so it must avoid catastrophic backtracking (Python's
  `re` has no timeout, and the user message is scanned while the plugin holds its state lock).
- A tag found in a tool result is added to the trace while the turn is still running.
- Arguments and results that are not plain strings (dicts, lists) are scanned as JSON text. A JSON escape written
  right before a match hides it from `\b`: in `{"a": "x\nP-0A9C15CE"}` the text is `x\nP-0A9C15CE` with a backslash and
  an `n`, so `\bP-...` finds no word boundary and no tag is added. A match after a space, a quote or other punctuation
  is found. A plain string (such as the user message) is scanned as it is, so a real newline is fine there.

## Capture modes

`HERMES_LANGFUSE_CAPTURE` controls how much *content* (prompts, responses, tool arguments/results) is
exported. Structural metadata (IDs, roles, tool names, token usage, cost, timing) is captured in every
mode.

| mode | behavior |
|------|----------|
| `metadata` | No content. Each content field is replaced by a shape/size stub (`{"omitted": true, "type": "text", "chars": N}`). |
| `sanitized` | **(default)** Content is exported after secret-pattern redaction (API keys, tokens, JWTs, private keys, `password=`-style assignments) and truncation. Redaction runs *before* truncation. |
| `full` | Raw content, truncated only. Explicit opt-in: traces will contain whatever passed through the conversation, including injected memory and file contents. |

The active mode is recorded on every trace as `metadata.capture_mode`. `sanitized` is pattern-based
defense in depth, not a DLP guarantee. For personal sessions or shared Langfuse projects, prefer
`metadata`.

## Disable or remove

**TapDash:** use the runbook's [Turn it off](../../durable-park/docs/runbook.md#turn-it-off): it stops the gateway
first, then runs `hermes plugins disable tapdash-langfuse`, then starts the gateway (never disable a plugin with the
gateway running; the plugin's own log hint to run `hermes plugins disable langfuse` is upstream's text and is not the
procedure here, see [`PATCHES.md`](PATCHES.md)). Never run `hermes plugins remove langfuse` on the VM: it would delete the catalog copy that is kept
installed as the rollback target. Upstream's text:

```bash
hermes plugins disable langfuse     # keep it installed, stop tracing
hermes plugins remove langfuse      # uninstall
```

The Langfuse client is cached after the first hook call. If credentials or the SDK are missing, that
decision is cached too, so later hooks return immediately.

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent
git clone https://github.com/NousResearch/hermes-official-plugins
cd hermes-official-plugins/langfuse
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

On a Hermes core that still bundles `observability/langfuse`, an installed copy of this plugin stays
inactive (the bundled copy keeps tracing) so traces are never sent twice.
