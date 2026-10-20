"""provision.py: pending receiver.json -> live receiver -> AAP provisioning -> final files."""

import asyncio
import json
import stat

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, PROVISIONER_TOKEN, TOKEN, FakeProvider

import app as receiver_app
import provision

URL = "https://proto-agents.taila95862.ts.net/aap/events"
NAME = "proto-agents-1"


class LiveReceiver:
    """Plays the durable-park-receiver service: every restart re-reads receiver.json from disk."""

    def __init__(self, etc_dir):
        self.etc_dir = etc_dir
        self.app = None
        self.restarts = 0
        self.broken = False

    def restart(self):
        self.restarts += 1
        self.app = receiver_app.create_app(config_path=self.etc_dir / "receiver.json",
                                           wake_secret_file=self.etc_dir / "wake_secret",
                                           wake_url="http://127.0.0.1:9/unused")

    async def __call__(self, scope, receive, send):
        if self.app is None or self.broken:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)

    def http(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self))

    async def handle(self, headers, body):
        """FakeProvider's verification hook: the provider reaches the receiver at its public URL."""
        async with self.http() as client:
            response = await client.post(URL, headers=headers, content=body)
        return response.status_code, response.content


@pytest.fixture
def env(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    token_file = tmp_path / "provisioner_token"
    token_file.write_text(PROVISIONER_TOKEN + "\n")
    live = LiveReceiver(etc)
    provider = FakeProvider()
    provider.receivers[URL] = live.handle
    owners = {}

    def chown(path, user, group):
        owners[path.name] = (user, group)

    return {"etc": etc, "token_file": token_file, "live": live, "provider": provider, "owners": owners, "chown": chown}


def _run(env, *extra, capsys=None, **overrides):
    args = provision.parse_args(["--provisioner-token-file", str(env["token_file"]), "--url", URL, "--name", NAME,
                                 "--aap-base-url", BASE_URL, *extra])

    async def go():
        kwargs = dict(etc_dir=env["etc"], chown=env["chown"], restart=env["live"].restart,
                      receiver_http=env["live"].http(), provider_http=env["provider"].async_http(),
                      retry_delay=0)
        kwargs.update(overrides)
        return await provision.run(args, **kwargs)

    return asyncio.run(go())


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_provision_writes_files_with_modes_and_owners_and_prints_only_the_instance_id(env, capsys):
    assert _run(env) == 0
    out, err = capsys.readouterr()
    (instance_id,) = env["provider"].instances
    assert out == instance_id + "\n"
    receiver = json.loads((env["etc"] / "receiver.json").read_text())
    assert receiver["instance_id"] == instance_id
    assert receiver["instance_token"] == TOKEN  # R18: the receiver's own copy of the credential
    assert receiver["signing_secret"].startswith("whsec_")
    assert receiver["url"] == URL and receiver["instance_name"] == NAME and receiver["aap_base_url"] == BASE_URL
    submission, _ = env["provider"].provisions[receiver["idempotency_key"]]
    assert submission["delivery"] == {"type": "webhook", "url": URL, "registration_id": receiver["registration_id"],
                                      "signing_secret": receiver["signing_secret"]}
    assert submission["instance_name"] == NAME
    assert (env["etc"] / "instance_token").read_text() == TOKEN + "\n"
    assert _mode(env["etc"] / "receiver.json") == 0o600
    assert _mode(env["etc"] / "instance_token") == 0o640
    assert env["owners"] == {"receiver.json": ("aaprecv", "aaprecv"), "instance_token": ("root", "hermes")}
    assert [p.name for p in env["etc"].iterdir() if p.name.startswith(".")] == []  # no temp files left
    for secret in (TOKEN, PROVISIONER_TOKEN, receiver["signing_secret"]):
        assert secret not in out and secret not in err
    assert env["live"].restarts == 2  # once with the pending secret, once with the final config


def test_live_receiver_holds_the_secret_before_withhuman_verifies(env):
    seen = {}
    original = env["live"].handle

    async def spy(headers, body):
        seen["config"] = json.loads((env["etc"] / "receiver.json").read_text())
        seen["restarts"] = env["live"].restarts
        return await original(headers, body)

    env["provider"].receivers[URL] = spy
    assert _run(env) == 0
    assert seen["restarts"] == 1
    assert seen["config"]["instance_id"] is None and seen["config"]["instance_token"] is None
    final = json.loads((env["etc"] / "receiver.json").read_text())
    assert seen["config"]["signing_secret"] == final["signing_secret"]
    assert seen["config"]["registration_id"] == final["registration_id"]


def test_rerun_does_not_create_a_second_instance(env, capsys):
    assert _run(env) == 0
    first = capsys.readouterr().out
    (env["etc"] / "instance_token").unlink()  # repaired from receiver.json on re-run
    calls = len(env["provider"].calls)
    assert _run(env) == 0
    assert capsys.readouterr().out == first
    assert len(env["provider"].instances) == 1
    assert len(env["provider"].calls) == calls  # no second provisioning call at all
    assert (env["etc"] / "instance_token").read_text() == TOKEN + "\n"


def test_failed_verification_keeps_the_registration_and_retries_with_the_same_key(env, capsys):
    env["provider"].receivers[URL] = lambda headers, body: _answer(500)
    assert _run(env) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "could not verify" in err
    assert not (env["etc"] / "instance_token").exists()
    pending = json.loads((env["etc"] / "receiver.json").read_text())
    assert pending["instance_id"] is None and env["provider"].instances == {}

    env["provider"].receivers[URL] = env["live"].handle
    assert _run(env) == 0
    final = json.loads((env["etc"] / "receiver.json").read_text())
    for key in ("signing_secret", "registration_id", "idempotency_key"):
        assert final[key] == pending[key]
    assert len(env["provider"].instances) == 1


async def _answer(status):
    return status, b""


def test_replay_after_a_crash_before_files_were_written_returns_the_same_instance(env, capsys):
    assert _run(env) == 0
    instance_id = capsys.readouterr().out.strip()
    final = json.loads((env["etc"] / "receiver.json").read_text())
    # simulate: provider created the instance but the files were never finalised
    final.update(instance_id=None, instance_token=None)
    (env["etc"] / "receiver.json").write_text(json.dumps(final))
    (env["etc"] / "instance_token").unlink()
    assert _run(env) == 0
    assert capsys.readouterr().out.strip() == instance_id
    assert len(env["provider"].instances) == 1
    assert (env["etc"] / "instance_token").read_text() == TOKEN + "\n"


def test_receiver_not_answering_aborts_before_anything_is_sent_to_withhuman(env, capsys):
    env["live"].broken = True
    assert _run(env) == 1
    assert "did not answer" in capsys.readouterr().err
    assert env["provider"].calls == []


def _recording(env, urls):
    live_http = env["live"].http()

    class Recording(httpx.AsyncClient):
        async def post(self, url, **kwargs):
            urls.append(str(url))
            return await live_http.post(url, **kwargs)

    return Recording()


def test_self_test_checks_the_local_then_the_public_url_before_provisioning(env):
    urls = []
    assert _run(env, receiver_http=_recording(env, urls)) == 0
    assert urls == [provision.DEFAULT_LOCAL_URL, URL, provision.DEFAULT_LOCAL_URL]


def test_skip_public_check_uses_only_the_local_url(env):
    urls = []
    assert _run(env, "--skip-public-check", receiver_http=_recording(env, urls)) == 0
    assert urls == [provision.DEFAULT_LOCAL_URL, provision.DEFAULT_LOCAL_URL]


@pytest.mark.parametrize("content", ["", "\n", "two words", "line1\nline2"])
def test_bad_provisioner_token_file_is_refused_without_echoing_it(env, capsys, content):
    env["token_file"].write_text(content)
    assert _run(env) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "provisioner token" in err
    if content.strip():
        assert content.strip().split()[0] not in err
    assert env["provider"].calls == []


def test_wrong_provisioner_token_reports_status_only(env, capsys):
    env["token_file"].write_text("whp_wrong_token_value\n")
    assert _run(env) == 1
    out, err = capsys.readouterr()
    assert "HTTP 401" in err
    assert "whp_wrong_token_value" not in out + err


def test_pending_registration_for_another_url_is_not_silently_replaced(env, capsys):
    env["provider"].receivers[URL] = lambda headers, body: _answer(500)
    assert _run(env) == 1
    capsys.readouterr()
    args = ["--provisioner-token-file", str(env["token_file"]), "--url", URL.replace("events", "other"), "--name", NAME]
    rc = asyncio.run(provision.run(provision.parse_args(args), etc_dir=env["etc"], chown=env["chown"],
                                   restart=env["live"].restart, receiver_http=env["live"].http(),
                                   provider_http=env["provider"].async_http(), retry_delay=0))
    assert rc == 2
    assert "--new-registration" in capsys.readouterr().err


def test_already_provisioned_for_another_name_is_refused(env, capsys):
    assert _run(env) == 0
    capsys.readouterr()
    args = ["--provisioner-token-file", str(env["token_file"]), "--url", URL, "--name", "other-name"]
    rc = asyncio.run(provision.run(provision.parse_args(args), etc_dir=env["etc"], chown=env["chown"],
                                   restart=env["live"].restart, receiver_http=env["live"].http(),
                                   provider_http=env["provider"].async_http(), retry_delay=0))
    assert rc == 2
    assert "already provisioned" in capsys.readouterr().err


def test_main_refuses_to_run_as_non_root(monkeypatch, capsys):
    monkeypatch.setattr(provision.os, "geteuid", lambda: 1001)
    assert provision.main(["--provisioner-token-file", "/nonexistent", "--url", URL, "--name", NAME]) == 2
    assert "root" in capsys.readouterr().err
