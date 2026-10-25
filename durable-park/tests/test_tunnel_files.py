"""deploy/tunnel/: plain-text checks of the tunnel image, compose file and scripts (no YAML package)."""

import re
from pathlib import Path

TUNNEL = Path(__file__).resolve().parents[1] / "deploy" / "tunnel"
SHA_FILE = TUNNEL / "maverick-mcp.sha"


def _maverick_sha():
    return SHA_FILE.read_text()


def test_maverick_sha_file_is_one_full_commit():
    text = _maverick_sha()
    assert re.fullmatch(r"[0-9a-f]{40}\n", text), "one line: the 40-character maverick-mcp commit"


# --- Task 2: the tunnel image carries the Maverick server -------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

COMPOSE = TUNNEL / "docker-compose.yml"
DOCKERFILE = TUNNEL / "Dockerfile"
BUILD = TUNNEL / "build.sh"


def _shellcheck():
    local = Path(sys.executable).with_name("shellcheck")  # shellcheck-py in the dev venv, if installed
    return str(local) if local.exists() else shutil.which("shellcheck")


def _code(path):
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _service_block(name):
    """The lines of one compose service (services sit at two spaces of indent), comments dropped."""
    lines, inside = [], False
    for raw in COMPOSE.read_text().splitlines():
        if raw.startswith("  ") and not raw.startswith("   ") and raw.strip().endswith(":"):
            inside = raw.strip() == f"{name}:"
            continue
        if raw and not raw.startswith(" "):
            inside = False
        if inside and raw.strip() and not raw.strip().startswith("#"):
            lines.append(raw.strip())
    return lines


def test_both_services_use_the_same_tag_with_maverick_sha():
    want = f"image: durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-{_maverick_sha().strip()[:7]}"
    images = [ln for ln in _code(COMPOSE) if ln.startswith("image:")]
    assert images == [want, want]
    assert want in _service_block("wh-tunnel") and want in _service_block("check")


def test_compose_has_maverick_build_context():
    block = _service_block("wh-tunnel")
    assert "additional_contexts:" in block
    assert "maverick-src: ${MAVERICK_MCP_SRC:-/opt/maverick-mcp-src}" in block


def test_compose_mounts_the_maverick_inbox_volume_in_the_tunnel_only():
    assert "- maverick-inbox:/data/maverick-inbox" in _service_block("wh-tunnel")
    assert not any("maverick-inbox" in ln for ln in _service_block("check"))
    text = COMPOSE.read_text()
    assert "  maverick-inbox:\n    name: durable-park-maverick-inbox\n" in text


def test_dockerfile_installs_with_require_hashes():
    code = " ".join(_code(DOCKERFILE))
    assert "FROM ${TUNNEL_IMAGE} AS maverick" in code
    assert "COPY --from=maverick-src" in code
    # runtime packages: only from the hashed lock, wheels only (nothing compiled from an unpinned sdist)
    assert ("/opt/maverick-mcp/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: "
            "-r /src/maverick/requirements.lock") in code
    # the build tool (setuptools) is hash-checked too, from the dev lock; then the package itself, without deps
    assert "--require-hashes --only-binary=:all: -r /src/maverick/requirements-dev.lock" in code
    assert "--no-deps --no-build-isolation --check-build-dependencies" in code
    assert "/opt/maverick-mcp/bin/pip install --no-cache-dir --no-deps --no-index" in code
    for forbidden in ("pip install --upgrade", "pip install -U ", "git clone", "curl ", "wget "):
        assert forbidden not in code, forbidden


def test_dockerfile_runtime_stage_has_the_launcher_and_the_inbox():
    code = _code(DOCKERFILE)
    joined = " ".join(code)
    assert "COPY --from=maverick /opt/maverick-mcp /opt/maverick-mcp" in joined
    assert 'exec /opt/maverick-mcp/bin/python -m maverick_mcp "$@"' in joined
    assert "/usr/local/bin/maverick-mcp-server" in joined
    assert "mkdir -p /data/maverick-inbox" in joined
    assert "chown withhuman:withhuman /data/maverick-inbox" in joined
    assert "chmod 0700 /data/maverick-inbox" in joined
    assert code[-1] == "USER withhuman"


def test_dockerignore_still_excludes_env():
    lines = (TUNNEL / ".dockerignore").read_text().split()
    assert "*.env" in lines and "*.env.*" in lines


def test_build_script_checks_sha_and_clean_tree():
    subprocess.run(["bash", "-n", str(BUILD)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(BUILD)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    text = BUILD.read_text()
    assert "maverick-mcp.sha" in text
    assert "rev-parse HEAD" in text
    assert "status --porcelain --ignored --untracked-files=all" in text
    assert "docker compose build" in text
    assert text.index("rev-parse HEAD") < text.index("docker compose build")
    assert text.index("status --porcelain") < text.index("docker compose build")
    # PR 53 review I1: root's checkout, no safe.directory, no replace objects or grafts
    assert "-c safe.directory" not in text
    assert "SRC=${MAVERICK_MCP_SRC:-/opt/maverick-mcp-src}" in text
    assert "GIT_NO_REPLACE_OBJECTS=1 git --no-replace-objects" in text
    assert ".git/info/grafts" in text and "refs/replace/" in text
    assert "OWNER=${MAVERICK_MCP_SRC_OWNER:-root}" in text


_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(src, *args, env):
    return subprocess.run(["git", "-C", str(src), *args], check=True, env=env, capture_output=True,
                          text=True).stdout.strip()


def _pin(tun, sha):
    (tun / "maverick-mcp.sha").write_text(sha + "\n")
    (tun / "docker-compose.yml").write_text(f"    image: durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-{sha[:7]}\n")


def _fake_checkout(tmp_path):
    """A one-commit git repo (tmp_path/src), a copy of build.sh pinned to it, and a fake `docker` on PATH."""
    env = dict(_GIT_ENV, HOME=str(tmp_path), PATH="/usr/bin:/bin")
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(src)], check=True, env=env)
    (src / "a.txt").write_text("a\n")
    (src / ".gitignore").write_text("__pycache__/\n")
    _git(src, "add", "a.txt", ".gitignore", env=env)
    _git(src, "commit", "-q", "-m", "a", env=env)
    tun = tmp_path / "tunnel"
    tun.mkdir()
    shutil.copy(BUILD, tun / "build.sh")
    _pin(tun, _git(src, "rev-parse", "HEAD", env=env))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\necho "FAKE DOCKER $*"\n')
    docker.chmod(0o755)
    import getpass
    return src, tun, dict(env, PATH=f"{bindir}:/usr/bin:/bin", MAVERICK_MCP_SRC=str(src),
                          MAVERICK_MCP_SRC_OWNER=getpass.getuser())


def _run_build(tun, env):
    return subprocess.run(["bash", str(tun / "build.sh")], capture_output=True, text=True, env=env)


def test_build_script_builds_when_the_checkout_is_the_pinned_clean_commit(tmp_path):
    _, tun, env = _fake_checkout(tmp_path)
    result = _run_build(tun, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAKE DOCKER compose build" in result.stdout


def test_build_script_stops_when_the_checkout_is_another_commit(tmp_path):
    _, tun, env = _fake_checkout(tmp_path)
    _pin(tun, "0" * 40)
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert "not the pinned commit" in result.stderr


@pytest.mark.parametrize("dirt", ["edit", "untracked", "ignored"])
def test_build_script_stops_when_the_checkout_has_local_changes(tmp_path, dirt):
    src, tun, env = _fake_checkout(tmp_path)
    if dirt == "edit":
        (src / "a.txt").write_text("changed\n")
    elif dirt == "untracked":
        (src / "new.py").write_text("x = 1\n")
    else:  # ignored files would still be sent into the image build
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_bytes(b"x")
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert "local changes" in result.stderr


def test_build_script_stops_when_the_image_tag_does_not_name_the_pinned_commit(tmp_path):
    _, tun, env = _fake_checkout(tmp_path)
    (tun / "docker-compose.yml").write_text("    image: durable-park/wh-tunnel:0.0.369-qbo-31a1dd5-mav-0000000\n")
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert "image tag" in result.stderr


def test_build_script_stops_when_the_checkout_is_not_the_owners(tmp_path):
    _, tun, env = _fake_checkout(tmp_path)
    result = _run_build(tun, dict(env, MAVERICK_MCP_SRC_OWNER="root"))
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout
    assert "must be owned by root" in result.stderr


@pytest.mark.parametrize("where", ["top", "file", "git"])
def test_build_script_stops_when_something_is_group_or_world_writable(tmp_path, where):
    src, tun, env = _fake_checkout(tmp_path)
    target = {"top": src, "file": src / "a.txt", "git": src / ".git" / "config"}[where]
    target.chmod(target.stat().st_mode | 0o020)
    result = _run_build(tun, env)
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout
    assert "writable by group or others" in result.stderr


def test_build_script_stops_on_grafts_or_replace_refs(tmp_path):
    src, tun, env = _fake_checkout(tmp_path)
    (src / ".git" / "info").mkdir(exist_ok=True)
    (src / ".git" / "info" / "grafts").write_text("")
    result = _run_build(tun, env)
    assert result.returncode != 0 and "grafts" in result.stderr and "FAKE DOCKER" not in result.stdout
    (src / ".git" / "info" / "grafts").unlink()
    head = _git(src, "rev-parse", "HEAD", env=env)
    _git(src, "update-ref", f"refs/replace/{head}", head, env=env)
    result = _run_build(tun, env)
    assert result.returncode != 0 and "replace refs" in result.stderr and "FAKE DOCKER" not in result.stdout


def test_build_script_stops_when_the_sha_file_is_not_one_full_commit(tmp_path):
    _, tun, env = _fake_checkout(tmp_path)
    (tun / "maverick-mcp.sha").write_text("main\n")
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout


# --- Task 3: Maverick settings file and the tunnel entry ---------------------------------------------------------

TUNNEL_YAML = TUNNEL / "tunnel.yaml"
ENTER_MAVERICK = TUNNEL / "enter-maverick-secrets.sh"
ENTER_QBO = TUNNEL / "enter-secrets.sh"


def test_tunnel_yaml_has_maverick_server():
    text = TUNNEL_YAML.read_text()
    assert "servers:\n  quickbooks:\n" in text  # QuickBooks stays
    want = (
        "  maverick:\n"
        '    command: ["maverick-mcp-server"]\n'
        "    env:\n"
        "      MAVERICK_TOKEN: ${MAVERICK_TOKEN}\n"
        "      MAVERICK_GATEWAY_URL: ${MAVERICK_GATEWAY_URL}\n"
        "      MAVERICK_DASHBOARD_URL: ${MAVERICK_DASHBOARD_URL}\n"
        "      MAVERICK_INBOX_DIR: /data/maverick-inbox\n"
    )
    assert want in text
    # the QuickBooks server must not see the Maverick token, nor Maverick the QuickBooks keys
    qbo_part, mav_part = text.split("  maverick:\n", 1)
    assert "MAVERICK_" not in qbo_part.split("  quickbooks:\n", 1)[1]
    assert "QUICKBOOKS_" not in mav_part and "WITHHUMAN_" not in mav_part


def test_both_services_load_maverick_env():
    want = ["env_file:", "- ${TUNNEL_ENV_FILE:-/etc/durable-park/tunnel.env}",
            "- ${MAVERICK_ENV_FILE:-/etc/durable-park/maverick.env}"]
    for service in ("wh-tunnel", "check"):
        block = _service_block(service)
        i = block.index("env_file:")
        assert block[i:i + 3] == want, service
    assert not any(ln.startswith("env_file: ") for ln in _code(COMPOSE))  # the old one-file form is gone


def test_enter_maverick_secrets_script():
    subprocess.run(["bash", "-n", str(ENTER_MAVERICK)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(ENTER_MAVERICK)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    text = ENTER_MAVERICK.read_text()
    code = "\n".join(_code(ENTER_MAVERICK))
    assert "names=(MAVERICK_TOKEN MAVERICK_GATEWAY_URL MAVERICK_DASHBOARD_URL)" in code
    assert 'read -rs -p "${labels[$i]}: " val' in code
    assert "set +x" in code and "umask 077" in code
    assert 'FILE=$DIR/maverick.env' in code
    # the same character rule as enter-secrets.sh, line for line
    rule = next(ln.strip() for ln in ENTER_QBO.read_text().splitlines() if "[[:space:][:cntrl:]" in ln)
    assert rule in code
    assert "https://" in code and "_URL" in code
    assert 'mktemp "$DIR/.maverick.env.XXXXXX"' in code
    assert 'mv -f -- "$tmp" "$FILE"' in code
    assert 'chmod 0600 "$tmp"' in code and 'chown root:root "$tmp"' in code
    assert "Overwrite? [y/N]" in code
    # never shows a value
    for line in _code(ENTER_MAVERICK):
        assert not re.search(r"\b(echo|printf)\b.*\$val", line) or line.startswith("printf '%s=%s\\n'"), line
    assert "EUID" in text and "-t 0" in text


def _enter_maverick(tmp_path, answers):
    """Run the script on a pseudo-terminal, as if root, writing into tmp_path; returns (rc, output, file).

    Each answer is typed only after the script shows a prompt (a line ending in ": "), as a person would."""
    import os
    import pty
    import select

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n")  # the test does not run as root
    (fake_bin / "install").write_text('#!/bin/sh\nfor a; do last=$a; done\nmkdir -p "$last"\n')
    for name in ("chown", "install"):
        (fake_bin / name).chmod(0o755)
    script = tmp_path / "enter.sh"
    script.write_text(ENTER_MAVERICK.read_text().replace("${EUID}", "0", 1))
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "DURABLE_PARK_ETC_DIR": str(tmp_path / "etc"), "HOME": str(tmp_path)}
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.execve("/bin/bash", ["bash", str(script)], env)
    out, pending = b"", list(answers)
    while True:
        ready, _, _ = select.select([fd], [], [], 10)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if pending and out.endswith(b": "):
            os.write(fd, pending.pop(0).encode() + b"\n")
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace"), tmp_path / "etc" / "maverick.env"


def test_enter_maverick_secrets_writes_three_values(tmp_path):
    rc, out, file = _enter_maverick(tmp_path, ["pending", "https://maverick.invalid", "https://maverick.invalid"])
    assert rc == 0, out
    assert "saved 3 values" in out
    assert file.read_text() == ("MAVERICK_TOKEN=pending\nMAVERICK_GATEWAY_URL=https://maverick.invalid\n"
                                "MAVERICK_DASHBOARD_URL=https://maverick.invalid\n")
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert "pending" not in out  # nothing typed is echoed


def test_enter_maverick_secrets_refuses_a_url_that_is_not_https(tmp_path):
    rc, out, file = _enter_maverick(tmp_path, ["pending", "http://maverick.invalid", "https://maverick.invalid",
                                               "https://maverick.invalid"])
    assert rc == 0, out
    assert "must start with https://" in out
    assert "MAVERICK_GATEWAY_URL=https://maverick.invalid\n" in file.read_text()


def test_enter_maverick_secrets_refuses_characters_the_env_file_cannot_hold(tmp_path):
    rc, out, file = _enter_maverick(tmp_path, ["a$b", "ok-token", "https://g.invalid", "https://d.invalid"])
    assert rc == 0, out
    assert "not allowed in the env file" in out
    assert file.read_text().startswith("MAVERICK_TOKEN=ok-token\n")
