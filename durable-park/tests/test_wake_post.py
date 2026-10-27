import hashlib
import hmac
import http.server
import json
import threading

import pytest

from wake_post import DEFAULT_WAKE_URL, post_wake

SECRET = "wake-secret-value"


class _Hermes(http.server.BaseHTTPRequestHandler):
    """Validates X-Hub-Signature-256 exactly like Hermes's webhook platform
    (gateway/platforms/webhook.py: ``"sha256=" + hex_hmac(secret, raw_body)``, compare_digest)."""

    seen: list = []
    status_if_valid = 202

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        provided = self.headers.get("X-Hub-Signature-256", "")
        valid = hmac.compare_digest(provided.encode(), expected.encode())
        type(self).seen.append({"path": self.path, "body": body, "valid": valid,
                                "content_type": self.headers.get("Content-Type"),
                                "request_id": self.headers.get("X-Request-ID")})
        self.send_response(type(self).status_if_valid if valid else 401)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def hermes():
    _Hermes.seen = []
    _Hermes.status_if_valid = 202
    server = http.server.HTTPServer(("127.0.0.1", 0), _Hermes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/webhooks/durable-park-wake"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_post_wake_signature_accepted(hermes):
    assert post_wake("P-1A2B3C4D", SECRET, url=hermes) == 202
    (seen,) = _Hermes.seen
    assert seen["valid"] is True
    assert seen["path"] == "/webhooks/durable-park-wake"
    assert json.loads(seen["body"]) == {"type": "wake", "park_id": "P-1A2B3C4D"}
    assert seen["content_type"] == "application/json"
    # no delivery id header: Hermes then uses a fresh uuid per request, so no wake is ever deduped
    assert seen["request_id"] is None


def test_post_wake_wrong_secret_is_rejected_with_status(hermes):
    assert post_wake("P-1A2B3C4D", "not-the-secret", url=hermes) == 401
    assert _Hermes.seen[0]["valid"] is False


def test_post_wake_returns_non_2xx_status_instead_of_raising(hermes):
    _Hermes.status_if_valid = 429
    assert post_wake("P-1", SECRET, url=hermes) == 429


def test_post_wake_connection_error_raises():
    with pytest.raises(OSError):  # urllib.error.URLError is an OSError
        post_wake("P-1", SECRET, url="http://127.0.0.1:9/webhooks/durable-park-wake")


def test_post_wake_does_not_follow_redirects(hermes):
    class Redirect(_Hermes):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert post_wake("P-1", SECRET, url=f"http://127.0.0.1:{server.server_port}/x") == 307
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_default_url_is_the_local_hermes_route():
    assert DEFAULT_WAKE_URL == "http://127.0.0.1:8644/webhooks/durable-park-wake"


# ---- per-profile wake address ----------------------------------------------------------------

def test_wake_url_for_a_person():
    from wake_post import wake_url_for
    assert wake_url_for("sarah") == "http://127.0.0.1:8644/p/sarah/webhooks/durable-park-wake-sarah"


@pytest.mark.parametrize("profile", [None, "", "default", "main"])
def test_wake_url_for_the_default_profile_is_the_original_route(profile):
    from wake_post import wake_url_for
    assert wake_url_for(profile) == DEFAULT_WAKE_URL


def test_wake_url_for_takes_another_base():
    from wake_post import wake_url_for
    assert wake_url_for("tom", "http://127.0.0.1:9000/") == "http://127.0.0.1:9000/p/tom/webhooks/durable-park-wake-tom"
    assert wake_url_for(None, "http://127.0.0.1:9000/") == "http://127.0.0.1:9000/webhooks/durable-park-wake"


@pytest.mark.parametrize("bad", ["../x", "Sarah", "a" * 64, "x/y", "x y", "x.y", "-x", "sarah\n", "x?y=1", "x#y", 5])
def test_wake_url_for_a_bad_name_raises_and_builds_no_url(bad):
    from wake_post import wake_url_for
    with pytest.raises(ValueError) as caught:
        wake_url_for(bad)
    assert str(bad) not in str(caught.value) or str(bad) == "5"
