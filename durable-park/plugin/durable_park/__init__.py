"""durable-park: ask withHuman about every tool call; park the ones a person must decide and resume them after
the decision."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from . import compat
from . import formcheck
from .breaker import STATUS_FILENAME as BREAKER_STATUS_FILENAME, Breaker
from .executor import EXECUTE_SCHEMA, crash_after_consume_enabled, make_executor
from .gatecall import GateHolder
from .ledger import Ledger
from .park_hook import DEFAULT_EXEMPT_TOOLS, DEFAULT_FORM_CHECKED_TOOLS, fail_closed_hook, make_pre_tool_call
from .wake import drop_wake_hook, handle_wake, make_async_client_factory

log = logging.getLogger("durable_park")

DEFAULT_AAP_BASE_URL = "https://app.withhuman.ai/api/aap"
DEFAULT_TOKEN_FILE = "/etc/durable-park/instance_token"
EXECUTOR_TOOL = EXECUTE_SCHEMA["name"]
HOOK_REGISTRY_UNAVAILABLE = "<hook registry unavailable>"
RISKY_TOOLS_RENAMED = "risky_tools is renamed form_checked_tools; ignoring it"


def _names(value: Any) -> list[str]:
    """A setting that is a list of strings (a lone string counts as a one-item list)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _compile_form_checked(patterns: Any) -> list[re.Pattern]:
    compiled: list[re.Pattern] = []
    invalid = False
    for pattern in _names(patterns):
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            invalid = True
            log.error("ignoring invalid form_checked_tools pattern %r: %s", pattern, exc)
    if invalid:  # never end up checking less than the defaults because of a typo
        compiled.extend(re.compile(p) for p in DEFAULT_FORM_CHECKED_TOOLS)
    return compiled


def _callback_name(cb: Any) -> str:
    """``module.qualname`` of a hook callback (of its class, for a callable object)."""
    module = getattr(cb, "__module__", None) or type(cb).__module__
    qualname = getattr(cb, "__qualname__", None) or type(cb).__qualname__
    return f"{module}.{qualname}"


def foreign_pre_tool_call_hooks(ctx: Any, own: set[int], allowed: set[str]) -> list[str]:
    """Names of the ``pre_tool_call`` callbacks in Hermes's hook registry that are neither this plugin's own
    (``id`` in ``own``) nor listed in ``allowed`` (``observer_hooks``). Such a hook could return a ``modify``
    directive and change a call's arguments after withHuman approved them.

    Hermes v0.22.2: ``PluginContext._manager`` (hermes_cli/plugins.py:235) is the ``PluginManager``;
    ``PluginManager.iter_hook_callbacks(hook_name)`` (hermes_cli/plugins_dispatch.py:523) returns a snapshot
    tuple of the callables ``register_hook`` stored (hermes_cli/plugins.py:933, stored unwrapped). When the
    registry cannot be read the answer is ``["<hook registry unavailable>"]``: fail closed."""
    try:
        callbacks = tuple(ctx._manager.iter_hook_callbacks("pre_tool_call"))
    except Exception:  # noqa: BLE001 - no manager, no method, or it raised: we cannot know, so refuse
        return [HOOK_REGISTRY_UNAVAILABLE]
    foreign: list[str] = []
    for cb in callbacks:
        if id(cb) in own:
            continue
        name = _callback_name(cb)
        if name not in allowed:
            foreign.append(name)
    return foreign


def _exempt_tools(ctx: Any) -> tuple[str, ...]:
    exempt = tuple(_names(ctx.get_config("exempt_tools", list(DEFAULT_EXEMPT_TOOLS))))
    if EXECUTOR_TOOL not in exempt:
        log.warning("exempt_tools does not list %s: approved parks will be asked about again and cannot run",
                    EXECUTOR_TOOL)
    return exempt


def register(ctx) -> None:
    """Hermes plugin entry point: the ``pre_tool_call`` hook (asks withHuman about every non-exempt call), the
    ``parked_action_execute`` tool and the ``pre_gateway_dispatch`` wake handler."""
    compat.check_hermes_version(log)  # warns (never refuses) on a Hermes other than v0.22.2
    exempt = _exempt_tools(ctx)
    own: set[int] = set()  # ids of the hook callbacks this register passed to ctx.register_hook

    def register_hook(name: str, callback: Any) -> None:
        own.add(id(callback))
        ctx.register_hook(name, callback)

    ledger_path = ctx.get_config("ledger_path", str(Path(ctx.state.data_dir) / "ledger.db"))
    try:
        Path(ledger_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(str(Path(ledger_path).expanduser()))
    except Exception as exc:  # noqa: BLE001
        log.error("durable-park ledger unavailable (%s); refusing every tool call", type(exc).__name__)
        register_hook("pre_tool_call", fail_closed_hook(exempt))
        register_hook("pre_gateway_dispatch", drop_wake_hook())  # wakes must still never reach the agent
        return

    if ctx.get_config("risky_tools", None) is not None:
        log.warning(RISKY_TOOLS_RENAMED)
    form_checked = _compile_form_checked(ctx.get_config("form_checked_tools", list(DEFAULT_FORM_CHECKED_TOOLS)))
    observers = frozenset(_names(ctx.get_config("observer_hooks", [])))
    base_url = ctx.get_config("aap_base_url", DEFAULT_AAP_BASE_URL)
    token_file = ctx.get_config("token_file", DEFAULT_TOKEN_FILE)
    state_dir = Path(ledger_path).expanduser().parent

    # Register the wake hook first so a later registration failure can never leave wakes reaching the agent.
    wake_client_factory = make_async_client_factory(base_url, token_file)

    async def durable_park_wake(event=None, **_kw):
        # Hermes awaits async hooks on the gateway loop and passes event=, gateway=, session_store=.
        # ctx.inject_message is sync and only schedules the turn on that loop (plugins_injection.py).
        return await handle_wake(event, ledger=ledger, async_client_factory=wake_client_factory,
                                 inject=ctx.inject_message)

    register_hook("pre_gateway_dispatch", durable_park_wake)

    # One gate for the hook and the executor (rebuilt when the token file changes; read lazily, so a missing
    # token never breaks plugin load and refuses calls instead). One breaker, its status next to the ledger.
    holder = GateHolder(base_url, token_file)
    breaker = Breaker(str(state_dir / BREAKER_STATUS_FILENAME))
    checker = formcheck.FormChecker()

    def foreign_hooks() -> list[str]:
        # Checked on every call: a plugin that registers a hook later is still caught.
        return foreign_pre_tool_call_hooks(ctx, own, observers)

    register_hook("pre_tool_call", make_pre_tool_call(
        ledger, holder.get, form_checked, exempt=exempt, breaker=breaker, foreign_hooks_fn=foreign_hooks,
        form_checker=checker))
    # Background self-check (Hermes processes only): proves the form check can read and use every form-checked
    # tool's rules and writes the result next to the ledger, where the sweep raises an alert.
    formcheck.start_selfcheck(checker, form_checked, str(state_dir / formcheck.STATUS_FILENAME))
    # ctx.dispatch_tool -> registry.dispatch does not run pre_tool_call hooks, so the executor's own
    # dispatch cannot be blocked by the park hook.
    ctx.register_tool(
        name=EXECUTOR_TOOL, toolset="durable_park", schema=EXECUTE_SCHEMA,
        handler=make_executor(ledger, holder.get, ctx.dispatch_tool,
                              crash_after_consume=crash_after_consume_enabled()),
        description=EXECUTE_SCHEMA["description"])
    log.info("durable-park loaded: ledger=%s, %d exempt tool(s), %d form-checked pattern(s), %d observer "
             "hook(s), aap=%s", ledger_path, len(exempt), len(form_checked), len(observers), base_url)

