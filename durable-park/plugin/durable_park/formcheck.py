"""Check a risky call's arguments against the tool's own input rules before it is parked.

The rules are the JSON Schema Hermes registered for the tool (the same schema the model is shown), read
fresh on every call. A call that breaks them is refused with the first problem found, so the model can fix
it and no approval is wasted on a call the server would reject. Anything that stops the check from running
(no rules, unusable rules, a lookup error) raises ``FormCheckUnavailable`` and the caller fails closed.

Safety choices:
- the withHuman gateway's own top-level fields (``withhuman_scope``, ``withhuman_context``) are removed
  from both the arguments and the rules first: the gateway strips them before the server sees the call;
- ``$ref`` is followed only inside the rules themselves; every other reference is refused (never fetched);
- ``format`` is not enforced and defaults are never filled in, so the check is never stricter than the
  rules' structure and never changes the arguments.
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

GATEWAY_FIELDS = frozenset({"withhuman_scope", "withhuman_context"})
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


def strip_gateway_args(args: dict) -> dict:
    """Top-level arguments without the gateway's own fields (nested keys are the server's and are kept)."""
    return {k: v for k, v in args.items() if k not in GATEWAY_FIELDS}


def _strip_gateway_rules(schema: dict) -> dict:
    rules = copy.deepcopy(schema)
    props = rules.get("properties")
    if isinstance(props, dict):
        for name in GATEWAY_FIELDS:
            props.pop(name, None)
    required = rules.get("required")
    if isinstance(required, list):
        rules["required"] = [r for r in required if r not in GATEWAY_FIELDS]
    return rules


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

    def __init__(self, schema_fn: Callable[[str], Any] | None = None) -> None:
        self._schema_fn = schema_fn
        self._cache: OrderedDict[str, Callable[[Any], Any]] = OrderedDict()
        self._lock = threading.Lock()

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
        rules = _strip_gateway_rules(schema)
        try:
            key = hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise FormCheckUnavailable("rules are not plain JSON") from exc
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
        data = copy.deepcopy(strip_gateway_args(args if isinstance(args, dict) else {}))
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
                  risky: list[re.Pattern]) -> dict:
    """Prove the check works end to end: the validator, the rules lookup, and every risky tool's rules."""
    problems: list[str] = []
    canary = FormChecker(lambda _tool: _CANARY_RULES)
    try:
        if canary.check("canary", {"a": "x"}) is not None or canary.check("canary", {}) is None:
            problems.append("the form checker gives wrong answers")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the form checker does not run ({type(exc).__name__})")
    checked = 0
    try:
        names = sorted(n for n in names_fn() if any(p.fullmatch(n) for p in risky))
    except Exception as exc:  # noqa: BLE001
        names = []
        problems.append(f"could not read the tool registry ({type(exc).__name__})")
    else:
        if not names:
            problems.append("no risky tools are registered (is the gateway connected?)")
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


def selfcheck_loop(checker: FormChecker, names_fn: Callable[[], Iterable[str]], risky: list[re.Pattern],
                   status_path: str, *, interval: float = 1800.0, wait_timeout: float = 600.0,
                   poll: float = 10.0, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic, iterations: int | None = None) -> None:
    """Wait until risky tools are registered (MCP discovery is asynchronous), then check and write the
    status every ``interval`` seconds. Never raises."""
    deadline = clock() + wait_timeout
    while clock() < deadline:
        try:
            if any(any(p.fullmatch(n) for p in risky) for n in names_fn()):
                break
        except Exception:  # noqa: BLE001
            pass
        sleep(poll)
    done = 0
    while iterations is None or done < iterations:
        try:
            status = run_selfcheck(checker, names_fn, risky)
            write_status(status_path, status)
            if status["ok"]:
                log.info("durable-park form check ok: %d risky tool(s)", status["tools_checked"])
            else:
                log.error("durable-park form check FAILED: %s", "; ".join(status["problems"]))
        except Exception as exc:  # noqa: BLE001
            log.error("durable-park form self-check could not run: %s", type(exc).__name__)
        done += 1
        if iterations is not None and done >= iterations:
            return
        sleep(interval)


_started = False
_started_lock = threading.Lock()


def start_selfcheck(checker: FormChecker, risky: list[re.Pattern], status_path: str) -> bool:
    """Start the background self-check once per Hermes process. Returns False outside Hermes."""
    global _started
    try:
        import tools.registry  # noqa: F401 - only inside a Hermes runtime
    except ImportError:
        return False
    with _started_lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=selfcheck_loop, args=(checker, registry_tool_names, risky, status_path),
                     name="durable-park-formcheck", daemon=True).start()
    return True
