# Per-person roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each Teams user of the Ops agent gets a role (Admin, Ops, Viewer) held in withHuman, their own Hermes profile and their own withHuman key, so withHuman knows who asked and what they may do.

**Architecture:** Hermes routes each Teams user to their own profile (`gateway.profile_routes`). Each profile reads its own key file through Hermes's secrets command and durable-park's `token_file`. withHuman role agents hold tool lists and rules. The bridge accepts the role agents and matches the same person (withHuman instance). A root-run `people.sh` adds, moves and removes people.

**Tech Stack:** Python 3 (durable-park plugin, bridge, sweep, deploy helpers; pytest `-W error`), bash (deploy scripts), Hermes Agent v0.22.3 (pinned), withHuman (hosted).

**Spec:** `docs/specs/2026-10-26-per-person-roles-design.md` (read it first; this plan argues from it).

## Global Constraints

- Hermes stays pinned at v0.22.3 with updates blocked (`durable-park/deploy/hermes_pin.sh check` passes after every task). Plugin path only: never edit Hermes's own files.
- Never edit the live clone `/home/hermes/tapdash-agents`; work in a worktree under `/home/hermes/tapdash-agents-wt/` as `hermes`. Push as `hermes`, open PRs with `sudo -u ubuntu -H gh pr create`. Commit identity `-c user.name="Claude (AI agent)" -c user.email=noreply@anthropic.com`. **Merging is Yaakov's job.**
- **Any live change** (withHuman agents, grants, keys, rules; Hermes routes or profiles; a restart) needs Yaakov's OK first and happens in a quiet window he agrees to.
- Never restart the gateway, tunnel or bridge with a ledger row `parked`, `approved` or `executing` (table `parks` in `/home/hermes/.hermes/plugin-data/durable-park/ledger.db`). After a gateway restart, `sudo ss -ltnp | grep -c ':3978 '` prints `1` (TAP2-152). Never run `hermes plugins enable|disable` while the gateway runs.
- Never print secrets (`/etc/durable-park/*`, `.env` files, keys, journal lines with tokens). A key or token shown once by withHuman is written straight to its file, never echoed. A unique heredoc delimiter for every multi-line command.
- Keys: `/etc/durable-park/instances/<profile>.token`, owner `root`, group `hermes`, mode 640 (same as `/etc/durable-park/instance_token` today); directory `/etc/durable-park/instances` root:hermes 750. The default profile keeps `/etc/durable-park/instance_token`. Keys are never copied into `config.yaml` or `.env`. Provisioner tokens `/etc/durable-park/provisioners/<role>.token` and the withHuman admin key `/etc/durable-park/wh_admin_key` are root:root 600.
- Profile names: `^[a-z0-9][a-z0-9_-]{0,62}$`, never `default`. Roles: `admin`, `ops`, `viewer`. Role agents (option R): `ops-admin`, `ops-ops`, `ops-viewer`, kind `service`. Option P (fallback only): `ops-<role>--<name>`.
- Teams only. Routes: `platform: teams`, `user_id` (Entra object ID), `guild_id` (the value of `TEAMS_TENANT_ID`).
- Approvals unchanged: escalation path `notify-yaakov-yehuda`.
- Nothing fails silently: every failure ends in a refusal **and** a Teams alert line (sweep stdout) or a non-zero exit with a message.
- Docs in the same PR as the code (`python3 tools/docs_check/check_docs.py --base origin/main --head HEAD` and `node tools/diagrams/validate_mermaid.mjs <changed .md>` pass); plain words, project maps kept current.
- Tests: `cd durable-park && .venv/bin/pytest -q -W error` green (baseline on the VM: 2409 passed, 47 skipped).

## Review Focus

1. **Tom sends the exact call Sarah got approved** (same role agent, different instance): goes to a person, never runs. Test in Task 3.
2. **A profile with tools but no key file, a secrets command naming another profile, or two profiles sharing a key:** every copy refuses every tool call and the sweep alert names the profile. Test in Task 5.
3. **A Viewer read at both decision steps, on every server, and the same bare tool name on a server the Viewer list does not cover:** first passes, second is refused. Test in Task 6.
4. **An approved row whose profile has no key file** (person removed or typo): not resumed, sweep alert. Test in Task 4.
5. **`/restart` from a non-admin in a Teams group chat or channel:** refused. Config test in Task 7, live check in Task 8.

---

### Task 1: Spike (spec section 8, items 1-8; nothing kept except the record)

**Files:**
- Create: `durable-park/docs/spike-2026-10-27-per-person-roles.md`

**Interfaces:**
- Produces: a table **Decisions for later tasks** with exactly these keys and answers: `OPTION` (`R` or `P`), `INSTANCE_ON_BOTH_RECORDS` (yes/no: the park request and the gateway request each carry the right `agent_instance_id`), `PLUGIN_LOADS_AT_BOOT` (yes/no per routed profile), `SESSION_PROFILE` (does the ledger row get `profile=<name>` from `HERMES_SESSION_PROFILE`), `INJECT_CROSS_PROFILE` (yes/no: the default profile's wake resumes a routed profile's session), `DEFAULT_KEY_READS_OTHER_INSTANCE` (yes/no), `LLM_KEY_SOURCE` (how the routed profile got the model key, and that it got no Teams/Telegram bot keys), `TEAMS_USER_ID_SHAPE` (format only), `SLUG_SEPARATOR` (option P only).

- [ ] **Step 1: Get Yaakov's OK and a quiet window.** Tell him: two throwaway withHuman agents (`spike-a`, kind `service`, two instances), a temporary grant of two QuickBooks sandbox tools to `spike-a` (`get_customer` read, `create_customer` write), his own Teams account routed to a throwaway profile for about 15 minutes, two gateway restarts, and that each spike write will ask him **twice** (once at the park, once at the gateway, because the bridge and rules only trust `proto-agents`); he approves both.
- [ ] **Step 2: Profile.** As `hermes`: `hermes profile create spike` (blank, never `--clone`). Copy into its `config.yaml` from the default profile: `model.*`, `mcp_servers.wh`, the `plugins.entries.durable-park.settings` block with `token_file: /etc/durable-park/instances/spike.token` and the shared `ledger_path`, and `platforms.*.enabled: false` (no own bots). Run `hermes_tool_policy.py apply` with `HERMES_HOME=~/.hermes/profiles/spike` and the Hermes Python from `~/.hermes/installs/*/facts.json`. As root: write a `spike-a` instance key to `/etc/durable-park/instances/spike.token` (root:hermes 640, never printed) and set the profile's `secrets.command.enabled: true`, `secrets.command.override_existing: true` and `secrets.command.command` to a temporary root-owned helper that prints `DURABLE_PARK_WH_TOKEN=` from that file (the switch is off by default: `agent/secret_sources/command.py:165`). Find how the model key reaches the profile (record `LLM_KEY_SOURCE`); give it only the model key.
- [ ] **Step 3: Route and restart.** Add `gateway.profile_routes` entry `{name: spike, platform: teams, user_id: <Yaakov's Teams ID>, guild_id: <TEAMS_TENANT_ID>, profile: spike}` (read his ID from `agent.log` of a message he sends; record its shape only). Ledger check, restart, port check. Record `PLUGIN_LOADS_AT_BOOT` (is there a load proof for `spike` before any message).
- [ ] **Step 4: Read, then write.** From Teams: ask for one customer lookup, then one `create_customer` in the QuickBooks sandbox. Yaakov approves both prompts. If the gateway step times out or the row ends `unknown` before his second click, record that and **do not retry the write**. Record: ledger row `profile`; in withHuman, the park request and the gateway request each show `spike-a` and the **same spike instance** (not `proto-agents`: that would mean the profile's `wh` connection used the default key) (`INSTANCE_ON_BOTH_RECORDS`, from the request listing's `agent_instance_id`); the wake read the decision (`DEFAULT_KEY_READS_OTHER_INSTANCE`); the result posted in his Teams chat (`INJECT_CROSS_PROFILE`). Confirm with `RequestLister.list(agent_slug="spike-a")` (`bridge/whapi.py`) that records carry `agent_instance_id`. `OPTION=R` if all hold, else `P` (then check a `--` slug with `create_agent`, record `SLUG_SEPARATOR`, archive it).
- [ ] **Step 5: Clean up.** Remove the route, ledger check, restart, port check; `hermes profile delete spike`; delete the key file and the temporary helper; remove the grant; disable the instances; archive `spike-a`. Verify: the bot answers Yaakov as before, `hermes_pin.sh check` passes, no `spike` row is active.
- [ ] **Step 6: Commit the record** (`git commit -m "docs: per-person roles spike record"`), send Yaakov the decisions table, and **stop** if an answer blocks the design (for example `INJECT_CROSS_PROFILE=no`).

### Task 2: Viewer read lists (data; Yaakov approves the lists)

**Files:**
- Create: `durable-park/deploy/withhuman/viewer-read-lists.json`, `durable-park/deploy/withhuman/viewer_lists.py`
- Create: `durable-park/tests/fixtures/gateway_tools_2026-10-XX.json` (each server's tool names from `list_gateway_server_tools`, names only)
- Test: `durable-park/tests/test_viewer_read_lists.py`

**Interfaces:**
- Produces: JSON `{"<gateway server slug>": ["<tool>", ...], "hermes": ["<tool>", ...]}` for slugs `quickbooks`, `maverick`, `acceptblue`, `authnet`, `supabase-crm-dev`, `monday`, `docusign-demo`. In `viewer_lists.py`:
  - `@dataclass(frozen=True) class ServerReads: gateway_server: str; park_server: str; park_tools: frozenset[str]; gateway_tools: frozenset[str]`
  - `server_reads(lists: dict) -> list[ServerReads]` — `park_server = "park:" + slug.replace("-", "_")`; `park_tools = {"mcp__wh__" + slug.replace("-", "_") + "__" + t}`; `gateway_tools = {t, slug + "__" + t}`.
  - `hermes_reads(lists: dict) -> frozenset[str]`.
  - `load(path=DEFAULT_PATH) -> dict`.

- [ ] **Step 1: Draft the lists** from the fixture and revision 16's read rules: only tools that change nothing and tell no one. `hermes` = `web_search, web_extract, vision_analyze, session_search, todo_list, kanban_list, kanban_show, kanban_attachments, skills_list, skill_view, clarify, tool_search, tool_describe, tool_call` (no `memory`, no `image_generate`). Send Yaakov a short table per server; apply his changes.
- [ ] **Step 2: Write the failing tests**

```python
def test_every_viewer_tool_exists_on_its_server(): ...      # against the fixture
def test_no_viewer_tool_looks_like_a_change():
    assert not [t for t in all_viewer_tools() if CHANGE.match(t)]   # CHANGE = (^|[_-])(create|update|delete|save|send|void|refund|charge|upload|apply|deploy|merge|reset|rebase|add|remove|cancel|capture|adjust)([_-]|[A-Z]|$)
def test_server_reads_spellings():
    r, = server_reads({"supabase-crm-dev": ["list_tables"]})
    assert r.park_server == "park:supabase_crm_dev" and r.gateway_server == "supabase-crm-dev"
    assert r.park_tools == {"mcp__wh__supabase_crm_dev__list_tables"}
    assert r.gateway_tools == {"list_tables", "supabase-crm-dev__list_tables"}
def test_hermes_reads_excludes_memory_and_image_generate(): ...
```

- [ ] **Step 3: Run** `.venv/bin/pytest -q -W error tests/test_viewer_read_lists.py` → FAIL. **Step 4: implement** `viewer_lists.py`. **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `feat: viewer read lists (per-person roles)`.

### Task 3: Bridge accepts the role agents and matches the same person

**Files:**
- Modify: `durable-park/bridge/app.py` (`DEFAULT_EXPECTED_AGENT` line 54, `Bridge` lines 109-196, `parse_args` 219-247, `main` 260-270)
- Modify: `durable-park/bridge/verify.py` (`GatewayCall` line 77, `_is_match` line 93, `_own_request_verdict` line 155)
- Modify: `durable-park/deploy/durable-park-bridge.service` (env `DURABLE_PARK_EXPECTED_AGENTS`)
- Test: `durable-park/tests/test_bridge.py`, `durable-park/tests/test_bridge_verify.py`
- Docs: `durable-park/docs/architecture.md` (bridge rows, project map), `gateway-setup.md`

**Interfaces:**
- Consumes: Task 1 `OPTION`.
- Produces: `Bridge(expected_agents: frozenset[str], lister, clock=time.time, monotonic=time.monotonic)`; `GatewayCall(request_id, server, tool, arguments, agent_slug, person: str)`; CLI `--expected-agents proto-agents,ops-admin,ops-ops` (env `DURABLE_PARK_EXPECTED_AGENTS`; `--expected-agent X` still accepted as a one-item list; never `ops-viewer`). Option P only: `--expected-agent-pattern '^ops-(admin|ops)--[a-z0-9_-]+$'` (env `DURABLE_PARK_EXPECTED_AGENT_PATTERN`), matched with `re.fullmatch`.

- [ ] **Step 1: Write the failing tests** (use the existing `park_item` / `gateway_item` helpers and give each an `agent_instance_id`; the webhook body has no instance field, withHuman `internal/webhookdelivery/payload.go`)

```python
def test_role_agent_is_accepted(): ...                      # ops-ops, park and gateway records same instance -> approve
def test_viewer_agent_is_never_approved(): ...              # ops-viewer -> ("human", None, "agent")
def test_unknown_agent_goes_to_a_person(): ...
def test_approval_of_another_person_is_not_used():
    # park approved for instance A; the gateway call's own record (found in neighbours by request.id) is instance B
    assert decide(call_for("B"), [park_item(instance="A")], neighbours_with(own_instance="B"), NOW).approve is False
def test_gateway_record_missing_from_listing_goes_to_a_person(): ...   # reason "no-person"
def test_old_single_expected_agent_flag_still_works(): ...
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q -W error tests/test_bridge.py tests/test_bridge_verify.py` → FAIL.
- [ ] **Step 3: Implement.** Option R: `_decide` finds the call's own record in `neighbours` by `request.id` and sets `person = record["agent_instance_id"]`; missing or empty → `("human", None, "no-person")`. `_is_match` also requires `park.get("agent_instance_id") == call.person`. The "already used" conflict check (`_is_conflict`) stays agent-wide on purpose: it can only send more calls to a person, never fewer. Lister calls keep `agent_slug=slug`.
- [ ] **Step 4: Run** the bridge tests and the full suite → PASS.
- [ ] **Step 5: Docs + commit** `feat(bridge): accept role agents, match the same person`.

### Task 4: durable-park per profile (status files, key by profile, wakes, sweep)

**Files:**
- Create: `durable-park/plugin/durable_park/keys.py`
- Modify: `durable-park/plugin/durable_park/__init__.py` (lines 335-461: new setting `status_dir`; all status files under it)
- Modify: `durable-park/plugin/durable_park/wake.py` (`make_async_client_factory` line 98, `_decide_from_provider` 193, `_process` 222)
- Modify: `durable-park/scripts/durable_park_sweep.py` (per-profile status dirs; missing-key check)
- Modify: `durable-park/deploy/plugin_load_check.sh`
- Test: `tests/test_keys.py`, `tests/test_wake.py`, `tests/test_sweep.py`, `tests/test_plugin_load_check.py`
- Docs: `architecture.md` (files table, project map), `runbook.md` (alerts)

**Interfaces:**
- Consumes: Task 1 `PLUGIN_LOADS_AT_BOOT`, `INJECT_CROSS_PROFILE`.
- Produces:
  - `keys.INSTANCES_DIR = "/etc/durable-park/instances"`; `keys.valid_profile(name: str) -> bool` (`^[a-z0-9][a-z0-9_-]{0,62}$` and not `default`); `keys.token_file_for(profile: str | None, *, default: str = DEFAULT_TOKEN_FILE, instances_dir: str = INSTANCES_DIR) -> str` (None, "" or "default" → `default`; invalid → `ValueError`; else `<instances_dir>/<profile>.token`).
  - Plugin setting `status_dir` (default: the ledger's directory, so the default profile is unchanged). Routed profiles set `<ledger dir>/profiles/<profile>`. Every status file (`plugin_load.json`, `tool_policy_load.json`, `tool_policy_status.json` default path, `breaker_status.json`, lift status, `formcheck_status.json`) goes under `status_dir`.
  - Wake: `make_async_client_factory(base_url: str, token_file_for_row: Callable[[str | None], str])`; `_process` reads the decision with `token_file_for(row.profile)`.
  - Sweep: `--status-root` (default: ledger dir); runs each per-status alert for the root and for every `profiles/*/` directory, naming the profile in the line. New check: for each row `parked` or `approved`, if `token_file_for(row.profile)` does not exist → `durable-park ALERT: no key file for profile <p>; <park id> cannot resume. Add the key or remove the person (people.sh).` (once per row, state file `missing_key_alert_state.json`).
  - `plugin_load_check.sh`: if `PLUGIN_LOADS_AT_BOOT=yes`, waits (same 30 s) for a matching proof in the root and in every routed profile's status dir (profile list from `gateway.profile_routes`); if `no`, waits for the default only, and the sweep alerts for a routed profile with no proof 2 minutes after a ledger row of that profile appears.

- [ ] **Step 1: Write the failing tests**

```python
def test_token_file_for_default(): assert token_file_for(None) == DEFAULT_TOKEN_FILE and token_file_for("default") == DEFAULT_TOKEN_FILE
def test_token_file_for_named(): assert token_file_for("sarah") == "/etc/durable-park/instances/sarah.token"
def test_token_file_for_empty_is_default(): assert token_file_for("") == DEFAULT_TOKEN_FILE
@pytest.mark.parametrize("bad", ["../x", "Sarah", "a"*64, "x/y"])
def test_token_file_for_bad_name_raises(bad): pytest.raises(ValueError, token_file_for, bad)
def test_wake_reads_decision_with_row_profile_key(): ...       # row.profile="sarah" -> client built from sarah's file
def test_wake_missing_key_file_does_not_resume(): ...          # no inject; row stays approved
def test_status_files_go_to_status_dir(tmp_path): ...          # every status file lands under status_dir
def test_sweep_alerts_per_profile_status(tmp_path): ...        # failing formcheck in profiles/sarah -> line names "sarah"
def test_sweep_alerts_missing_profile_key_once(tmp_path): ...
def test_runtime_unreadable_key_refuses_and_alerts(): ...      # gate cannot read token -> call refused; breaker/lift status records it; sweep line
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** the full suite → PASS.
- [ ] **Step 5: Docs + commit** `feat: durable-park per-profile keys and status files`.

### Task 5: Secrets helper per profile and start checks for every profile

**Files:**
- Modify: `durable-park/deploy/configure_hermes.sh` (the generated helper `$VENV/libexec/hermes-secrets`, line 78 and its heredoc)
- Create: `durable-park/deploy/profile_keys_check.py`
- Modify: `durable-park/deploy/tool_policy_check.sh`
- Test: `tests/test_configure_script.py`, `tests/test_profile_keys_check.py`, `tests/test_tool_policy.py`
- Docs: `runbook.md` (start check, secrets), `gateway-setup.md`

**Interfaces:**
- Consumes: Task 4 `keys.token_file_for`, `keys.valid_profile`, setting `status_dir`.
- Produces:
  - `hermes-secrets` with no argument: unchanged output. `hermes-secrets <profile>`: validates the name (same rule), prints only `DURABLE_PARK_WH_TOKEN=<first line of /etc/durable-park/instances/<profile>.token>`; missing file → prints nothing, exit 0 (the start check reports it).
  - `profile_keys_check.py check --hermes-home ~/.hermes` → exit 0 ok / 1 problems, problems printed one per line, never key text. Pure function `key_problems(profiles: dict[str, ProfileFacts]) -> list[str]` with `@dataclass ProfileFacts: has_tools: bool; key_sha256: str | None; secrets_command: str | None; secrets_enabled: bool; secrets_override: bool`. `has_tools` = the profile's effective tool list or `mcp_servers` is non-empty after the policy apply. The default profile is checked against its own rule: helper with **no** argument and key file `/etc/durable-park/instance_token`. Exact messages: `profile <p>: no key file`, `profile <p>: secrets command does not name <p>`, `profile <p>: secrets command is off`, `profile <p>: secrets command does not override`, `profiles <a>, <b> share a key`, `profile not-set-up: has tools`.
  - `tool_policy_check.sh`: always uses the Hermes Python from `~/.hermes/installs/*/facts.json`; runs the policy check once per served profile with `HERMES_HOME=<that home>` and `--status-file <that profile's status_dir>/tool_policy_status.json`; then `profile_keys_check.py`; a key problem makes **every** profile's report failing. Still always exits 0 (the gateway starts; every copy refuses calls; the sweep's existing policy alert carries the problem text).

- [ ] **Step 1: Write the failing tests**

```python
# F(has_tools, key_sha256, secrets_command, secrets_enabled=True, secrets_override=True) builds a ProfileFacts.
# The default profile needs the switch on but not the override (today's setting).
def test_ok(): assert key_problems({"a": F(True,"h1","hermes-secrets a"), "b": F(True,"h2","hermes-secrets b")}) == []
def test_missing_key(): assert key_problems({"a": F(True,None,"hermes-secrets a")}) == ["profile a: no key file"]
def test_wrong_secrets_command(): assert key_problems({"a": F(True,"h1","hermes-secrets b")}) == ["profile a: secrets command does not name a"]
def test_shared_key(): assert key_problems({"a": F(True,"h","hermes-secrets a"), "b": F(True,"h","hermes-secrets b")}) == ["profiles a, b share a key"]
def test_not_set_up_must_have_no_tools(): assert key_problems({"not-set-up": F(True,None,None,False,False)}) == ["profile not-set-up: has tools"]
def test_default_profile_uses_helper_without_argument(): assert key_problems({"default": F(True,"h0","/opt/durable-park/libexec/hermes-secrets",True,False)}) == []
def test_secrets_switch_off(): assert "profile a: secrets command is off" in key_problems({"a": F(True,"h1","hermes-secrets a",False,True)})
def test_secrets_helper_profile_mode_prints_only_the_wh_key(tmp_path): ...
def test_secrets_helper_rejects_bad_profile_name(tmp_path): ...
def test_policy_check_writes_a_report_per_profile(tmp_path): ...
def test_key_problem_fails_every_report(tmp_path): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Docs + commit** `feat: per-profile secrets helper and start checks`.

### Task 6: withHuman rules for roles

**Files:**
- Modify: `durable-park/deploy/withhuman/entry-pipeline.json`
- Test: `durable-park/tests/test_pipeline_policy.py`
- Docs: `architecture.md` (rules table), `gateway-setup.md`

**Interfaces:**
- Consumes: Task 2 `server_reads`, `hermes_reads`; Task 1 `OPTION`.
- Produces: a pipeline file for the next revision:
  1. First block `viewer-read` (branch, agent is the Viewer: `/agent/slug equals ops-viewer`; option P `starts_with ops-viewer--`): one inner branch per `ServerReads` (`/request/server equals park_server` and tool `in park_tools`, or `server equals gateway_server` and tool `in gateway_tools`) plus Hermes (`server equals hermes`, tool `in hermes_reads`) → `approve`. A bare tool name is approved only together with its own server.
  2. Second block `viewer-refuse` (branch): agent is the Viewer → `deny`, reason "Viewers can only look things up." Both blocks sit before every other block, `pa-park-money` and `money-server-human` included. No `not` condition is needed (withHuman has none, and the local evaluator supports none).
  3. Every `/agent/slug equals proto-agents` condition becomes `/agent/slug in [proto-agents, ops-admin, ops-ops]` (option P: `regex ^(proto-agents|ops-(admin|ops)--[a-z0-9_-]+)$`). New condition IDs are fresh UUIDs.

- [ ] **Step 1: Write the failing tests** with the existing evaluator `_decide(blocks, agent, server, tool, arguments=None) -> (block_id, outcome)` (`tests/test_pipeline_policy.py:230`):

```python
def test_viewer_blocks_are_first(): assert [b["id"] for b in blocks()[:2]] == ["viewer-read", "viewer-refuse"]
def test_viewer_change_is_denied_not_asked():
    assert _decide(blocks(), "ops-viewer", "park:quickbooks", "mcp__wh__quickbooks__create_payment") == ("viewer-refuse", "deny")
@pytest.mark.parametrize("reads", server_reads(load()))
def test_viewer_reads_pass_both_steps(reads):
    for t in reads.park_tools: assert _decide(blocks(), "ops-viewer", reads.park_server, t)[1] == "approve"
    for t in reads.gateway_tools: assert _decide(blocks(), "ops-viewer", reads.gateway_server, t)[1] == "approve"
def test_viewer_read_name_on_another_server_is_denied():
    assert _decide(blocks(), "ops-viewer", "quickbooks", "list_tables") == ("viewer-refuse", "deny")
def test_viewer_call_without_server_is_denied():
    assert _decide(blocks(), "ops-viewer", None, "anything") == ("viewer-refuse", "deny")
@pytest.mark.parametrize("agent", ["proto-agents", "ops-admin", "ops-ops"])
def test_role_agents_get_revision_16_outcomes(agent): ...   # parametrize the existing DECISIONS table over agent
def test_unknown_agent_still_asks(): ...
```

  Update the existing tests that pin the old shape: `EXPECTED_ORDER` (line 20) gains `viewer-read`, `viewer-refuse` at the front; the shape test near line 280 that asserts the first condition is `/agent/slug equals proto-agents` asserts the new `in` list instead.
- [ ] **Step 2: Run** → FAIL. **Step 3: Edit the JSON.** **Step 4: Run** → PASS.
- [ ] **Step 5: Docs + commit** `feat(rules): roles in the entry pipeline`. In the PR, list the five sample outcomes (Viewer read, Viewer change, Ops read, Ops money, Ops auto-draft) from the local evaluator. Yaakov saves the file as the new revision in the withHuman app (Task 8).

### Task 7: `people.sh`, the profile template and a quiet-restart helper

**Files:**
- Create: `durable-park/deploy/people.sh` (root check, then `python3 -I people.py "$@"`), `durable-park/deploy/people.py`
- Create: `durable-park/deploy/restart_when_quiet.sh`
- Create: `durable-park/deploy/profile-template/settings.json` (the keys a person profile gets, with placeholders), `durable-park/deploy/profile-template/SOUL.not-set-up.md`
- Test: `tests/test_people.py` (every withHuman call, `hermes` CLI call and restart faked)
- Docs: `runbook.md` new section "Add, move or remove a person"; README and `durable-park/README.md` project maps ("who can do what")

**Interfaces:**
- Consumes: Task 4 `keys`, Task 5 helper and `profile_keys_check.py`, Task 1 `LLM_KEY_SOURCE`, `OPTION`.
- Produces:
  - `restart_when_quiet.sh [--wait-minutes N] [--check-only]` (default 10; `--check-only` waits for a quiet moment and exits 0 without restarting): polls the ledger every 15 s; when no row is `parked|approved|executing`, restarts `hermes-gateway`, waits 25 s, checks `is-active` and that exactly one listener holds `:3978`; exit 0 ok, 3 timed out (nothing restarted), 1 restart problem (message says which).
  - `people.sh init`: creates `/etc/durable-park/instances` (root:hermes 750) and `/etc/durable-park/provisioners` (root 700); creates profile `not-set-up` from the template with no tools and `SOUL.not-set-up.md`; adds the Teams catch-all route `{platform: teams, profile: not-set-up}`; creates `platforms.teams.extra.allow_admin_from` and `group_allow_admin_from` if absent, filled with the Teams IDs of people already added as `admin` (empty until the first `add ... admin`; Hermes gating turns on as soon as one admin is listed). Idempotent.
  - `people.sh add <name> <teams-user-id> <role>`: validates (`keys.valid_profile`, role in `admin|ops|viewer`, Teams ID is a UUID); creates the withHuman instance of the role agent with `/etc/durable-park/provisioners/<role>.token`, writing the shown key straight to `/etc/durable-park/instances/<name>.token` (root:hermes 640); `hermes profile create <name>` (blank) as `hermes`; applies the template: `model.*` copied from the default profile, the model key per `LLM_KEY_SOURCE`, `mcp_servers.wh` as in `deploy/hermes_mcp.sh`, `secrets.command.enabled: true`, `secrets.command.override_existing: true`, `secrets.command.command: /opt/durable-park/libexec/hermes-secrets <name>`, `plugins.entries.durable-park` enabled in the profile's config file before the profile is served (never `hermes plugins enable` on the running gateway) with `token_file`, the shared `ledger_path`, `status_dir: <ledger dir>/profiles/<name>`, the `paylink_*` settings copied; `platforms.*.enabled: false`; then `hermes_tool_policy.py apply` with that `HERMES_HOME`; adds the route (`guild_id` from `TEAMS_TENANT_ID`); adds the Teams ID to `TEAMS_ALLOWED_USERS` in the default profile's `~/.hermes/.env` (atomic rewrite of that one line, no other line printed); for `admin`, adds the ID to both admin lists; runs `profile_keys_check.py`; then `restart_when_quiet.sh`. Any failure → undo every earlier step in reverse, exit 1 with the step name.
  - `people.sh move <name> <role>`: exit 1 if the person has a row `parked|approved|executing`. Then **first** waits for a quiet moment (`restart_when_quiet.sh --check-only`, same 10 minutes; on time-out exit 1 with nothing changed). Then: new instance under the new role (key file replaced in place), old instance disabled with `/etc/durable-park/wh_admin_key` (a withHuman org key with agent write rights, made by Yaakov in the app), admin lists updated, restart. If the restart itself fails: exit 1, the person's calls are refused (old key disabled) and the message tells Yaakov to restart by hand. Test that a call with a disabled key is refused **and** produces a sweep line (breaker or refusal).
  - `people.sh remove <name>`: same active-row refusal; disable the instance, remove route, allowlist entry and admin-list entries, delete the key file, `restart_when_quiet.sh`; the profile folder (chat history) is kept and listed for deletion after the 365-day retention.
  - `people.sh list`: name, role, Teams ID (shortened), instance status. `people.sh sync-role <role>`: option P only.
  - Exit codes: 0 ok, 1 refused or failed (message says why), 2 usage.

- [ ] **Step 1: Write the failing tests**

```python
def test_add_creates_everything_and_never_prints_the_key(capsys): ...
def test_add_rolls_back_when_a_step_fails(): ...                  # fail at "route": instance disabled, key file, profile, allowlist entry gone
def test_add_rolls_back_when_restart_times_out(): ...             # restart_when_quiet exits 3 -> everything undone, exit 1
def test_admin_goes_into_both_admin_lists(): ...
def test_non_admin_is_in_neither_admin_list(): ...                 # Review Focus 5 (config side)
def test_move_refused_with_active_rows(): assert people.main(["move", "sarah", "viewer"]) == 1
def test_move_replaces_key_disables_old_and_restarts(): ...
def test_move_changes_nothing_when_no_quiet_moment(): ...       # --check-only exits 3 -> exit 1, key and instance untouched
def test_disabled_key_call_is_refused_and_alerted(): ...        # gate gets 401 -> refused; sweep line present
def test_remove_refused_with_active_rows(): ...
def test_remove_keeps_the_profile_folder(): ...
def test_bad_name_role_or_teams_id_is_usage(): assert people.main(["add", "../x", "id", "ops"]) == 2
def test_init_is_idempotent(): ...
def test_restart_when_quiet_never_restarts_with_active_rows(): ...  # shell test with a fake ledger and fake systemctl
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Docs + commit** `feat: people.sh adds, moves and removes people`.

### Task 8: Roll out and accept (live; every step with Yaakov's OK)

**Files:**
- Create: `durable-park/docs/acceptance-2026-10-XX-per-person-roles.md`
- Modify: `durable-park/docs/runbook.md` (live settings rows, rollback), README project maps

- [ ] **Step 1: Before anything live, write the rollback** in the acceptance record and runbook: activate pipeline revision 16 again; restart the bridge with `--expected-agent proto-agents`; `people.sh remove` each person (or remove routes and restart); disable the role agents' instances. Yaakov reads it.
- [ ] **Step 2: Pull the live clone** after Yaakov merges Tasks 2-7; run `configure_hermes.sh` (new helper) in a quiet window; port check.
- [ ] **Step 3: withHuman (Yaakov's OK):** create `ops-admin`, `ops-ops`, `ops-viewer`, kind `service`. Grants: Admin and Ops copy every `proto-agents` grant (mode and **scopes**: `crm_dev_readonly`, `tapdash_agents`, `demo_user`, `default`); Viewer gets mode `selected` with Task 2's lists and the same scopes. Mint one provisioner token per role, each written straight to `/etc/durable-park/provisioners/<role>.token`. Yaakov makes `/etc/durable-park/wh_admin_key` (an org key with agent write rights), typed in with `read -rs`. Yaakov saves Task 6's pipeline as the new revision.
- [ ] **Step 4: Bridge:** set `DURABLE_PARK_EXPECTED_AGENTS=proto-agents,ops-admin,ops-ops` and restart the bridge in a quiet window.
- [ ] **Step 5: People:** `people.sh init`; `people.sh add yaakov <his Teams ID> admin`; a test Ops and a test Viewer person (second Teams account from Yaakov).
- [ ] **Step 6: Run every acceptance line of spec section 8** in the sandbox and record each (asked, what withHuman showed, what happened), including the Review Focus items: the identical call by another person; `/restart` by the non-admin in a direct chat and in a group chat; one Viewer read per server; an approved row whose key file is missing. The "restart during a pending approval" line needs Yaakov's explicit OK at that moment (it breaks the usual restart rule on purpose).
- [ ] **Step 7:** `hermes_pin.sh check` passes; full suite green; docs and Mermaid checks pass. Commit the record, open the PR, comment on Linear TAP2-27 with every PR link and the results.

---

## Self-review notes

- Spec coverage: roles and Viewer lists (6.2 → Tasks 2, 6, 8), bridge (6.1 → 3), Hermes config and template (6.3 → 7), durable-park per profile (6.4 → 4, 5), keys in one place (6.2 → 5, 7), people.sh including init, move/remove refusal and retention (6.5 → 7), failures (7 → 3, 4, 5, 7), tests (8 → 1, 8), order of work (9).
- Option R/P is the only open fork; Tasks 3, 6 and 7 name both forms.
