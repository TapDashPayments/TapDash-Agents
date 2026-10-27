# Per-person roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each Teams user of the Ops agent gets a role (Admin, Ops, Viewer) held in withHuman, their own Hermes profile and their own withHuman key, so withHuman knows who asked and what they may do.

**Architecture:** Hermes routes each Teams user to their own profile (`gateway.profile_routes`); each profile reaches withHuman with that person's key (durable-park `token_file` and the profile's `DURABLE_PARK_WH_TOKEN`). withHuman role agents (or person agents, decided by the spike) hold tool lists and rules. The bridge accepts the role agents and matches the same person. A root-run `people.sh` adds, moves and removes people.

**Tech Stack:** Python 3 (durable-park plugin, bridge, sweep; pytest `-W error`), bash (deploy scripts), Hermes Agent v0.22.3 (pinned), withHuman (hosted).

**Spec:** `docs/specs/2026-10-26-per-person-roles-design.md` (read it first; this plan argues from it).

## Global Constraints

- Hermes stays pinned at v0.22.3 with updates blocked (`durable-park/deploy/hermes_pin.sh check` must pass after every task). Plugin path only: never edit Hermes's own files.
- Never edit the live clone `/home/hermes/tapdash-agents`; work in a worktree under `/home/hermes/tapdash-agents-wt/` as `hermes`. Push as `hermes`, open PRs with `sudo -u ubuntu -H gh pr create`. Commit identity `-c user.name="Claude (AI agent)" -c user.email=noreply@anthropic.com`. **Merging is Yaakov's job.**
- Never restart the gateway or tunnel with a ledger row `parked`, `approved` or `executing`. Never run `hermes plugins enable|disable` while the gateway runs.
- Never print secrets (`/etc/durable-park/*`, `.env` files, keys, journal lines with tokens). A unique heredoc delimiter for every multi-line command.
- Nothing fails silently: every failure ends in a refusal **and** a Teams alert line (sweep stdout) or a non-zero exit with a message.
- Key naming rule (spec 6.3): `/etc/durable-park/instances/<profile>.token`; the default profile keeps `/etc/durable-park/instance_token`. Key files and profile `.env` files: owner `hermes`, mode 600. Provisioner tokens: root-only (`/etc/durable-park/provisioners/<role>.token`, mode 600, owner root).
- Roles: `admin`, `ops`, `viewer`. Role agents (option R): `ops-admin`, `ops-ops`, `ops-viewer`. Person agents (option P): `ops-<role>--<name>` (or single `-` if the spike shows `--` is refused).
- Teams only. Routes use `platform: teams`, `user_id` (Entra object ID), `guild_id` (TapDash tenant ID).
- Approvals unchanged: escalation path `notify-yaakov-yehuda`.
- Docs are updated in the same PR as the code (`python3 tools/docs_check/check_docs.py --base origin/main --head HEAD` and the Mermaid check pass); plain words, project maps kept current.
- Tests: `cd durable-park && .venv/bin/pytest -q -W error` green (baseline 2409 passed, 47 skipped on the VM).

## Review Focus

1. **One person's approval used by another** with the identical call (same role): must go to a person, never run. Test in Task 3.
2. **A profile whose key file and `.env` key differ**, or two profiles sharing a key: gateway must not start, alert names the profile. Test in Task 5.
3. **A Viewer read at the second (gateway) decision step**: must pass in both spellings, for every server. Test in Task 6.
4. **A wake for a row whose profile has no key file** (person removed while a row was active, or a typo): row not resumed, sweep alert. Test in Task 4.
5. **Admin commands in a Teams group chat or channel** from a non-admin: refused. Test in Task 8 (acceptance) and config check in Task 7.

---

### Task 1: Spike (answers spec section 8, items 1-6; no product code kept)

**Files:**
- Create: `durable-park/docs/spike-2026-10-XX-per-person-roles.md` (findings record, the date of the run)

**Interfaces:**
- Produces: in the record, a table **Decisions for later tasks** with exactly these keys: `OPTION` (`R` expected, `P` only if R fails), `PERSON_FIELD` (`agent_instance_id` of the request records the bridge lists, expected; see Step 2), `SLUG_SEPARATOR` (`--` or `-`; option P only), `PLUGIN_PER_PROFILE` (yes/no), `SESSION_PROFILE_AVAILABLE` (where the plugin reads the profile: `HERMES_SESSION_PROFILE` or other), `INJECT_CROSS_PROFILE` (yes/no), `TEAMS_USER_ID_EXAMPLE` (format only, redacted), `DEFAULT_KEY_READS_OTHER_INSTANCE` (yes/no).

- [ ] **Step 1: Ask Yaakov for a quiet window** (no active ledger rows; Teams users told the bot may restart) and for permission to create two throwaway withHuman agents `spike-a`, `spike-b` (kind `service`).
- [ ] **Step 2: Can the bridge tell instances apart (decides R/P).** Known from withHuman's source (commit e7dfa4c): the webhook body's `agent` has only `slug` and `name` (`internal/webhookdelivery/payload.go`), but each request record in the product listing the bridge already reads has `agent_instance_id` and `agent_instance_name` (`internal/httpapi/representations.go`). Confirm on the hosted service: create `spike-a` with two instances, make one sandbox gateway request from each, list them with the bridge's lister (`bridge/whapi.py`, `RequestLister.list(agent_slug="spike-a")`) and check both records carry their own `agent_instance_id`. If yes, `OPTION=R`, `PERSON_FIELD=agent_instance_id`; else `OPTION=P`.
- [ ] **Step 3: Slug check.** Try `create_agent` with slug `spike--x`; record `SLUG_SEPARATOR`. Archive it.
- [ ] **Step 4: Profile + plugin.** As `hermes`: `hermes profile create spike` (blank, never `--clone`), copy the default profile's lockdown and plugin settings into it, set `plugins.entries.durable-park.settings.token_file` to `/etc/durable-park/instances/spike.token` (a `spike-a` instance key) and the same key as `DURABLE_PARK_WH_TOKEN` in `~/.hermes/profiles/spike/.env`, and `ledger_path` to the shared ledger. Add a route for Yaakov's own Teams user ID → `spike`. Restart (empty ledger). Record: does `plugin_load.json` appear for the spike profile's copy (`PLUGIN_PER_PROFILE`), does the park row store `profile=spike` (`SESSION_PROFILE_AVAILABLE`), does withHuman show the request under the `spike-a` instance.
- [ ] **Step 5: Park → approve → run.** From Teams (routed to `spike`), ask for one QuickBooks sandbox write that parks. Approve. Record whether the default profile's wake reads the decision (`DEFAULT_KEY_READS_OTHER_INSTANCE`), whether the result posts in the spike chat (`INJECT_CROSS_PROFILE`), and the Teams user ID format (`TEAMS_USER_ID_EXAMPLE`, redacted to its shape).
- [ ] **Step 6: Clean up.** Remove the route, `hermes profile delete spike`, disable the instances, archive `spike-a`/`spike-b`, delete `/etc/durable-park/instances/spike.token`, restart (empty ledger), `hermes_pin.sh check` passes, the bot answers Yaakov as before.
- [ ] **Step 7: Commit the record** (docs only) and send Yaakov the decisions table. **Stop if any answer blocks the design** (for example `PLUGIN_PER_PROFILE=no`); tell Yaakov before Task 2.

```bash
git add durable-park/docs/spike-2026-10-XX-per-person-roles.md
git commit -m "docs: per-person roles spike record"
```

### Task 2: Viewer read lists (data, with Yaakov)

**Files:**
- Create: `durable-park/deploy/withhuman/viewer-read-lists.json`
- Test: `durable-park/tests/test_viewer_read_lists.py`

**Interfaces:**
- Produces: JSON `{"<gateway server slug>": ["<tool>", ...], "hermes": ["<tool>", ...]}`; gateway servers: `quickbooks`, `maverick`, `acceptblue`, `authnet`, `supabase-crm-dev`, `monday`, `docusign-demo`. Helper `viewer_names(lists: dict) -> tuple[set[str], set[str]]` in `durable-park/deploy/withhuman/viewer_lists.py` returning (park-time names `mcp__wh__<server_with_underscores>__<tool>` plus Hermes names, gateway names `<tool>` and `<server>__<tool>`).

- [ ] **Step 1: Draft the lists** from each server's tool listing (`list_gateway_server_tools`) and today's read rules (revision 16): only tools that change nothing and tell no one. `hermes` list = `web_search, web_extract, vision_analyze, session_search, todo_list, kanban_list, kanban_show, kanban_attachments, skills_list, skill_view, clarify, tool_search, tool_describe, tool_call` (no `memory`, no `image_generate`). Send the draft to Yaakov as a short table; apply his changes.
- [ ] **Step 2: Write the failing tests**

```python
def test_every_viewer_tool_exists_on_its_server(): ...      # against tests/fixtures/gateway_tools_2026-10-XX.json (a saved listing)
def test_no_viewer_tool_is_a_known_change():                 # names starting create/update/delete/save/send/void/refund/charge/upload/apply/deploy/merge/reset/rebase are refused
    assert not [t for t in all_viewer_tools() if CHANGE_PREFIX.match(t)]
def test_viewer_names_has_both_spellings():
    park, gw = viewer_names({"supabase-crm-dev": ["list_tables"], "hermes": ["clarify"]})
    assert "mcp__wh__supabase_crm_dev__list_tables" in park and "clarify" in park
    assert {"list_tables", "supabase-crm-dev__list_tables"} <= gw
```

- [ ] **Step 3: Run** `.venv/bin/pytest -q -W error tests/test_viewer_read_lists.py` → FAIL. **Step 4: implement** `viewer_lists.py`. **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `feat: viewer read lists (per-person roles)`.

### Task 3: Bridge accepts the role agents and matches the same person

**Files:**
- Modify: `durable-park/bridge/app.py:54, 109-175, 198-247` (expected agents, person field)
- Modify: `durable-park/bridge/verify.py:77-116` (`GatewayCall`, `_is_match`)
- Modify: `durable-park/deploy/durable-park-bridge.service` (env)
- Test: `durable-park/tests/test_bridge.py`, `durable-park/tests/test_bridge_verify.py`
- Docs: `durable-park/docs/architecture.md` (bridge rows), `gateway-setup.md`

**Interfaces:**
- Consumes: Task 1 `OPTION`, `PERSON_FIELD`.
- Produces: `Bridge(expected_agents: frozenset[str], lister, clock, monotonic)`; `GatewayCall(..., agent_slug: str, person: str | None)`; CLI `--expected-agents a,b,c` (env `DURABLE_PARK_EXPECTED_AGENTS`; the old `--expected-agent` still accepted as one-item list); option P: `--expected-agent-prefixes ops-` (env `DURABLE_PARK_EXPECTED_AGENT_PREFIXES`) matched with `startswith`.

- [ ] **Step 1: Write the failing tests**

```python
def test_role_agent_is_accepted(): ...            # slug ops-ops with an approved matching park -> "approve"
def test_unknown_agent_goes_to_a_person(): ...    # slug other-agent -> ("human", None, "agent")
def test_approval_of_another_person_is_not_used():
    # park approved for person A; gateway call identical but person B, same role agent
    assert bridge.process(body_for(person="B"), ...)["outcome"] == "human"
def test_missing_person_field_goes_to_a_person(): ...   # reason "no-person"
def test_old_single_expected_agent_flag_still_works(): ...
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q -W error tests/test_bridge.py tests/test_bridge_verify.py` → FAIL.
- [ ] **Step 3: Implement.** Option R: the person of the gateway call is the `agent_instance_id` of its own request record, found in `neighbours` by `request.id` (the bridge already lists them); not found or empty → `("human", None, "no-person")`. `_is_match` also requires `park["agent_instance_id"] == call.person`. Option P: `call.person = slug`, and the slug check is `startswith` one of the prefixes. Lister calls keep `agent_slug=slug`.
- [ ] **Step 4: Run** the bridge tests and the full suite → PASS.
- [ ] **Step 5: Docs + commit** `feat(bridge): accept role agents, match the same person`.

### Task 4: durable-park per profile (status files, key by profile, wakes)

**Files:**
- Modify: `durable-park/plugin/durable_park/__init__.py:335-461` (new setting `status_dir`; status files there)
- Create: `durable-park/plugin/durable_park/keys.py`
- Modify: `durable-park/plugin/durable_park/wake.py:98-112, 193-260` (client per row)
- Modify: `durable-park/scripts/durable_park_sweep.py` (read every profile's status dir; missing-key alert)
- Modify: `durable-park/deploy/plugin_load_check.sh` (proof path per profile)
- Test: `tests/test_keys.py`, `tests/test_wake.py`, `tests/test_sweep.py`, `tests/test_plugin_load_check.py`
- Docs: `architecture.md` (files table, project map), `runbook.md` (alerts)

**Interfaces:**
- Produces: `keys.token_file_for(profile: str | None, *, default: str = DEFAULT_TOKEN_FILE, instances_dir: str = "/etc/durable-park/instances") -> str` (None, "" or "default" → `default`; else `<instances_dir>/<profile>.token`; a profile name not matching `^[a-z0-9][a-z0-9_-]{0,62}$` raises `ValueError`). Plugin setting `status_dir` (default: ledger's directory, unchanged for the default profile; routed profiles set `<ledger dir>/profiles/<profile>`). Sweep option `--status-root` (default ledger dir) scanning `profiles/*/`.

- [ ] **Step 1: Write the failing tests**

```python
def test_token_file_for_default_profile(): assert token_file_for(None) == DEFAULT_TOKEN_FILE
def test_token_file_for_named_profile(): assert token_file_for("sarah") == "/etc/durable-park/instances/sarah.token"
def test_token_file_for_bad_name_raises(): pytest.raises(ValueError, token_file_for, "../x")
def test_wake_reads_decision_with_row_profile_key(): ...   # row.profile="sarah" -> factory built with sarah's file
def test_wake_row_with_missing_key_is_not_resumed_and_flags(): ...  # writes wake_key_missing marker; returns no inject
def test_status_files_go_to_status_dir(): ...  # breaker, lift, formcheck, policy load, load proof under status_dir
def test_sweep_alerts_for_each_profile_status_dir(): ...   # formcheck failing in profiles/sarah -> alert names "sarah"
def test_sweep_alerts_missing_profile_key(): ...           # "durable-park ALERT: no key file for profile sarah; its approved calls cannot resume"
```

- [ ] **Step 2: Run** them → FAIL. **Step 3: Implement** (`keys.py`; `make_async_client_factory` takes the token file per call; `_process` picks `token_file_for(row.profile)`; status paths from `status_dir`). **Step 4: Run** the full suite → PASS.
- [ ] **Step 5: Docs + commit** `feat: durable-park per-profile keys and status files`.

### Task 5: Start checks for every profile

**Files:**
- Create: `durable-park/deploy/profile_keys_check.py`
- Modify: `durable-park/deploy/tool_policy_check.sh` (run the policy check for each served profile with `HERMES_HOME=<profile home>`; then `profile_keys_check.py`)
- Test: `tests/test_profile_keys_check.py`, `tests/test_tool_policy.py`
- Docs: `runbook.md` (start check), `gateway-setup.md`

**Interfaces:**
- Produces: `profile_keys_check.py check --hermes-home ~/.hermes` → exit 0 ok / 1 problems; function `key_problems(profiles: dict[str, tuple[str | None, str | None]]) -> list[str]` where the tuple is (key file content, `.env` `DURABLE_PARK_WH_TOKEN`), never logging key text (compare SHA-256 only).

- [ ] **Step 1: Write the failing tests**

```python
def test_ok_when_each_profile_has_one_matching_unique_key(): assert key_problems({"a": ("k1","k1"), "b": ("k2","k2")}) == []
def test_missing_key_file(): assert key_problems({"a": (None, "k1")}) == ["profile a: no key file"]
def test_key_file_and_env_differ(): assert key_problems({"a": ("k1","k2")}) == ["profile a: key file and .env key differ"]
def test_shared_key(): assert "profiles a, b share a key" in key_problems({"a": ("k","k"), "b": ("k","k")})
def test_problem_text_never_contains_key(): assert all("k1" not in p for p in key_problems({"a": ("k1","x")}))
def test_policy_check_runs_for_not_set_up_profile(): ...
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**; a failure writes a failing policy report (so every copy refuses calls) and the sweep's existing policy alert names the problem. **Step 4: Run** → PASS.
- [ ] **Step 5: Docs + commit** `feat: start check covers every profile and its key`.

### Task 6: withHuman rules for roles

**Files:**
- Modify: `durable-park/deploy/withhuman/entry-pipeline.json`
- Test: `durable-park/tests/test_pipeline_policy.py`
- Docs: `architecture.md` (rules table), `gateway-setup.md`

**Interfaces:**
- Consumes: Task 2 `viewer_names`, Task 1 `OPTION`.
- Produces: pipeline file for revision 17+: block `viewer-refuse` first (Viewer agent and tool not in either Viewer list → `deny`, reason "Viewers can only look things up."), block `viewer-read` second (→ `approve`), every `/agent/slug equals proto-agents` condition widened to `in [proto-agents, ops-admin, ops-ops]` (option R) or `regex ^(proto-agents|ops-(admin|ops)--.+)$` (option P).

- [ ] **Step 1: Write the failing tests**

```python
def test_viewer_refuse_is_first_block(): assert blocks[0]["id"] == "viewer-refuse"
def test_viewer_change_is_denied_not_asked(): assert run(sample(agent=VIEWER, tool="mcp__wh__quickbooks__create_payment", server="park:quickbooks")) == "deny"
def test_viewer_read_approved_at_both_steps():
    for server, tool in each_viewer_read():          # every server, both spellings
        assert run(park_sample(VIEWER, server, tool)) == "approve"
        assert run(gateway_sample(VIEWER, server, tool)) == "approve"
def test_ops_keeps_todays_outcomes(): ...  # every revision-16 test case replayed with ops-ops gives the same outcome
def test_unknown_agent_still_asks(): ...
```

(`run` is the existing local evaluator in `test_pipeline_policy.py`.)

- [ ] **Step 2: Run** → FAIL. **Step 3: Edit the JSON** (new UUIDs for every node). **Step 4: Run** → PASS; then `preview_pipeline` on 5 samples (Viewer read, Viewer change, Ops read, Ops money, Ops auto-draft) and paste the outcomes in the PR.
- [ ] **Step 5: Docs + commit** `feat(rules): roles in the entry pipeline`. Yaakov saves the file as the new revision in the withHuman app after merge.

### Task 7: `people.sh` and the profile template

**Files:**
- Create: `durable-park/deploy/people.sh`, `durable-park/deploy/people.py` (logic; bash only wraps sudo/user switching), `durable-park/deploy/profile-template/config.yaml`, `durable-park/deploy/profile-template/SOUL.not-set-up.md`
- Test: `tests/test_people.py` (all withHuman and Hermes calls faked)
- Docs: `runbook.md` new section "Add, move or remove a person", README project map

**Interfaces:**
- Consumes: Task 4 `token_file_for`, Task 5 `profile_keys_check.py`, Task 1 `OPTION`.
- Produces: `people.sh add <name> <teams-user-id> <role>`, `move <name> <role>`, `remove <name>`, `list`, `sync-role <role>` (option P), `init` (creates `not-set-up` profile, admin lists, catch-all route once). Exit 0 ok, 1 refused/failed (message says why), 2 usage. Writes: profile `~/.hermes/profiles/<name>/` (blank + template), key file, `.env` key, route in `gateway.profile_routes` (with `guild_id` = `TEAMS_TENANT_ID`), `TEAMS_ALLOWED_USERS`, `platforms.teams.extra.allow_admin_from` and `group_allow_admin_from` (Admins only).

- [ ] **Step 1: Write the failing tests**

```python
def test_add_creates_everything_and_never_prints_the_key(capsys): ...
def test_add_rolls_back_when_a_step_fails(): ...                    # fail at "route": profile, key, instance all gone
def test_add_waits_at_most_10_minutes_then_rolls_back(): ...        # active row stays -> exit 1, rolled back
def test_move_refused_with_active_rows(): assert people.main(["move","sarah","viewer"]) == 1
def test_move_writes_new_key_to_both_places_and_restarts(): ...
def test_remove_refused_with_active_rows(): ...
def test_admin_goes_into_both_admin_lists(): ...
def test_non_admin_is_in_neither_admin_list(): ...
def test_name_must_be_a_safe_profile_name(): assert people.main(["add","../x","id","ops"]) == 2
def test_role_must_be_known(): assert people.main(["add","sarah","id","boss"]) == 2
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (withHuman: `create_agent_instance` via the role's provisioner token; Hermes config via `hermes config set` / `hermes profile create` as `hermes`; restart only through the existing ledger check). **Step 4: Run** → PASS.
- [ ] **Step 5: Docs + commit** `feat: people.sh adds, moves and removes people`.

### Task 8: Roll out and accept

**Files:**
- Create: `durable-park/docs/acceptance-2026-10-XX-per-person-roles.md`
- Modify: `README.md`, `durable-park/README.md` (project maps, "who can do what"), `runbook.md` live settings rows

- [ ] **Step 1: After Yaakov merges Tasks 2-7:** pull the live clone; create the withHuman agents and grants (Ops/Admin = today's `proto-agents` grants; Viewer = Task 2 lists, mode `selected`); store provisioner tokens; Yaakov saves the rules revision; restart the bridge with the new flags.
- [ ] **Step 2:** `people.sh init`; `people.sh add yaakov <his Teams ID> admin`; a test Ops and a test Viewer (second Teams account from Yaakov).
- [ ] **Step 3: Run every acceptance line of spec section 8** in the sandbox and record each result (asked, what withHuman showed, what happened). Include the Review Focus items: identical call by another person; group-chat `/restart` by a non-admin; Viewer read on every server.
- [ ] **Step 4:** `hermes_pin.sh check` passes; full test suite green; docs check passes.
- [ ] **Step 5: Commit** the record, open the PR, comment on Linear TAP2-27 with every PR link and the results.

---

## Self-review notes

- Spec coverage: roles (6.2 → Tasks 2, 6, 8), bridge (6.1 → 3), Hermes config (6.3 → 7), durable-park (6.4 → 4, 5), people.sh (6.5 → 7), failures (7 → 3, 4, 5, 7), tests (8 → 1, 8), order of work (9 → task order).
- Option R/P is the only open fork; Tasks 3, 6 and 7 name both forms.
