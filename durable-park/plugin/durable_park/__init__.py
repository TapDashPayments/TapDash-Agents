"""durable-park: park risky tool calls and resume them after a human decision in withHuman."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .executor import EXECUTE_SCHEMA, crash_after_consume_enabled, make_executor
from .gatecall import make_gate
from .ledger import Ledger
from .park_hook import DEFAULT_RISKY_TOOLS, fail_closed_hook, make_pre_tool_call
from .wake import drop_wake_hook, handle_wake, make_async_client_factory

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
    """Hermes plugin entry point: the ``pre_tool_call`` park hook, the ``parked_action_execute`` tool and
    the ``pre_gateway_dispatch`` wake handler."""
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
        ctx.register_hook("pre_gateway_dispatch", drop_wake_hook())  # wakes must still never reach the agent
        return
    # Register the wake hook first so a later registration failure can never leave wakes reaching the agent.
    wake_client_factory = make_async_client_factory(base_url, token_file)

    async def durable_park_wake(event=None, **_kw):
        # Hermes awaits async hooks on the gateway loop and passes event=, gateway=, session_store=.
        # ctx.inject_message is sync and only schedules the turn on that loop (plugins_injection.py).
        return await handle_wake(event, ledger=ledger, async_client_factory=wake_client_factory,
                                 inject=ctx.inject_message)

    ctx.register_hook("pre_gateway_dispatch", durable_park_wake)
    ctx.register_hook("pre_tool_call", make_pre_tool_call(ledger, gate_factory, risky))
    # ctx.dispatch_tool -> registry.dispatch does not run pre_tool_call hooks, so the executor's own
    # dispatch cannot be blocked by the park hook.
    ctx.register_tool(
        name=EXECUTE_SCHEMA["name"], toolset="durable_park", schema=EXECUTE_SCHEMA,
        handler=make_executor(ledger, gate_factory, ctx.dispatch_tool,
                              crash_after_consume=crash_after_consume_enabled()),
        description=EXECUTE_SCHEMA["description"])
