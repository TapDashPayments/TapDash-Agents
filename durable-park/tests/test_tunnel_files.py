"""deploy/tunnel/: plain-text checks of the tunnel image, compose file and scripts (no YAML package).

The image carries four stdio MCP servers: QuickBooks (Intuit, pinned inside the Dockerfile) and three TapDash servers
built from root's read-only checkouts, each at one pinned commit: Maverick, accept.blue and Authorize.net.
"""

import re
from pathlib import Path

TUNNEL = Path(__file__).resolve().parents[1] / "deploy" / "tunnel"

# key -> (sha file, image-tag part, env variable that moves the checkout, default checkout, compose build context)
PINNED = {
    "maverick": ("maverick-mcp.sha", "mav", "MAVERICK_MCP_SRC", "/opt/maverick-mcp-src", "maverick-src"),
    "acceptblue": ("acceptblue-mcp.sha", "ab", "ACCEPTBLUE_MCP_SRC", "/opt/acceptblue-mcp-src", "acceptblue-src"),
    "authnet": ("authorizenet-mcp.sha", "an", "AUTHORIZENET_MCP_SRC", "/opt/authorizenet-mcp-src", "authnet-src"),
}
SHA_FILE = TUNNEL / "maverick-mcp.sha"
QBO_TAG = "0.0.369-qbo-31a1dd5"


def _maverick_sha():
    return SHA_FILE.read_text()


def _sha(key):
    return (TUNNEL / PINNED[key][0]).read_text().strip()


def _tag(shas=None):
    """The image tag the pins give: QuickBooks, then -mav-, -ab- and -an- with the first 7 of each commit."""
    shas = shas or {key: _sha(key) for key in PINNED}
    return QBO_TAG + "".join(f"-{PINNED[key][1]}-{shas[key][:7]}" for key in PINNED)


def test_every_sha_file_is_one_full_commit():
    for key, (name, *_rest) in PINNED.items():
        text = (TUNNEL / name).read_text()
        assert re.fullmatch(r"[0-9a-f]{40}\n", text), f"{name}: one line, the 40-character commit"


def test_maverick_sha_file_is_one_full_commit():
    assert re.fullmatch(r"[0-9a-f]{40}\n", _maverick_sha()), "one line: the 40-character maverick-mcp commit"


def test_the_payment_server_pins_are_the_reviewed_commits():
    """The approval lists in tests/payments_servers.py were taken at exactly these commits."""
    from payments_servers import ACCEPTBLUE_COMMIT, AUTHORIZENET_COMMIT

    assert _sha("acceptblue") == ACCEPTBLUE_COMMIT == "9bb8d722a0d5fc9dce08437eedc8328d34005bcc"
    assert _sha("authnet") == AUTHORIZENET_COMMIT == "4dd427863d1103c788bfa03d3dfde46777bde3a0"


def test_the_image_tag_names_all_four_servers_and_fits_docker():
    tag = _tag()
    assert tag == (f"0.0.369-qbo-31a1dd5-mav-{_sha('maverick')[:7]}-ab-9bb8d72-an-4dd4278")
    assert len(tag) <= 128 and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)


# --- Task 2: the tunnel image carries the TapDash servers -------------------------------------------------------

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


def test_both_services_use_the_same_tag_with_all_three_shas():
    want = f"image: durable-park/wh-tunnel:{_tag()}"
    images = [ln for ln in _code(COMPOSE) if ln.startswith("image:")]
    assert images == [want, want]
    assert want in _service_block("wh-tunnel") and want in _service_block("check")


def test_compose_has_a_build_context_for_each_checkout():
    block = _service_block("wh-tunnel")
    assert "additional_contexts:" in block
    for key, (_sha_file, _part, var, default, context) in PINNED.items():
        assert f"{context}: ${{{var}:-{default}}}" in block, key
    # three contexts and nothing else under additional_contexts
    start = block.index("additional_contexts:")
    assert block[start + 1:start + 4] == [f"{PINNED[k][4]}: ${{{PINNED[k][2]}:-{PINNED[k][3]}}}" for k in PINNED]
    assert block[start + 4] == "restart: unless-stopped"


def test_compose_mounts_the_maverick_inbox_volume_in_the_tunnel_only():
    assert "- maverick-inbox:/data/maverick-inbox" in _service_block("wh-tunnel")
    assert not any("maverick-inbox" in ln for ln in _service_block("check"))
    text = COMPOSE.read_text()
    assert "  maverick-inbox:\n    name: durable-park-maverick-inbox\n" in text


def test_the_payment_servers_keep_nothing_on_disk():
    """accept.blue and Authorize.net write no files: compose names them only as a build context and an env file."""
    lines = [ln for ln in _code(COMPOSE) if "acceptblue" in ln.lower() or "authnet" in ln.lower()]
    assert len(lines) == 2 * 2 + 2  # one env file line per service for each of them, plus the two build contexts
    for ln in lines:
        assert ln.startswith(("acceptblue-src:", "authnet-src:", "- ${ACCEPTBLUE_ENV_FILE", "- ${AUTHNET_ENV_FILE")), ln


def _stage(name):
    """The lines of one Dockerfile stage (from its FROM to the next FROM), comments dropped, joined."""
    code, inside = [], False
    for ln in _code(DOCKERFILE):
        if ln.startswith("FROM "):
            inside = ln.endswith(f" AS {name}") or (name == "runtime" and " AS " not in ln)
        if inside:
            code.append(ln)
    return " ".join(code)


@pytest.mark.parametrize("stage,context,short,package", [
    ("maverick", "maverick-src", "maverick", "maverick_mcp"),
    ("acceptblue", "acceptblue-src", "acceptblue", "acceptblue_mcp"),
    ("authnet", "authnet-src", "authnet", "authorizenet_mcp"),
])
def test_dockerfile_installs_with_require_hashes(stage, context, short, package):
    code = " ".join(_code(DOCKERFILE))
    s = _stage(stage)
    assert f"FROM ${{TUNNEL_IMAGE}} AS {stage}" in code
    assert (f"COPY --from={context} requirements.lock requirements-dev.lock pyproject.toml /src/{short}/") in s
    assert f"COPY --from={context} src /src/{short}/src" in s
    # runtime packages: only from the hashed lock, wheels only (nothing compiled from an unpinned sdist)
    assert (f"/opt/{short}-mcp/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: "
            f"-r /src/{short}/requirements.lock") in s
    # the build tool (setuptools) is hash-checked too, from the dev lock; then the package itself, without deps
    assert f"--require-hashes --only-binary=:all: -r /src/{short}/requirements-dev.lock" in s
    assert "--no-deps --no-build-isolation --check-build-dependencies" in s
    assert f"/opt/{short}-mcp/bin/pip install --no-cache-dir --no-deps --no-index /src/wheels/{package}-*.whl" in s
    # the build fails when the server cannot even list its tools
    assert s.rstrip().endswith(f"/opt/{short}-mcp/bin/python -m {package} --list-approvals > /dev/null")
    assert f"python3 -m venv /opt/{short}-build" in s and f"python3 -m venv /opt/{short}-mcp" in s
    for forbidden in ("pip install --upgrade", "pip install -U ", "git clone", "curl ", "wget "):
        assert forbidden not in code, forbidden


def test_dockerfile_runtime_stage_has_the_launchers_and_the_inbox():
    code = _code(DOCKERFILE)
    joined = " ".join(code)
    for short, package, launcher in (("maverick", "maverick_mcp", "maverick-mcp-server"),
                                     ("acceptblue", "acceptblue_mcp", "acceptblue-mcp-server"),
                                     ("authnet", "authorizenet_mcp", "authnet-mcp-server")):
        venv = f"/opt/{short}-mcp"
        assert f"COPY --from={short} {venv} {venv}" in joined
        assert f'exec {venv}/bin/python -m {package} "$@"' in joined
        assert f"/usr/local/bin/{launcher}" in joined
        assert f"chmod 0755 /usr/local/bin/{launcher}" in joined
    assert "mkdir -p /data/maverick-inbox" in joined
    assert "chown withhuman:withhuman /data/maverick-inbox" in joined
    assert "chmod 0700 /data/maverick-inbox" in joined
    assert code[-1] == "USER withhuman"
    # only Maverick has an inbox: the other two servers write no file
    assert "inbox" not in _stage("acceptblue") + _stage("authnet")
    assert joined.count("mkdir -p /data") == 2  # /data and /data/maverick-inbox


def test_dockerignore_still_excludes_env():
    lines = (TUNNEL / ".dockerignore").read_text().split()
    assert "*.env" in lines and "*.env.*" in lines


def test_build_script_checks_sha_and_clean_tree():
    subprocess.run(["bash", "-n", str(BUILD)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(BUILD)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    text = BUILD.read_text()
    for _key, (sha_file, _part, var, default, _context) in PINNED.items():
        assert sha_file in text
        assert var in text and default in text
    assert "rev-parse HEAD" in text
    assert "status --porcelain --ignored --untracked-files=all" in text
    assert "docker compose build" in text
    assert text.index("rev-parse HEAD") < text.index("docker compose build")
    assert text.index("status --porcelain") < text.index("docker compose build")
    # PR 53 review I1: root's checkout, no safe.directory, no replace objects or grafts
    assert "-c safe.directory" not in text
    assert "GIT_NO_REPLACE_OBJECTS=1 git --no-replace-objects" in text
    assert ".git/info/grafts" in text and "refs/replace/" in text
    assert "OWNER=${TUNNEL_SRC_OWNER:-root}" in text
    # every checkout is handed to compose, so the build uses the checkout that was checked
    build_line = next(ln for ln in text.splitlines() if ln.rstrip().endswith("docker compose build"))
    for _key, (_f, _p, var, *_r) in PINNED.items():
        assert f"{var}=" in build_line


_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(src, *args, env):
    return subprocess.run(["git", "-C", str(src), *args], check=True, env=env, capture_output=True,
                          text=True).stdout.strip()


def _write_pins(tun, shas):
    for key, (sha_file, *_rest) in PINNED.items():
        (tun / sha_file).write_text(shas[key] + "\n")
    (tun / "docker-compose.yml").write_text(f"    image: durable-park/wh-tunnel:{_tag(shas)}\n")


# launcher in the image -> the committed file that must equal its --list-approvals output
LAUNCHERS = {"maverick": "maverick-mcp-server", "acceptblue": "acceptblue-mcp-server", "authnet": "authnet-mcp-server"}
APPROVAL_FILES = {key: f"{key}-approvals.json" for key in LAUNCHERS}
# `docker compose build` prints a line; `docker run --entrypoint <launcher> ... --list-approvals` prints the file the
# fake image "says" for that launcher (and is logged); FAKE_DOCKER_FAIL=run makes `docker run` fail.
FAKE_DOCKER = """#!/bin/sh
if [ "$1" = run ]; then
  echo "$*" >> "$FAKE_DOCKER_LOG"
  [ "${FAKE_DOCKER_FAIL:-}" = run ] && exit 1
  while [ $# -gt 0 ]; do
    [ "$1" = --entrypoint ] && launcher=$2
    shift
  done
  case $launcher in
    maverick-mcp-server) cat "$FAKE_IMAGE_SAYS/maverick-approvals.json" ;;
    acceptblue-mcp-server) cat "$FAKE_IMAGE_SAYS/acceptblue-approvals.json" ;;
    authnet-mcp-server) cat "$FAKE_IMAGE_SAYS/authnet-approvals.json" ;;
  esac
  exit 0
fi
echo "FAKE DOCKER $*"
"""


def _fake_checkouts(tmp_path):
    """A one-commit git repo per server (tmp_path/src-<key>), a copy of build.sh pinned to them and a fake `docker`.

    Returns ({key: repo}, tunnel dir, environment)."""
    import getpass

    env = dict(_GIT_ENV, HOME=str(tmp_path), PATH="/usr/bin:/bin")
    srcs, shas = {}, {}
    for key in PINNED:
        src = tmp_path / f"src-{key}"
        subprocess.run(["git", "init", "-q", str(src)], check=True, env=env)
        (src / "a.txt").write_text(f"{key}\n")
        (src / ".gitignore").write_text("__pycache__/\n")
        _git(src, "add", "a.txt", ".gitignore", env=env)
        _git(src, "commit", "-q", "-m", "a", env=env)
        srcs[key], shas[key] = src, _git(src, "rev-parse", "HEAD", env=env)
    tun = tmp_path / "tunnel"
    tun.mkdir()
    shutil.copy(BUILD, tun / "build.sh")
    _write_pins(tun, shas)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # What the built image prints is a copy of the committed files; the committed files are copies in `tun`.
    said = tmp_path / "image-says"
    said.mkdir()
    for _key, name in APPROVAL_FILES.items():
        shutil.copy(TUNNEL / name, tun / name)
        shutil.copy(TUNNEL / name, said / name)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    out = dict(env, PATH=f"{bindir}:/usr/bin:/bin", TUNNEL_SRC_OWNER=getpass.getuser(),
               FAKE_IMAGE_SAYS=str(said), FAKE_DOCKER_LOG=str(tmp_path / "docker.log"))
    for key, (_f, _p, var, *_r) in PINNED.items():
        out[var] = str(srcs[key])
    return srcs, tun, out, shas


def _run_build(tun, env):
    return subprocess.run(["bash", str(tun / "build.sh")], capture_output=True, text=True, env=env)


def test_build_script_builds_when_every_checkout_is_the_pinned_clean_commit(tmp_path):
    _, tun, env, shas = _fake_checkouts(tmp_path)
    result = _run_build(tun, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAKE DOCKER compose build" in result.stdout
    for key, (sha_file, *_rest) in PINNED.items():  # it says which commit each server is built from
        assert f"{sha_file[:-4]} source:" in result.stdout and f"at {shas[key]} (clean)" in result.stdout


def test_build_script_compares_every_launchers_approvals_with_the_committed_file(tmp_path):
    _, tun, env, shas = _fake_checkouts(tmp_path)
    result = _run_build(tun, env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert len(calls) == 3
    for key, launcher in LAUNCHERS.items():
        call = next(c for c in calls if f"--entrypoint {launcher} " in c)
        assert call == f"run --rm --pull=never --entrypoint {launcher} durable-park/wh-tunnel:{_tag(shas)} --list-approvals"
        assert f"{key} approvals: ok" in result.stdout


def test_build_script_runs_the_approvals_checks_after_the_build_and_names_every_file():
    text = BUILD.read_text()
    assert text.index("docker compose build\n") < text.index("check_approvals maverick")
    assert "docker run --rm --pull=never --entrypoint" in text and "--list-approvals" in text
    for launcher in LAUNCHERS.values():
        assert launcher in text
    for name in APPROVAL_FILES.values():
        assert name in text


@pytest.mark.parametrize("key", sorted(LAUNCHERS))
def test_build_script_fails_loudly_when_the_image_says_something_else(tmp_path, key):
    import json

    _, tun, env, _shas = _fake_checkouts(tmp_path)
    said = Path(env["FAKE_IMAGE_SAYS"]) / APPROVAL_FILES[key]
    data = json.loads(said.read_text())
    name = sorted(data)[0]
    data[name] = "auto" if data[name] == "you" else "you"  # one tool changes its level in the image
    said.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    result = _run_build(tun, env)
    assert result.returncode == 1
    assert LAUNCHERS[key] in result.stderr and APPROVAL_FILES[key] in result.stderr
    assert "do not run this image" in result.stderr
    assert name in result.stderr  # the difference is shown


def test_build_script_fails_when_the_committed_file_is_edited(tmp_path):
    _, tun, env, _shas = _fake_checkouts(tmp_path)
    expected = tun / APPROVAL_FILES["acceptblue"]
    expected.write_text(expected.read_text().replace('"ablue_read": "auto"', '"ablue_read": "you"'))
    result = _run_build(tun, env)
    assert result.returncode == 1
    assert "acceptblue-mcp-server" in result.stderr and "ablue_read" in result.stderr


def test_build_script_fails_when_the_image_says_nothing(tmp_path):
    _, tun, env, _shas = _fake_checkouts(tmp_path)
    (Path(env["FAKE_IMAGE_SAYS"]) / APPROVAL_FILES["authnet"]).write_text("")
    result = _run_build(tun, env)
    assert result.returncode == 1 and "authnet-mcp-server" in result.stderr


def test_build_script_fails_when_the_launcher_does_not_run(tmp_path):
    _, tun, env, _shas = _fake_checkouts(tmp_path)
    result = _run_build(tun, dict(env, FAKE_DOCKER_FAIL="run"))
    assert result.returncode == 1 and "--list-approvals" in result.stderr


@pytest.mark.parametrize("key", sorted(LAUNCHERS))
def test_build_script_fails_when_a_committed_file_is_missing(tmp_path, key):
    _, tun, env, _shas = _fake_checkouts(tmp_path)
    (tun / APPROVAL_FILES[key]).unlink()
    result = _run_build(tun, env)
    assert result.returncode == 1 and APPROVAL_FILES[key] in result.stderr


@pytest.mark.parametrize("key", sorted(PINNED))
def test_build_script_stops_when_a_checkout_is_another_commit(tmp_path, key):
    _, tun, env, shas = _fake_checkouts(tmp_path)
    _write_pins(tun, {**shas, key: "0" * 40})
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert "not the pinned commit" in result.stderr and PINNED[key][0] in result.stderr


@pytest.mark.parametrize("key", sorted(PINNED))
@pytest.mark.parametrize("dirt", ["edit", "untracked", "ignored"])
def test_build_script_stops_when_a_checkout_has_local_changes(tmp_path, key, dirt):
    srcs, tun, env, _ = _fake_checkouts(tmp_path)
    src = srcs[key]
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


@pytest.mark.parametrize("part,index", [("mav", 0), ("ab", 1), ("an", 2)])
def test_build_script_stops_when_the_image_tag_does_not_name_a_pinned_commit(tmp_path, part, index):
    _, tun, env, shas = _fake_checkouts(tmp_path)
    tag = _tag(shas)
    bad = tag.replace(f"-{part}-{shas[list(PINNED)[index]][:7]}", f"-{part}-0000000")
    assert bad != tag
    (tun / "docker-compose.yml").write_text(f"    image: durable-park/wh-tunnel:{bad}\n")
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert "image tag" in result.stderr


def test_build_script_stops_when_one_image_line_is_stale(tmp_path):
    """Both compose services must carry the tag: a second image line with an older tag stops the build."""
    _, tun, env, shas = _fake_checkouts(tmp_path)
    old_tag = QBO_TAG + f"-mav-{shas['maverick'][:7]}"
    (tun / "docker-compose.yml").write_text(f"    image: durable-park/wh-tunnel:{_tag(shas)}\n"
                                            f"    image: durable-park/wh-tunnel:{old_tag}\n")
    result = _run_build(tun, env)
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout and "image tag" in result.stderr


def test_build_script_stops_when_a_checkout_is_not_the_owners(tmp_path):
    _, tun, env, _ = _fake_checkouts(tmp_path)
    result = _run_build(tun, dict(env, TUNNEL_SRC_OWNER="root"))
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout
    assert "must be owned by root" in result.stderr


@pytest.mark.parametrize("key", sorted(PINNED))
@pytest.mark.parametrize("where", ["top", "file", "git"])
def test_build_script_stops_when_something_is_group_or_world_writable(tmp_path, key, where):
    srcs, tun, env, _ = _fake_checkouts(tmp_path)
    src = srcs[key]
    target = {"top": src, "file": src / "a.txt", "git": src / ".git" / "config"}[where]
    target.chmod(target.stat().st_mode | 0o020)
    result = _run_build(tun, env)
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout
    assert "writable by group or others" in result.stderr


@pytest.mark.parametrize("key", sorted(PINNED))
def test_build_script_stops_on_grafts_or_replace_refs(tmp_path, key):
    srcs, tun, env, _ = _fake_checkouts(tmp_path)
    src = srcs[key]
    (src / ".git" / "info").mkdir(exist_ok=True)
    (src / ".git" / "info" / "grafts").write_text("")
    result = _run_build(tun, env)
    assert result.returncode != 0 and "grafts" in result.stderr and "FAKE DOCKER" not in result.stdout
    (src / ".git" / "info" / "grafts").unlink()
    head = _git(src, "rev-parse", "HEAD", env=env)
    _git(src, "update-ref", f"refs/replace/{head}", head, env=env)
    result = _run_build(tun, env)
    assert result.returncode != 0 and "replace refs" in result.stderr and "FAKE DOCKER" not in result.stdout


@pytest.mark.parametrize("key", sorted(PINNED))
def test_build_script_stops_when_a_sha_file_is_not_one_full_commit(tmp_path, key):
    _, tun, env, _ = _fake_checkouts(tmp_path)
    (tun / PINNED[key][0]).write_text("main\n")
    result = _run_build(tun, env)
    assert result.returncode != 0
    assert "FAKE DOCKER" not in result.stdout
    assert PINNED[key][0] in result.stderr


def test_build_script_stops_when_a_checkout_is_missing(tmp_path):
    srcs, tun, env, _ = _fake_checkouts(tmp_path)
    result = _run_build(tun, dict(env, ACCEPTBLUE_MCP_SRC=str(tmp_path / "nowhere")))
    assert result.returncode != 0 and "FAKE DOCKER" not in result.stdout
    assert "is not a git checkout" in result.stderr


# --- Task 3: settings files and the tunnel entries ----------------------------------------------------------------

TUNNEL_YAML = TUNNEL / "tunnel.yaml"
ENTER_MAVERICK = TUNNEL / "enter-maverick-secrets.sh"
ENTER_ACCEPTBLUE = TUNNEL / "enter-acceptblue-secrets.sh"
ENTER_AUTHNET = TUNNEL / "enter-authnet-secrets.sh"
ENTER_QBO = TUNNEL / "enter-secrets.sh"


def _servers():
    """tunnel.yaml's `servers:` as {key: text of its block}."""
    text = TUNNEL_YAML.read_text()
    body = text.split("\nservers:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z]+):\n", body)
    return dict(zip(parts[1::2], parts[2::2]))


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


def test_tunnel_yaml_has_the_accept_blue_and_authorize_net_servers():
    servers = _servers()
    assert list(servers) == ["quickbooks", "maverick", "acceptblue", "authnet"]
    assert servers["acceptblue"] == (
        '    command: ["acceptblue-mcp-server"]\n'
        "    env:\n"
        "      ACCEPTBLUE_SOURCE_KEY: ${ACCEPTBLUE_SOURCE_KEY}\n"
        "      ACCEPTBLUE_PIN: ${ACCEPTBLUE_PIN}\n"
        "      ACCEPTBLUE_BASE_URL: ${ACCEPTBLUE_BASE_URL}\n")
    # AUTHNET_ENV is fixed to sandbox here: production is a reviewed change to this file, not a typed value
    assert servers["authnet"] == (
        '    command: ["authnet-mcp-server"]\n'
        "    env:\n"
        "      AUTHNET_ENV: sandbox\n"
        "      AUTHNET_LOGIN_ID: ${AUTHNET_LOGIN_ID}\n"
        "      AUTHNET_TRANSACTION_KEY: ${AUTHNET_TRANSACTION_KEY}\n")


def test_each_tunnel_server_sees_only_its_own_variables():
    servers = _servers()
    own = {"quickbooks": "QUICKBOOKS_", "maverick": "MAVERICK_", "acceptblue": "ACCEPTBLUE_", "authnet": "AUTHNET_"}
    for key, block in servers.items():
        names = re.findall(r"(?m)^      ([A-Z_]+):", block)
        assert names and all(n.startswith(own[key]) for n in names), key
        for other_key, prefix in own.items():
            if other_key != key:
                assert prefix not in block, (key, prefix)
        assert "WITHHUMAN_" not in block, key
    # the optional webhook receiver is not wired: nothing sets its listen address or signature key
    assert "AUTHNET_WEBHOOK_LISTEN" not in TUNNEL_YAML.read_text()
    assert "AUTHNET_SIGNATURE_KEY" not in TUNNEL_YAML.read_text()
    assert "AUTHNET_API_URL" not in TUNNEL_YAML.read_text()  # the sandbox addresses are the server's defaults


def test_all_services_load_every_env_file():
    want = ["env_file:", "- ${TUNNEL_ENV_FILE:-/etc/durable-park/tunnel.env}",
            "- ${MAVERICK_ENV_FILE:-/etc/durable-park/maverick.env}",
            "- ${ACCEPTBLUE_ENV_FILE:-/etc/durable-park/acceptblue.env}",
            "- ${AUTHNET_ENV_FILE:-/etc/durable-park/authnet.env}"]
    for service in ("wh-tunnel", "check"):
        block = _service_block(service)
        i = block.index("env_file:")
        assert block[i:i + 5] == want, service
    assert not any(ln.startswith("env_file: ") for ln in _code(COMPOSE))  # the old one-file form is gone


def _shellcheck_and_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    if _shellcheck() is not None:
        result = subprocess.run([_shellcheck(), str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def _common_script_rules(script, names_line, env_file):
    text = script.read_text()
    code = "\n".join(_code(script))
    assert names_line in code
    assert 'read -rs -p "${labels[$i]}: " val' in code
    assert "set +x" in code and "umask 077" in code
    assert f"FILE=$DIR/{env_file}" in code
    # the same character rule as enter-secrets.sh, line for line
    rule = next(ln.strip() for ln in ENTER_QBO.read_text().splitlines() if "[[:space:][:cntrl:]" in ln)
    assert rule in code
    assert f'mktemp "$DIR/.{env_file}.XXXXXX"' in code
    assert 'mv -f -- "$tmp" "$FILE"' in code
    assert 'chmod 0600 "$tmp"' in code and 'chown root:root "$tmp"' in code
    assert "Overwrite? [y/N]" in code
    # never shows a value
    for line in _code(script):
        assert not re.search(r"\b(echo|printf)\b.*\$val", line) or line.startswith("printf '%s=%s\\n'"), line
    assert "EUID" in text and "-t 0" in text


def test_enter_maverick_secrets_script():
    _shellcheck_and_syntax(ENTER_MAVERICK)
    _common_script_rules(ENTER_MAVERICK, "names=(MAVERICK_TOKEN MAVERICK_GATEWAY_URL MAVERICK_DASHBOARD_URL)",
                         "maverick.env")
    assert "https://" in ENTER_MAVERICK.read_text() and "_URL" in ENTER_MAVERICK.read_text()


def test_enter_acceptblue_secrets_script():
    _shellcheck_and_syntax(ENTER_ACCEPTBLUE)
    _common_script_rules(ENTER_ACCEPTBLUE, "names=(ACCEPTBLUE_SOURCE_KEY ACCEPTBLUE_PIN ACCEPTBLUE_BASE_URL)",
                         "acceptblue.env")
    text = ENTER_ACCEPTBLUE.read_text()
    assert "https://" in text and "_URL" in text and "pk_" in text and "/api/v2" in text


def test_enter_authnet_secrets_script():
    _shellcheck_and_syntax(ENTER_AUTHNET)
    _common_script_rules(ENTER_AUTHNET, "names=(AUTHNET_LOGIN_ID AUTHNET_TRANSACTION_KEY)", "authnet.env")
    text = ENTER_AUTHNET.read_text()
    assert "longest=(20 16)" in text  # the server's own limits: login id 20, transaction key 16 characters
    assert "AUTHNET_ENV" not in "\n".join(_code(ENTER_AUTHNET))  # fixed in tunnel.yaml, not typed


def _enter(script, env_file, tmp_path, answers):
    """Run a script on a pseudo-terminal, as if root, writing into tmp_path; returns (rc, output, file).

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
    runner = tmp_path / "enter.sh"
    runner.write_text(script.read_text().replace("${EUID}", "0", 1))
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "DURABLE_PARK_ETC_DIR": str(tmp_path / "etc"), "HOME": str(tmp_path)}
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.execve("/bin/bash", ["bash", str(runner)], env)
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
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace"), tmp_path / "etc" / env_file


def _enter_maverick(tmp_path, answers):
    return _enter(ENTER_MAVERICK, "maverick.env", tmp_path, answers)


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


SANDBOX_URL = "https://api.sandbox.accept.blue/api/v2"


def _enter_acceptblue(tmp_path, answers):
    return _enter(ENTER_ACCEPTBLUE, "acceptblue.env", tmp_path, answers)


def test_enter_acceptblue_secrets_writes_three_values(tmp_path):
    rc, out, file = _enter_acceptblue(tmp_path, ["pending", "pending", SANDBOX_URL])
    assert rc == 0, out
    assert "saved 3 values" in out
    assert file.read_text() == (f"ACCEPTBLUE_SOURCE_KEY=pending\nACCEPTBLUE_PIN=pending\n"
                                f"ACCEPTBLUE_BASE_URL={SANDBOX_URL}\n")
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert "pending" not in out  # nothing typed is echoed


def test_enter_acceptblue_secrets_refuses_an_address_the_server_would_refuse(tmp_path):
    answers = ["pending", "pending", "http://api.sandbox.accept.blue/api/v2",
               "https://acceptblue.invalid", "https://api.sandbox.accept.blue/api/v1",
               "https://evil.example/api/v2", "https://accept.blue.evil.example/api/v2", SANDBOX_URL]
    rc, out, file = _enter_acceptblue(tmp_path, answers)
    assert rc == 0, out
    assert "must start with https://" in out
    assert out.count("must be https://<host>.accept.blue/api/v2") == 4
    assert file.read_text().endswith(f"ACCEPTBLUE_BASE_URL={SANDBOX_URL}\n")


def test_enter_acceptblue_secrets_accepts_production_and_a_trailing_slash(tmp_path):
    rc, out, file = _enter_acceptblue(tmp_path, ["k", "p", "https://api.accept.blue/api/v2/"])
    assert rc == 0, out
    assert file.read_text().endswith("ACCEPTBLUE_BASE_URL=https://api.accept.blue/api/v2/\n")


def test_enter_acceptblue_secrets_refuses_a_public_key(tmp_path):
    rc, out, file = _enter_acceptblue(tmp_path, ["pk_live_abc", "PK_x", "real-key", "pending", SANDBOX_URL])
    assert rc == 0, out
    assert out.count("public web-page key") == 2
    assert file.read_text().startswith("ACCEPTBLUE_SOURCE_KEY=real-key\nACCEPTBLUE_PIN=pending\n")
    assert "pk_" not in file.read_text()


def test_enter_acceptblue_secrets_refuses_characters_the_env_file_cannot_hold(tmp_path):
    rc, out, file = _enter_acceptblue(tmp_path, ["a$b", "ok-key", "a b", "ok-pin", SANDBOX_URL])
    assert rc == 0, out
    assert out.count("not allowed in the env file") == 2
    assert file.read_text().startswith("ACCEPTBLUE_SOURCE_KEY=ok-key\nACCEPTBLUE_PIN=ok-pin\n")


def _enter_authnet(tmp_path, answers):
    return _enter(ENTER_AUTHNET, "authnet.env", tmp_path, answers)


def test_enter_authnet_secrets_writes_two_values(tmp_path):
    rc, out, file = _enter_authnet(tmp_path, ["pending", "pending"])
    assert rc == 0, out
    assert "saved 2 values" in out
    assert file.read_text() == "AUTHNET_LOGIN_ID=pending\nAUTHNET_TRANSACTION_KEY=pending\n"
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert "pending" not in out


def test_enter_authnet_secrets_refuses_values_longer_than_the_servers_limits(tmp_path):
    answers = ["x" * 21, "L" * 20, "k" * 17, "k" * 16]
    rc, out, file = _enter_authnet(tmp_path, answers)
    assert rc == 0, out
    assert "at most 20 characters" in out and "at most 16 characters" in out
    assert file.read_text() == f"AUTHNET_LOGIN_ID={'L' * 20}\nAUTHNET_TRANSACTION_KEY={'k' * 16}\n"


def test_enter_authnet_secrets_refuses_characters_the_env_file_cannot_hold(tmp_path):
    rc, out, file = _enter_authnet(tmp_path, ["a$b", "ok-id", "a#b", "ok-key"])
    assert rc == 0, out
    assert out.count("not allowed in the env file") == 2
    assert file.read_text() == "AUTHNET_LOGIN_ID=ok-id\nAUTHNET_TRANSACTION_KEY=ok-key\n"


def test_every_documented_tunnel_start_uses_no_build():
    """`docker compose up` builds a missing image on its own and would skip build.sh's checks."""
    repo = TUNNEL.parents[2]
    files = [TUNNEL / "docker-compose.yml", repo / "durable-park" / "docs" / "runbook.md",
             repo / "durable-park" / "docs" / "gateway-setup.md"]
    seen = 0
    for path in files:
        for line in path.read_text().splitlines():
            if "compose up -d" in line:
                seen += 1
                assert "up -d --no-build wh-tunnel" in line, f"{path.name}: {line.strip()}"
    assert seen >= 5
