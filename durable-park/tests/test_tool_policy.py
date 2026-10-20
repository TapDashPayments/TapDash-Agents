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
