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
    apply_at = text.index("tool_policy apply || die")
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
    assert "ALLOWED_PREFIX=mcp__wh__quickbooks__" in text
    scope = text.index('other_tools=$(grep -v -- "^${ALLOWED_PREFIX}"')
    assert restart < scope and "die" in text[scope:scope + 400]


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
    assert policy.PHASE2_TOOLSETS == {"terminal", "file", "code_execution", "delegation", "cronjob", "skills",
                                      "computer_use"}
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


def test_check_writes_status_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_bootstrap", lambda: None)
    monkeypatch.setattr(policy, "_plugin_toolsets", lambda: [])
    monkeypatch.setattr(policy, "_platforms", lambda: ["cli"])
    monkeypatch.setattr(policy, "allowed_tool_names", lambda allowed: set())
    monkeypatch.setattr(policy, "effective_tools", lambda platform, enabled="config": ["parked_action_execute", "terminal"])
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
    monkeypatch.setattr(policy, "effective_tools", lambda platform, enabled="config": ["parked_action_execute", "web_search"])
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
