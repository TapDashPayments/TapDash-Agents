"""How register wires the optional audit_* settings: bad ones only switch the name off."""

import asyncio
from pathlib import Path
import logging

import httpx
import pytest
from test_park_hook import FakeCtx

import durable_park
from durable_park import register, wake as wake_mod

AUDIT = {"event_type": "approval.decided", "actor_type": "human", "actor_display_name": "Ann", "subject_type":
         "approval_request", "subject_id": "r", "data": {"channel": "web"}, "occurred_at": "2026-10-21T00:00:00Z"}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", lambda *a, **k: None)  # a passing tool policy report
    """Records what register hands the wake handler, and counts HTTP calls the audit factory would make."""
    seen = {"calls": [], "http": []}
    real = durable_park.make_audit_client_factory

    def recording(base_url, key_file, transport=None):
        seen["args"] = (base_url, key_file)
        return real(base_url, key_file,
                    transport=httpx.MockTransport(lambda r: seen["http"].append(r) or httpx.Response(200, json={"events": [AUDIT]})))
    monkeypatch.setattr(durable_park, "make_audit_client_factory", recording)

    async def fake_handle_wake(event, **kw):
        seen["calls"].append(kw)
    monkeypatch.setattr(durable_park, "handle_wake", fake_handle_wake)
    monkeypatch.setattr(wake_mod, "_logged_no_audit_key", False)

    def go(tmp_path, config=None):
        ctx = FakeCtx(tmp_path / "data", config)
        ctx.inject_message = lambda *a, **k: True
        register(ctx)
        asyncio.run(ctx.hooks["pre_gateway_dispatch"](event=object()))
        return ctx, seen["calls"][-1].get("audit_client_factory")
    return seen, go


def test_defaults_are_the_documented_path_and_url(tmp_path, wired):
    seen, go = wired
    ctx, factory = go(tmp_path)
    assert {"audit_key_file", "audit_base_url"} <= set(ctx.asked)
    assert seen["args"] == ("https://app.withhuman.ai", durable_park.DEFAULT_AUDIT_KEY_FILE)
    assert factory is not None


def test_the_source_defaults_are_the_documented_ones():
    text = Path(durable_park.__file__).read_text()  # conftest patches the constant, so read the source
    assert 'DEFAULT_AUDIT_KEY_FILE = "/etc/durable-park/audit_key"' in text
    assert 'DEFAULT_AUDIT_BASE_URL = "https://app.withhuman.ai"' in text


def test_settings_are_passed_through(tmp_path, wired):
    seen, go = wired
    key = tmp_path / "k"
    go(tmp_path, {"audit_key_file": str(key), "audit_base_url": "https://wh.example.test"})
    assert seen["args"] == ("https://wh.example.test", str(key))


@pytest.mark.parametrize("config", [
    {"audit_key_file": 5}, {"audit_key_file": None}, {"audit_key_file": ["a"]}, {"audit_key_file": ""},
    {"audit_base_url": 7}, {"audit_base_url": ""}, {"audit_base_url": {"x": 1}},
])
def test_a_non_string_setting_warns_and_disables_the_name_only(tmp_path, wired, caplog, config):
    seen, go = wired
    with caplog.at_level(logging.WARNING, logger="durable_park"):
        ctx, factory = go(tmp_path, config)
    assert factory is None and "args" not in seen
    assert any("must be non-empty strings" in r.getMessage() for r in caplog.records)
    # not the fail-closed path: the real hooks and the executor are still registered, ledger opened
    assert (tmp_path / "data" / "ledger.db").exists() and "pre_tool_call" in ctx.hooks
    assert "failed to load" not in caplog.text and "refusing every tool call" not in caplog.text


def test_expanduser_runtime_error_disables_the_name_only(tmp_path, wired, caplog, monkeypatch):
    seen, go = wired
    real = type(tmp_path).expanduser

    def selective(self):  # only the audit key path cannot be resolved (e.g. no home directory)
        if str(self).endswith("audit_key"):
            raise RuntimeError("Could not determine home directory.")
        return real(self)
    monkeypatch.setattr(type(tmp_path), "expanduser", selective)
    with caplog.at_level(logging.WARNING, logger="durable_park"):
        ctx, factory = go(tmp_path, {"audit_key_file": "~nobody/audit_key"})
    assert factory is None
    assert any("audit_key_file unusable (RuntimeError)" in r.getMessage() for r in caplog.records)
    assert "pre_tool_call" in ctx.hooks and "refusing every tool call" not in caplog.text


def test_missing_key_file_makes_no_http_call_and_logs_once(tmp_path, wired, caplog):
    seen, go = wired
    with caplog.at_level(logging.INFO, logger="durable_park"):
        ctx, factory = go(tmp_path, {"audit_key_file": str(tmp_path / "absent")})
        assert asyncio.run(wake_mod.lookup_decider("r", factory)) is None
        assert asyncio.run(wake_mod.lookup_decider("r2", factory)) is None
    assert seen["http"] == []
    missing = [r for r in caplog.records if "no audit key file" in r.getMessage()]
    assert len(missing) == 1 and missing[0].levelno == logging.WARNING


def test_present_key_file_does_reach_the_audit_endpoint(tmp_path, wired):
    seen, go = wired
    key = tmp_path / "audit_key"
    key.write_text("who_x\n")
    ctx, factory = go(tmp_path, {"audit_key_file": str(key)})
    assert asyncio.run(wake_mod.lookup_decider("r", factory)) == "Ann (web)"
    assert len(seen["http"]) == 1
