# Plan: the gateway bridge runs as its own user, with systemd holding its port

Date: 2026-10-22. Approved by Yaakov 2026-10-22. Spec: `docs/specs/2026-10-22-bridge-verifies-withhuman.md`
(this plan adds the isolation that spec assumed). Branch: `feat/bridge-verifies-withhuman`.

## Why

The bridge answers withHuman "approve" or "human", and withHuman does not sign those answers. Today the bridge
runs as `hermes`, the bot's own user. Anything running as `hermes` can kill it (same uid) and, while systemd waits to
restart it, bind 127.0.0.1:8791 itself and answer "approve" to every gateway call. Fix: a dedicated system user the
bot cannot signal, and a systemd socket unit that holds the port at all times, even while the bridge restarts.

## Global constraints

- System user `dpbridge`: `useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin
  --user-group dpbridge`. No login, no home, no Hermes, no other groups (NOT in `dpwake`, NOT in `hermes`).
- Socket unit `durable-park-bridge.socket`: `ListenStream=127.0.0.1:8791`, `Accept=no`, `NoDelay=true`,
  `WantedBy=sockets.target`. The service is `durable-park-bridge.service` (same name as today, replaced in place).
- Service: `User=dpbridge`, `Group=dpbridge`, no `SupplementaryGroups`; `Requires=durable-park-bridge.socket`,
  `After=durable-park-bridge.socket network-online.target`; `Restart=always`, `RestartSec=1`;
  `LoadCredential=bridge_secret:/etc/durable-park/bridge_secret` and
  `LoadCredential=audit_key:/etc/durable-park/audit_key`; ExecStart:
  `/opt/durable-park/bin/python -B -m bridge.app --systemd-socket --secret-file %d/bridge_secret
  --key-file %d/audit_key --expected-agent proto-agents` (no `--host`/`--port` with `--systemd-socket`).
  Keep `WorkingDirectory=/opt/durable-park`, the PYTHONPATH and every existing hardening line; `ProtectHome=yes`;
  add `ProtectProc=invisible` and `ProcSubset=pid`.
- `bridge_secret` becomes `root:root 0600` (systemd reads it as root through LoadCredential, so the bot no longer
  can). `audit_key` stays `root:hermes 0640` (the plugin reads it for the decider's name).
- Key rotation now needs `sudo systemctl restart durable-park-bridge` (LoadCredential copies at start). The socket
  keeps the port during the restart.
- Fail closed everywhere. Never print or log a secret.

## Tasks (independent files; run in parallel worktrees)

### Task 1: `--systemd-socket` in `durable-park/bridge/app.py` (tests in `tests/test_bridge.py`)
- New flag `--systemd-socket` (env `DURABLE_PARK_BRIDGE_SYSTEMD_SOCKET=1` also enables it). With it, `main` must
  check systemd's socket-activation environment: `LISTEN_PID` equals `os.getpid()` and `LISTEN_FDS` equals `"1"`;
  otherwise exit non-zero with `--systemd-socket: no socket from systemd (LISTEN_PID/LISTEN_FDS)` and serve nothing.
  Then unset `LISTEN_PID`, `LISTEN_FDS`, `LISTEN_FDNAMES` from `os.environ` and call
  `uvicorn.run(app, fd=3, log_level="info", access_log=False)`.
- `--systemd-socket` together with an explicit `--host` or `--port` is a usage error (argparse error).
- Without the flag, behaviour is unchanged (`host`/`port`, defaults 127.0.0.1:8791).
- Put the env check in a small pure function `systemd_listen_fd(environ, pid) -> int` (returns 3 or raises
  `SystemExit`/a dedicated error) so it is unit-testable; `main` uses it. Tests monkeypatch `uvicorn.run`.

### Task 2: units and `durable-park/deploy/configure_hermes.sh` (tests in `tests/test_configure_script.py`)
- New file `durable-park/deploy/durable-park-bridge.socket` per the constraints.
- Update `durable-park/deploy/durable-park-bridge.service` per the constraints.
- `configure_hermes.sh` bridge section: create `dpbridge` idempotently (exact useradd above, only if `getent passwd
  dpbridge` fails); if `/etc/durable-park/bridge_secret` exists, `chown root:root` and `chmod 0600` it; install both
  units with the script's existing install helper; `systemctl daemon-reload`; `systemctl enable --now
  durable-park-bridge.socket`; restart the service with the existing restart helper; keep the 401 health gate; then
  run `python3 /opt/durable-park/deploy/bridge_check.py` if present (Task 3) and fail the script if it fails. Copy
  `deploy/bridge_check.py` into `/opt/durable-park/deploy/` with the existing sync helper (root-owned, 0755).
- Follow the script's existing style and its test harness exactly.

### Task 3: `durable-park/deploy/bridge_check.py` (tests in `tests/test_bridge_check.py`)
A read-only health check, run as root (`sudo python3 /opt/durable-park/deploy/bridge_check.py`), that prints one
line per check (`ok: ...` / `FAIL: ...`) and exits 0 only if every check passes:
1. `durable-park-bridge.socket` is active and `systemctl show -p Listen` names `127.0.0.1:8791`.
2. `durable-park-bridge.service` is active and its `MainPID` process runs as user `dpbridge`.
3. The only listener on 127.0.0.1:8791 (from `ss -Hltnp 'sport = :8791'`) is held by systemd (pid 1) and/or the
   service's MainPID; any other process is a FAIL naming the pid and its user.
4. No other process runs `bridge.app` (scan `/proc/*/cmdline`), besides the MainPID.
5. No user-level unit named `durable-park-bridge*` exists for any user: check `/home/*/.config/systemd/user/`,
   `/root/.config/systemd/user/` and `/etc/systemd/user/` for files matching `durable-park-bridge*`.
6. `dpbridge` exists, has shell `/usr/sbin/nologin`, and is not a member of `dpwake` or `hermes`.
7. `/etc/durable-park/bridge_secret` is `root:root` and mode `0600`.
Design: all system access through a small injectable runner (`run(cmd) -> str`, plus injectable filesystem root
for /proc, /home, /etc) so tests use fakes and never touch the real system. Never read secret file contents (stat
only). Python 3 standard library only.

### Task 4 (after 1-3 are merged): docs
Update `durable-park/docs/architecture.md`, `runbook.md` (health check runs bridge_check.py; key rotation now
needs a bridge restart; rollback restores both units and notes `dpbridge` can stay; deploy steps), `gateway-setup.md`,
`receiver-setup.md` (secrets table: bridge_secret root:root 0600; users table: dpbridge), `backlog.md`, the spec's
deploy section, and `.github/docs-map.json` for the new files. Re-read every diagram/table naming the bridge's user.
