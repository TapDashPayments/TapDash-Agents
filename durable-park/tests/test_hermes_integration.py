"""durable-park against the REAL Hermes code, where Hermes is importable (the proto VM); skipped everywhere else.

CI has no Hermes, so the unit tests keep their own picture of what Hermes does (``hermes_unwrap`` in
test_tool_search_compat.py, the bridge-name constants in park_hook.py and deploy/hermes_tool_policy.py). A picture can
drift from the code it copies. These tests run the real Hermes functions and pin the pictures to them:

* the bridge tool names Hermes synthesizes (``tools.tool_search_catalog.BRIDGE_TOOL_NAMES``) equal the plugin's and
  the tool policy's constants;
* the real ``agent.tool_executor._parse_tool_call``, the function that decides what ``pre_tool_call`` (durable-park's
  hook) receives for a ``tool_call``, gives the real tool name and the raw arguments for a single valid entry and keeps
  the ``tool_call`` wrapper in every other case, and ``hermes_unwrap`` agrees on every case it models.

Hermetic: ``HERMES_HOME`` points at an empty scratch directory (no real config, no source-update copy: Hermes modules
are only imported), nothing touches the network, and the only two things replaced are the ones that would read a live
session: the registry's toolset lookup (stand-in MCP and plugin tools) and the session's tool scope (one set of names).

Run on the VM (the Hermes venv has no pytest: borrow the pure-Python pytest packages from the durable-park venv):

    PYTHONPATH=~/.hermes/hermes-agent:<dir with symlinks to pytest, _pytest, pluggy, iniconfig, pygments> \\
      <hermes venv>/bin/python -m pytest -q -W error -p no:cacheprovider tests/test_hermes_integration.py

See docs/runbook.md, "Check durable-park against the real Hermes".
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from durable_park import park_hook
from durable_park.park_hook import BRIDGE_CALL_MESSAGE

# Pure-Python test helpers (the unit tests' picture of Hermes; the hook fixtures).
from test_tool_search_compat import PAYMENT, as_hermes, build, env, hermes_unwrap  # noqa: F401  (env is a fixture)


def _hermes_importable() -> bool:
    """True when the Hermes source tree is on ``sys.path``. Looks at the files only: importing ``agent`` here would
    run before the scratch ``HERMES_HOME`` is set."""
    try:
        agent, tools = importlib.util.find_spec("agent"), importlib.util.find_spec("tools")
        has = lambda spec, name: spec is not None and any(  # noqa: E731
            (Path(p) / name).is_file() for p in (spec.submodule_search_locations or ()))
        return has(agent, "tool_executor.py") and has(tools, "tool_search_catalog.py")
    except (ImportError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _hermes_importable(), reason="Hermes is not importable here (CI has none); run on the VM, see the module docstring")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
MCP_TOOLSET, PLUGIN_TOOLSET = "mcp-wh", "durable_park"
STAND_INS = {PAYMENT: MCP_TOOLSET, "parked_action_execute": PLUGIN_TOOLSET}
OUT_OF_SCOPE = "mcp__elsewhere__delete_everything"  # registered like an MCP tool, but not in this session's scope


@pytest.fixture(scope="module")
def hermes(tmp_path_factory):
    home = tmp_path_factory.mktemp("hermes-home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_HOME", str(home))
        from agent import tool_executor
        from tools import tool_search_catalog
        from tools.registry import registry

        real_get_entry = registry.get_entry
        toolsets = {**STAND_INS, OUT_OF_SCOPE: MCP_TOOLSET}
        mp.setattr(registry, "get_entry",
                   lambda name, *a, **kw: SimpleNamespace(toolset=toolsets[name]) if name in toolsets
                   else real_get_entry(name, *a, **kw))
        mp.setattr(tool_executor, "_tool_search_scoped_names", lambda agent: frozenset(STAND_INS))
        yield SimpleNamespace(executor=tool_executor, catalog=tool_search_catalog, home=home)


def model_call(hermes, arguments):
    """What Hermes's executor hands the hook for one model ``tool_call``: (name, args, parse_error, scope_block)."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    tc = SimpleNamespace(id="outer-1", function=SimpleNamespace(name="tool_call", arguments=raw))
    parsed = hermes.executor._parse_tool_call(SimpleNamespace(), tc)
    return parsed.name, parsed.args, parsed.parse_error, parsed.scope_block


def one(name, arguments=None):
    return {"name": name, "arguments": arguments if arguments is not None else {}}


ARGS = {"amount": 5, "customer": "C-1"}

# (id, what the model sends as tool_call's arguments, the (name, args) the hook receives when Hermes unwraps it)
UNWRAPPED = [
    ("calls list", {"calls": [one(PAYMENT, ARGS)]}, (PAYMENT, ARGS)),
    ("calls as one object", {"calls": one(PAYMENT, ARGS)}, (PAYMENT, ARGS)),
    ("calls as a JSON string", {"calls": json.dumps([one(PAYMENT, ARGS)])}, (PAYMENT, ARGS)),
    ("entry arguments as a JSON string", {"calls": [one(PAYMENT, json.dumps(ARGS))]}, (PAYMENT, ARGS)),
    ("legacy single shape", one(PAYMENT, ARGS), (PAYMENT, ARGS)),
    ("legacy single shape, JSON-string arguments", one(PAYMENT, json.dumps(ARGS)), (PAYMENT, ARGS)),
    ("no arguments", {"calls": [{"name": PAYMENT}]}, (PAYMENT, {})),
    ("blank arguments string", {"calls": [one(PAYMENT, "  ")]}, (PAYMENT, {})),
    ("the parked executor", {"calls": [one("parked_action_execute", {"park_id": "P-1A2B3C4D"})]},
     ("parked_action_execute", {"park_id": "P-1A2B3C4D"})),
]
# (id, arguments, models_scope): Hermes keeps the wrapper, so the hook sees tool_call. models_scope False: the case
# depends on the live session's tool scope, which hermes_unwrap does not model.
WRAPPED = [
    ("batch of two", {"calls": [one(PAYMENT, ARGS), one(PAYMENT, ARGS)]}, True),
    ("core tool named through the bridge", {"calls": [one("clarify", {"question": "x"})]}, True),
    ("connectors__ name", {"calls": [one("connectors__gmail__send_email", {"to": "a@b.c"})]}, True),
    ("nested tool_call", {"calls": [one("tool_call", {"calls": [one(PAYMENT, ARGS)]})]}, True),
    ("calls is not JSON", {"calls": "nope"}, True),
    ("entry arguments are not JSON", {"calls": [one(PAYMENT, "{not json")]}, True),
    ("entry arguments are a list", {"calls": [one(PAYMENT, [1, 2])]}, True),
    ("empty calls", {"calls": []}, True),
    ("no calls and no name", {}, True),
    ("tool outside the session's scope", {"calls": [one(OUT_OF_SCOPE, {})]}, False),
]


def test_the_bridge_names_are_the_ones_hermes_synthesizes(hermes):
    from_hermes = frozenset(hermes.catalog.BRIDGE_TOOL_NAMES)
    assert from_hermes == frozenset({"tool_search", "tool_describe", "tool_call"})
    assert from_hermes == frozenset(park_hook.BRIDGE_TOOLS)
    assert (hermes.catalog.TOOL_SEARCH_NAME, hermes.catalog.TOOL_DESCRIBE_NAME, hermes.catalog.TOOL_CALL_NAME) == (
        park_hook.BRIDGE_SEARCH_TOOL, park_hook.BRIDGE_DESCRIBE_TOOL, park_hook.BRIDGE_CALL_TOOL)
    assert from_hermes == frozenset(_policy().BRIDGE_TOOL_NAMES)
    assert park_hook.BRIDGE_CALL_TOOL not in park_hook.DEFAULT_EXEMPT_TOOLS
    assert set(park_hook.BRIDGE_READ_TOOLS) <= set(park_hook.DEFAULT_EXEMPT_TOOLS)


def _policy():
    spec = importlib.util.spec_from_file_location("hermes_tool_policy_under_test", DEPLOY / "hermes_tool_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", UNWRAPPED, ids=[c[0] for c in UNWRAPPED])
def test_a_single_valid_entry_reaches_the_hook_as_the_real_tool_with_the_raw_arguments(hermes, case):
    _, sent, expected = case
    name, args, parse_error, scope_block = model_call(hermes, sent)
    assert (name, args) == expected and parse_error is None and scope_block is None
    assert name != "tool_call"


@pytest.mark.parametrize("case", WRAPPED, ids=[c[0] for c in WRAPPED])
def test_every_other_tool_call_keeps_the_wrapper_and_reaches_the_hook_as_tool_call(hermes, case):
    _, sent, _ = case
    name, args, parse_error, _scope_block = model_call(hermes, sent)
    assert name == "tool_call" and parse_error is None
    assert args == (sent if isinstance(sent, dict) else json.loads(sent))  # the raw arguments, untouched


def test_tool_call_arguments_that_are_not_json_never_reach_the_hook_unwrapped(hermes):
    name, _args, parse_error, _scope_block = model_call(hermes, "{not json")
    assert name == "tool_call" and parse_error  # Hermes answers the model itself; it names no other tool


@pytest.mark.parametrize("case", [c for c in UNWRAPPED], ids=[c[0] for c in UNWRAPPED])
def test_the_unit_tests_picture_of_hermes_agrees_on_every_unwrapped_case(hermes, case):
    _, sent, expected = case
    assert hermes_unwrap("tool_call", sent) == expected == model_call(hermes, sent)[:2]


@pytest.mark.parametrize("case", [c for c in WRAPPED if c[2]], ids=[c[0] for c in WRAPPED if c[2]])
def test_the_unit_tests_picture_of_hermes_agrees_on_every_wrapped_case(hermes, case):
    _, sent, _ = case
    assert hermes_unwrap("tool_call", sent)[0] == "tool_call" == model_call(hermes, sent)[0]


def test_the_real_unwrap_drives_the_park_hook_end_to_end(hermes, env):
    """Hermes's own parse, then durable-park's hook: a wrapped payment parks under its real name; a wrapper is refused."""
    hook = build(env)
    name, args, *_ = model_call(hermes, {"calls": [one(PAYMENT, json.dumps(ARGS))]})
    result = hook(tool_name=name, args=args, session_id="sess-1", tool_call_id="outer-1", task_id="", turn_id="t1",
                  api_request_id="", middleware_trace=[])
    (row,) = env.ledger.list_by_status("parked")
    assert result["action"] == "block" and row.tool == PAYMENT and json.loads(row.args_json) == ARGS

    name, args, *_ = model_call(hermes, {"calls": [one(PAYMENT, ARGS), one(PAYMENT, ARGS)]})
    refused = hook(tool_name=name, args=args, session_id="sess-1", tool_call_id="outer-2", task_id="", turn_id="t1",
                   api_request_id="", middleware_trace=[])
    assert refused == {"action": "block", "message": BRIDGE_CALL_MESSAGE}
    assert env.ledger.get_by_call("sess-1", "outer-2") is None and len(env.provider.requests) == 1
