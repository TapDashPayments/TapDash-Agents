"""R21-4: startup warning when the running Hermes is not the version durable-park was built against."""

import logging
import sys
import types

import pytest

from durable_park import compat, register

from test_park_hook import FakeCtx

HOOK_NAMES = ("pre_tool_call", "pre_gateway_dispatch", "ctx.inject_message", "ctx.dispatch_tool",
              "ctx.register_tool", "MCP tool-error wording", "iter_hook_callbacks",
              "_middleware")


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "durable_park"]


@pytest.mark.parametrize("version", ["0.22.2", "v0.22.2"])
def test_supported_version_logs_nothing(caplog, version):
    with caplog.at_level(logging.DEBUG, logger="durable_park"):
        assert compat.check_hermes_version(logging.getLogger("durable_park"), get_version=lambda: version) is True
    assert _warnings(caplog) == []


@pytest.mark.parametrize("version, shown", [("0.23.0", "0.23.0"), (None, "an unknown version")])
def test_other_version_logs_one_warning_naming_every_dependency(caplog, version, shown):
    with caplog.at_level(logging.DEBUG, logger="durable_park"):
        assert compat.check_hermes_version(logging.getLogger("durable_park"), get_version=lambda: version) is False
    (record,) = _warnings(caplog)
    message = record.getMessage()
    assert "0.22.2" in message and shown in message
    for name in HOOK_NAMES:
        assert name in message


def test_a_failing_version_lookup_is_an_unknown_version_not_a_crash(caplog):
    def boom():
        raise RuntimeError("stamp unreadable")

    with caplog.at_level(logging.DEBUG, logger="durable_park"):
        assert compat.check_hermes_version(logging.getLogger("durable_park"), get_version=boom) is False
    (record,) = _warnings(caplog)
    assert "an unknown version" in record.getMessage()


def test_hermes_version_reads_hermes_version_info(monkeypatch):
    info = types.SimpleNamespace(base_version="0.22.2")
    module = types.ModuleType("hermes_cli.version_info")
    module.get_version_info = lambda: info
    package = types.ModuleType("hermes_cli")
    package.version_info = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.version_info", module)
    assert compat.hermes_version() == "0.22.2"


def test_hermes_version_falls_back_to_dunder_version(monkeypatch):
    package = types.ModuleType("hermes_cli")
    package.__version__ = "0.22.1"
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.version_info", None)  # import fails
    assert compat.hermes_version() == "0.22.1"


def test_hermes_version_outside_hermes_is_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.version_info", None)
    assert compat.hermes_version() is None


@pytest.mark.parametrize("version", ["0.99.0", "boom"])
def test_register_warns_but_still_loads_on_an_unsupported_version(tmp_path, monkeypatch, caplog, version):
    def get():
        if version == "boom":
            raise RuntimeError("x")
        return version

    monkeypatch.setattr(compat, "hermes_version", get)
    ctx = FakeCtx(tmp_path / "data")
    with caplog.at_level(logging.DEBUG, logger="durable_park"):
        register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "pre_gateway_dispatch"}
    assert set(ctx.tools) == {"parked_action_execute"}
    assert len([r for r in _warnings(caplog) if "0.22.2" in r.getMessage()]) == 1


def test_register_on_the_supported_version_does_not_warn(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(compat, "hermes_version", lambda: "0.22.2")
    with caplog.at_level(logging.DEBUG, logger="durable_park"):
        register(FakeCtx(tmp_path / "data"))
    assert [r for r in _warnings(caplog) if "0.22.2" in r.getMessage()] == []
