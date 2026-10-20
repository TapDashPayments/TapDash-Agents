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
    ("mcp__other__x", False), ("mcp__wh_evil__x", False),
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
    for platform in ("cli", "telegram", "webhook"):
        assert settings[f"platform_toolsets.{platform}"] == ["durable_park", "clarify", mcp_entry]
        assert settings[f"known_plugin_toolsets.{platform}"] == ["a2a", "browser-cdp", "durable_park"]


def test_deploy_scripts_apply_and_check_the_policy():
    configure = (DEPLOY / "configure_hermes.sh").read_text()
    assert 'tool_policy apply || die' in configure
    assert 'policy_report=$(tool_policy check) ||' in configure
    mcp = (DEPLOY / "hermes_mcp.sh").read_text()
    assert mcp.index('tool_policy apply') < mcp.index("systemctl --user restart hermes-gateway") \
        < mcp.index('policy_report=$(tool_policy check)')
