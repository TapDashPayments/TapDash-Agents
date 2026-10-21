"""durable-park tool policy for the proto VM's Hermes (v0.22.2): the model gets NO local file/command access.

Why: the agent's tools run as user ``hermes``, which owns the ledger and can read instance_token. A
prompt-injected agent with a terminal or file tool could forge an ``executing`` row and get the bridge to
approve a call. v1 closes this by giving the model only:

* ``durable_park``  - ``parked_action_execute`` (runs an approved, parked call; the point of the plugin)
* ``clarify``       - asks the user a question in the chat; no local access (tools/clarify_tool.py:111-130)
* MCP server ``wh`` - the withHuman gateway's QuickBooks tools only (``mcp__wh__quickbooks__*``,
                      deploy/hermes_mcp.sh); every call goes through the approval pipeline

and the gateway delivers model-named files (``MEDIA:`` paths) only from the Hermes caches: see 5. below.

Run inside Hermes's own Python environment, as the hermes user (deploy/configure_hermes.sh does that):

    <hermes env python> -I deploy/hermes_tool_policy.py apply [--allow-file PATH]
    <hermes env python> -I deploy/hermes_tool_policy.py check [--allow-file PATH] [--status-file PATH]

Opening a toolset: the root-owned file ``ALLOW_FILE`` (/etc/durable-park/allowed_toolsets) lists extra
toolsets, one per line (``#`` comments), that are taken out of ``agent.disabled_toolsets`` and added to every
platform list. Empty or missing = the v1 lockdown above, unchanged. Toolsets with local file/command/code
access (``PHASE2_TOOLSETS``) are refused until Clean Setup Phase 2. Exit codes: 0 ok, 1 check failed,
2 usage error or invalid allow file (message on stderr, nothing applied). ``check --status-file`` writes
``{"ok", "checked_at", "problems", "allowed"}`` atomically, also when the check fails or crashes.

Policy, three layers (Hermes v0.22.2, ~/.hermes/hermes-agent):
1. ``platform_toolsets.<platform> = [durable_park, clarify, wh]`` for EVERY platform key an agent can run
   under (see ``_platforms``): an explicit saved list replaces the full ``hermes-*``
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
5. ``gateway.strict: true`` + ``gateway.trust_recent_files: false``: the gateway uploads a file the model names
   in its reply (``MEDIA:/path``) without any tool. Non-strict, anything outside a small denylist goes out,
   e.g. the ledger (gateway/platforms/base.py:1165-1201); strict without recency trust = only the Hermes
   caches and ``media_delivery_allow_dirs`` (base.py:816-821, 909-927; hermes_cli/config_defaults.py:2213-2231;
   bridged to env by gateway/media_policy.py:21,98 at gateway start, gateway/run.py:2089-2096).
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

HERMES_AGENT = os.path.expanduser("~hermes/.hermes/hermes-agent")
ALLOW_FILE = "/etc/durable-park/allowed_toolsets"

ALLOWED_TOOLSETS = ["durable_park", "clarify"]
MCP_SERVER = "wh"
ALLOWED_TOOL_NAMES = {"parked_action_execute", "clarify"}
ALLOWED_TOOL_PREFIXES = (f"mcp__{MCP_SERVER}__quickbooks__",)
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
# Toolsets with local file / command / code / process access, agent spawning or scheduling: the ledger-bypass
# set. Refused in the allow file until Clean Setup Phase 2. All seven are real Hermes v0.22.2 toolset names
# (toolsets.py:96,102,103,117,122,155,156).
PHASE2_TOOLSETS = frozenset({"terminal", "file", "code_execution", "delegation", "cronjob", "skills",
                             "computer_use"})


def read_allowed(path: str) -> list[str]:
    """The toolsets opened in the allow file: one per line, ``#`` comments and blank lines ignored, order kept,
    duplicates dropped. A missing file is an empty list. A name that is not a known forbidden toolset, or a
    Phase-2-only one, raises ValueError (nothing is applied from a bad file)."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    allowed: list[str] = []
    for line in lines:
        name = line.split("#", 1)[0].strip()
        if not name or name in allowed:
            continue
        if name in PHASE2_TOOLSETS:
            raise ValueError(f"{name} stays off until Clean Setup Phase 2")
        if name not in FORBIDDEN_TOOLSETS:
            raise ValueError(f"unknown toolset: {name}")
        allowed.append(name)
    return allowed


def _bootstrap() -> None:
    if HERMES_AGENT not in sys.path:
        sys.path.insert(0, HERMES_AGENT)
    import logging

    import hermes_bootstrap  # noqa: F401  (same import the hermes launcher does)

    logging.disable(logging.WARNING)


# Session platforms whose config key is ``cli`` (tui_gateway/server.py:2048-2086, agent_callbacks.py:414) or
# that exist only for client-surface toolsets (toolsets.py:146-153); listed too, so nothing depends on that.
_EXTRA_PLATFORM_KEYS = ("acp", "tui", "desktop")


def _platforms() -> list[str]:
    """Every platform key ``_get_platform_tools`` can be called with in Hermes v0.22.2:

    * the built-in table (hermes_cli/platforms.py PLATFORMS, used by tools_config.py:63), incl. ``cron``;
    * every gateway ``Platform`` (gateway/config.py:217-242; ``local`` runs under ``cli``, gateway/run.py:2849-2851;
      the gateway resolves tools by that key, gateway/run_turn.py:2437, 2946, 2386-2402);
    * every bundled platform plugin and its manifest alias (gateway/config.py:279-300) and every
      runtime-registered platform (gateway/platform_registry.py:373), e.g. teams, google_chat, irc, line;
    * ``acp`` (acp_adapter/session.py:521), ``tui`` / ``desktop`` (see ``_EXTRA_PLATFORM_KEYS``);
    * any key already present in ``platform_toolsets`` / ``known_plugin_toolsets`` (stale or hand-written).
    """
    from gateway.config import Platform
    from hermes_cli.config import load_config
    from hermes_cli.platforms import PLATFORMS

    keys = set(PLATFORMS) | set(_EXTRA_PLATFORM_KEYS)
    keys |= {"cli" if p is Platform.LOCAL else p.value for p in Platform}
    names, aliases = Platform._scan_bundled_plugin_platforms()
    keys |= set(names) | set(aliases)
    try:
        from gateway.platform_registry import platform_registry
        keys |= set(platform_registry.registered_names())
    except Exception:  # noqa: BLE001 - the bundled scan above already covers the shipped plugins
        pass
    config = load_config()
    for section in ("platform_toolsets", "known_plugin_toolsets"):
        keys |= {str(k) for k in (config.get(section) or {})}
    return sorted(keys)


def _plugin_toolsets() -> list[str]:
    import model_tools  # noqa: F401  (imports the tool modules; some register toolsets such as browser-cdp)
    from hermes_cli.plugins import discover_plugins
    from toolsets import _get_plugin_toolset_names

    discover_plugins()
    return sorted(_get_plugin_toolset_names())


def _wh_configured() -> bool:
    from hermes_cli.config import load_config

    return isinstance((load_config().get("mcp_servers") or {}).get(MCP_SERVER), dict)


def desired_settings(allowed: list[str] | None = None) -> dict:
    allowed = list(allowed or [])
    plugin_toolsets = _plugin_toolsets()
    mcp = MCP_SERVER if _wh_configured() else "no_mcp"
    disabled = [t for t in FORBIDDEN_TOOLSETS if t not in allowed]
    settings: dict = {"agent.disabled_toolsets": disabled, "tools.tool_search.enabled": "off",
                      "gateway.strict": True, "gateway.trust_recent_files": False}
    for platform in _platforms():
        settings[f"platform_toolsets.{platform}"] = [*ALLOWED_TOOLSETS, *allowed, mcp]
        settings[f"known_plugin_toolsets.{platform}"] = plugin_toolsets
    return settings


def _get(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def apply(allowed: list[str] | None = None) -> int:
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
    for key, value in desired_settings(allowed).items():
        if _get(raw, key) == value:
            continue
        with redirect_stdout(io.StringIO()):
            set_config_value(key, value if isinstance(value, str) else json.dumps(value), force=True)
        changed += 1
    print(f"tool policy: {changed} setting(s) written")
    return 0


# Sessions that do NOT take their toolsets from platform_toolsets: a code-workspace TUI session gets the
# ``coding`` posture (tui_gateway/server.py:2057-2062), an operator pin (HERMES_TUI_TOOLSETS) or a caller may
# pass any list, ACP expands ``hermes-acp`` (acp_adapter/session.py:103-107), and ``None`` means every toolset
# (model_tools.py:327-331). agent.disabled_toolsets must strip the forbidden tools from all of them.
POSTURES = {
    "all-toolsets": None,
    "coding-posture": ["coding", "desktop_ui", "project", "catalog"],
    "acp-default": ["hermes-acp"],
    "cli-composite": ["hermes-cli"],
}


def _disabled(config: dict):
    from agent.skill_utils import parse_config_string_list

    return parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets")) or None


def effective_tools(platform: str, enabled: list[str] | None | str = "config") -> list[str]:
    """The tool names a session on *platform* gets, through the same calls the gateway / CLI make:
    gateway/run_turn.py:2404-2409 -> agent/agent_init.py:1114-1121 (session gating, definitions, drops).
    ``enabled="config"`` resolves the platform's configured toolsets; anything else is used as given."""
    import model_tools
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import session_disabled_toolsets, session_platform_tool_drops

    config = load_config()
    if enabled == "config":
        enabled = sorted(_get_platform_tools(config, platform))
    disabled = _disabled(config)
    disabled = session_disabled_toolsets(disabled, platform)
    defs = model_tools.get_tool_definitions(enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True)
    drops = session_platform_tool_drops(platform)
    return sorted(d["function"]["name"] for d in defs if d["function"]["name"] not in drops)


def allowed_tool_names(allowed: list[str]) -> set[str]:
    """The tool names Hermes defines for the opened toolsets."""
    import model_tools

    # HERMES_KANBAN_TASK (which makes Hermes add kanban tools, model_tools.py:322-324) is never set for the gateway:
    # only a kanban worker has it, so the definitions below are exactly the toolsets' own tools.
    # An empty list selects nothing (model_tools.py:316-321: only ``None`` means every toolset).

    defs = model_tools.get_tool_definitions(enabled_toolsets=list(allowed), quiet_mode=True)
    return {d["function"]["name"] for d in defs}


def is_allowed(name: str, extra: set[str] | frozenset[str] = frozenset()) -> bool:
    return name in ALLOWED_TOOL_NAMES or name in extra or name.startswith(ALLOWED_TOOL_PREFIXES)


def media_check() -> list[str]:
    """Problems with model-named file delivery (empty = ok): config values, then the real validator."""
    import tempfile

    from gateway.media_policy import apply_media_policy_env
    from gateway.platforms.base import validate_media_delivery_path
    from hermes_cli.config import load_config

    config = load_config()
    gateway_cfg = config.get("gateway") or {}
    problems = [f"gateway.{k} is {gateway_cfg.get(k)!r}, want {v!r}"
                for k, v in (("strict", True), ("trust_recent_files", False)) if gateway_cfg.get(k) is not v]
    for name in ("HERMES_MEDIA_DELIVERY_STRICT", "HERMES_MEDIA_TRUST_RECENT_FILES"):
        os.environ.pop(name, None)  # judge the config, as a fresh gateway start would
    apply_media_policy_env(config)
    with tempfile.NamedTemporaryFile(dir=os.path.expanduser("~"), prefix=".dp-media-", delete=True) as fresh:
        for path in ("/etc/durable-park/wake_secret", config_ledger_path(config), fresh.name):
            if path and validate_media_delivery_path(path):
                problems.append(f"the gateway would upload {path} if the model named it")
    return problems


def config_ledger_path(config: dict) -> str | None:
    entry = ((config.get("plugins") or {}).get("entries") or {}).get("durable-park") or {}
    return (entry.get("settings") or {}).get("ledger_path")


def check(allowed: list[str] | None = None, problems: list[str] | None = None) -> int:
    """Print each platform's tools; return 1 on a leak. Every problem is also appended to *problems*."""
    allowed = list(allowed or [])
    problems = problems if problems is not None else []
    _plugin_toolsets()  # discovery registers plugin tools, as at agent start
    extra = allowed_tool_names(allowed)
    for platform in _platforms():
        names = effective_tools(platform)
        leaks = [n for n in names if not is_allowed(n, extra)]
        missing = [] if REQUIRED_TOOL in names else [REQUIRED_TOOL]
        problems += [f"{platform}: LEAK {n}" for n in leaks] + [f"{platform}: MISSING {n}" for n in missing]
        print(f"{platform}: {', '.join(names) or '(none)'}" + (f"  LEAK: {', '.join(leaks)}" if leaks else "")
              + (f"  MISSING: {REQUIRED_TOOL}" if missing else ""))
    for posture, enabled in POSTURES.items():
        for platform in ("cli", "tui", "desktop", "acp", "telegram"):
            leaks = [n for n in effective_tools(platform, enabled) if not is_allowed(n, extra)]
            problems += [f"{posture}@{platform}: LEAK {n}" for n in leaks]
            if leaks:
                print(f"{posture}@{platform}: LEAK: {', '.join(leaks)}")
    print(f"postures ({', '.join(POSTURES)}) on cli/tui/desktop/acp/telegram: "
          + ("ok" if not problems else "see LEAK lines"))
    media = media_check()
    for problem in media:
        print(f"media: {problem}")
    problems += [f"media: {p}" for p in media]
    if not media:
        print("media: ok (strict, no recency trust; wake_secret, ledger and a fresh file are not deliverable)")
    if problems:
        print(f"tool policy: FAILED, {len(problems)} problem(s): forbidden tool reachable, {REQUIRED_TOOL} missing "
              "or media delivery open", file=sys.stderr)
        return 1
    opened = f" plus the opened toolsets ({', '.join(allowed)})" if allowed else ""
    print(f"tool policy: ok on {len(_platforms())} platform keys (only durable_park, clarify and "
          f"mcp__wh__quickbooks__* tools{opened})")
    return 0


def write_status(path: str, ok: bool, problems: list[str], allowed: list[str]) -> None:
    """Write the check result as JSON, atomically (temp file in the same directory, then rename)."""
    status = {"ok": ok, "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "problems": problems, "allowed": allowed}
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes_tool_policy.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "check"):
        p = sub.add_parser(name)
        p.add_argument("--allow-file", default=ALLOW_FILE, metavar="PATH")
        if name == "check":
            p.add_argument("--status-file", default=None, metavar="PATH")
    return parser


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: usage error -> 2 (--help -> 0)
        return int(exc.code or 0)
    try:
        allowed = read_allowed(args.allow_file)
    except ValueError as exc:
        print(f"hermes_tool_policy.py: invalid allow file {args.allow_file}: {exc}", file=sys.stderr)
        return 2
    status_file = getattr(args, "status_file", None)
    problems: list[str] = []
    if status_file:  # replace any earlier pass first: a crash or a failed final write must not leave a stale ok
        try:
            write_status(status_file, False, ["check did not finish"], allowed)
        except OSError as exc:  # cannot record a failure: report one, run nothing
            print(f"hermes_tool_policy.py: cannot write status file {status_file}: {exc}", file=sys.stderr)
            return 1
    try:
        _bootstrap()
        if args.command == "apply":
            return apply(allowed)
        rc = check(allowed, problems)
    except Exception as exc:  # noqa: BLE001 - a crashed check must still leave a failing status file
        if not status_file:
            raise
        problems.append(f"check crashed: {type(exc).__name__}: {exc}")
        print(problems[-1], file=sys.stderr)
        rc = 1
    except BaseException:  # SystemExit / KeyboardInterrupt: the pre-written failure stays; refresh its reason
        if status_file:
            write_status(status_file, False, ["check interrupted"], allowed)
        raise
    if status_file:
        write_status(status_file, rc == 0, problems, allowed)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
