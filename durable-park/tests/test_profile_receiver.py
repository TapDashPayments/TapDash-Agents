"""Receiver: a wake goes to the Hermes profile that made the call (its session key says which)."""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from agentapprovalprotocol.testing import FakeProvider

import app as receiver_app
import wake_post
from test_receiver_app import INSTANCE_ID, WAKE_SECRET, _Hermes, _app, _post, _request, _settle, files, hermes, run  # noqa: F401

DEFAULT_URL = "http://hermes.test/webhooks/durable-park-wake"
BASE = "http://hermes.test:9999"
REQUEST = SimpleNamespace(id="req-1", status="approved")


@pytest.fixture
def wake_secret(tmp_path):
    path = tmp_path / "wake_secret"
    path.write_text(WAKE_SECRET + "\n")
    return str(path)


def run_resume(wake_secret, session_key, **kw):
    calls = []

    def post(pid, secret, url):
        calls.append((pid, url))
        return 202

    resume = receiver_app.make_resumer(wake_secret, DEFAULT_URL, post, wake_base=BASE)
    handle = {"framework": "hermes", "park_id": "P-7"}
    if session_key is not None:
        handle["session_key"] = session_key
    asyncio.run(resume(handle, REQUEST))
    return calls


def test_receiver_posts_wake_to_the_row_profile(wake_secret):
    assert run_resume(wake_secret, "agent:sarah:teams:dm:x") == [
        ("P-7", f"{BASE}/p/sarah/webhooks/durable-park-wake-sarah")]


@pytest.mark.parametrize("session_key", ["agent:main:teams:dm:x", "agent:main:telegram:dm:1", None, "",
                                         "no-colons-at-all", "agent", "agent:", 42,
                                         "agent:default:teams:dm:x"])
def test_default_profile_and_malformed_keys_use_the_wake_url_argument(wake_secret, session_key):
    assert run_resume(wake_secret, session_key) == [("P-7", DEFAULT_URL)]


@pytest.mark.parametrize("session_key", ["agent:Bad Name:teams:dm:x", "agent:../evil:teams:dm:x",
                                         "agent:" + "a" * 64 + ":teams:dm:x",
                                         "agent:sarah\n:teams:dm:x"])
def test_a_bad_profile_name_in_the_session_key_posts_nothing(wake_secret, caplog, session_key):
    caplog.set_level(logging.INFO)
    assert run_resume(wake_secret, session_key) == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and any("P-7" in w for w in warnings)
    assert all(session_key not in w for w in warnings)  # the park id, never the raw session key


def test_the_wake_secret_is_not_logged_for_a_per_profile_post(wake_secret, caplog):
    caplog.set_level(logging.DEBUG)
    run_resume(wake_secret, "agent:sarah:teams:dm:x")
    assert WAKE_SECRET not in caplog.text


@run
async def test_create_app_takes_a_wake_base_for_other_profiles(files, hermes):
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1A2B3C4D",
                                     "session_key": "agent:sarah:teams:dm:x"})
    config, wake_secret_file = files
    base = hermes.rsplit("/webhooks/", 1)[0]
    app = receiver_app.create_app(config_path=config, wake_secret_file=wake_secret_file,
                                  wake_url="http://127.0.0.1:1/never-used", wake_base=base,
                                  client_factory=provider.async_client)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    assert (await _post(app, headers, body)).status_code == 204
    await _settle(app)
    assert _Hermes.seen == [{"path": "/p/sarah/webhooks/durable-park-wake-sarah",
                             "body": {"type": "wake", "park_id": "P-1A2B3C4D"}, "valid": True}]


def test_wake_base_defaults_to_the_local_hermes():
    assert receiver_app.DEFAULT_WAKE_BASE == wake_post.DEFAULT_BASE_URL
