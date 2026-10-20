"""Hermes version check (R21-4): durable-park leans on Hermes internals that are not a stable API.

It was built and live-tested against Hermes Agent v0.22.2. On any other version it still loads (refusing
would leave risky tools unguarded), but logs one WARNING naming everything it depends on, so whoever upgrades
Hermes knows what to re-verify:

- ``pre_tool_call`` hook: kwargs ``tool_name, args, session_id, tool_call_id`` and the block directive
  (hermes_cli/plugins.py:1971-1991);
- ``pre_gateway_dispatch`` hook: ``event=`` kwarg, ``{"action": "skip"}`` result, called before auth
  (gateway/run_inbound.py:73-107, :232);
- ``ctx.inject_message(content, session_key=)`` + ``allow_gateway_injection`` (hermes_cli/plugins.py:603-630);
- ``ctx.dispatch_tool`` skipping ``pre_tool_call`` (hermes_cli/plugins.py:674-683, tools/registry.py:893-921);
- ``ctx.register_tool`` (plugin toolset on by default, hermes_cli/tools_config.py:510-517);
- MCP tool-error wording the executor classifies as failed/unknown (tools/mcp_tool_handlers.py, executor R15),
  and the display failure heuristic (agent/display.py:999-1037).
"""

from __future__ import annotations

import logging
from typing import Callable

SUPPORTED_HERMES_VERSION = "0.22.2"
DEPENDS_ON = (
    "pre_tool_call hook (kwargs + block directive)",
    "pre_gateway_dispatch hook (event=, {'action': 'skip'})",
    "ctx.inject_message (session_key=, allow_gateway_injection)",
    "ctx.dispatch_tool (must not re-run pre_tool_call)",
    "ctx.register_tool",
    "MCP tool-error wording (tools/mcp_tool_handlers.py; executor failed/unknown classification)",
)


def _normalise(version: object) -> str | None:
    text = str(version).strip() if version is not None else ""
    text = text[1:] if text[:1] in ("v", "V") else text
    return text if text and text != "unknown" and text != "0.0.0" else None


def hermes_version() -> str | None:
    """The running Hermes base version (``"0.22.2"``), or None outside Hermes / when it cannot be read.

    Hermes v0.22.2: ``hermes_cli.version_info.get_version_info().base_version`` (install stamp, then git;
    hermes_cli/version_info.py:277-305); ``hermes_cli.__version__`` (install stamp ``baseVersion``,
    hermes_cli/__init__.py) as a fallback.
    """
    try:
        from hermes_cli.version_info import get_version_info

        version = _normalise(get_version_info().base_version)
        if version:
            return version
    except Exception:  # noqa: BLE001 - any failure means "unknown", never a load failure
        pass
    try:
        import hermes_cli

        return _normalise(getattr(hermes_cli, "__version__", None))
    except Exception:  # noqa: BLE001
        return None


def check_hermes_version(logger: logging.Logger, get_version: Callable[[], str | None] | None = None) -> bool:
    """True on the supported version; otherwise log ONE warning and return False. Never raises."""
    try:
        version = _normalise((get_version or hermes_version)())
    except Exception:  # noqa: BLE001
        version = None
    if version == SUPPORTED_HERMES_VERSION:
        return True
    logger.warning(
        "durable-park was built and tested against Hermes v%s but this is %s; it still loads, but re-verify "
        "what it depends on before trusting it: %s",
        SUPPORTED_HERMES_VERSION, f"v{version}" if version else "an unknown version", "; ".join(DEPENDS_ON))
    return False
