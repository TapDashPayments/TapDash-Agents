"""bridge.whapi: the bridge's read-only lister of withHuman approval requests (org key, no network in tests)."""

import httpx
import pytest

from bridge.whapi import DEFAULT_API_BASE, DEFAULT_KEY_FILE, ListError, RequestLister

BASE = "https://wh.test/api/v1"
KEY = "whk_DISTINCTIVE-fake-key-9f3a77c1"


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "audit_key"
    path.write_text(KEY + "\n")
    return path


class Recorder:
    """A MockTransport handler that records every request it receives and answers with ``reply``."""

    def __init__(self, reply=None):
        self.requests = []
        self.reply = reply or (lambda request: httpx.Response(200, json={"requests": []}))

    def __call__(self, request):
        self.requests.append(request)
        return self.reply(request)

    @property
    def transport(self):
        return httpx.MockTransport(self)


def make(key_file, recorder, base=BASE, **kwargs):
    return RequestLister(base, str(key_file), transport=recorder.transport, **kwargs)


def code_of(excinfo):
    return excinfo.value.code


def test_defaults_name_the_withhuman_api_and_the_audit_key():
    assert DEFAULT_API_BASE == "https://app.withhuman.ai/api/v1"
    assert DEFAULT_KEY_FILE == "/etc/durable-park/audit_key"


def test_sends_get_to_requests_with_params_and_bearer_header(key_file):
    rec = Recorder()
    make(key_file, rec).list(agent_slug="hermes", q="create_payment", limit=25)
    (req,) = rec.requests
    assert req.method == "GET"
    assert req.url.scheme == "https"
    assert req.url.host == "wh.test"
    assert req.url.path == "/api/v1/requests"
    assert dict(req.url.params) == {"agent_slug": "hermes", "q": "create_payment", "limit": "25"}
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["accept"] == "application/json"


def test_default_limit_is_50(key_file):
    rec = Recorder()
    make(key_file, rec).list(agent_slug="a", q="t")
    assert rec.requests[0].url.params["limit"] == "50"


@pytest.mark.parametrize("asked,sent", [(0, "1"), (-5, "1"), (1, "1"), (200, "200"), (5000, "200")])
def test_limit_is_clamped_to_1_through_200(key_file, asked, sent):
    rec = Recorder()
    make(key_file, rec).list(agent_slug="a", q="t", limit=asked)
    assert rec.requests[0].url.params["limit"] == sent


def test_trailing_slash_on_base_url_is_stripped(key_file):
    rec = Recorder()
    make(key_file, rec, base=BASE + "/").list(agent_slug="a", q="t")
    assert rec.requests[0].url.path == "/api/v1/requests"


def test_returns_the_requests_list_as_is(key_file):
    items = [{"id": "r1", "tool": "x"}, {"id": "r2"}, "odd", 7]
    rec = Recorder(lambda request: httpx.Response(200, json={"requests": items, "next": "ignored"}))
    assert make(key_file, rec).list(agent_slug="a", q="t") == items


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_non_200_raises_http_status(key_file, status):
    rec = Recorder(lambda request: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(ListError) as exc:
        make(key_file, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == f"http-{status}"


def test_204_is_not_accepted_as_success(key_file):
    rec = Recorder(lambda request: httpx.Response(204))
    with pytest.raises(ListError) as exc:
        make(key_file, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == "http-204"


def test_redirect_is_not_followed(key_file):
    def reply(request):
        if request.url.host == "wh.test":
            return httpx.Response(302, headers={"location": "https://evil.test/steal"})
        return httpx.Response(200, json={"requests": []})
    rec = Recorder(reply)
    with pytest.raises(ListError) as exc:
        make(key_file, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == "http-302"
    assert [r.url.host for r in rec.requests] == ["wh.test"]


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not json{"),
    httpx.Response(200, content=b""),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"items": []}),
    httpx.Response(200, json={"requests": {"id": "r1"}}),
    httpx.Response(200, json={"requests": None}),
    httpx.Response(200, json=[{"id": "r1"}]),
    httpx.Response(200, json="requests"),
])
def test_malformed_body_raises_shape(key_file, response):
    rec = Recorder(lambda request: response)
    with pytest.raises(ListError) as exc:
        make(key_file, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == "shape"


@pytest.mark.parametrize("error", [
    httpx.ConnectError("boom"),
    httpx.ReadTimeout("slow"),
    httpx.RemoteProtocolError("bad"),
])
def test_transport_errors_raise_transport(key_file, error):
    def reply(request):
        raise error
    with pytest.raises(ListError) as exc:
        make(key_file, Recorder(reply)).list(agent_slug="a", q="t")
    assert code_of(exc) == "transport"


@pytest.mark.parametrize("content", [None, "", "  \n\t "])
def test_missing_or_empty_key_file_raises_no_key_and_sends_nothing(tmp_path, content):
    path = tmp_path / "audit_key"
    if content is not None:
        path.write_text(content)
    rec = Recorder()
    with pytest.raises(ListError) as exc:
        make(path, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == "no-key"
    assert rec.requests == []


def test_unreadable_key_file_raises_no_key(tmp_path):
    rec = Recorder()
    with pytest.raises(ListError) as exc:
        make(tmp_path, rec).list(agent_slug="a", q="t")  # a directory cannot be read as a file
    assert code_of(exc) == "no-key"
    assert rec.requests == []


def test_key_file_that_is_not_text_raises_no_key(tmp_path):
    path = tmp_path / "audit_key"
    path.write_bytes(b"\xff\xfe\x00\x80")
    rec = Recorder()
    with pytest.raises(ListError) as exc:
        make(path, rec).list(agent_slug="a", q="t")
    assert code_of(exc) == "no-key"
    assert rec.requests == []


def test_key_is_reread_on_every_call_so_rotation_takes_effect(key_file):
    rec = Recorder()
    lister = make(key_file, rec)
    lister.list(agent_slug="a", q="t")
    key_file.write_text("whk_ROTATED-key-2\n")
    lister.list(agent_slug="a", q="t")
    assert [r.headers["authorization"] for r in rec.requests] == [f"Bearer {KEY}", "Bearer whk_ROTATED-key-2"]


def test_key_file_is_not_read_at_construction(tmp_path):
    path = tmp_path / "later"
    rec = Recorder()
    lister = make(path, rec)  # does not exist yet: constructing must not fail
    path.write_text(KEY)
    lister.list(agent_slug="a", q="t")
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_expired_deadline_raises_deadline_and_sends_nothing(key_file):
    rec = Recorder()
    clock = FakeClock(1000.0)
    lister = make(key_file, rec, clock=clock)
    with pytest.raises(ListError) as exc:
        lister.list(agent_slug="a", q="t", deadline=1000.0)  # exactly now: nothing left
    assert code_of(exc) == "deadline"
    with pytest.raises(ListError) as exc:
        lister.list(agent_slug="a", q="t", deadline=990.0)
    assert code_of(exc) == "deadline"
    assert rec.requests == []


def test_timeout_is_the_per_call_timeout_when_the_deadline_is_far(key_file):
    rec = Recorder()
    clock = FakeClock(1000.0)
    make(key_file, rec, per_call_timeout=3.0, clock=clock).list(agent_slug="a", q="t", deadline=1100.0)
    assert set(rec.requests[0].extensions["timeout"].values()) == {3.0}


def test_timeout_is_the_remaining_time_when_the_deadline_is_near(key_file):
    rec = Recorder()
    clock = FakeClock(1000.0)
    make(key_file, rec, per_call_timeout=3.0, clock=clock).list(agent_slug="a", q="t", deadline=1001.25)
    assert set(rec.requests[0].extensions["timeout"].values()) == {1.25}


def test_timeout_without_a_deadline_is_the_per_call_timeout(key_file):
    rec = Recorder()
    make(key_file, rec, per_call_timeout=2.5).list(agent_slug="a", q="t")
    assert set(rec.requests[0].extensions["timeout"].values()) == {2.5}


@pytest.mark.parametrize("base", [
    "http://wh.test/api/v1",
    "ftp://wh.test",
    "wh.test/api/v1",
    "",
    "HTTP://wh.test",
])
def test_non_https_base_url_is_rejected_at_construction(key_file, base):
    with pytest.raises(ListError) as exc:
        RequestLister(base, str(key_file))
    assert code_of(exc) == "bad-base-url"


def test_key_never_appears_in_errors_or_repr(key_file):
    rec = Recorder(lambda request: httpx.Response(403, json={"error": request.headers["authorization"]}))
    lister = make(key_file, rec)
    with pytest.raises(ListError) as exc:
        lister.list(agent_slug="a", q="t")
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    assert KEY not in repr(lister)
    assert KEY not in str(lister)
    assert BASE in repr(lister)
    assert str(key_file) in repr(lister)


def test_transport_error_message_does_not_leak_the_key(key_file):
    def reply(request):
        raise httpx.ConnectError(f"failed with {request.headers['authorization']}")
    with pytest.raises(ListError) as exc:
        make(key_file, Recorder(reply)).list(agent_slug="a", q="t")
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    assert exc.value.__cause__ is None or KEY not in str(exc.value.__cause__)


def test_list_error_str_is_its_code():
    err = ListError("no-key")
    assert err.code == "no-key"
    assert str(err) == "no-key"
