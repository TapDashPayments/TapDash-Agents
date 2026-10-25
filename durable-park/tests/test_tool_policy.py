"""deploy/hermes_tool_policy.py: the agent must never get local file/command access (ledger bypass)."""

import importlib.util
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("hermes_tool_policy", DEPLOY / "hermes_tool_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

# Every Hermes v0.22.2 toolset whose tools can read/write local files, run commands or code, control a
# browser/desktop, spawn agents or schedule runs. Adding one of these to the allowlist (or dropping it
# from the denylist) must fail this test.
DANGEROUS = {"terminal", "file", "code_execution", "delegation", "cronjob", "skills", "browser", "browser-cdp",
             "browser-use", "computer_use", "kanban", "memory", "session_search", "vision", "video",
             "connections", "catalog", "desktop_ui", "a2a", "web", "image_gen", "tts", "todo"}


def test_allowlist_is_exactly_the_plugin_tool_and_clarify():
    assert policy.ALLOWED_TOOLSETS == ["durable_park", "clarify"]
    assert policy.MCP_SERVER == "wh"
    assert policy.ALLOWED_TOOL_NAMES == {"parked_action_execute", "clarify"}


@pytest.mark.parametrize("toolset", sorted(DANGEROUS))
def test_dangerous_toolset_is_disabled_and_never_allowed(toolset):
    assert toolset in policy.FORBIDDEN_TOOLSETS
    assert toolset not in policy.ALLOWED_TOOLSETS


def test_no_toolset_is_both_allowed_and_forbidden():
    assert not set(policy.ALLOWED_TOOLSETS) & set(policy.FORBIDDEN_TOOLSETS)
    assert policy.MCP_SERVER not in policy.FORBIDDEN_TOOLSETS


@pytest.mark.parametrize("name, allowed", [
    ("parked_action_execute", True), ("clarify", True), ("mcp__wh__quickbooks__get_company_info", True),
    ("terminal", False), ("read_file", False), ("write_file", False), ("patch", False), ("execute_code", False),
    ("delegate_task", False), ("cronjob_manage", False), ("process_manage", False), ("skill_manage", False),
    ("browser_navigate", False), ("memory", False), ("tool_call", False), ("tool_search", False),
    ("mcp__other__x", False), ("mcp__wh_evil__x", False), ("mcp__wh__withhuman__get_agent", False),
    ("mcp__wh__docusign__createEnvelope", False), ("mcp__wh__quickbooksX__create_invoice", False),
    ("mcp__wh__maverick__maverick_read", True), ("mcp__wh__maverick__maverick_refund_transaction", True),
    ("mcp__wh__maverickX__a", False), ("mcp__wh__maverick_x__a", False), ("mcp__wh__mav__maverick_read", False),
    ("mcp__wh__supabase_crm_dev__list_tables", True), ("mcp__wh__monday__get_board_info", True),
    ("mcp__wh__docusign_demo__getEnvelope", True), ("mcp__wh__supabase_crm__x", False),
    ("mcp__wh__docusign__getEnvelope", False), ("mcp__wh__mondayX__a", False),
])
def test_is_allowed(name, allowed):
    assert policy.is_allowed(name) is allowed


@pytest.mark.parametrize("wh, mcp_entry", [(True, "wh"), (False, "no_mcp")])
def test_settings_cover_every_platform_and_turn_off_tool_search(monkeypatch, wh, mcp_entry):
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli", "telegram", "webhook"])
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: ["a2a", "browser-cdp", "durable_park"])
    monkeypatch.setattr(policy, "_wh_configured", lambda: wh)
    settings = policy.desired_settings()
    assert settings["agent.disabled_toolsets"] == policy.FORBIDDEN_TOOLSETS
    assert settings["tools.tool_search.enabled"] == "off"
    assert settings["gateway.strict"] is True and settings["gateway.trust_recent_files"] is False
    for platform in ("cli", "telegram", "webhook"):
        assert settings[f"platform_toolsets.{platform}"] == ["durable_park", "clarify", mcp_entry]
        assert settings[f"known_plugin_toolsets.{platform}"] == ["a2a", "browser-cdp", "durable_park"]


def _checks(text):
    starts, at = [], 0
    while (at := text.find("policy_report=$(tool_policy check)", at)) != -1:
        starts.append(at)
        at += 1
    return starts


def test_configure_checks_the_policy_before_and_after_the_restart():
    text = (DEPLOY / "configure_hermes.sh").read_text()
    apply_at = text.index("tool_policy apply")
    restarts = [text.index('systemctl restart "user@$HERMES_UID.service"'),
                text.index("user_systemctl restart hermes-gateway")]
    checks = _checks(text)
    assert len(checks) == 2
    assert apply_at < checks[0] < min(restarts) and checks[1] > max(restarts)
    pre = text[checks[0]:checks[0] + 300]
    assert "die" in pre and "nothing restarted" in pre


def test_hermes_mcp_checks_before_and_after_and_scopes_wh_tools():
    text = (DEPLOY / "hermes_mcp.sh").read_text()
    restart = text.index("as_hermes systemctl --user restart hermes-gateway")
    checks = _checks(text)
    assert len(checks) == 2 and text.index("tool_policy apply") < checks[0] < restart < checks[1]
    assert "ALLOWED_PREFIX_RE='^mcp__wh__(quickbooks|maverick|supabase_crm_dev|monday|docusign_demo)__'" in text
    scope = text.index('other_tools=$(grep -vE -- "${ALLOWED_PREFIX_RE}"')
    assert restart < scope and "die" in text[scope:scope + 400]
    assert "ALLOWED_PREFIX=" not in text  # the old QuickBooks-only prefix is gone


def test_hermes_mcp_scope_regex_flags_every_other_wh_tool():
    import re
    import subprocess

    text = (DEPLOY / "hermes_mcp.sh").read_text()
    regex = re.search(r"^ALLOWED_PREFIX_RE='([^']+)'", text, re.M).group(1)
    names = ["mcp__wh__quickbooks__get_company_info", "mcp__wh__maverick__maverick_read",
             "mcp__wh__supabase_crm_dev__list_tables", "mcp__wh__monday__get_board_info",
             "mcp__wh__docusign_demo__getEnvelope",
             "mcp__wh__maverickX__a", "mcp__wh__withhuman__get_agent", "mcp__wh__quickbooks_maverick__x",
             "mcp__wh__docusign__createEnvelope", "mcp__wh__monday_x__a"]
    other = subprocess.run(["grep", "-vE", "--", regex], input="\n".join(names) + "\n", capture_output=True,
                           text=True).stdout.split()
    assert other == names[5:]


def test_platform_keys_come_from_every_hermes_source():
    source = (DEPLOY / "hermes_tool_policy.py").read_text()
    for needle in ("hermes_cli.platforms import PLATFORMS", "for p in Platform", "_scan_bundled_plugin_platforms",
                   "platform_registry.registered_names", '"acp", "tui", "desktop"',
                   '("platform_toolsets", "known_plugin_toolsets")'):
        assert needle in source


def test_postures_cover_sessions_that_bypass_platform_toolsets():
    assert policy.POSTURES["all-toolsets"] is None
    assert "coding" in policy.POSTURES["coding-posture"] and policy.POSTURES["acp-default"] == ["hermes-acp"]


# --- Task 11: one root-owned list of allowed toolsets ----------------------------------------------------

import json  # noqa: E402


def _stub_sources(monkeypatch, wh=True):
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli", "telegram"])
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: ["a2a", "durable_park"])
    monkeypatch.setattr(policy, "_wh_configured", lambda: wh)


def test_read_allowed(tmp_path):
    assert policy.ALLOW_FILE == "/etc/durable-park/allowed_toolsets"
    assert policy.read_allowed(str(tmp_path / "missing")) == []
    f = tmp_path / "allowed"
    f.write_text("# opened for the finance agent\n\nweb\n  vision  # trailing\nweb\n")
    assert policy.read_allowed(str(f)) == ["web", "vision"]
    f.write_text("web\nnot_a_toolset\n")
    with pytest.raises(ValueError, match="unknown toolset: not_a_toolset"):
        policy.read_allowed(str(f))
    for name in sorted(policy.PHASE2_TOOLSETS):
        f.write_text(f"{name}\n")
        with pytest.raises(ValueError, match=f"{name} stays off until Clean Setup Phase 2"):
            policy.read_allowed(str(f))


def test_phase2_toolsets_are_forbidden_names():
    # skills left Phase 2 (Maverick plan Task 5): reading skills is allowed while the pipeline denies skill_manage
    assert policy.PHASE2_TOOLSETS == {"terminal", "file", "code_execution", "delegation", "cronjob", "computer_use"}
    assert policy.PHASE2_TOOLSETS <= set(policy.FORBIDDEN_TOOLSETS)


def test_desired_settings_opens_only_the_allowed(monkeypatch):
    _stub_sources(monkeypatch)
    settings = policy.desired_settings(["web", "vision"])
    disabled = settings["agent.disabled_toolsets"]
    assert "web" not in disabled and "vision" not in disabled and "terminal" in disabled
    assert disabled == [t for t in policy.FORBIDDEN_TOOLSETS if t not in ("web", "vision")]
    for platform in ("cli", "telegram"):
        assert settings[f"platform_toolsets.{platform}"] == ["durable_park", "clarify", "web", "vision", "wh"]


def test_desired_settings_empty_equals_todays_lockdown(monkeypatch):
    _stub_sources(monkeypatch, wh=False)
    today = {"agent.disabled_toolsets": policy.FORBIDDEN_TOOLSETS, "tools.tool_search.enabled": "off",
             "gateway.strict": True, "gateway.trust_recent_files": False}
    for platform in ("cli", "telegram"):
        today[f"platform_toolsets.{platform}"] = ["durable_park", "clarify", "no_mcp"]
        today[f"known_plugin_toolsets.{platform}"] = ["a2a", "durable_park"]
    assert policy.desired_settings([]) == today


def test_is_allowed_extra_names():
    assert policy.is_allowed("web_search") is False
    assert policy.is_allowed("web_search", {"web_search"}) is True
    assert policy.is_allowed("terminal", {"web_search"}) is False


def test_allowed_tool_names_uses_hermes_definitions(monkeypatch):
    import sys
    import types

    calls = []
    fake = types.ModuleType("model_tools")
    fake.get_tool_definitions = lambda **kw: calls.append(kw) or [{"function": {"name": "web_search"}}]
    monkeypatch.setitem(sys.modules, "model_tools", fake)
    assert policy.allowed_tool_names(["web"]) == {"web_search"}
    assert calls[0]["enabled_toolsets"] == ["web"]


def test_cli_usage_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(policy, "_bootstrap", lambda: (_ for _ in ()).throw(AssertionError("must not start Hermes")))
    bad = tmp_path / "bad"
    bad.write_text("terminal\n")
    for argv in ([], ["bogus"], ["apply", "--status-file", str(tmp_path / "s.json")],
                 ["check", "--allow-file", str(bad)], ["apply", "--allow-file", str(bad)]):
        assert policy.main(argv) == 2, argv
    assert "Phase 2" in capsys.readouterr().err
    assert not (tmp_path / "s.json").exists()


def test_invalid_allow_file_with_a_status_file_writes_the_reason_then_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: (_ for _ in ()).throw(AssertionError("must not start Hermes")))
    bad = tmp_path / "bad"
    bad.write_text("terminal\n")
    out = tmp_path / "status.json"
    out.write_text('{"ok": true, "checked_at": "2099-01-01T00:00:00Z", "problems": [], "allowed": []}')
    assert policy.main(["check", "--allow-file", str(bad), "--status-file", str(out)]) == 2
    status = json.loads(out.read_text())
    assert status["ok"] is False and status["allowed"] == [] and status["checked_at"].endswith("Z")
    assert len(status["problems"]) == 1 and status["problems"][0].startswith("invalid allow file: ")
    assert "Phase 2" in status["problems"][0]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".status")] == []  # no temp file left


def test_check_writes_status_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: set())
    monkeypatch.setattr(policy, "effective_tools", lambda platform, enabled="config", assemble=True: ["parked_action_execute", "terminal"])
    monkeypatch.setattr(policy, "media_check", lambda: [])
    allow = tmp_path / "allowed"
    allow.write_text("web\n")
    out = tmp_path / "status.json"
    assert policy.main(["check", "--allow-file", str(allow), "--status-file", str(out)]) == 1
    status = json.loads(out.read_text())
    assert status["ok"] is False and status["allowed"] == ["web"]
    assert any("terminal" in p for p in status["problems"])
    assert status["checked_at"].endswith("Z") and len(status["checked_at"]) == 20
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("status")] == ["status.json"]


def test_check_writes_status_file_when_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: {"web_search"})
    monkeypatch.setattr(policy, "effective_tools", lambda platform, enabled="config", assemble=True: ["parked_action_execute", "web_search"])
    monkeypatch.setattr(policy, "media_check", lambda: [])
    allow = tmp_path / "allowed"
    allow.write_text("web\n")
    out = tmp_path / "status.json"
    assert policy.main(["check", "--allow-file", str(allow), "--status-file", str(out)]) == 0
    assert json.loads(out.read_text())["ok"] is True


def test_check_writes_status_file_when_the_check_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = tmp_path / "status.json"
    assert policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)]) == 1
    status = json.loads(out.read_text())
    assert status["ok"] is False and "boom" in status["problems"][0]


# --- Task 11 review fixes ----------------------------------------------------------------------------------

def test_main_passes_the_allowed_list_from_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    seen = {}
    monkeypatch.setattr(policy, "apply", lambda allowed=None: seen.setdefault("apply", allowed) and 0)
    monkeypatch.setattr(policy, "check", lambda allowed=None, problems=None: seen.setdefault("check", allowed) and 0)
    allow = tmp_path / "allowed"
    allow.write_text("web\n")
    assert policy.main(["apply", "--allow-file", str(allow)]) == 0
    assert policy.main(["check", "--allow-file", str(allow)]) == 0
    assert seen == {"apply": ["web"], "check": ["web"]}


def _ok_check_stubs(monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "check", lambda allowed=None, problems=None: 0)


def test_status_file_is_failing_before_check_runs(tmp_path, monkeypatch):
    out = tmp_path / "status.json"
    out.write_text(json.dumps({"ok": True, "checked_at": "2026-01-01T00:00:00Z", "problems": [], "allowed": []}))
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    seen = {}

    def fake_check(allowed=None, problems=None):
        seen["during"] = json.loads(out.read_text())
        raise RuntimeError("boom")

    monkeypatch.setattr(policy, "check", fake_check)
    assert policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)]) == 1
    assert seen["during"]["ok"] is False and seen["during"]["problems"] == ["check did not finish"]
    assert json.loads(out.read_text())["ok"] is False


def test_failed_final_status_write_leaves_the_prewritten_failure(tmp_path, monkeypatch):
    _ok_check_stubs(monkeypatch)
    out = tmp_path / "status.json"
    out.write_text(json.dumps({"ok": True, "checked_at": "x", "problems": [], "allowed": []}))
    real = policy.write_status
    calls = []

    def flaky(path, ok, problems, allowed):
        calls.append(ok)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, ok, problems, allowed)

    monkeypatch.setattr(policy, "write_status", flaky)
    with pytest.raises(OSError):
        policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)])
    status = json.loads(out.read_text())
    assert status["ok"] is False and status["problems"] == ["check did not finish"]


def test_bootstrap_failure_still_leaves_a_failing_status_file(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: (_ for _ in ()).throw(ImportError("no hermes")))
    out = tmp_path / "status.json"
    assert policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)]) == 1
    status = json.loads(out.read_text())
    assert status["ok"] is False and "no hermes" in status["problems"][0]


def test_interrupt_leaves_a_failing_status_file_and_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "check", lambda allowed=None, problems=None: (_ for _ in ()).throw(KeyboardInterrupt()))
    out = tmp_path / "status.json"
    with pytest.raises(KeyboardInterrupt):
        policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)])
    assert json.loads(out.read_text())["ok"] is False


def test_allowed_tool_names_empty_asks_hermes_for_nothing_selected(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("model_tools")
    fake.get_tool_definitions = lambda **kw: [] if kw["enabled_toolsets"] == [] else [{"function": {"name": "x"}}]
    monkeypatch.setitem(sys.modules, "model_tools", fake)
    assert policy.allowed_tool_names([]) == set()


def test_unwritable_status_directory_is_a_failed_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(policy, "_bootstrap", lambda: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(policy, "check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not check")))
    out = tmp_path / "no-such-dir" / "status.json"
    assert policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)]) == 1
    assert "status file" in capsys.readouterr().err
    assert not out.exists()


# --- Hermes "Tool Search" (tools.tool_search.enabled: on) -------------------------------------------------
#
# With tool search on, Hermes hides MCP and plugin tools (parked_action_execute, mcp__wh__*) behind three
# synthesized bridge tools. Run against the real Hermes v0.22.3 on the proto VM (2026-10-21), the unchanged
# check reported clarify + tool_call/tool_describe/tool_search: three LEAKs and parked_action_execute MISSING,
# and it could no longer see the hidden tools at all. The check now judges both lists.

from durable_park import park_hook  # noqa: E402

CATALOG = ["clarify", "mcp__wh__quickbooks__get_company_info", "parked_action_execute"]
BRIDGE = ["tool_call", "tool_describe", "tool_search"]


def test_the_bridge_names_match_the_plugin_constants():
    # Both sides are local constants: they are pinned to the names Hermes really synthesizes by
    # tests/test_hermes_integration.py (skipped where Hermes is not importable).
    assert policy.BRIDGE_TOOL_NAMES == frozenset({"tool_search", "tool_describe", "tool_call"})
    assert policy.BRIDGE_TOOL_NAMES == frozenset(park_hook.BRIDGE_TOOLS)
    for name in policy.BRIDGE_TOOL_NAMES:  # strict by default: nothing is allowed by name, bridge or not
        assert policy.is_allowed(name) is False


def test_tool_search_on_passes_with_exactly_the_three_bridge_tools():
    assert policy.tool_problems(CATALOG, ["clarify", *BRIDGE]) == []


def test_tool_search_off_passes_as_before():
    assert policy.tool_problems(CATALOG, CATALOG) == []


@pytest.mark.parametrize("leak", ["terminal", "read_file", "execute_code", "mcp__wh__withhuman__get_agent",
                                  "mcp__other__x", "manage_connections"])
def test_a_forbidden_tool_is_a_leak_in_the_catalog_even_when_the_bridge_hides_it(leak):
    # hidden behind the bridge the model's tool list looks clean, but tool_call can reach it
    assert policy.tool_problems([*CATALOG, leak], ["clarify", *BRIDGE]) == [f"LEAK {leak}"]


def test_a_forbidden_tool_is_a_leak_in_what_the_model_sees():
    assert policy.tool_problems(CATALOG, ["clarify", *BRIDGE, "terminal"]) == ["LEAK terminal"]
    assert policy.tool_problems(CATALOG, [*CATALOG, "terminal"]) == ["LEAK terminal"]


def test_a_bridge_name_in_the_catalog_is_a_leak():
    # the bridge tools are synthesized, never registered: a registered tool with such a name is hostile
    assert policy.tool_problems([*CATALOG, "tool_call"], ["clarify", *BRIDGE]) == ["LEAK tool_call"]


@pytest.mark.parametrize("partial", [["tool_search"], ["tool_search", "tool_describe"], ["tool_call"]])
def test_a_partial_bridge_is_a_problem(partial):
    problems = policy.tool_problems(CATALOG, ["clarify", *partial])
    missing = sorted(policy.BRIDGE_TOOL_NAMES - set(partial))
    assert f"BRIDGE incomplete: {', '.join(missing)} missing" in problems


def test_the_executor_hidden_behind_a_bridge_that_is_not_there_is_missing():
    # parked_action_execute is a plugin tool: deferred when tool search is on. Hidden and no bridge = unreachable.
    assert policy.tool_problems(["clarify", "parked_action_execute"], ["clarify"]) == ["MISSING parked_action_execute"]
    assert policy.tool_problems(["clarify"], ["clarify", *BRIDGE]) == ["MISSING parked_action_execute"]


def test_opened_toolsets_are_allowed_in_the_catalog_and_the_visible_list():
    assert policy.tool_problems([*CATALOG, "web_search"], ["clarify", *BRIDGE, "web_search"],
                                {"web_search"}) == []
    assert policy.tool_problems([*CATALOG, "web_search"], ["clarify", *BRIDGE]) == ["LEAK web_search"]


def _check_with(monkeypatch, tmp_path, catalog, visible, extra=frozenset()):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli", "telegram"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: set(extra))
    monkeypatch.setattr(policy, "effective_tools",
                        lambda platform, enabled="config", assemble=True: list(visible if assemble else catalog))
    monkeypatch.setattr(policy, "media_check", lambda: [])
    out = tmp_path / "status.json"
    rc = policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)])
    return rc, json.loads(out.read_text())


def test_check_passes_with_the_bridge_tools_present(tmp_path, monkeypatch, capsys):
    rc, status = _check_with(monkeypatch, tmp_path, CATALOG, ["clarify", *BRIDGE])
    assert rc == 0 and status["ok"] is True and status["problems"] == []
    assert "tool policy: ok" in capsys.readouterr().out


def test_check_fails_when_terminal_is_present_with_the_bridge_tools(tmp_path, monkeypatch):
    rc, status = _check_with(monkeypatch, tmp_path, [*CATALOG, "terminal"], ["clarify", *BRIDGE])
    assert rc == 1 and status["ok"] is False
    assert [p for p in status["problems"] if p.startswith(("cli:", "telegram:"))] == [
        "cli: LEAK terminal", "telegram: LEAK terminal"]
    rc, status = _check_with(monkeypatch, tmp_path, CATALOG, ["clarify", *BRIDGE, "terminal"])
    assert rc == 1 and "cli: LEAK terminal" in status["problems"]


def test_check_judges_the_postures_the_same_way(tmp_path, monkeypatch):
    seen = []

    def tools(platform, enabled="config", assemble=True):
        seen.append((platform, enabled, assemble))
        if enabled == "config":
            return list(CATALOG if not assemble else ["clarify", *BRIDGE])
        return [*CATALOG, "terminal"] if not assemble else ["clarify", *BRIDGE]

    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: set())
    monkeypatch.setattr(policy, "effective_tools", tools)
    monkeypatch.setattr(policy, "media_check", lambda: [])
    out = tmp_path / "status.json"
    assert policy.main(["check", "--allow-file", str(tmp_path / "none"), "--status-file", str(out)]) == 1
    problems = json.loads(out.read_text())["problems"]
    assert "all-toolsets@cli: LEAK terminal" in problems and not any(p.startswith("cli:") for p in problems)
    assert ("cli", None, False) in seen and ("cli", None, True) in seen


class _Defs:
    """A stand-in for Hermes's get_tool_definitions that records how it was called."""

    def __init__(self, raw, assembled):
        self.raw, self.assembled, self.calls = raw, assembled, []

    def __call__(self, **kw):
        self.calls.append(kw)
        names = self.raw if kw.get("skip_tool_search_assembly") else self.assembled
        return [{"function": {"name": n}} for n in names]


def _fake_hermes(monkeypatch, defs):
    import sys
    import types

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("model_tools", get_tool_definitions=defs)
    module("hermes_cli")
    module("hermes_cli.config", load_config=lambda: {"agent": {"disabled_toolsets": ["terminal"]}})
    module("hermes_cli.tools_config", _get_platform_tools=lambda config, platform: {"durable_park", "clarify"})
    module("toolsets", session_disabled_toolsets=lambda disabled, platform: disabled,
           session_platform_tool_drops=lambda platform: set())
    module("agent")
    module("agent.skill_utils", parse_config_string_list=lambda value: list(value or []))


def test_effective_tools_assembled_is_what_the_model_sees_and_raw_is_the_catalog(monkeypatch):
    defs = _Defs(["clarify", "parked_action_execute"], ["clarify", "tool_call", "tool_describe", "tool_search"])
    _fake_hermes(monkeypatch, defs)
    assert policy.effective_tools("telegram") == ["clarify", "tool_call", "tool_describe", "tool_search"]
    assert policy.effective_tools("telegram", assemble=False) == ["clarify", "parked_action_execute"]
    assert [bool(c.get("skip_tool_search_assembly")) for c in defs.calls] == [False, True]
    assert all(c["enabled_toolsets"] == ["clarify", "durable_park"] for c in defs.calls)
    assert all(c["disabled_toolsets"] == ["terminal"] for c in defs.calls)


def test_allowed_tool_names_reads_the_catalog_not_the_bridge(monkeypatch):
    defs = _Defs(["todo_list", "web_search"], ["web_search", "tool_call"])  # todo_list is deferred by default
    _fake_hermes(monkeypatch, defs)
    assert policy.allowed_tool_names(["todo", "web"]) == {"todo_list", "web_search"}
    assert defs.calls[0]["skip_tool_search_assembly"] is True


@pytest.mark.parametrize("current, wanted", [("off", "off"), (None, "off"), ("auto", "off"), ("garbage", "off"),
                                               ("on", "on"), (True, "on"), ("true", "on"), ("ON", "on")])
def test_tool_search_setting_keeps_an_admin_s_on_and_turns_everything_else_off(current, wanted):
    raw = {} if current is None else {"tools": {"tool_search": {"enabled": current}}}
    assert policy.tool_search_setting(raw) == wanted


def test_desired_settings_writes_the_requested_tool_search_state(monkeypatch):
    _stub_sources(monkeypatch)
    assert policy.desired_settings([])["tools.tool_search.enabled"] == "off"
    assert policy.desired_settings([], tool_search="on")["tools.tool_search.enabled"] == "on"
    assert policy.desired_settings([], tool_search="off")["tools.tool_search.enabled"] == "off"
    with pytest.raises(ValueError):
        policy.desired_settings([], tool_search="auto")


def _fake_config_module(monkeypatch, raw):
    import sys
    import types

    writes = []
    mod = types.ModuleType("hermes_cli.config")
    mod.get_config_path = lambda: "config.yaml"
    mod.require_readable_config_before_write = lambda path: raw
    mod.set_config_value = lambda key, value, force=False: writes.append((key, value))
    mod.unset_config_value = lambda key: writes.append((key, None))
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", mod)
    return writes


@pytest.mark.parametrize("current, written", [("on", None), ("off", None), ("auto", "off"), (True, "on")])
def test_apply_leaves_an_admin_s_tool_search_on_alone(monkeypatch, current, written):
    _stub_sources(monkeypatch)
    raw = {"tools": {"tool_search": {"enabled": current}}, "gateway": {"strict": True, "trust_recent_files": False}}
    writes = _fake_config_module(monkeypatch, raw)
    assert policy.apply([]) == 0
    tool_search_writes = [w for w in writes if w[0] == "tools.tool_search.enabled"]
    assert tool_search_writes == ([] if written is None else [("tools.tool_search.enabled", written)])


# --- Task 5 (Maverick tunnel plan): skill reading under the lockdown ---------------------------------------------


def test_skills_toolset_can_be_opened(tmp_path):
    f = tmp_path / "allowed"
    f.write_text("skills\n")
    assert policy.read_allowed(str(f)) == ["skills"]


def test_the_other_phase2_toolsets_stay_refused(tmp_path):
    f = tmp_path / "allowed"
    for name in ("terminal", "file", "code_execution", "delegation", "cronjob", "computer_use"):
        f.write_text(f"{name}\n")
        with pytest.raises(ValueError, match=f"{name} stays off until Clean Setup Phase 2"):
            policy.read_allowed(str(f))


def test_skill_manage_is_always_denied():
    # the real pipeline file: skill_manage reaches the deny block before anything could approve it
    assert policy.PIPELINE_FILE.endswith("withhuman/entry-pipeline.json")
    assert policy.skill_manage_problem(policy.PIPELINE_FILE) is None
    blocks = json.loads(Path(policy.PIPELINE_FILE).read_text())["blocks"]
    deny = next(b for b in blocks if b["id"] == "pa-hermes-local-deny")
    tools = [c for c in deny["config"]["when"]["all"] if c["field"] == "/request/tool"][0]["value"]
    assert "skill_manage" in tools
    assert deny["config"]["blocks"][0]["config"]["outcome"] == "deny"


def _pipeline(tmp_path, blocks):
    p = tmp_path / "entry-pipeline.json"
    p.write_text(json.dumps({"default_escalation_path": "x", "blocks": blocks}))
    return str(p)


def _branch(bid, conditions, outcome, kind="all"):
    return {"id": bid, "type": "branch", "config": {"when": {"id": "c", kind: conditions}, "blocks": [
        {"id": bid + "-o", "type": "outcome", "config": {"outcome": outcome}}]}}


def _cond(field, operator, value):
    return {"id": "x", "field": field, "operator": operator, "value": value}


def test_skill_manage_problem_flags_a_missing_or_late_deny(tmp_path):
    deny = _branch("deny", [_cond("/request/server", "equals", "hermes"),
                            _cond("/request/tool", "in", ["terminal", "skill_manage"])], "deny")
    approve_all_hermes = _branch("approve", [_cond("/request/server", "equals", "hermes")], "approve")
    approve_reads = _branch("reads", [_cond("/request/server", "equals", "hermes"),
                                      _cond("/request/tool", "in", ["skill_view", "skills_list"])], "approve")
    assert policy.skill_manage_problem(_pipeline(tmp_path, [approve_reads, deny])) is None
    assert "approve" in policy.skill_manage_problem(_pipeline(tmp_path, [approve_all_hermes, deny]))
    assert "no block" in policy.skill_manage_problem(_pipeline(tmp_path, [approve_reads]))
    human = _branch("h", [_cond("/request/tool", "regex", "^skill_.*$")], "human")
    assert "human" in policy.skill_manage_problem(_pipeline(tmp_path, [human, deny]))
    # a condition the checker does not understand counts as "may match" (fail closed)
    odd = _branch("odd", [_cond("/request/tool", "starts_with", "zzz")], "approve")
    assert "approve" in policy.skill_manage_problem(_pipeline(tmp_path, [odd, deny]))
    assert "cannot read" in policy.skill_manage_problem(str(tmp_path / "missing.json"))


def test_check_refuses_skills_without_the_deny(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: {"skills_list", "skill_view"})
    monkeypatch.setattr(policy, "effective_tools",
                        lambda platform, enabled="config", assemble=True: ["parked_action_execute", "skill_view"])
    monkeypatch.setattr(policy, "media_check", lambda: [])
    approve_all_hermes = _branch("approve", [_cond("/request/server", "equals", "hermes")], "approve")
    monkeypatch.setattr(policy, "PIPELINE_FILE", _pipeline(tmp_path, [approve_all_hermes]))
    problems = []
    assert policy.check(["skills"], problems) == 1
    assert any("skill_manage" in p for p in problems)
    # the same check passes with the real pipeline file, and without `skills` the pipeline is not consulted
    monkeypatch.setattr(policy, "PIPELINE_FILE", str(DEPLOY / "withhuman" / "entry-pipeline.json"))
    assert policy.check(["skills"], []) == 0
    monkeypatch.setattr(policy, "PIPELINE_FILE", str(tmp_path / "missing.json"))
    assert policy.check([], []) == 0
