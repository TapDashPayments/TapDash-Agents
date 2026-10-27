"""Per-profile plugin settings: ``status_dir`` (where every status file goes) and ``profile`` (whose wakes it serves)."""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import durable_park
from durable_park import register
from durable_park.formcheck import STATUS_FILENAME as FORMCHECK_STATUS
from durable_park.gatecall import LIFT_STATUS_FILENAME
from test_park_hook import (COULD_NOT_OPEN, FakeCtx, POLICY_MESSAGE, REAL_POLICY_PROBLEM, UNREACHABLE,  # noqa: F401
                            fake_withhuman, with_session, write_policy_report)

SKIP = {"action": "skip", "reason": "durable-park wake"}


@pytest.fixture(autouse=True)
def _policy_passes(monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: None)


@pytest.fixture(autouse=True)
def _is_the_gateway(monkeypatch):
    monkeypatch.setattr(durable_park, "is_gateway_process", lambda: True)


class NamedCtx(FakeCtx):
    """A FakeCtx that also says which Hermes profile it runs in (``ctx.profile_name``, hermes_cli/plugins.py:392:
    ``"default"``, the ``~/.hermes/profiles/<name>`` id, or ``"custom"``). Without ``profile_name`` the attribute is
    missing, as on a Hermes that does not have it."""

    def __init__(self, data_dir, config=None, *, profile_name=None, **kw):
        super().__init__(data_dir, config, **kw)
        if profile_name is not None:
            self.profile_name = profile_name


class WakeCtx(NamedCtx):
    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        return True


def ev(chat_name, platform="webhook", park_id="P-1"):
    return SimpleNamespace(
        raw_message={"type": "wake", "park_id": park_id},
        source=SimpleNamespace(platform=SimpleNamespace(value=platform), chat_name=chat_name,
                               chat_type="webhook", chat_id="webhook:x:abc"))


def own_token(fake, profile):
    """A readable key file for *profile* next to the fake withHuman's token: ``<profile>.token`` for a person (the
    only name a named copy may use), the fixture's own token file for the default profile."""
    if profile in (None, "", "default"):
        return fake.token
    path = Path(fake.token).parent / f"{profile}.token"
    path.write_text("tok\n")
    return str(path)


def json_files(directory):
    return sorted(p.name for p in directory.iterdir() if p.suffix == ".json")


def call(ctx, tool="web_search", n="c"):
    return ctx.hooks["pre_tool_call"](tool_name=tool, args={"q": 1}, session_id="s", tool_call_id=n)


# --- item 1: every status file goes under status_dir -----------------------------------------------

def test_status_files_go_to_status_dir(tmp_path, monkeypatch, with_session):
    ledger_dir, status = tmp_path / "ledger", tmp_path / "ledger" / "profiles" / "sarah"
    captured = {}
    real_hook = durable_park.make_pre_tool_call

    def spy_hook(ledger, gate_factory, form_checked, **kw):
        captured.update(kw)
        return real_hook(ledger, gate_factory, form_checked, **kw)

    started = []
    monkeypatch.setattr(durable_park, "make_pre_tool_call", spy_hook)
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck",
                        lambda checker, risky, path, on_tick=None: started.append(path) or True)
    ctx = NamedCtx(tmp_path / "data", {"ledger_path": str(ledger_dir / "ledger.db"), "status_dir": str(status),
                                       "token_file": str(tmp_path / "sarah.token"), "profile": "sarah"},
                   profile_name="sarah")
    register(ctx)
    for n in ("c1", "c2", "c3", "c4"):  # an unreadable key opens the breaker: its status file is written
        assert call(ctx, n=n)["action"] == "block"
    assert status.is_dir()  # created on the way
    assert json.loads((status / "breaker_status.json").read_text())["open"] is True
    assert json.loads((status / "plugin_load.json").read_text())["mode"] == "asking"
    assert json.loads((status / "tool_policy_load.json").read_text())["ok"] is True
    assert captured["lift_status"].path == str(status / LIFT_STATUS_FILENAME)
    assert started == [str(status / FORMCHECK_STATUS)]
    assert captured["breaker"]._path == str(status / "breaker_status.json")
    # nothing but the ledger lands next to it
    assert json_files(ledger_dir) == []
    assert not (ledger_dir / LIFT_STATUS_FILENAME).exists()


def test_status_files_stay_next_to_the_ledger_without_the_setting(tmp_path, monkeypatch, with_session):
    ledger_dir = tmp_path / "ledger"
    captured, started = {}, []
    real_hook = durable_park.make_pre_tool_call

    def spy_hook(ledger, gate_factory, form_checked, **kw):
        captured.update(kw)
        return real_hook(ledger, gate_factory, form_checked, **kw)

    monkeypatch.setattr(durable_park, "make_pre_tool_call", spy_hook)
    monkeypatch.setattr("durable_park.formcheck.start_selfcheck",
                        lambda checker, risky, path, on_tick=None: started.append(path) or True)
    ctx = FakeCtx(tmp_path / "data", {"ledger_path": str(ledger_dir / "ledger.db"),
                                      "token_file": str(tmp_path / "no-such-token")})
    register(ctx)
    for n in ("c1", "c2", "c3"):
        call(ctx, n=n)
    assert json_files(ledger_dir) == ["breaker_status.json", "plugin_load.json", "tool_policy_load.json"]
    assert captured["lift_status"].path == str(ledger_dir / LIFT_STATUS_FILENAME)
    assert started == [str(ledger_dir / FORMCHECK_STATUS)]
    assert not (ledger_dir / "profiles").exists()


def test_the_tool_policy_report_is_read_from_status_dir_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(durable_park, "policy_problem", REAL_POLICY_PROBLEM)
    ledger_dir, status = tmp_path / "ledger", tmp_path / "status"
    ledger_dir.mkdir()
    status.mkdir()
    write_policy_report(status / "tool_policy_status.json", ok=False, problems=["terminal is open"])
    write_policy_report(ledger_dir / "tool_policy_status.json")  # a passing report next to the ledger: not read
    ctx = FakeCtx(tmp_path / "data", {"ledger_path": str(ledger_dir / "ledger.db"), "status_dir": str(status)})
    register(ctx)
    assert call(ctx) == {"action": "block", "message": POLICY_MESSAGE}
    verdict = json.loads((status / "tool_policy_load.json").read_text())
    assert verdict["ok"] is False and "terminal is open" in verdict["problem"]
    assert json.loads((status / "plugin_load.json").read_text())["mode"] == "refusing-policy"
    assert [p.name for p in ledger_dir.iterdir() if p.suffix == ".json"] == ["tool_policy_status.json"]


@pytest.mark.parametrize("how", ["bad-setting", "ledger-unavailable", "policy-refuses"])
def test_failure_paths_write_the_load_proof_to_status_dir(tmp_path, monkeypatch, how):
    ledger_dir, status = tmp_path / "ledger", tmp_path / "ledger" / "profiles" / "tom"
    config = {"ledger_path": str(ledger_dir / "ledger.db"), "status_dir": str(status)}
    if how == "bad-setting":
        config["exempt_tools"] = 5
    elif how == "ledger-unavailable":
        (ledger_dir / "ledger.db").mkdir(parents=True)
    else:
        monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    ctx = FakeCtx(tmp_path / "data", config)
    register(ctx)
    assert call(ctx)["action"] == "block"
    assert json.loads((status / "plugin_load.json").read_text())["mode"].startswith("refusing-")
    assert [p.name for p in ledger_dir.iterdir() if p.suffix == ".json"] == []


@pytest.mark.parametrize("bad", [5, "", [], {"a": 1}, True])
def test_a_bad_status_dir_refuses_everything(tmp_path, caplog, bad):
    caplog.set_level(logging.INFO, logger="durable_park")
    ledger_dir = tmp_path / "ledger"
    ctx = FakeCtx(tmp_path / "data", {"ledger_path": str(ledger_dir / "ledger.db"), "status_dir": bad})
    register(ctx)
    assert call(ctx)["action"] == "block"
    assert call(ctx, "clarify", "c2") is None  # the usual exemptions of the refusing hook
    assert "status_dir" in caplog.text
    # the load proof still has a home (the ledger's directory), and the failure is on record
    assert json.loads((ledger_dir / "plugin_load.json").read_text())["mode"] == "refusing-error"


def test_a_status_dir_that_cannot_be_created_is_logged_and_nothing_is_allowed(tmp_path, caplog, with_session):
    caplog.set_level(logging.INFO, logger="durable_park")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    ctx = FakeCtx(tmp_path / "data", {"status_dir": str(blocker / "sub"), "token_file": str(tmp_path / "no-token")})
    register(ctx)
    assert call(ctx)["action"] == "block"
    assert "status_dir" in caplog.text or "plugin_load.json" in caplog.text


# --- item 2: the profile setting ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../x", "Sarah", "main", "a b", "x" * 64, 5, ["sarah"], True, "sarah\n"])
def test_a_bad_profile_setting_refuses_everything(tmp_path, caplog, bad):
    caplog.set_level(logging.INFO, logger="durable_park")
    ctx = FakeCtx(tmp_path / "data", {"profile": bad})
    register(ctx)
    assert call(ctx)["action"] == "block"
    assert "profile" in caplog.text
    # the profile is unknown, so a wake of ANY profile is still kept from the agent
    drop = ctx.hooks["pre_gateway_dispatch"]
    for name in ("webhook/durable-park-wake", "webhook/durable-park-wake-sarah"):
        assert asyncio.run(drop(event=ev(name))) == SKIP
    assert asyncio.run(drop(event=ev("webhook/other"))) is None


@pytest.mark.parametrize("good", [None, "", "default", "sarah", "tom-2", "a_b"])
def test_a_good_profile_setting_loads_asking(tmp_path, with_session, fake_withhuman, good):
    config = {"token_file": own_token(fake_withhuman, good)}
    if good is not None:
        config["profile"] = good
    ctx = NamedCtx(tmp_path / "data", config, profile_name=good if good not in (None, "") else "default")
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")


# --- F1: the setting must match the profile the copy really runs in (ctx.profile_name) ------------------

def _refuses_everything(ctx, caplog, text):
    assert call(ctx)["action"] == "block"
    assert text in caplog.text
    # the profile is not trusted, so a wake of ANY profile is still kept from the agent
    drop = ctx.hooks["pre_gateway_dispatch"]
    for name in ("webhook/durable-park-wake", "webhook/durable-park-wake-sarah", "webhook/durable-park-wake-tom"):
        assert asyncio.run(drop(event=ev(name))) == SKIP


@pytest.mark.parametrize("setting, runs_in", [("sarah", "tom"), ("sarah", "default"), (None, "sarah"), ("", "tom"),
                                              ("default", "sarah"), ("sarah", "custom")])
def test_a_profile_setting_that_does_not_match_where_the_copy_runs_refuses_everything(tmp_path, caplog, setting,
                                                                                      runs_in):
    caplog.set_level(logging.INFO, logger="durable_park")
    config = {} if setting is None else {"profile": setting}
    ctx = NamedCtx(tmp_path / "data", config, profile_name=runs_in)
    register(ctx)
    _refuses_everything(ctx, caplog, "setting profile does not match the profile this copy runs in")


@pytest.mark.parametrize("config", [{}, {"profile": ""}, {"profile": "default"}])
def test_a_default_setting_may_run_in_a_custom_hermes_home(tmp_path, config):
    from durable_park import _profile_setting
    assert _profile_setting(NamedCtx(tmp_path / "data", config, profile_name="custom")) is None


def test_a_named_setting_with_no_profile_name_from_hermes_refuses_everything(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="durable_park")
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah"})  # no profile_name attribute at all
    register(ctx)
    _refuses_everything(ctx, caplog, "cannot confirm which profile this copy runs in")


@pytest.mark.parametrize("junk", [None, 5, ["sarah"], b"sarah"])
def test_a_profile_name_that_is_not_a_string_cannot_confirm_a_named_setting(tmp_path, caplog, junk):
    caplog.set_level(logging.INFO, logger="durable_park")
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah"})
    ctx.profile_name = junk
    register(ctx)
    _refuses_everything(ctx, caplog, "cannot confirm which profile this copy runs in")


@pytest.mark.parametrize("setting", [None, "", "default"])
def test_a_default_setting_without_a_profile_name_loads_as_before(tmp_path, with_session, fake_withhuman, setting):
    config = {"token_file": fake_withhuman.token}
    if setting is not None:
        config["profile"] = setting
    ctx = NamedCtx(tmp_path / "data", config)  # an older Hermes, or a stand-in: no profile_name
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")


@pytest.mark.parametrize("setting, runs_in", [(None, "default"), ("", "default"), ("default", "default"),
                                              ("sarah", "sarah"), ("tom-2", "tom-2")])
def test_a_matching_profile_setting_loads_asking(tmp_path, with_session, fake_withhuman, setting, runs_in):
    config = {"token_file": own_token(fake_withhuman, setting)}
    if setting is not None:
        config["profile"] = setting
    ctx = NamedCtx(tmp_path / "data", config, profile_name=runs_in)
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")


# --- F4a: the row's profile is the copy's own profile, so it can never disagree with the session key ---------

def _parked_rows(tmp_path):
    return durable_park.Ledger(str(tmp_path / "data" / "ledger.db")).list_by_status("parked")


@pytest.mark.parametrize("env_profile", ["", "tom", "sarah", "default", "main"])
def test_a_named_copy_stores_its_own_profile_whatever_the_turn_env_says(tmp_path, monkeypatch, with_session,
                                                                        fake_withhuman, env_profile):
    monkeypatch.setattr("durable_park.park_hook.default_user_profile", lambda: ("u-1", env_profile))
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah", "token_file": own_token(fake_withhuman, "sarah")},
                   profile_name="sarah")
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")
    (row,) = _parked_rows(tmp_path)
    assert row.profile == "sarah" and row.user_id == "u-1"


@pytest.mark.parametrize("env_profile, stored", [("", None), ("tom", "tom")])
def test_the_default_copy_keeps_storing_the_env_profile(tmp_path, monkeypatch, with_session, fake_withhuman,
                                                        env_profile, stored):
    monkeypatch.setattr("durable_park.park_hook.default_user_profile", lambda: ("u-1", env_profile))
    ctx = NamedCtx(tmp_path / "data", {"token_file": fake_withhuman.token}, profile_name="default")
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")
    (row,) = _parked_rows(tmp_path)
    assert row.profile == stored and row.user_id == "u-1"


# --- R2: a copy may only be configured with its own key file, checked at load (nothing is opened with it) ----

def _refused_and_nothing_opened(ctx, caplog, fake, own=None):
    assert call(ctx)["action"] == "block"
    assert "setting token_file does not belong to this profile" in caplog.text
    assert fake.provider.calls == []  # no approval request was opened, with anybody's key
    assert call(ctx, "clarify", "c2") is None  # the usual exemptions of the refusing hook
    # the profile setting itself is fine, so the copy drops its own route's wakes and no one else's
    drop = ctx.hooks["pre_gateway_dispatch"]
    for name in ("webhook/durable-park-wake", "webhook/durable-park-wake-sarah", "webhook/durable-park-wake-tom"):
        expected = SKIP if name == "webhook/durable-park-wake" + (f"-{own}" if own else "") else None
        assert asyncio.run(drop(event=ev(name))) == expected


def test_a_default_copy_wired_into_the_instances_dir_refuses_everything(tmp_path, monkeypatch, caplog, with_session,
                                                                        fake_withhuman):
    caplog.set_level(logging.INFO, logger="durable_park")
    instances = tmp_path / "instances"
    instances.mkdir()
    (instances / "sarah.token").write_text("tok\n")
    monkeypatch.setattr(durable_park.keys, "INSTANCES_DIR", str(instances))
    ctx = NamedCtx(tmp_path / "data", {"token_file": str(instances / "sarah.token")}, profile_name="default")
    register(ctx)
    _refused_and_nothing_opened(ctx, caplog, fake_withhuman)


def test_a_default_copy_wired_to_a_symlink_into_the_instances_dir_refuses(tmp_path, monkeypatch, caplog, with_session,
                                                                          fake_withhuman):
    caplog.set_level(logging.INFO, logger="durable_park")
    instances = tmp_path / "instances"
    instances.mkdir()
    (instances / "sarah.token").write_text("tok\n")
    link = tmp_path / "innocent"
    link.symlink_to(instances / "sarah.token")
    monkeypatch.setattr(durable_park.keys, "INSTANCES_DIR", str(instances))
    ctx = NamedCtx(tmp_path / "data", {"token_file": str(link)}, profile_name="default")
    register(ctx)
    _refused_and_nothing_opened(ctx, caplog, fake_withhuman)


def test_a_named_copy_wired_to_another_persons_key_refuses_everything(tmp_path, caplog, with_session, fake_withhuman):
    caplog.set_level(logging.INFO, logger="durable_park")
    sarah_key = own_token(fake_withhuman, "sarah")
    ctx = NamedCtx(tmp_path / "data", {"profile": "tom", "token_file": sarah_key}, profile_name="tom")
    register(ctx)
    _refused_and_nothing_opened(ctx, caplog, fake_withhuman, own="tom")


@pytest.mark.parametrize("name", ["sarah.token.bak", "token", "tom.token", "sarah", "xsarah.token"])
def test_a_named_copy_needs_the_exact_basename(tmp_path, caplog, with_session, fake_withhuman, name):
    caplog.set_level(logging.INFO, logger="durable_park")
    path = tmp_path / "keys" / name
    path.parent.mkdir()
    path.write_text("tok\n")
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah", "token_file": str(path)}, profile_name="sarah")
    register(ctx)
    _refused_and_nothing_opened(ctx, caplog, fake_withhuman, own="sarah")


@pytest.mark.parametrize("bad", [5, ["x"], True, {"a": 1}])
def test_a_token_file_that_is_not_a_string_refuses_everything(tmp_path, caplog, bad):
    caplog.set_level(logging.INFO, logger="durable_park")
    ctx = NamedCtx(tmp_path / "data", {"token_file": bad})
    register(ctx)
    assert call(ctx)["action"] == "block"
    assert "token_file" in caplog.text


def test_a_named_copy_with_its_own_key_loads_in_any_directory(tmp_path, with_session, fake_withhuman):
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah", "token_file": own_token(fake_withhuman, "sarah")},
                   profile_name="sarah")
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")
    assert len(fake_withhuman.provider.calls) > 0


def test_a_default_copy_with_a_normal_path_loads(tmp_path, monkeypatch, with_session, fake_withhuman):
    instances = tmp_path / "instances"
    instances.mkdir()
    monkeypatch.setattr(durable_park.keys, "INSTANCES_DIR", str(instances))
    ctx = NamedCtx(tmp_path / "data", {"token_file": fake_withhuman.token}, profile_name="default")
    register(ctx)
    assert call(ctx)["message"].startswith("PARKED ")


# --- item 3: the wake hook serves only this copy's route ------------------------------------------------

def test_the_wake_hook_is_given_its_profile_and_key(tmp_path, monkeypatch, with_session):
    seen = {}

    async def spy(event, **kw):
        seen.update(kw)
        return SKIP

    monkeypatch.setattr(durable_park, "handle_wake", spy)
    tok = tmp_path / "instances" / "sarah.token"
    tok.parent.mkdir()
    tok.write_text("tok-9\n")
    status = tmp_path / "status"
    ctx = WakeCtx(tmp_path / "data", {"profile": "sarah", "token_file": str(tok), "status_dir": str(status)},
                  profile_name="sarah")
    register(ctx)
    asyncio.run(ctx.hooks["pre_gateway_dispatch"](event=ev("webhook/durable-park-wake-sarah")))
    assert seen["own_profile"] == "sarah" and seen["token_file"] == str(tok) and seen["status_dir"] == str(status)
    client = asyncio.run(_build(seen["client_for_key_file"], str(tok)))
    assert client._headers["Authorization"] == "Bearer tok-9"


async def _build(factory, path):
    client = factory(path)
    await client.aclose()
    return client


def test_a_default_copy_passes_no_profile_and_its_own_status_dir(tmp_path, monkeypatch):
    seen = {}

    async def spy(event, **kw):
        seen.update(kw)
        return SKIP

    monkeypatch.setattr(durable_park, "handle_wake", spy)
    ctx = WakeCtx(tmp_path / "data", {"ledger_path": str(tmp_path / "led" / "ledger.db")})
    register(ctx)
    asyncio.run(ctx.hooks["pre_gateway_dispatch"](event=ev("webhook/durable-park-wake")))
    assert seen["own_profile"] is None
    assert seen["status_dir"] == str(tmp_path / "led")
    assert seen["token_file"] == durable_park.DEFAULT_TOKEN_FILE


@pytest.mark.parametrize("how", ["bad-setting", "ledger-unavailable", "policy-refuses"])
def test_every_fallback_hook_drops_only_its_own_route(tmp_path, monkeypatch, how):
    config = {"profile": "sarah"}
    if how == "bad-setting":
        config["exempt_tools"] = 5
    elif how == "ledger-unavailable":
        config["ledger_path"] = str(tmp_path)  # a directory
    else:
        monkeypatch.setattr(durable_park, "policy_problem", lambda path, now, max_age=None: "stale")
    ctx = WakeCtx(tmp_path / "data", config, profile_name="sarah")
    register(ctx)
    drop = ctx.hooks["pre_gateway_dispatch"]
    assert asyncio.run(drop(event=ev("webhook/durable-park-wake-sarah"))) == SKIP
    assert asyncio.run(drop(event=ev("webhook/durable-park-wake"))) is None
    assert asyncio.run(drop(event=ev("webhook/durable-park-wake-tom"))) is None


# --- item 9: an unreadable key at runtime ---------------------------------------------------------------

@pytest.mark.parametrize("problem", ["missing", "empty", "not_text"])
def test_runtime_unreadable_key_refuses_and_alerts(tmp_path, with_session, problem):
    """The key file goes bad while the gateway runs: every call is refused (never allowed), and the breaker
    status file under status_dir says so after three calls, which is what the sweep alerts on."""
    key = tmp_path / "instances" / "sarah.token"
    key.parent.mkdir()
    status = tmp_path / "status"
    ledger_dir = tmp_path / "ledger"
    ctx = NamedCtx(tmp_path / "data", {"profile": "sarah", "token_file": str(key), "status_dir": str(status),
                                       "ledger_path": str(ledger_dir / "ledger.db")}, profile_name="sarah")
    if problem == "empty":
        key.write_text("")
    elif problem == "not_text":
        key.write_bytes(b"\xff\xfe")
    register(ctx)
    results = [call(ctx, n=f"c{i}") for i in range(5)]
    assert all(r is not None and r["action"] == "block" for r in results)  # never let through
    status_file = status / "breaker_status.json"
    assert json.loads(status_file.read_text())["open"] is True
    assert not (ledger_dir / "breaker_status.json").exists()
    ledger = durable_park.Ledger(str(ledger_dir / "ledger.db"))
    assert ledger.list_by_status("allowed", "parked", "approved", "executing") == []
    # once open, calls are refused at once with the "unreachable" message
    assert results[-1]["message"] == UNREACHABLE.format(tool="web_search")


def test_two_unreadable_key_calls_do_not_open_the_breaker_yet(tmp_path, with_session):
    ctx = FakeCtx(tmp_path / "data", {"token_file": str(tmp_path / "gone")})
    register(ctx)
    for n in ("c1", "c2"):
        assert call(ctx, n=n) == {"action": "block", "message": COULD_NOT_OPEN}
    assert not (tmp_path / "data" / "breaker_status.json").exists()  # the threshold is three, as for an outage
