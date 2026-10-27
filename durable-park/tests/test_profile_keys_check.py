"""deploy/profile_keys_check.py: which Hermes profiles exist, and whether each one that has tools has its own key."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("profile_keys_check", DEPLOY / "profile_keys_check.py")
pkc = importlib.util.module_from_spec(spec)
sys.modules["profile_keys_check"] = pkc  # dataclasses look their module up by name
spec.loader.exec_module(pkc)

HELPER = "/opt/durable-park/libexec/hermes-secrets"


def F(has_tools, key_sha256, secrets_command, secrets_enabled=True, secrets_override=True, **more):
    return pkc.ProfileFacts(has_tools, key_sha256, secrets_command, secrets_enabled, secrets_override, **more)


# ---- key_problems: the plan's table ----------------------------------------------------------------------

def test_ok():
    assert pkc.key_problems({"a": F(True, "h1", "hermes-secrets a"), "b": F(True, "h2", "hermes-secrets b")}) == []


def test_missing_key():
    assert pkc.key_problems({"a": F(True, None, "hermes-secrets a")}) == ["profile a: no key file"]


def test_unreadable_or_empty_key_says_so():
    assert pkc.key_problems({"a": F(True, None, "hermes-secrets a", key_unreadable=True)}) == [
        "profile a: key file is empty or unreadable"]


def test_wrong_secrets_command():
    assert pkc.key_problems({"a": F(True, "h1", "hermes-secrets b")}) == ["profile a: secrets command does not name a"]


def test_no_secrets_command_does_not_name_the_profile():
    assert pkc.key_problems({"a": F(True, "h1", None)}) == ["profile a: secrets command does not name a"]


def test_secrets_command_with_extra_words_does_not_name_only_the_profile():
    assert pkc.key_problems({"a": F(True, "h1", "hermes-secrets a b")}) == ["profile a: secrets command does not name a"]


def test_shared_key():
    got = pkc.key_problems({"a": F(True, "h", "hermes-secrets a"), "b": F(True, "h", "hermes-secrets b")})
    assert got == ["profiles a, b share a key"]


def test_three_profiles_sharing_a_key_are_named_in_order():
    profiles = {n: F(True, "h", f"hermes-secrets {n}") for n in ("c", "a", "b")}
    assert pkc.key_problems(profiles) == ["profiles a, b, c share a key"]


def test_a_person_sharing_the_default_profiles_key_is_a_problem():
    got = pkc.key_problems({"default": F(True, "h", HELPER, True, False), "a": F(True, "h", "hermes-secrets a")})
    assert got == ["profiles a, default share a key"]


def test_not_set_up_must_have_no_tools():
    assert pkc.key_problems({"not-set-up": F(True, None, None, False, False)}) == ["profile not-set-up: has tools"]


def test_not_set_up_without_tools_needs_nothing():
    assert pkc.key_problems({"not-set-up": F(False, None, None, False, False)}) == []


def test_not_set_up_never_counts_in_a_shared_key():
    got = pkc.key_problems({"not-set-up": F(False, "h", None, False, False), "a": F(True, "h", "hermes-secrets a")})
    assert got == []


def test_a_profile_without_tools_needs_no_key():
    assert pkc.key_problems({"quiet": F(False, None, None, False, False)}) == []


def test_default_profile_uses_helper_without_argument():
    assert pkc.key_problems({"default": F(True, "h0", HELPER, True, False)}) == []


def test_default_profile_must_not_name_a_profile():
    assert pkc.key_problems({"default": F(True, "h0", f"{HELPER} sarah", True, False)}) == [
        "profile default: secrets command must not name a profile"]


def test_default_profile_without_a_key_file():
    assert pkc.key_problems({"default": F(True, None, HELPER, True, False)}) == ["profile default: no key file"]


def test_secrets_switch_off():
    assert "profile a: secrets command is off" in pkc.key_problems({"a": F(True, "h1", "hermes-secrets a", False, True)})


def test_secrets_switch_off_for_the_default_profile():
    assert pkc.key_problems({"default": F(True, "h", HELPER, False, False)}) == ["profile default: secrets command is off"]


def test_secrets_without_override():
    assert pkc.key_problems({"a": F(True, "h1", "hermes-secrets a", True, False)}) == [
        "profile a: secrets command does not override"]


def test_another_program_is_not_the_helper():
    assert pkc.key_problems({"a": F(True, "h1", "printf a")}) == ["profile a: secrets command is not the durable-park helper"]


def test_unparseable_command_is_a_problem_not_a_crash():
    assert pkc.key_problems({"a": F(True, "h1", "hermes-secrets 'a")}) == ["profile a: secrets command does not name a"]


def test_problems_come_out_in_profile_order():
    got = pkc.key_problems({"b": F(True, None, "hermes-secrets b"), "a": F(True, None, "hermes-secrets a")})
    assert got == ["profile a: no key file", "profile b: no key file"]


# ---- which profile folders count --------------------------------------------------------------------------

def make_profile(home, name, marker="config.yaml"):
    folder = home / "profiles" / name
    folder.mkdir(parents=True, exist_ok=True)
    if marker:
        (folder / marker).write_text("{}")
    return folder


def names(home, **kw):
    return [p.name for p in pkc.installed_profiles(home, **kw)]


def test_no_profiles_folder_means_no_profiles(tmp_path):
    assert names(tmp_path) == []


def test_a_folder_with_a_marker_is_a_profile(tmp_path):
    for i, marker in enumerate(("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")):
        make_profile(tmp_path, f"p{i}", marker)
    assert names(tmp_path) == ["p0", "p1", "p2", "p3", "p4", "p5"]


def test_a_folder_with_no_marker_is_not_a_profile(tmp_path):
    make_profile(tmp_path, "ghost", marker=None)
    assert names(tmp_path) == []


def test_a_dangling_symlinked_marker_still_counts(tmp_path):
    folder = make_profile(tmp_path, "linked", marker=None)
    (folder / "config.yaml").symlink_to(tmp_path / "nowhere")
    assert names(tmp_path) == ["linked"]


@pytest.mark.parametrize("bad", ["Sarah", ".hidden", ".deleted", "a b", "x.y", "-x"])
def test_names_hermes_would_not_serve_are_skipped(tmp_path, bad):
    make_profile(tmp_path, bad)
    assert names(tmp_path) == []


def test_default_is_never_a_named_profile(tmp_path):
    make_profile(tmp_path, "default")
    assert names(tmp_path) == []


def test_a_deleted_profile_is_skipped(tmp_path):
    make_profile(tmp_path, "gone")
    (tmp_path / "profiles" / ".deleted").mkdir()
    (tmp_path / "profiles" / ".deleted" / "gone").write_text("x")
    assert names(tmp_path) == []


def test_a_file_in_the_profiles_folder_is_not_a_profile(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "notes").write_text("x")
    assert names(tmp_path) == []


def test_parked_profiles_are_listed_but_flagged(tmp_path):
    make_profile(tmp_path, "asleep")
    (tmp_path / "profiles" / "asleep" / "gateway.parked").write_text("")
    make_profile(tmp_path, "awake")
    found = {p.name: p.parked for p in pkc.installed_profiles(tmp_path)}
    assert found == {"asleep": True, "awake": False}
    assert names(tmp_path, serving_only=True) == ["awake"]


def test_a_64_character_name_is_a_hermes_profile(tmp_path):
    make_profile(tmp_path, "a" * 64)
    assert names(tmp_path) == ["a" * 64]


# ---- facts from the files ---------------------------------------------------------------------------------

def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_json_config(folder, secrets=None, gateway=None):
    config = {}
    if secrets is not None:
        config["secrets"] = {"command": secrets}
    if gateway is not None:
        config["gateway"] = gateway
    (folder / "config.yaml").write_text(json.dumps(config))


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    keys = tmp_path / "etc"
    (keys / "instances").mkdir(parents=True)
    status = tmp_path / "state"
    status.mkdir()
    return home, keys, status


def gather(world, **kw):
    home, keys, status = world
    return pkc.gather(home, instances_dir=str(keys / "instances"), default_key=str(keys / "instance_token"),
                      status_root=status, read_config=lambda path: json.loads(path.read_text()), **kw)


def test_facts_of_a_default_profile_and_a_person(world):
    home, keys, status = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").write_text("key-default\nignored second line\n")
    sarah = make_profile(home, "sarah")
    write_json_config(sarah, {"enabled": True, "override_existing": True, "command": f"{HELPER} sarah"})
    (keys / "instances" / "sarah.token").write_text("key-sarah\n")
    facts, extra = gather(world)
    assert extra == []
    assert facts["default"] == pkc.ProfileFacts(True, sha("key-default"), HELPER, True, False)
    assert facts["sarah"] == pkc.ProfileFacts(True, sha("key-sarah"), f"{HELPER} sarah", True, True)
    assert pkc.key_problems(facts) == []


def test_the_key_hash_is_of_the_first_line_only(world):
    home, keys, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").write_text("k\nsecond\n")
    facts, _ = gather(world)
    assert facts["default"].key_sha256 == sha("k")


def test_a_missing_key_file(world):
    home, _, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    facts, _ = gather(world)
    assert facts["default"].key_sha256 is None and facts["default"].key_unreadable is False


@pytest.mark.parametrize("content", ["", "\n", "   \n"])
def test_an_empty_key_file_is_unreadable_not_missing(world, content):
    home, keys, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").write_text(content)
    facts, _ = gather(world)
    assert facts["default"].key_sha256 is None and facts["default"].key_unreadable is True


def test_a_key_that_is_a_directory_is_unreadable(world):
    home, keys, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").mkdir()
    facts, _ = gather(world)
    assert facts["default"].key_unreadable is True


def test_every_profile_but_not_set_up_has_tools_whatever_the_status_says(world):
    home, keys, status = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (status / "tool_policy_status.json").write_text(json.dumps({"ok": True, "has_tools": False}))
    facts, _ = gather(world)
    assert facts["default"].has_tools is True


def test_not_set_up_has_no_tools_only_when_its_own_report_says_so(world):
    home, _, status = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    quiet = make_profile(home, "not-set-up")
    write_json_config(quiet)
    report = status / "profiles" / "not-set-up"
    report.mkdir(parents=True)
    (report / "tool_policy_status.json").write_text(json.dumps({"ok": True, "has_tools": False}))
    assert gather(world)[0]["not-set-up"].has_tools is False
    (report / "tool_policy_status.json").write_text(json.dumps({"ok": True, "has_tools": True}))
    assert gather(world)[0]["not-set-up"].has_tools is True


@pytest.mark.parametrize("report", [None, "{not json", "[]", '{"ok": true}', '{"has_tools": "no"}', '{"has_tools": 0}'])
def test_not_set_up_without_a_clear_report_is_treated_as_having_tools(world, report):
    home, _, status = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    write_json_config(make_profile(home, "not-set-up"))
    if report is not None:
        folder = status / "profiles" / "not-set-up"
        folder.mkdir(parents=True)
        (folder / "tool_policy_status.json").write_text(report)
    assert gather(world)[0]["not-set-up"].has_tools is True


def test_an_unreadable_config_is_a_named_problem_and_fails_closed(world):
    home, keys, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").write_text("k")
    broken = make_profile(home, "sarah")
    (broken / "config.yaml").write_text("{not json")
    facts, extra = gather(world)
    assert extra == ["profile sarah: config cannot be read"]
    assert facts["sarah"].has_tools is True and facts["sarah"].secrets_command is None


def test_the_default_profiles_unreadable_config_is_named(world):
    home, _, _ = world
    (home / "config.yaml").write_text("{not json")
    _, extra = gather(world)
    assert extra == ["profile default: config cannot be read"]


def test_a_profile_without_config_yaml_has_no_secrets_command(world):
    home, _, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    make_profile(home, "sarah", marker=".env")
    facts, extra = gather(world)
    assert extra == [] and facts["sarah"].secrets_command is None and facts["sarah"].secrets_enabled is False


@pytest.mark.parametrize("flag, standalone", [(True, True), ("true", True), ("on", True), (False, False), (None, False)])
def test_a_standalone_profile_is_a_problem(world, flag, standalone):
    home, _, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    write_json_config(make_profile(home, "sarah"), gateway={"standalone": flag})
    _, extra = gather(world)
    assert (extra == ["profile sarah: gateway.standalone is on; this setup does not support it"]) is standalone


def test_a_name_the_key_rule_does_not_allow_is_a_problem(world):
    home, _, _ = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    write_json_config(make_profile(home, "a" * 64))
    write_json_config(make_profile(home, "main"))
    facts, extra = gather(world)
    assert extra == [f"profile {'a' * 64}: name is not allowed", "profile main: name is not allowed"]
    assert set(facts) == {"default"}


def test_the_raw_secrets_values_must_be_exactly_true(world):
    home, _, _ = world
    write_json_config(home, {"enabled": "yes", "override_existing": 1, "command": HELPER})
    facts, _ = gather(world)
    assert facts["default"].secrets_enabled is False and facts["default"].secrets_override is False


# ---- the command line -------------------------------------------------------------------------------------

def run(world, *extra, monkeypatch=None):
    home, keys, status = world
    if monkeypatch is not None:
        monkeypatch.setattr(pkc, "read_yaml", lambda path: json.loads(path.read_text()))
    return pkc.main(["check", "--hermes-home", str(home), "--status-root", str(status),
                     "--instances-dir", str(keys / "instances"), "--default-key", str(keys / "instance_token"), *extra])


def good_world(world):
    home, keys, status = world
    write_json_config(home, {"enabled": True, "command": HELPER})
    (keys / "instance_token").write_text("kd")
    sarah = make_profile(home, "sarah")
    write_json_config(sarah, {"enabled": True, "override_existing": True, "command": f"{HELPER} sarah"})
    (keys / "instances" / "sarah.token").write_text("ks")


def passing_report(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ok": True, "checked_at": "2026-10-27T10:00:00Z", "problems": [], "allowed": []}))


def test_check_passes_and_touches_nothing(world, monkeypatch, capsys):
    good_world(world)
    _, _, status = world
    passing_report(status / "tool_policy_status.json")
    before = (status / "tool_policy_status.json").read_text()
    assert run(world, monkeypatch=monkeypatch) == 0
    assert capsys.readouterr().out == ""
    assert (status / "tool_policy_status.json").read_text() == before
    assert not (status / "profiles").exists()


def test_check_with_a_problem_fails_every_report(world, monkeypatch, capsys):
    good_world(world)
    home, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    make_profile(home, "tara")  # a second profile, with no config of its own
    passing_report(status / "tool_policy_status.json")
    passing_report(status / "profiles" / "sarah" / "tool_policy_status.json")
    assert run(world, monkeypatch=monkeypatch) == 1
    out = capsys.readouterr().out.splitlines()
    assert "profile sarah: no key file" in out and "profile tara: no key file" in out
    for report in (status / "tool_policy_status.json", status / "profiles" / "sarah" / "tool_policy_status.json",
                   status / "profiles" / "tara" / "tool_policy_status.json"):
        data = json.loads(report.read_text())
        assert data["ok"] is False and "profile sarah: no key file" in data["problems"], report
        assert data["checked_at"].endswith("Z") and len(data["checked_at"]) == 20
    assert not list(status.rglob(".status-*"))  # no temp file left


def test_check_keeps_what_the_report_already_said(world, monkeypatch):
    good_world(world)
    _, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    report = status / "tool_policy_status.json"
    report.write_text(json.dumps({"ok": False, "checked_at": "2026-10-27T10:00:00Z", "problems": ["cli: LEAK terminal"],
                                  "allowed": ["web"], "has_tools": False}))
    assert run(world, monkeypatch=monkeypatch) == 1
    data = json.loads(report.read_text())
    assert data["problems"] == ["cli: LEAK terminal", "profile sarah: no key file"]
    assert data["allowed"] == ["web"] and data["has_tools"] is False


def test_check_does_not_repeat_a_problem_it_already_wrote(world, monkeypatch):
    good_world(world)
    _, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    assert run(world, monkeypatch=monkeypatch) == 1
    assert run(world, monkeypatch=monkeypatch) == 1
    assert json.loads((status / "tool_policy_status.json").read_text())["problems"] == ["profile sarah: no key file"]


def test_check_replaces_a_report_that_is_not_json(world, monkeypatch):
    good_world(world)
    _, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    (status / "tool_policy_status.json").write_text("{broken")
    assert run(world, monkeypatch=monkeypatch) == 1
    assert json.loads((status / "tool_policy_status.json").read_text())["ok"] is False


def test_dry_run_reports_but_writes_nothing(world, monkeypatch, capsys):
    good_world(world)
    _, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    assert run(world, "--dry-run", monkeypatch=monkeypatch) == 1
    assert "profile sarah: no key file" in capsys.readouterr().out
    assert list(status.iterdir()) == []


def test_check_never_prints_key_text(world, monkeypatch, capsys):
    good_world(world)
    home, keys, status = world
    (keys / "instance_token").write_text("SECRET-DEFAULT-KEY")
    (keys / "instances" / "sarah.token").write_text("SECRET-SHARED-KEY")
    (keys / "instances" / "tara.token").write_text("SECRET-SHARED-KEY")
    write_json_config(make_profile(home, "tara"), {"enabled": True, "override_existing": True, "command": f"{HELPER} tara"})
    assert run(world, monkeypatch=monkeypatch) == 1
    seen = capsys.readouterr()
    assert "profiles sarah, tara share a key" in seen.out
    written = "".join(p.read_text() for p in status.rglob("*.json"))
    for text in (seen.out, seen.err, written):
        assert "SECRET" not in text and sha("SECRET-SHARED-KEY") not in text and sha("SECRET-DEFAULT-KEY") not in text


def test_check_an_unwritable_report_is_said_on_stderr_and_still_exits_1(world, monkeypatch, capsys):
    good_world(world)
    _, keys, status = world
    (keys / "instances" / "sarah.token").unlink()
    monkeypatch.setattr(pkc, "mark_failing", lambda path, problems: (_ for _ in ()).throw(OSError("read-only")))
    assert run(world, monkeypatch=monkeypatch) == 3  # "did not finish": the caller must fail the reports itself
    assert "could not write" in capsys.readouterr().err


def test_check_with_no_yaml_reader_fails_closed(world, monkeypatch, capsys):
    good_world(world)
    monkeypatch.setattr(pkc, "read_yaml", lambda path: (_ for _ in ()).throw(ImportError("no yaml")))
    home, keys, status = world
    assert pkc.main(["check", "--hermes-home", str(home), "--status-root", str(status),
                     "--instances-dir", str(keys / "instances"), "--default-key", str(keys / "instance_token")]) == 1
    assert "config cannot be read" in capsys.readouterr().out


def test_list_prints_the_profiles(tmp_path, capsys):
    make_profile(tmp_path, "sarah")
    make_profile(tmp_path, "asleep")
    (tmp_path / "profiles" / "asleep" / "gateway.parked").write_text("")
    assert pkc.main(["list", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["asleep", "sarah"]
    assert pkc.main(["list", "--hermes-home", str(tmp_path), "--serving"]) == 0
    assert capsys.readouterr().out.split() == ["sarah"]


def test_list_with_no_profiles_folder_prints_nothing(tmp_path, capsys):
    assert pkc.main(["list", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_list_that_cannot_read_the_folder_exits_3(tmp_path, monkeypatch, capsys):
    (tmp_path / "profiles").mkdir()
    monkeypatch.setattr(pkc.Path, "iterdir", lambda self: (_ for _ in ()).throw(PermissionError("no")))
    assert pkc.main(["list", "--hermes-home", str(tmp_path)]) == 3
    assert "cannot list" in capsys.readouterr().err


def test_usage_errors_exit_2(capsys):
    assert pkc.main([]) == 2
    assert pkc.main(["bogus"]) == 2
    assert pkc.main(["check"]) == 2  # --hermes-home is required


def test_the_real_yaml_reader_reads_yaml(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "config.yaml"
    path.write_text("secrets:\n  command:\n    enabled: true\n    command: /x/hermes-secrets sarah\n")
    assert pkc.read_yaml(path)["secrets"]["command"]["command"] == "/x/hermes-secrets sarah"


def test_the_real_yaml_reader_rejects_a_non_mapping(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        pkc.read_yaml(path)


def test_a_crash_is_exit_3_never_exit_1(tmp_path, monkeypatch, capsys):
    """Exit 1 means "problems found, reports already failed": a crash must not look like that."""
    monkeypatch.setattr(pkc, "gather", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pkc.main(["check", "--hermes-home", str(tmp_path), "--status-root", str(tmp_path / "s")]) == 3
    assert "crashed (RuntimeError)" in capsys.readouterr().err


def test_the_ruamel_fallback_reads_yaml_when_pyyaml_is_missing(tmp_path, monkeypatch):
    """Hermes's own environment has ruamel.yaml and no PyYAML."""
    pytest.importorskip("ruamel.yaml")
    monkeypatch.setitem(sys.modules, "yaml", None)  # ``import yaml`` now raises ImportError
    path = tmp_path / "config.yaml"
    path.write_text("secrets:\n  command:\n    enabled: true\n    command: /x/hermes-secrets sarah\n")
    config = pkc.read_yaml(path)
    assert config["secrets"]["command"] == {"enabled": True, "command": "/x/hermes-secrets sarah"}


def test_no_yaml_library_at_all_is_an_import_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    path = tmp_path / "config.yaml"
    path.write_text("a: 1\n")
    with pytest.raises(ImportError):
        pkc.read_yaml(path)
