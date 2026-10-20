"""durable-park tool policy for the proto VM's Hermes (v0.22.2): the model gets NO local file/command access.

Why: the agent's tools run as user ``hermes``, which owns the ledger and can read instance_token. A
prompt-injected agent with a terminal or file tool could forge an ``executing`` row and get the bridge to
approve a call. v1 closes this by giving the model only:

* ``durable_park``  - ``parked_action_execute`` (runs an approved, parked call; the point of the plugin)
* ``clarify``       - asks the user a question in the chat; no local access (tools/clarify_tool.py:111-130)
* MCP server ``wh`` - the withHuman gateway tools (QuickBooks only, deploy/hermes_mcp.sh); every call goes
                      through the approval pipeline

Run inside Hermes's own Python environment, as the hermes user (deploy/configure_hermes.sh does that):

    <hermes env python> -I deploy/hermes_tool_policy.py apply    # write the policy with Hermes's config writer
    <hermes env python> -I deploy/hermes_tool_policy.py check    # print each platform's tools; exit 1 on a leak

Policy, three layers (Hermes v0.22.2, ~/.hermes/hermes-agent):
1. ``platform_toolsets.<platform> = [durable_park, clarify, wh]`` for EVERY platform Hermes knows
   (hermes_cli/tools_config.py:63 PLATFORMS): an explicit saved list replaces the full ``hermes-*``
   composites (toolsets.py ``_HERMES_CORE_TOOLS``), and naming ``wh`` makes the MCP server list an
   allowlist (tools_config.py:682-694 ``_merge_mcp_servers``). Until ``mcp_servers.wh`` exists
   (deploy/hermes_mcp.sh) the list carries the ``no_mcp`` sentinel instead: no MCP server at all.
2. ``known_plugin_toolsets.<platform>`` = every plugin toolset: a plugin toolset that is neither listed nor
   "known" is switched on by default (tools_config.py:510-517), e.g. ``browser-cdp`` / ``browser-use``.
3. ``agent.disabled_toolsets`` = every other toolset. It is subtracted LAST at tool granularity
   (model_tools.py:316-338 ``_select_tool_names``; tools_config.py:626-632), so it also covers adapter
   per-source overrides (gateway/run_turn.py:2386-2409), cron agents (cron/scheduler.py:414-435), the
   tool_search bridge (model_tools.py:701-745) and delegate children (agent/agent_init.py:1114-1116).
The model can only call names in its tool list (agent/turn_tool_round.py:107-127).
4. ``tools.tool_search.enabled: off`` (hermes_cli/config_defaults.py:1991; read at tools/tool_search.py:112-121): otherwise Hermes hides plugin/MCP tools (``parked_action_execute``,
   ``mcp__wh__*``) behind the tool_search/tool_describe/tool_call bridge whenever any are deferrable
   (tools/tool_search.py:198-201, 335-370), so the model would not see ``parked_action_execute`` by the name
   the wake message tells it to call. With only a handful of tools there is nothing to save.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

HERMES_AGENT = os.path.expanduser("~hermes/.hermes/hermes-agent")

ALLOWED_TOOLSETS = ["durable_park", "clarify"]
MCP_SERVER = "wh"
ALLOWED_TOOL_NAMES = {"parked_action_execute", "clarify"}
ALLOWED_TOOL_PREFIXES = (f"mcp__{MCP_SERVER}__",)
REQUIRED_TOOL = "parked_action_execute"

# Toolsets that give local file / command / code / process access (or reach other machines/agents).
# Hermes v0.22.2 handlers: terminal + process_manage (tools/terminal_tool.py:1631, tools/process_registry.py:2785);
# read_file/write_file/patch/search_files (tools/file_tools.py:1395-1420); execute_code
# (tools/code_execution_tool.py:966); delegate_task (tools/delegate_tool.py:795, a subagent with its own
# tools); cronjob_manage (tools/cronjob_tools.py:1193, schedules agent runs); skill_manage / skill_view
# (tools/skill_manager_tool.py:926 writes skill files, tools/skills_tool.py:725 reads them); browser_*
# (local browser, can open file:// and loopback), browser_exec / browser_cdp (tools/browser_use_cli.py:904,
# tools/browser_cdp_tool.py:397); computer_use (tools/computer_use_tool.py:14, desktop control);
# vision_analyze / video_analyze read local paths (tools/vision_tools.py:890,975,1070); memory writes
# ~/.hermes/memories (tools/memory_tool.py:477); session_search reads the session store
# (tools/session_search_tool.py:787); kanban_* attach local files and spawn workers; manage_connections /
# manage_catalog (tools/connectors/tool.py:154,172: remote connectors, plugin installs); desktop_ui
# read_terminal (tools/read_terminal_tool.py:76); a2a_* (other agents); image_generate / text_to_speech
# write files; web_* (outbound fetches; not needed); todo (not needed).
FORBIDDEN_TOOLSETS = [
    "a2a", "bot_room", "browser", "browser-cdp", "browser-use", "catalog", "code_execution", "computer_use",
    "connections", "context_engine", "cronjob", "delegation", "desktop_ui", "discord", "discord_admin",
    "feishu_doc", "feishu_drive", "file", "image_gen", "kanban", "memory", "onboarding", "project", "search",
    "session_search", "skills", "terminal", "todo", "tts", "video", "video_gen", "vision", "web", "x_search",
    "yuanbao",
]


def _bootstrap() -> None:
    if HERMES_AGENT not in sys.path:
        sys.path.insert(0, HERMES_AGENT)
    import logging

    import hermes_bootstrap  # noqa: F401  (same import the hermes launcher does)

    logging.disable(logging.WARNING)


def _platforms() -> list[str]:
    from hermes_cli.tools_config import PLATFORMS

    return sorted(PLATFORMS)


def _plugin_toolsets() -> list[str]:
    import model_tools  # noqa: F401  (imports the tool modules; some register toolsets such as browser-cdp)
    from hermes_cli.plugins import discover_plugins
    from toolsets import _get_plugin_toolset_names

    discover_plugins()
    return sorted(_get_plugin_toolset_names())


def _wh_configured() -> bool:
    from hermes_cli.config import load_config

    return isinstance((load_config().get("mcp_servers") or {}).get(MCP_SERVER), dict)


def desired_settings() -> dict:
    plugin_toolsets = _plugin_toolsets()
    mcp = MCP_SERVER if _wh_configured() else "no_mcp"
    settings: dict = {"agent.disabled_toolsets": FORBIDDEN_TOOLSETS, "tools.tool_search.enabled": "off"}
    for platform in _platforms():
        settings[f"platform_toolsets.{platform}"] = [*ALLOWED_TOOLSETS, mcp]
        settings[f"known_plugin_toolsets.{platform}"] = plugin_toolsets
    return settings


def _get(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def apply() -> int:
    """Write every setting that differs, through Hermes's own ``hermes config set`` implementation."""
    from hermes_cli.config import (get_config_path, require_readable_config_before_write, set_config_value,
                                   unset_config_value)

    raw = require_readable_config_before_write(get_config_path()) or {}  # the raw user config, as config set reads it
    changed = 0
    if isinstance(raw.get("tool_search"), dict) and set(raw["tool_search"]) == {"enabled"}:
        # an earlier version of this script wrote the key at the wrong (top-level) path; Hermes never reads it
        with redirect_stdout(io.StringIO()):
            unset_config_value("tool_search")
        changed += 1
    for key, value in desired_settings().items():
        if _get(raw, key) == value:
            continue
        with redirect_stdout(io.StringIO()):
            set_config_value(key, value if isinstance(value, str) else json.dumps(value), force=True)
        changed += 1
    print(f"tool policy: {changed} setting(s) written")
    return 0


def effective_tools(platform: str) -> list[str]:
    """The tool names a session on *platform* gets, through the same calls the gateway / CLI make:
    gateway/run_turn.py:2404-2409 -> agent/agent_init.py:1114-1121 (session gating, definitions, drops)."""
    import model_tools
    from agent.skill_utils import parse_config_string_list
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import session_disabled_toolsets, session_platform_tool_drops

    config = load_config()
    enabled = sorted(_get_platform_tools(config, platform))
    disabled = parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets")) or None
    disabled = session_disabled_toolsets(disabled, platform)
    defs = model_tools.get_tool_definitions(enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True)
    drops = session_platform_tool_drops(platform)
    return sorted(d["function"]["name"] for d in defs if d["function"]["name"] not in drops)


def is_allowed(name: str) -> bool:
    return name in ALLOWED_TOOL_NAMES or name.startswith(ALLOWED_TOOL_PREFIXES)


def check() -> int:
    _plugin_toolsets()  # discovery registers plugin tools, as at agent start
    bad = 0
    for platform in _platforms():
        names = effective_tools(platform)
        leaks = [n for n in names if not is_allowed(n)]
        missing = [] if REQUIRED_TOOL in names else [REQUIRED_TOOL]
        bad += len(leaks) + len(missing)
        print(f"{platform}: {', '.join(names) or '(none)'}" + (f"  LEAK: {', '.join(leaks)}" if leaks else "")
              + (f"  MISSING: {REQUIRED_TOOL}" if missing else ""))
    if bad:
        print(f"tool policy: FAILED, {bad} problem(s): forbidden tool reachable or {REQUIRED_TOOL} missing",
              file=sys.stderr)
        return 1
    print("tool policy: ok (only durable_park, clarify and mcp__wh__* tools)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("apply", "check"):
        print("usage: hermes_tool_policy.py apply|check", file=sys.stderr)
        return 2
    _bootstrap()
    return apply() if argv[0] == "apply" else check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
