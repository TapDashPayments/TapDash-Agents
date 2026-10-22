#!/usr/bin/env python3
"""Read-only health check for the durable-park gateway bridge's isolation.

Run as root (``sudo python3 /opt/durable-park/deploy/bridge_check.py``). Prints one line per check
(``ok: ...`` / ``FAIL: ...``) and exits 0 only if every check passes. Any check it cannot complete is a FAIL.

It only reads: ``systemctl show``, ``ss``, ``getent``, ``id``, /proc, directory listings and ``stat`` of the secret
file. It never reads a secret's contents and never writes, restarts or signals anything. Standard library only.
"""
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SOCKET_UNIT = "durable-park-bridge.socket"
SERVICE_UNIT = "durable-park-bridge.service"
LISTEN_ADDRESS = "127.0.0.1:8791"
PORT_FILTER = "sport = :8791"
BRIDGE_USER = "dpbridge"
SYSTEMD_PID = 1
FORBIDDEN_GROUPS = ("dpwake", "hermes")
NOLOGIN_SHELL = "/usr/sbin/nologin"
SECRET_FILE = "etc/durable-park/bridge_secret"

_TRUSTED_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


class RunError(Exception):
    """A command could not run or exited non-zero."""


@dataclass(frozen=True)
class Result:
    ok: bool
    text: str

    def __str__(self):
        return f"{'ok' if self.ok else 'FAIL'}: {self.text}"


def default_run(cmd):
    """Run ``cmd`` (a list, never a shell) with a fixed environment and return its stdout."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, env=_TRUSTED_ENV)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunError(f"{cmd[0]}: {exc}") from exc
    if done.returncode != 0:
        raise RunError(f"{cmd[0]} exited {done.returncode}")
    return done.stdout


class Context:
    """Everything a check may touch: the command runner, the filesystem root and lstat."""

    def __init__(self, run, root, lstat):
        self.run = run
        self.root = Path(root)
        self.lstat = lstat

    def show(self, unit, *props):
        """``systemctl show -p A -p B unit`` as key -> list of values (Listen can repeat)."""
        cmd = ["systemctl", "show"]
        for prop in props:
            cmd += ["-p", prop]
        values = {}
        for line in self.run(cmd + [unit]).splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values.setdefault(key, []).append(value)
        return values

    def service_state(self):
        shown = self.show(SERVICE_UNIT, "ActiveState", "MainPID")
        state = (shown.get("ActiveState") or ["?"])[0]
        pid = (shown.get("MainPID") or ["0"])[0]
        return state, int(pid) if pid.isdigit() else 0

    def main_pid(self):
        """The service's MainPID, or 0 when it has none or cannot be read (check 2 reports that)."""
        try:
            return self.service_state()[1]
        except (RunError, ValueError):
            return 0

    def uids(self, pid):
        """The four uids (real, effective, saved, fs) of a process, from /proc/<pid>/status."""
        for line in (self.root / "proc" / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return tuple(int(v) for v in line.split()[1:5])
        raise ValueError(f"no Uid line for pid {pid}")

    def user_name(self, uid):
        try:
            return self.run(["getent", "passwd", str(uid)]).split(":", 1)[0].strip() or f"uid {uid}"
        except RunError:
            return f"uid {uid}"

    def who(self, pid):
        """``pid 555 (user hermes)`` for a message; never raises."""
        try:
            return f"pid {pid} (user {self.user_name(self.uids(pid)[1])})"
        except (OSError, ValueError):
            return f"pid {pid} (user unknown)"

    def passwd_entry(self, name):
        """The ``getent passwd`` fields of ``name``, or None when there is no such user."""
        try:
            return self.run(["getent", "passwd", name]).strip().split(":")
        except RunError:
            return None


def check_socket(ctx):
    """1. The socket unit is active and listens on exactly 127.0.0.1:8791."""
    shown = ctx.show(SOCKET_UNIT, "ActiveState", "Listen")
    state = (shown.get("ActiveState") or ["?"])[0]
    if state != "active":
        return Result(False, f"{SOCKET_UNIT} is {state}, not active")
    listens = [v.rsplit(" (", 1)[0].strip() for v in shown.get("Listen", []) if v.strip()]
    if listens != [LISTEN_ADDRESS]:
        return Result(False, f"{SOCKET_UNIT} listens on {', '.join(listens) or 'nothing'}, not only {LISTEN_ADDRESS}")
    return Result(True, f"{SOCKET_UNIT} is active and listens on {LISTEN_ADDRESS}")


def check_service(ctx):
    """2. The service is active and its MainPID process runs as dpbridge (all four uids)."""
    state, pid = ctx.service_state()
    if state != "active":
        return Result(False, f"{SERVICE_UNIT} is {state}, not active")
    if pid <= 0:
        return Result(False, f"{SERVICE_UNIT} is active but has no main process")
    entry = ctx.passwd_entry(BRIDGE_USER)
    if entry is None or len(entry) < 3:
        return Result(False, f"user {BRIDGE_USER} does not exist")
    expected = int(entry[2])
    try:
        uids = ctx.uids(pid)
    except (OSError, ValueError) as exc:
        return Result(False, f"cannot read the uid of main process {pid}: {type(exc).__name__}")
    if uids != (expected,) * 4:
        return Result(False, f"main process {pid} runs as {ctx.user_name(uids[1])}, not {BRIDGE_USER} "
                             f"(uids {'/'.join(map(str, uids))})")
    return Result(True, f"{SERVICE_UNIT} is active; main process {pid} runs as {BRIDGE_USER}")


def check_listener(ctx):
    """3. Only systemd (pid 1) and/or the service's MainPID hold a listener on 127.0.0.1:8791."""
    allowed = {SYSTEMD_PID, ctx.main_pid()}
    lines = [ln.split() for ln in ctx.run(["ss", "-Hltnp", PORT_FILTER]).splitlines() if ln.strip()]
    if not lines:
        return Result(False, f"nothing listens on {LISTEN_ADDRESS}")
    problems = []
    for fields in lines:
        local = fields[3] if len(fields) > 3 else "?"
        pids = [int(p) for p in re.findall(r"pid=(\d+)", " ".join(fields[5:]))]
        if local != LISTEN_ADDRESS:
            problems.append(f"a listener on {local} (not {LISTEN_ADDRESS})")
        if not pids:
            problems.append(f"a listener on {local} that no process is attributed to")
        problems += [f"{ctx.who(pid)} holds {local}" for pid in pids if pid not in allowed]
    if problems:
        return Result(False, "; ".join(problems))
    return Result(True, f"only systemd and the bridge hold {LISTEN_ADDRESS}")


def _runs_bridge(argv):
    """True when a command line starts the bridge app: ``-m bridge.app``, ``-mbridge.app``, a uvicorn
    ``bridge.app:app`` target, or the source file ``bridge/app.py``."""
    for i, arg in enumerate(argv):
        if arg == "-mbridge.app" or arg.startswith("bridge.app:"):
            return True
        if arg == "bridge.app" and i > 0 and argv[i - 1] == "-m":
            return True
        if arg == "bridge/app.py" or arg.endswith("/bridge/app.py"):
            return True
    return False


def check_no_other_bridge(ctx):
    """4. No process other than the service's MainPID runs bridge.app (scan /proc/*/cmdline)."""
    main_pid = ctx.main_pid()
    others = []
    for entry in sorted((ctx.root / "proc").iterdir(), key=lambda e: e.name):
        if not entry.name.isdigit() or int(entry.name) == main_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue  # the process exited while we scanned
        except OSError:
            others.append(f"cannot read the command line of pid {entry.name}")
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if _runs_bridge(argv):
            others.append(f"{ctx.who(int(entry.name))} runs bridge.app")
    if others:
        return Result(False, "; ".join(others))
    return Result(True, "no process other than the service's main process runs bridge.app")


UNIT_PREFIX = "durable-park-bridge"


def _matching(directory, depth=1):
    """Entries named durable-park-bridge* in ``directory`` and ``depth`` levels below it (``*.wants`` etc.)."""
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = []
    for entry in entries:
        if entry.name.startswith(UNIT_PREFIX):
            found.append(Path(entry.path))
        elif depth and entry.is_dir(follow_symlinks=False):
            found += _matching(entry.path, depth - 1)
    return found


def check_no_user_units(ctx):
    """5. No user-level unit named durable-park-bridge* exists for any user."""
    dirs = [ctx.root / "etc/systemd/user", ctx.root / "root/.config/systemd/user"]
    homes = ctx.root / "home"
    if homes.is_dir():
        dirs += [home / ".config/systemd/user" for home in sorted(homes.iterdir())]
    found = [p for d in dirs for p in _matching(d)]
    if found:
        return Result(False, "user-level bridge unit(s) exist: " + ", ".join("/" + str(p.relative_to(ctx.root)) for p in found))
    return Result(True, "no user-level durable-park-bridge unit exists for any user")


def check_account(ctx):
    """6. dpbridge exists, has the nologin shell, and is not a member of dpwake or hermes."""
    entry = ctx.passwd_entry(BRIDGE_USER)
    if entry is None:
        return Result(False, f"user {BRIDGE_USER} does not exist")
    shell = entry[6] if len(entry) > 6 else ""
    if shell != NOLOGIN_SHELL:
        return Result(False, f"{BRIDGE_USER} has shell {shell!r}, not {NOLOGIN_SHELL}")
    groups = ctx.run(["id", "-nG", BRIDGE_USER]).split()
    member_of = [g for g in FORBIDDEN_GROUPS if g in groups]
    if member_of:
        return Result(False, f"{BRIDGE_USER} is a member of {', '.join(member_of)}")
    return Result(True, f"{BRIDGE_USER} exists with shell {NOLOGIN_SHELL} and is in neither "
                        f"{' nor '.join(FORBIDDEN_GROUPS)}")


def check_secret_mode(ctx):
    """7. /etc/durable-park/bridge_secret is root:root, mode 0600 (stat only; the contents are never read)."""
    shown = "/" + SECRET_FILE
    try:
        st = ctx.lstat(ctx.root / SECRET_FILE)
    except FileNotFoundError:
        return Result(False, f"{shown} is missing")
    if not stat.S_ISREG(st.st_mode):
        return Result(False, f"{shown} is not a regular file")
    perm = stat.S_IMODE(st.st_mode)
    if (st.st_uid, st.st_gid, perm) != (0, 0, 0o600):
        return Result(False, f"{shown} is {st.st_uid}:{st.st_gid} mode {perm:04o}, not 0:0 mode 0600")
    return Result(True, f"{shown} is root:root mode 0600")


CHECKS = (check_socket, check_service, check_listener, check_no_other_bridge, check_no_user_units,
          check_account, check_secret_mode)


def check_all(run=default_run, root=Path("/"), lstat=os.lstat):
    """Run every check; a check that raises is a FAIL, and the rest still run."""
    ctx = Context(run, root, lstat)
    results = []
    for check in CHECKS:
        try:
            results.append(check(ctx))
        except Exception as exc:  # fail closed: a check we could not finish is not a pass
            results.append(Result(False, f"{check.__name__} could not complete: {type(exc).__name__}: {exc}"))
    return results


def main(run=default_run, root=Path("/"), lstat=os.lstat, out=None):
    out = out or sys.stdout
    results = check_all(run, root, lstat)
    for result in results:
        print(result, file=out)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
