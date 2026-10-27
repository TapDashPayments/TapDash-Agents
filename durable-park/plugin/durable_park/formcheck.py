"""Check a form-checked call's arguments against the tool's own input rules before withHuman is asked.

The rules are the JSON Schema Hermes registered for the tool (the same schema the model is shown), read
fresh on every call. A call that breaks them is refused with the first problem found, so the model can fix
it and no approval is wasted on a call the server would reject. Anything that stops the check from running
(no rules, unusable rules, a lookup error) raises ``FormCheckUnavailable`` and the caller fails closed.

Safety choices (the check must never block what the server would accept, and never change a call):
- arguments get Hermes's own type fixing first (``"5"`` -> ``5``), as dispatch would, on a copy;
- the withHuman gateway's own top-level ``withhuman_*`` fields are removed from the arguments and the
  rules: the gateway strips them before the server sees the call;
- fields Hermes marked ``nullable`` accept ``null`` (Hermes folds ``anyOf: [X, null]`` into ``X``);
- ``format``, ``multipleOf`` (float rounding), ``pattern`` and ``propertyNames`` (regexes from outside
  could hang the hook) are left to the server; ``patternProperties`` is dropped and its object opened up;
- ``$ref`` is followed only inside the rules themselves; every other reference is refused (never fetched);
- defaults are never filled in.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ._vendor import fastjsonschema

log = logging.getLogger("durable_park")

GATEWAY_PREFIX = "withhuman_"  # withhuman_scope, withhuman_context, and any the gateway adds later
THREAD_NAME = "durable-park-formcheck"
_LEFT_TO_SERVER = ("format", "multipleOf", "pattern", "propertyNames")
_SCHEMA_MAPS = ("properties", "patternProperties", "definitions", "$defs", "dependencies")
_SCHEMA_VALUES = ("items", "additionalItems", "additionalProperties", "contains", "not", "if", "then",
                  "else", "anyOf", "allOf", "oneOf")
MAX_PROBLEM_CHARS = 300
STATUS_FILENAME = "formcheck_status.json"
_CACHE_MAX = 128
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]+")


class FormCheckUnavailable(Exception):
    """The check could not run (no rules, unusable rules, lookup error). Callers must fail closed."""


class _RefuseRemoteRefs(dict):
    """``handlers`` for fastjsonschema that claims every URI scheme and refuses it: no ref is ever fetched."""

    def __contains__(self, scheme: object) -> bool:
        return True

    def __getitem__(self, scheme: str) -> Callable[[str], Any]:
        def refuse(uri: str) -> Any:
            raise FormCheckUnavailable("rules reference something outside themselves")
        return refuse


def registry_schema(tool_name: str) -> dict | None:
    """The input rules Hermes registered for ``tool_name`` (``tools.registry`` is a plugin-allowed import)."""
    from tools.registry import registry  # Hermes runtime only

    schema = registry.get_schema(tool_name)
    params = schema.get("parameters") if isinstance(schema, dict) else None
    return params if isinstance(params, dict) else None


def registry_tool_names() -> list[str]:
    from tools.registry import registry  # Hermes runtime only

    return list(registry.get_all_tool_names())


def hermes_coerce(tool_name: str, args: dict) -> dict:
    """Hermes's own schema-guided type fixing, the same step dispatch applies (raises outside Hermes)."""
    from tools.arg_coercion import coerce_tool_args  # Hermes runtime only (not a documented plugin API)

    return coerce_tool_args(tool_name, args)


def _is_gateway_field(name: object) -> bool:
    return isinstance(name, str) and name.startswith(GATEWAY_PREFIX)


def _fast_copy(val: Any) -> Any:
    """Fast deepcopy for JSON-serializable dicts/lists without copy.deepcopy reflection overhead."""
    if type(val) is dict:
        return {k: _fast_copy(v) for k, v in val.items()}
    if type(val) is list:
        return [_fast_copy(v) for v in val]
    return val


def strip_gateway_args(args: dict) -> dict:
    """Top-level arguments without the gateway's own fields (nested keys are the server's and are kept)."""
    return {k: v for k, v in args.items() if not _is_gateway_field(k)}


def _loosen(node: Any) -> Any:
    """Walk a schema (keywords only, never property names) applying the safety choices above."""
    if isinstance(node, list):
        return [_loosen(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for key, value in node.items():
        if key in _LEFT_TO_SERVER or key == "patternProperties":
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _loosen(sub) for name, sub in value.items()}
        elif key in _SCHEMA_VALUES:
            out[key] = _loosen(value)
        else:
            out[key] = value
    if "patternProperties" in node:
        out["additionalProperties"] = True
    if node.get("nullable") is True:
        kind = out.get("type")
        if isinstance(kind, str) and kind != "null":
            out["type"] = [kind, "null"]
        elif isinstance(kind, list) and "null" not in kind:
            out["type"] = [*kind, "null"]
        if isinstance(out.get("enum"), list) and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
    return out


def _prepare_rules(schema: dict) -> dict:
    try:
        rules = json.loads(json.dumps(schema))  # plain JSON data only
    except (TypeError, ValueError) as exc:
        raise FormCheckUnavailable("rules are not plain JSON") from exc
    if not isinstance(rules, dict):
        raise FormCheckUnavailable("no rules found for this tool")
    props = rules.get("properties")
    if isinstance(props, dict):
        for name in [n for n in props if _is_gateway_field(n)]:
            del props[name]
    required = rules.get("required")
    if isinstance(required, list):
        rules["required"] = [r for r in required if not _is_gateway_field(r)]
    return _loosen(rules)


def _describe(exc: Exception) -> str:
    text = str(getattr(exc, "message", "") or exc)
    if text.startswith("data"):
        text = "arguments" + text[len("data"):]
    text = _UNPRINTABLE.sub(" ", text).strip()
    if len(text) > MAX_PROBLEM_CHARS:
        text = text[:MAX_PROBLEM_CHARS - 3] + "..."
    return text or "arguments do not follow the tool's rules"


class FormChecker:
    """Validates arguments against the rules ``schema_fn(tool)`` returns (default: Hermes's registry,
    looked up when called). Thread-safe."""

    def __init__(self, schema_fn: Callable[[str], Any] | None = None,
                 coerce_fn: Callable[[str, dict], dict] | None = None) -> None:
        self._schema_fn = schema_fn
        self._coerce_fn = coerce_fn
        self._cache: OrderedDict[str, Callable[[Any], Any]] = OrderedDict()
        self._lock = threading.Lock()

    def coercion_problem(self) -> str | None:
        """Why Hermes's type fixing is unavailable (arguments are then checked as sent), else ``None``."""
        if self._coerce_fn is not None:
            return None
        try:
            from tools.arg_coercion import coerce_tool_args  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return (f"Hermes type fixing is unavailable ({type(exc).__name__}); "
                    "arguments are checked exactly as the model sent them")
        return None

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def prepare(self, tool: str) -> Callable[[Any], Any]:
        """Load and compile the tool's rules (cached by content). Raises ``FormCheckUnavailable``."""
        try:
            schema = (self._schema_fn or registry_schema)(tool)
        except Exception as exc:  # noqa: BLE001
            raise FormCheckUnavailable(f"could not read the rules ({type(exc).__name__})") from exc
        if not isinstance(schema, dict):
            raise FormCheckUnavailable("no rules found for this tool")
        rules = _prepare_rules(schema)
        key = hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            validate = self._cache.get(key)
            if validate is not None:
                self._cache.move_to_end(key)
                return validate
        try:
            validate = fastjsonschema.compile(rules, handlers=_RefuseRemoteRefs(), use_default=False,
                                              use_formats=False)
        except FormCheckUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any compile failure means the rules are unusable
            raise FormCheckUnavailable(f"rules could not be used ({type(exc).__name__})") from exc
        with self._lock:
            self._cache[key] = validate
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return validate

    def check(self, tool: str, args: dict) -> str | None:
        """``None`` when ``args`` follow the rules, else a short description of the first problem."""
        validate = self.prepare(tool)
        # ⚡ Optimization: Use _fast_copy() instead of copy.deepcopy() to avoid generic reflection
        # overhead (~1.5x speedup). coerce_fn receives a copy so raw is untouched if coercion fails after mutating.
        raw = _fast_copy(args) if isinstance(args, dict) else {}
        try:
            fixed = (self._coerce_fn or hermes_coerce)(tool, _fast_copy(raw))
        except Exception:  # noqa: BLE001 - without type fixing, check the arguments as sent
            fixed = raw
        data = strip_gateway_args(fixed if isinstance(fixed, dict) else raw)
        try:
            validate(data)
        except fastjsonschema.JsonSchemaValueException as exc:
            return _describe(exc)
        except Exception as exc:  # noqa: BLE001
            raise FormCheckUnavailable(f"the check failed ({type(exc).__name__})") from exc
        return None


# ---- self-check ------------------------------------------------------------------------------------

_CANARY_RULES = {"type": "object", "required": ["a"], "additionalProperties": False,
                 "properties": {"a": {"type": "string"}}}


def run_selfcheck(checker: FormChecker, names_fn: Callable[[], Iterable[str]],
                  form_checked: list[re.Pattern]) -> dict:
    """Prove the check works end to end: the validator, the rules lookup, and every form-checked tool's rules."""
    problems: list[str] = []
    canary = FormChecker(lambda _tool: _CANARY_RULES, coerce_fn=lambda _tool, args: args)
    try:
        if canary.check("canary", {"a": "x"}) is not None or canary.check("canary", {}) is None:
            problems.append("the form checker gives wrong answers")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the form checker does not run ({type(exc).__name__})")
    if (coercion := checker.coercion_problem()) is not None:
        problems.append(coercion)
    checked = 0
    names: list[str] | None = None
    error: Exception | None = None
    for _attempt in range(2):  # the registry can change while it is read (MCP re-registration)
        try:
            names = sorted(n for n in list(names_fn()) if any(p.fullmatch(n) for p in form_checked))
            break
        except Exception as exc:  # noqa: BLE001
            error = exc
    if names is None:
        names = []
        problems.append(f"could not read the tool registry ({type(error).__name__})")
    else:
        if not names:
            problems.append("no form-checked tools are registered (is the gateway connected?)")
    for name in names:
        try:
            checker.prepare(name)
            checked += 1
        except FormCheckUnavailable as exc:
            problems.append(f"{name}: {exc}")
    return {"ok": not problems, "tools_checked": checked, "problems": problems[:10]}


def write_status(path: str, status: dict) -> None:
    """Write the self-check result atomically (the sweep reads it)."""
    data = {**status, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pid": os.getpid()}
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".formcheck-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def selfcheck_loop(checker: FormChecker, names_fn: Callable[[], Iterable[str]], form_checked: list[re.Pattern],
                   status_path: str, *, interval: float = 1800.0, wait_timeout: float = 600.0,
                   poll: float = 10.0, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic, iterations: int | None = None,
                   on_tick: Callable[[], None] | None = None) -> None:
    """Wait until form-checked tools are registered (MCP discovery is asynchronous), then check and write the
    status every ``interval`` seconds. A process that never sees a form-checked tool (a short-lived Hermes helper
    without the gateway's MCP servers) writes nothing: it cannot speak for the gateway, and a missing or
    stale report is itself alerted on by the sweep. ``on_tick`` (optional) runs after each check (the park hook's
    ``LiftStatus.flush``: retries a lift report that could not be written). Never raises."""
    deadline = clock() + wait_timeout
    seen = False
    while not seen and clock() < deadline:
        try:
            seen = any(any(p.fullmatch(n) for p in form_checked) for n in names_fn())
        except Exception:  # noqa: BLE001
            seen = False
        if not seen:
            sleep(poll)
    if not seen:
        log.info("durable-park form self-check: no form-checked tools in this process; not reporting")
        return
    done = 0
    while iterations is None or done < iterations:
        try:
            status = run_selfcheck(checker, names_fn, form_checked)
            write_status(status_path, status)
            if status["ok"]:
                log.info("durable-park form check ok: %d form-checked tool(s)", status["tools_checked"])
            else:
                log.error("durable-park form check FAILED: %s", "; ".join(status["problems"]))
        except Exception as exc:  # noqa: BLE001
            log.error("durable-park form self-check could not run: %s", type(exc).__name__)
        if on_tick is not None:
            try:
                on_tick()
            except Exception as exc:  # noqa: BLE001
                log.error("durable-park self-check tick failed: %s", type(exc).__name__)
        done += 1
        if iterations is not None and done >= iterations:
            return
        sleep(interval)


_started = False
_started_lock = threading.Lock()


def _selfcheck_thread_alive() -> bool:
    """Process-wide: survives a re-import of this module (which would reset ``_started``)."""
    return any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())


def start_selfcheck(checker: FormChecker, form_checked: list[re.Pattern], status_path: str,
                    on_tick: Callable[[], None] | None = None) -> bool:
    """Start the background self-check once per Hermes process. Returns False outside Hermes."""
    global _started
    try:
        import tools.registry  # noqa: F401 - only inside a Hermes runtime
    except ImportError:
        return False
    with _started_lock:
        if _started or _selfcheck_thread_alive():
            return False
        _started = True
        threading.Thread(target=selfcheck_loop, args=(checker, registry_tool_names, form_checked, status_path),
                         kwargs={"on_tick": on_tick}, name=THREAD_NAME, daemon=True).start()
    return True
