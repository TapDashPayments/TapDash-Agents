"""durable-park: ask withHuman about every tool call; park the ones a person must decide and resume them after
the decision."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import compat
from . import formcheck
from .breaker import STATUS_FILENAME as BREAKER_STATUS_FILENAME, Breaker
from .executor import EXECUTE_SCHEMA, crash_after_consume_enabled, make_executor, make_refusing_executor
from .gatecall import GateHolder
from .ledger import Ledger
from .park_hook import DEFAULT_EXEMPT_TOOLS, DEFAULT_FORM_CHECKED_TOOLS, fail_closed_hook, make_pre_tool_call
from .wake import drop_wake_hook, handle_wake, make_async_client_factory

log = logging.getLogger("durable_park")

DEFAULT_AAP_BASE_URL = "https://app.withhuman.ai/api/aap"
DEFAULT_TOKEN_FILE = "/etc/durable-park/instance_token"
EXECUTOR_TOOL = EXECUTE_SCHEMA["name"]
HOOK_REGISTRY_UNAVAILABLE = "<hook registry unavailable>"
POLICY_STATUS_FILENAME = "tool_policy_status.json"
POLICY_LOAD_FILENAME = "tool_policy_load.json"
POLICY_MAX_AGE = timedelta(minutes=15)
POLICY_MESSAGE = ("durable-park: the tool policy check did not pass at gateway start; tool calls are refused "
                  "until an admin fixes it.")
NO_POLICY_REPORT = "no tool policy report"
RISKY_TOOLS_RENAMED = "risky_tools is renamed form_checked_tools; ignoring it"


class BadSetting(ValueError):
    """A plugin setting has the wrong type."""


def policy_problem(path: str, now: datetime, max_age: timedelta = POLICY_MAX_AGE) -> str | None:
    """What is wrong with the tool policy report written at gateway start (deploy/tool_policy_check.sh), or
    ``None`` when it is fresh and passing. Missing, unreadable, invalid or malformed -> "no tool policy report";
    ``ok`` not exactly true -> the joined ``problems``; ``checked_at`` older than ``max_age`` (or unparsable or
    in the future by more than a minute) -> "stale"."""
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return NO_POLICY_REPORT
    if not isinstance(report, dict):
        return NO_POLICY_REPORT
    if report.get("ok") is not True:
        problems = report.get("problems")
        if isinstance(problems, list) and problems:
            return "; ".join(str(p) for p in problems)
        return "the tool policy check failed"
    try:
        checked = datetime.strptime(str(report.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "stale"
    age = now - checked
    if age > max_age or age < -timedelta(minutes=1):
        return "stale"
    return None


def write_policy_load(path: str, problem: str | None) -> None:
    """Record this load's own verdict on the tool policy report (``ok``, ``problem``, ``checked_at``) for the
    sweep, atomically. The sweep cannot judge a report's age itself, so a stale report (the plugin was reloaded
    more than 15 minutes after the gateway start) would otherwise be invisible to it. Never raises."""
    tmp = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        verdict = {"ok": problem is None, "problem": problem,
                   "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".policy-load-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(verdict, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - a failed verdict write must never change what the plugin does
        log.warning("durable-park could not write %s (%s)", POLICY_LOAD_FILENAME, type(exc).__name__)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _names(key: str, value: Any) -> list[str]:
    """A list-of-strings setting (a lone string counts as a one-item list). Anything else - a number, a boolean,
    a mapping, null, a list holding a non-string - raises ``BadSetting``; ``register`` then refuses every call."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise BadSetting(f"setting {key} must be a list of strings, got {type(value).__name__}")


def _compile_form_checked(patterns: list[str]) -> list[re.Pattern]:
    compiled: list[re.Pattern] = []
    invalid = False
    for pattern in patterns:
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


# Middleware kinds that see a tool call's arguments (Hermes v0.22.2 hermes_cli/middleware.py:19-20).
# ``tool_execution`` runs AFTER ``pre_tool_call`` and may pass other args to ``next_call``
# (agent/agent_runtime_helpers.py:2619-2624, hermes_cli/middleware.py ``_run_execution_chain``);
# ``tool_request`` rewrites args before the hook and may run again on paths durable-park does not see.
TOOL_MIDDLEWARE_KINDS = ("tool_request", "tool_execution")


def foreign_pre_tool_call_hooks(ctx: Any, own: set[int], allowed: set[str]) -> list[str]:
    """Names of the callbacks that could change a call's arguments after withHuman approved them and are
    neither this plugin's own (``id`` in ``own``) nor listed in ``allowed`` (``observer_hooks``): other
    ``pre_tool_call`` hooks (a ``modify`` directive) and ``tool_request`` / ``tool_execution`` middleware.

    Hermes v0.22.2: ``PluginContext._manager`` (hermes_cli/plugins.py:235) is the ``PluginManager``;
    ``PluginManager.iter_hook_callbacks(hook_name)`` (hermes_cli/plugins_dispatch.py:523) returns a snapshot
    tuple of the callables ``register_hook`` stored (hermes_cli/plugins.py:933, stored unwrapped).
    ``PluginManager._middleware`` (hermes_cli/plugins.py:1183) is a dict of lists keyed by kind that
    ``register_middleware`` appends the callable itself to (hermes_cli/plugins.py:937-952); there is no public
    accessor, and ``_run_execution_chain`` reads the same dict. When either registry cannot be read the answer
    is ``["<hook registry unavailable>"]``: fail closed."""
    try:
        callbacks = list(ctx._manager.iter_hook_callbacks("pre_tool_call"))
        middleware = ctx._manager._middleware
        if not isinstance(middleware, dict):
            raise TypeError("middleware registry is not a mapping")
        for kind in TOOL_MIDDLEWARE_KINDS:
            callbacks.extend(tuple(middleware.get(kind, ())))
    except Exception:  # noqa: BLE001 - no manager, no registry, or it raised: we cannot know, so refuse
        return [HOOK_REGISTRY_UNAVAILABLE]
    foreign: list[str] = []
    for cb in callbacks:
        if id(cb) in own:
            continue
        name = _callback_name(cb)
        if name not in allowed:
            foreign.append(name)
    return foreign


def _exempt_tools(ctx: Any, form_checked: list[re.Pattern]) -> tuple[str, ...]:
    """The ``exempt_tools`` setting. Anything listed runs with no approval and no ledger row, so:
    an MCP tool (``mcp__...``) or a form-checked tool is refused as a bad setting (``BadSetting``: every call is
    then refused), and any other name outside ``DEFAULT_EXEMPT_TOOLS`` is logged as an ERROR."""
    exempt = tuple(_names("exempt_tools", ctx.get_config("exempt_tools", list(DEFAULT_EXEMPT_TOOLS))))
    for name in exempt:
        if name.startswith("mcp__") or any(p.fullmatch(name) for p in form_checked):
            raise BadSetting(f"setting exempt_tools must not list MCP or form-checked tool {name!r}")
    for name in exempt:
        if name not in DEFAULT_EXEMPT_TOOLS:
            log.error("exempt_tools lists %s: it runs with no approval and no ledger row", name)
    if EXECUTOR_TOOL not in exempt:
        log.warning("exempt_tools does not list %s: approved parks will be asked about again and cannot run",
                    EXECUTOR_TOOL)
    return exempt


def register(ctx) -> None:
    """Hermes plugin entry point: the ``pre_tool_call`` hook (asks withHuman about every non-exempt call), the
    ``parked_action_execute`` tool and the ``pre_gateway_dispatch`` wake handler.

    Never raises: when ``register`` raises, Hermes v0.22.2 disposes every registration of the plugin and carries
    on with no ``pre_tool_call`` hook at all (hermes_cli/plugins_loader.py:525-537), so every tool would run
    unchecked. Any error (a setting of the wrong type, a wiring failure) instead registers a hook that refuses
    every call except ``DEFAULT_EXEMPT_TOOLS`` and drops wakes."""
    compat.check_hermes_version(log)  # warns (never refuses) on a Hermes other than v0.22.2
    own: set[int] = set()  # ids of the hook callbacks this register passed to ctx.register_hook

    def register_hook(name: str, callback: Any) -> None:
        own.add(id(callback))
        ctx.register_hook(name, callback)

    try:
        _register(ctx, own, register_hook)
    except Exception as exc:  # noqa: BLE001 - fail closed, never leave Hermes without a blocking hook
        if isinstance(exc, BadSetting):
            log.error("durable-park %s; refusing every tool call", exc)
        else:
            log.error("durable-park failed to load (%s); refusing every tool call", type(exc).__name__)
        register_hook("pre_tool_call", fail_closed_hook(DEFAULT_EXEMPT_TOOLS))
        register_hook("pre_gateway_dispatch", drop_wake_hook())  # wakes must still never reach the agent


def _register(ctx: Any, own: set[int], register_hook: Any) -> None:
    if ctx.get_config("risky_tools", None) is not None:
        log.warning(RISKY_TOOLS_RENAMED)
    # Every list setting is validated before anything is registered (BadSetting -> refuse everything).
    form_checked = _compile_form_checked(
        _names("form_checked_tools", ctx.get_config("form_checked_tools", list(DEFAULT_FORM_CHECKED_TOOLS))))
    exempt = _exempt_tools(ctx, form_checked)
    observers = frozenset(_names("observer_hooks", ctx.get_config("observer_hooks", [])))

    ledger_path = ctx.get_config("ledger_path", str(Path(ctx.state.data_dir) / "ledger.db"))
    default_policy = str(Path(ledger_path).expanduser().parent / POLICY_STATUS_FILENAME)
    policy_path = ctx.get_config("tool_policy_status", default_policy)
    if not isinstance(policy_path, str) or not policy_path:
        raise BadSetting(f"setting tool_policy_status must be a path, got {type(policy_path).__name__}")
    problem = policy_problem(str(Path(policy_path).expanduser()), datetime.now(timezone.utc))
    write_policy_load(str(Path(ledger_path).expanduser().parent / POLICY_LOAD_FILENAME), problem)
    if problem is not None:
        log.error("durable-park tool policy check failed (%s); refusing every tool call", problem)
        register_hook("pre_tool_call", fail_closed_hook(DEFAULT_EXEMPT_TOOLS, POLICY_MESSAGE))
        register_hook("pre_gateway_dispatch", drop_wake_hook())  # wakes must still never reach the agent
        # The executor stays registered so the model's call gets a clean refusal (nothing consumed: an
        # approved park stays approved and can run after the admin fixes the policy and restarts the gateway).
        ctx.register_tool(
            name=EXECUTOR_TOOL, toolset="durable_park", schema=EXECUTE_SCHEMA,
            handler=make_refusing_executor(POLICY_MESSAGE), description=EXECUTE_SCHEMA["description"])
        return
    try:
        Path(ledger_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(str(Path(ledger_path).expanduser()))
    except Exception as exc:  # noqa: BLE001
        log.error("durable-park ledger unavailable (%s); refusing every tool call", type(exc).__name__)
        register_hook("pre_tool_call", fail_closed_hook(DEFAULT_EXEMPT_TOOLS))
        register_hook("pre_gateway_dispatch", drop_wake_hook())  # wakes must still never reach the agent
        return

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

