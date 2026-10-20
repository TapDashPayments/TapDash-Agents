"""Receiver app: AAP webhook -> signed wake to Hermes's local webhook route."""

import asyncio
import functools
import hashlib
import hmac
import http.server
import json
import logging
import threading
import time

import httpx
import pytest
from agentapprovalprotocol.testing import BASE_URL, SECRET, TOKEN, FakeProvider

import app as receiver_app

WAKE_SECRET = "0123456789abcdef" * 4
INSTANCE_ID = "b29a43a7-1949-4b9f-9d61-b7ae66f85d31"
REGISTRATION_ID = "6f1c4f3e-2b8a-4c1e-9d7a-0a1b2c3d4e5f"


class _Hermes(http.server.BaseHTTPRequestHandler):
    """Checks X-Hub-Signature-256 like Hermes gateway/platforms/webhook.py ``_validate_signature``
    (``"sha256=" + hex(HMAC-SHA256(secret, raw body))``, compare_digest)."""

    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        expected = "sha256=" + hmac.new(WAKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(self.headers.get("X-Hub-Signature-256", "").encode(), expected.encode())
        type(self).seen.append({"path": self.path, "body": json.loads(body), "valid": valid})
        self.send_response(202 if valid else 401)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def hermes():
    _Hermes.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Hermes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/webhooks/durable-park-wake"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def files(tmp_path):
    config = tmp_path / "receiver.json"
    config.write_text(json.dumps({
        "signing_secret": SECRET, "registration_id": REGISTRATION_ID, "instance_id": INSTANCE_ID,
        "instance_token": TOKEN, "aap_base_url": BASE_URL}))
    wake_secret = tmp_path / "wake_secret"
    wake_secret.write_text(WAKE_SECRET + "\n")
    return config, wake_secret


def run(coro_fn):
    """Run an async test with asyncio.run (no pytest-asyncio in this repo)."""
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


def _request(provider, resume):
    submission = {"tool": "qbo_create_invoice", "arguments": {"amount": 5}, "timeout": "30m"}
    if resume is not None:
        submission["context"] = {"resume": resume}
    return provider.client().create(submission, "key-" + json.dumps(resume, sort_keys=True)).id


async def _post(app, headers, body, path="/aap/events"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver") as client:
        return await client.post(path, headers=headers, content=body)


async def _settle(app):
    """Wait for the background wake tasks the SDK started after answering 204."""
    for _ in range(200):
        if not app.state.receiver._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("wake task did not finish")


def _app(files, hermes, provider):
    config, wake_secret = files
    return receiver_app.create_app(config_path=config, wake_secret_file=wake_secret, wake_url=hermes,
                                   client_factory=provider.async_client)


@run
async def test_resolved_event_wakes_hermes_with_the_park_id(files, hermes):
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1A2B3C4D", "session_key": "s"})
    app = _app(files, hermes, provider)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    response = await _post(app, headers, body)
    assert response.status_code == 204
    await _settle(app)
    assert _Hermes.seen == [{"path": "/webhooks/durable-park-wake",
                             "body": {"type": "wake", "park_id": "P-1A2B3C4D"}, "valid": True}]


@run
async def test_bad_signature_is_rejected_and_nothing_is_woken(files, hermes):
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1"})
    app = _app(files, hermes, provider)
    other = "whsec_" + "A" * 43 + "="
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID, secret=other)
    response = await _post(app, headers, body)
    assert response.status_code == 401
    await _settle(app)
    assert _Hermes.seen == []
    assert ("GET", f"/{request_id}") not in provider.calls


@run
async def test_tampered_body_is_rejected(files, hermes):
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1"})
    app = _app(files, hermes, provider)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    response = await _post(app, headers, body.replace(request_id.encode(), b"00000000-0000-0000-0000-000000000000"))
    assert response.status_code == 401
    assert _Hermes.seen == []


@run
async def test_event_for_another_instance_is_refused(files, hermes):
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1"})
    app = _app(files, hermes, provider)
    headers, body = provider.webhook(request_id, instance_id="11111111-1111-1111-1111-111111111111")
    assert (await _post(app, headers, body)).status_code == 403
    await _settle(app)
    assert _Hermes.seen == []


@run
async def test_request_without_hermes_resume_handle_is_not_woken(files, hermes):
    provider = FakeProvider(auto="approved")
    app = _app(files, hermes, provider)
    for resume in (None, {"framework": "langgraph", "thread": "t"}, {"framework": "hermes"}):
        request_id = _request(provider, resume)
        headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
        assert (await _post(app, headers, body)).status_code == 204
        await _settle(app)
    assert _Hermes.seen == []


@run
async def test_verification_challenge_is_echoed_for_our_registration_only(files, hermes):
    from agentapprovalprotocol.receiver import sign

    provider = FakeProvider()
    app = _app(files, hermes, provider)

    async def verify(registration_id):
        message = {"id": "m-1", "type": "webhook.verification", "registration_id": registration_id,
                   "instance_id": INSTANCE_ID, "created_at": "2026-10-19T00:00:00Z", "challenge": "c-123"}
        body = json.dumps(message).encode()
        stamp = int(time.time())
        headers = {"webhook-id": "m-1", "webhook-timestamp": str(stamp), "webhook-signature": sign(SECRET, "m-1", stamp, body)}
        return await _post(app, headers, body)

    ok = await verify(REGISTRATION_ID)
    assert ok.status_code == 200 and ok.json() == {"challenge": "c-123"}
    assert (await verify("not-ours")).status_code == 403


@run
async def test_only_post_on_the_events_path(files, hermes):
    app = _app(files, hermes, FakeProvider())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver") as client:
        assert (await client.get("/aap/events")).status_code == 405
        assert (await client.post("/other", content=b"{}")).status_code == 404


@pytest.mark.parametrize("content", [None, "not json", "[]", json.dumps({"signing_secret": "not-whsec"})])
@run
async def test_unprovisioned_or_broken_config_refuses_events_without_crashing(tmp_path, hermes, content):
    config = tmp_path / "receiver.json"
    if content is not None:
        config.write_text(content)
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1"})
    app = receiver_app.create_app(config_path=config, wake_secret_file=tmp_path / "wake_secret", wake_url=hermes,
                                  client_factory=provider.async_client)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    assert (await _post(app, headers, body)).status_code == 401
    assert _Hermes.seen == []


@run
async def test_pending_config_answers_verification_but_has_no_client(tmp_path, hermes, caplog):
    """Before provisioning finishes: secret + registration id only (what provision.py writes first)."""
    config = tmp_path / "receiver.json"
    config.write_text(json.dumps({"signing_secret": SECRET, "registration_id": REGISTRATION_ID,
                                  "instance_id": None, "instance_token": None}))
    app = receiver_app.create_app(config_path=config, wake_secret_file=tmp_path / "wake_secret", wake_url=hermes)
    provider = FakeProvider(auto="approved")
    headers, body = provider.webhook("00000000-0000-0000-0000-000000000001", instance_id=INSTANCE_ID)
    with caplog.at_level(logging.WARNING):
        assert (await _post(app, headers, body)).status_code == 204
        await _settle(app)
    assert _Hermes.seen == []
    assert "no client yet" in caplog.text


@run
async def test_unreadable_wake_secret_fails_the_wake_without_posting(files, hermes, caplog):
    config, wake_secret = files
    wake_secret.unlink()
    provider = FakeProvider(auto="approved")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-1"})
    app = _app(files, hermes, provider)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    with caplog.at_level(logging.INFO):
        assert (await _post(app, headers, body)).status_code == 204
        await _settle(app)
    assert _Hermes.seen == []
    assert "resuming the run" in caplog.text


@run
async def test_hermes_refusal_is_logged_with_status_and_no_secret(files, caplog):
    config, wake_secret = files
    provider = FakeProvider(auto="denied")
    request_id = _request(provider, {"framework": "hermes", "park_id": "P-9"})
    calls = []

    def refusing_post(park_id, secret, url):
        calls.append((park_id, secret, url))
        return 401

    app = receiver_app.create_app(config_path=config, wake_secret_file=wake_secret, wake_url="http://hermes.test/x",
                                  client_factory=provider.async_client, post=refusing_post)
    headers, body = provider.webhook(request_id, instance_id=INSTANCE_ID)
    with caplog.at_level(logging.DEBUG):
        assert (await _post(app, headers, body)).status_code == 204
        await _settle(app)
    assert calls == [("P-9", WAKE_SECRET, "http://hermes.test/x")]
    assert "HTTP 401" in caplog.text
    for secret in (WAKE_SECRET, SECRET, TOKEN):
        assert secret not in caplog.text


def test_client_factory_comes_from_the_instance_token_in_the_config(files):
    """R18: the receiver gets the instance credential from its own receiver.json (0600 aaprecv),
    never from root:hermes instance_token."""
    config, _ = files
    receiver = receiver_app.build_receiver(receiver_app.load_config(config), lambda handle, request: None)
    client = receiver._factory()
    assert client.base_url == BASE_URL
    assert client._headers["Authorization"] == f"Bearer {TOKEN}"
    assert receiver.instance_id == INSTANCE_ID and receiver.registration_id == REGISTRATION_ID
    assert receiver_app.build_receiver({}, lambda h, r: None)._factory is None
