"""durable-park: park risky tool calls and resume them after a human decision in withHuman."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .gatecall import make_gate
from .ledger import Ledger
from .park_hook import DEFAULT_RISKY_TOOLS, fail_closed_hook, make_pre_tool_call

log = logging.getLogger("durable_park")

DEFAULT_AAP_BASE_URL = "https://app.withhuman.ai/api/aap"
DEFAULT_TOKEN_FILE = "/etc/durable-park/instance_token"


def _compile_risky(patterns) -> list[re.Pattern]:
    if isinstance(patterns, str):
        patterns = [patterns]
    compiled: list[re.Pattern] = []
    invalid = False
    for pattern in patterns:
        try:
            compiled.append(re.compile(str(pattern)))
        except re.error as exc:
            invalid = True
            log.error("ignoring invalid risky_tools pattern %r: %s", pattern, exc)
    if invalid:  # never end up guarding less than the defaults because of a typo
        compiled.extend(re.compile(p) for p in DEFAULT_RISKY_TOOLS)
    return compiled


def register(ctx) -> None:
    """Hermes plugin entry point. Task 4 wires ``pre_tool_call``; the tool and gateway hook come later."""
    risky = _compile_risky(ctx.get_config("risky_tools", list(DEFAULT_RISKY_TOOLS)))
    base_url = ctx.get_config("aap_base_url", DEFAULT_AAP_BASE_URL)
    token_file = ctx.get_config("token_file", DEFAULT_TOKEN_FILE)
    ledger_path = ctx.get_config("ledger_path", str(Path(ctx.state.data_dir) / "ledger.db"))

    def gate_factory():
        # Read lazily so a missing/rotated token never breaks plugin load; never logged.
        token = Path(token_file).read_text().strip()
        return make_gate(base_url, token)

    try:
        Path(ledger_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(str(Path(ledger_path).expanduser()))
    except Exception as exc:  # noqa: BLE001
        log.error("durable-park ledger unavailable (%s); refusing all risky tools", type(exc).__name__)
        ctx.register_hook("pre_tool_call", fail_closed_hook(risky))
        return
    ctx.register_hook("pre_tool_call", make_pre_tool_call(ledger, gate_factory, risky))
