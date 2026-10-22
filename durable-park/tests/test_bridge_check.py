"""deploy/bridge_check.py: the read-only health check that proves the gateway bridge is isolated.

Every test runs against a fake world: a fake command runner, a tmp_path standing in for /proc, /home and /etc,
and a fake lstat for the secret file. Nothing here touches the real system.
"""
import builtins
import importlib.util
import io
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("bridge_check", DEPLOY / "bridge_check.py")
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)

SOCKET = "durable-park-bridge.socket"
SERVICE = "durable-park-bridge.service"
BRIDGE_ARGV = ["/opt/durable-park/bin/python", "-B", "-m", "bridge.app", "--systemd-socket",
               "--secret-file", "/run/credentials/durable-park-bridge.service/bridge_secret",
               "--key-file", "/run/credentials/durable-park-bridge.service/audit_key",
               "--expected-agent", "proto-agents"]
HEALTHY_SS = ('LISTEN 0      2048   127.0.0.1:8791 0.0.0.0:*    '
              'users:(("systemd",pid=1,fd=40),("python",pid=4242,fd=3))\n')


class World:
    """A healthy fake system; each test breaks exactly one thing."""

    def __init__(self, root):
        self.root = root
        self.users = {"root": (0, "/bin/bash"), "dpbridge": (990, "/usr/sbin/nologin"),
                      "hermes": (1001, "/bin/bash"), "ubuntu": (1000, "/bin/bash")}
        self.groups = {"dpbridge": ["dpbridge"]}
        self.socket_show = "ActiveState=active\nListen=127.0.0.1:8791 (Stream)\n"
        self.service_show = "ActiveState=active\nMainPID=4242\n"
        self.ss = HEALTHY_SS
        self.secret = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0)
        self.broken = set()
        self.calls = []
        self.add_process(1, ["/sbin/init"], 0)
        self.add_process(4242, BRIDGE_ARGV, 990)
        # a shell whose command line merely mentions bridge.app must not count as a bridge
        self.add_process(77, ["bash", "-lc", "pgrep -f bridge.app"], 1000)

    def add_process(self, pid, argv, uid):
        d = self.root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n")

    def add_unit_file(self, relative):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[Service]\n")
        return p

    def run(self, cmd):
        cmd = tuple(cmd)
        self.calls.append(cmd)
        if cmd[0] in self.broken or f"{cmd[0]}:{cmd[-1]}" in self.broken:
            raise bc.RunError(f"{cmd[0]} failed")
        if cmd[0] == "systemctl":
            return {SOCKET: self.socket_show, SERVICE: self.service_show}[cmd[-1]]
        if cmd[0] == "ss":
            return self.ss
        if cmd[0] == "getent" and cmd[1] == "passwd":
            key = cmd[2]
            for name, (uid, shell) in self.users.items():
                if key in (name, str(uid)):
                    return f"{name}:x:{uid}:{uid}::/nonexistent:{shell}\n"
            raise bc.RunError(f"getent passwd {key}: not found")
        if cmd[0] == "id" and cmd[1] == "-nG":
            if cmd[2] not in self.groups:
                raise bc.RunError("id: no such user")
            return " ".join(self.groups[cmd[2]]) + "\n"
        raise AssertionError(f"unexpected command {cmd}")

    def lstat(self, path):
        if Path(path) == self.root / "etc/durable-park/bridge_secret":
            if self.secret is None:
                raise FileNotFoundError(path)
            return self.secret
        return os.lstat(path)

    def results(self):
        return bc.check_all(self.run, self.root, self.lstat)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def failing(results):
    return [i + 1 for i, r in enumerate(results) if not r.ok]


# --- the whole check ---------------------------------------------------------------------------------------

def test_healthy_system_passes_all_seven_checks(world):
    results = world.results()
    assert len(results) == 7
    assert [str(r) for r in results if not r.ok] == []


def test_main_prints_one_line_per_check_and_exits_zero_when_healthy(world, capsys):
    code = bc.main(run=world.run, root=world.root, lstat=world.lstat)
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert len(lines) == 7
    assert all(line.startswith("ok: ") for line in lines)


def test_main_exits_one_and_still_prints_every_check_when_one_fails(world, capsys):
    world.socket_show = "ActiveState=inactive\nListen=\n"
    code = bc.main(run=world.run, root=world.root, lstat=world.lstat)
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert len(lines) == 7
    assert lines[0].startswith("FAIL: ")
    assert all(line.startswith("ok: ") for line in lines[1:])


# --- 1. socket unit ----------------------------------------------------------------------------------------

def test_check1_socket_inactive_fails(world):
    world.socket_show = "ActiveState=inactive\nListen=127.0.0.1:8791 (Stream)\n"
    results = world.results()
    assert failing(results) == [1]
    assert "inactive" in results[0].text


def test_check1_socket_without_listen_address_fails(world):
    world.socket_show = "ActiveState=active\nListen=\n"
    assert failing(world.results()) == [1]


def test_check1_socket_listening_elsewhere_fails(world):
    world.socket_show = "ActiveState=active\nListen=0.0.0.0:8791 (Stream)\n"
    results = world.results()
    assert failing(results) == [1]
    assert "0.0.0.0:8791" in results[0].text


def test_check1_socket_with_an_extra_listen_address_fails(world):
    world.socket_show = ("ActiveState=active\nListen=127.0.0.1:8791 (Stream)\n"
                         "Listen=[::]:8791 (Stream)\n")
    assert failing(world.results()) == [1]


def test_check1_systemctl_error_fails_closed_without_raising(world):
    world.broken.add(f"systemctl:{SOCKET}")
    assert failing(world.results()) == [1]


# --- 2. service unit and its main process --------------------------------------------------------------------

def test_check2_service_inactive_fails(world):
    world.service_show = "ActiveState=inactive\nMainPID=0\n"
    results = world.results()
    assert 2 in failing(results)
    assert "inactive" in results[1].text


def test_check2_service_without_a_main_process_fails(world):
    world.service_show = "ActiveState=active\nMainPID=0\n"
    assert 2 in failing(world.results())


def test_check2_main_process_running_as_hermes_fails_and_names_the_user(world):
    world.add_process(4242, BRIDGE_ARGV, 1001)
    results = world.results()
    assert 2 in failing(results)
    assert "hermes" in results[1].text and "4242" in results[1].text


def test_check2_any_uid_field_other_than_dpbridge_fails(world):
    # real uid dpbridge but effective uid root is not isolation
    (world.root / "proc/4242/status").write_text("Uid:\t990\t0\t990\t990\n")
    assert 2 in failing(world.results())


def test_check2_unknown_uid_is_named_by_number(world):
    world.add_process(4242, BRIDGE_ARGV, 5555)
    results = world.results()
    assert 2 in failing(results)
    assert "5555" in results[1].text


def test_check2_main_process_that_vanished_fails(world):
    world.service_show = "ActiveState=active\nMainPID=9999\n"
    assert 2 in failing(world.results())


def test_check2_missing_dpbridge_user_fails(world):
    del world.users["dpbridge"]
    assert 2 in failing(world.results())


def test_check2_systemctl_error_fails_closed_without_raising(world):
    world.broken.add(f"systemctl:{SERVICE}")
    assert 2 in failing(world.results())


# --- 3. the only listener on 127.0.0.1:8791 ------------------------------------------------------------------

def test_check3_socket_held_by_systemd_alone_while_the_service_restarts_passes(world):
    world.service_show = "ActiveState=activating\nMainPID=0\n"
    world.ss = 'LISTEN 0 2048 127.0.0.1:8791 0.0.0.0:* users:(("systemd",pid=1,fd=40))\n'
    assert world.results()[2].ok


def test_check3_bridge_and_systemd_both_holding_the_socket_passes(world):
    assert world.results()[2].ok


def test_check3_a_foreign_listener_fails_and_names_pid_and_user(world):
    world.add_process(555, ["python3", "-m", "http.server", "8791"], 1001)
    world.ss = HEALTHY_SS + 'LISTEN 0 5 127.0.0.1:8791 0.0.0.0:* users:(("python3",pid=555,fd=3))\n'
    results = world.results()
    assert failing(results) == [3]
    assert "555" in results[2].text and "hermes" in results[2].text


def test_check3_a_foreign_process_sharing_the_fd_list_fails(world):
    world.add_process(556, ["sleep", "9"], 1001)
    world.ss = ('LISTEN 0 2048 127.0.0.1:8791 0.0.0.0:* '
                'users:(("systemd",pid=1,fd=40),("python",pid=4242,fd=3),("sleep",pid=556,fd=3))\n')
    results = world.results()
    assert failing(results) == [3]
    assert "556" in results[2].text


def test_check3_a_listener_the_kernel_will_not_attribute_fails(world):
    world.ss = "LISTEN 0 2048 127.0.0.1:8791 0.0.0.0:*\n"
    assert failing(world.results()) == [3]


def test_check3_no_listener_at_all_fails(world):
    world.ss = ""
    assert failing(world.results()) == [3]


def test_check3_a_listener_on_another_address_of_the_same_port_fails(world):
    world.ss = ('LISTEN 0 2048 0.0.0.0:8791 0.0.0.0:* users:(("systemd",pid=1,fd=40),("python",pid=4242,fd=3))\n')
    assert failing(world.results()) == [3]


def test_check3_foreign_listener_on_wildcard_address_fails(world):
    world.add_process(555, ["nc", "-l", "8791"], 1001)
    world.ss = HEALTHY_SS + 'LISTEN 0 1 [::]:8791 [::]:* users:(("nc",pid=555,fd=3))\n'
    assert failing(world.results()) == [3]


def test_check3_ss_error_fails_closed_without_raising(world):
    world.broken.add("ss")
    assert failing(world.results()) == [3]


def test_check3_asks_ss_only_about_port_8791(world):
    world.results()
    assert ("ss", "-Hltnp", "sport = :8791") in world.calls


# --- 4. no other process runs bridge.app ---------------------------------------------------------------------

def test_check4_only_the_main_process_runs_bridge_app(world):
    results = world.results()
    assert results[3].ok, str(results[3])


def test_check4_a_second_bridge_process_fails_and_names_pid_and_user(world):
    world.add_process(600, ["/usr/bin/python3", "-m", "bridge.app", "--port", "8791"], 1001)
    results = world.results()
    assert failing(results) == [4]
    assert "600" in results[3].text and "hermes" in results[3].text


def test_check4_a_bridge_started_as_dpbridge_without_systemd_still_fails(world):
    world.add_process(601, ["python", "-m", "bridge.app"], 990)
    assert failing(world.results()) == [4]


def test_check4_attached_module_flag_is_recognised(world):
    world.add_process(602, ["python", "-mbridge.app"], 1001)
    assert failing(world.results()) == [4]


def test_check4_uvicorn_serving_bridge_app_is_recognised(world):
    world.add_process(603, ["/usr/bin/python3", "-m", "uvicorn", "bridge.app:app", "--port", "8791"], 1001)
    assert failing(world.results()) == [4]


def test_check4_running_the_bridge_source_file_directly_is_recognised(world):
    world.add_process(604, ["python3", "/tmp/copy/durable-park/bridge/app.py"], 1001)
    assert failing(world.results()) == [4]


def test_check4_a_command_line_that_only_mentions_bridge_app_is_not_a_bridge(world):
    world.add_process(605, ["bash", "-c", "grep -r bridge.app /etc; echo -m bridge.app"], 1001)
    assert world.results()[3].ok


def test_check4_non_process_entries_in_proc_are_ignored(world):
    (world.root / "proc/self").mkdir()
    (world.root / "proc/cpuinfo").write_text("processor : 0\n")
    assert world.results()[3].ok


def test_check4_a_process_that_exits_during_the_scan_is_ignored(world):
    (world.root / "proc/700").mkdir()
    assert world.results()[3].ok


def test_check4_a_process_we_cannot_read_fails_closed(world):
    (world.root / "proc/701").mkdir()
    (world.root / "proc/701/cmdline").mkdir()
    results = world.results()
    assert failing(results) == [4]
    assert "701" in results[3].text


def test_check4_the_main_process_is_excluded_but_only_the_main_process(world):
    world.service_show = "ActiveState=active\nMainPID=4243\n"
    world.add_process(4243, BRIDGE_ARGV, 990)
    results = world.results()
    assert "4242" in results[3].text and 4 in failing(results)


# --- 5. no user-level bridge unit anywhere -------------------------------------------------------------------

@pytest.mark.parametrize("relative", [
    "home/hermes/.config/systemd/user/durable-park-bridge.service",
    "home/ubuntu/.config/systemd/user/durable-park-bridge.socket",
    "root/.config/systemd/user/durable-park-bridge.service",
    "etc/systemd/user/durable-park-bridge.service",
    "home/hermes/.config/systemd/user/durable-park-bridge.service.d/override.conf",
])
def test_check5_a_user_level_bridge_unit_fails_and_names_the_path(world, relative):
    world.add_unit_file(relative)
    results = world.results()
    assert failing(results) == [5]
    assert "durable-park-bridge" in results[4].text


def test_check5_an_enablement_symlink_one_level_down_fails_even_when_dangling(world):
    wants = world.root / "home/hermes/.config/systemd/user/default.target.wants"
    wants.mkdir(parents=True)
    (wants / "durable-park-bridge.service").symlink_to("/nonexistent/durable-park-bridge.service")
    assert failing(world.results()) == [5]


def test_check5_every_home_directory_is_searched(world):
    for name in ("alice", "bob", "carol"):
        (world.root / "home" / name).mkdir(parents=True, exist_ok=True)
    world.add_unit_file("home/carol/.config/systemd/user/durable-park-bridge.service")
    results = world.results()
    assert failing(results) == [5]
    assert "carol" in results[4].text


def test_check5_other_user_units_are_fine(world):
    world.add_unit_file("home/ubuntu/.config/systemd/user/hermes-gateway.service")
    world.add_unit_file("home/hermes/.config/systemd/user/durable-park-receiver.service")
    world.add_unit_file("etc/systemd/user/something-else.service")
    assert world.results()[4].ok


def test_check5_system_units_are_not_user_units(world):
    world.add_unit_file("etc/systemd/system/durable-park-bridge.service")
    assert world.results()[4].ok


def test_check5_missing_home_or_config_directories_are_fine(world):
    # tmp root has no /home, /root or /etc/systemd at all
    assert world.results()[4].ok


# --- 6. the dpbridge account ---------------------------------------------------------------------------------

def test_check6_dpbridge_with_nologin_and_no_other_groups_passes(world):
    assert world.results()[5].ok


@pytest.mark.parametrize("shell", ["/bin/bash", "/bin/false", "/sbin/nologin", "/usr/sbin/nologin.real", ""])
def test_check6_any_shell_but_usr_sbin_nologin_fails(world, shell):
    world.users["dpbridge"] = (990, shell)
    results = world.results()
    assert 6 in failing(results)
    assert "shell" in results[5].text


@pytest.mark.parametrize("group", ["dpwake", "hermes"])
def test_check6_membership_of_dpwake_or_hermes_fails_and_names_the_group(world, group):
    world.groups["dpbridge"] = ["dpbridge", group]
    results = world.results()
    assert failing(results) == [6]
    assert group in results[5].text


def test_check6_a_primary_group_called_hermes_fails(world):
    world.groups["dpbridge"] = ["hermes"]
    assert failing(world.results()) == [6]


def test_check6_an_unrelated_extra_group_is_not_this_checks_business(world):
    world.groups["dpbridge"] = ["dpbridge", "systemd-journal"]
    assert world.results()[5].ok


def test_check6_missing_user_fails(world):
    del world.users["dpbridge"]
    results = world.results()
    assert 6 in failing(results)
    assert "does not exist" in results[5].text


def test_check6_id_error_fails_closed_without_raising(world):
    world.broken.add("id")
    assert failing(world.results()) == [6]


# --- 7. bridge_secret ownership and mode ---------------------------------------------------------------------

def test_check7_root_root_0600_passes(world):
    assert world.results()[6].ok


@pytest.mark.parametrize("uid, gid, perm", [
    (1001, 0, 0o600),   # owned by the bot
    (0, 986, 0o600),    # group dpwake
    (0, 1001, 0o600),   # group hermes
    (0, 0, 0o640),      # group-readable (the old mode)
    (0, 0, 0o644),
    (0, 0, 0o400),      # not the mode the deploy sets
    (0, 0, 0o700),
    (0, 0, 0o1600),     # sticky bit
])
def test_check7_any_other_owner_group_or_mode_fails(world, uid, gid, perm):
    world.secret = SimpleNamespace(st_mode=stat.S_IFREG | perm, st_uid=uid, st_gid=gid)
    results = world.results()
    assert failing(results) == [7]
    assert f"{uid}:{gid}" in results[6].text


def test_check7_missing_file_fails(world):
    world.secret = None
    results = world.results()
    assert failing(results) == [7]
    assert "missing" in results[6].text


def test_check7_a_symlink_fails(world):
    world.secret = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_gid=0)
    assert failing(world.results()) == [7]


def test_check7_a_directory_fails(world):
    world.secret = SimpleNamespace(st_mode=stat.S_IFDIR | 0o600, st_uid=0, st_gid=0)
    assert failing(world.results()) == [7]


def test_the_secret_is_only_stat_ed_never_opened_and_never_printed(world, monkeypatch, capsys):
    secret = world.root / "etc/durable-park/bridge_secret"
    secret.parent.mkdir(parents=True)
    secret.write_text("TOPSECRET-VALUE")
    opened = []
    real_open, real_io_open = builtins.open, io.open

    def spy(real):
        def inner(file, *a, **kw):
            opened.append(str(file))
            return real(file, *a, **kw)
        return inner
    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_io_open))
    bc.main(run=world.run, root=world.root, lstat=world.lstat)
    assert not [p for p in opened if "durable-park" in p]
    assert "TOPSECRET" not in capsys.readouterr().out


# --- the whole thing is read-only and fails closed -------------------------------------------------------------

def test_only_read_only_commands_are_run(world):
    world.results()
    allowed = {("systemctl", "show"), ("ss", "-Hltnp"), ("getent", "passwd"), ("id", "-nG")}
    assert world.calls
    assert {c[:2] for c in world.calls} <= allowed


def test_running_the_check_changes_nothing_on_disk(world):
    world.add_unit_file("home/hermes/.config/systemd/user/hermes-gateway.service")

    def snapshot():
        return sorted((str(p), p.lstat().st_mtime_ns, p.lstat().st_size) for p in world.root.rglob("*"))
    before = snapshot()
    world.results()
    assert snapshot() == before


def test_a_check_that_crashes_is_a_fail_and_the_other_checks_still_run(world, monkeypatch):
    def boom(ctx):
        raise ValueError("kaboom")
    monkeypatch.setattr(bc, "CHECKS", (boom, bc.check_socket))
    results = world.results()
    assert [r.ok for r in results] == [False, True]
    assert "boom" in results[0].text and "kaboom" in results[0].text


def test_default_run_uses_no_shell_a_fixed_environment_and_a_timeout(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return SimpleNamespace(returncode=0, stdout="out\n")
    monkeypatch.setattr(bc.subprocess, "run", fake)
    assert bc.default_run(["id", "-nG", "dpbridge"]) == "out\n"
    assert seen["cmd"] == ["id", "-nG", "dpbridge"]
    assert not seen.get("shell")
    assert seen["env"]["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert seen["timeout"] > 0


def test_default_run_turns_failures_into_run_error(monkeypatch):
    monkeypatch.setattr(bc.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=3, stdout=""))
    with pytest.raises(bc.RunError):
        bc.default_run(["ss"])

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(bc.subprocess, "run", missing)
    with pytest.raises(bc.RunError):
        bc.default_run(["ss"])

    def slow(cmd, **kw):
        raise bc.subprocess.TimeoutExpired(cmd, 15)
    monkeypatch.setattr(bc.subprocess, "run", slow)
    with pytest.raises(bc.RunError):
        bc.default_run(["ss"])
