"""Pay links (TAP2-82): the plugin side (paylink.py, the executor and the outcome line) and the service."""

import hashlib
import json
import os
import re
import stat
from types import SimpleNamespace

import httpx
import pytest

import paylink_app
from durable_park import paylink
from durable_park.ledger import Ledger
from durable_park.outcome import outcome_text
from durable_park.paylink import PayLinkError, file_name, make_form_link, make_link, parse_form

ORIGIN = "https://proto-agents.example.ts.net"
KEY = "66oTeT0c6/r39pwCcfkNpcjF+I01JUvaULhkE3qWzfz=.5KP3u95bQpv"
FORM_TOOL = "mcp__wh__authnet__authnet_create_payment_form"
NOW = 1_800_000_000.0


def launcher(action="https://test.authorize.net/payment/payment", fields=(("token", KEY),)):
    inputs = "".join(f'<input type="hidden" name="{n}" value="{v}">' for n, v in fields)
    return (f'<!doctype html><meta charset="utf-8"><title>Authorize.net</title><form method="post" action="{action}">'
            f"{inputs}<noscript><button>Continue</button></noscript></form><script>document.forms[0].submit()</script>")


def markdown(page=None, minutes=15):
    """The connector's markdown answer, as authorizenet-mcp 4dd4278 renders it."""
    text = (f"- post_url: https://test.authorize.net/payment/payment\n- hosted_form_key: {KEY}\n- form_field: token\n"
            f"- extra_fields: (none)\n- expires_in_minutes: {minutes}\n- launcher_html: {page or launcher()}\n"
            "- how_to_use: Open launcher_html in a browser (or send it to the customer): it opens Authorize.net's "
            "own form, where the customer types the card. It works for 15 minutes.")
    return json.dumps({"result": text, "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "authorizenet"}}})


def as_json(page=None):
    body = {"post_url": "https://test.authorize.net/payment/payment", "hosted_form_key": KEY, "form_field": "token",
            "extra_fields": {}, "expires_in_minutes": 15, "launcher_html": page or launcher()}
    return json.dumps({"result": json.dumps(body, indent=2)})


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "paylinks"
    d.mkdir()
    return d


def link_id_of(url):
    match = re.fullmatch(re.escape(ORIGIN) + r"/pay/([A-Za-z0-9_-]{32})", url)
    assert match, url
    return match.group(1)


# ---- reading the form out of the connector's answer --------------------------------------------------------------


def test_parse_markdown_answer():
    assert parse_form(markdown()) == ("https://test.authorize.net/payment/payment", {"token": KEY}, 15)


def test_parse_json_answer():
    assert parse_form(as_json()) == ("https://test.authorize.net/payment/payment", {"token": KEY}, 15)


def test_parse_profile_form_with_extra_field():
    page = launcher("https://test.authorize.net/customer/editPayment", (("token", KEY), ("paymentProfileId", "123")))
    post_url, fields, _ = parse_form(markdown(page))
    assert post_url == "https://test.authorize.net/customer/editPayment"
    assert fields == {"token": KEY, "paymentProfileId": "123"}


@pytest.mark.parametrize("page, reason", [
    (launcher(fields=(("token", "abc.[hidden]" + "x" * 30),)), "safety filter"),
    (launcher(action="https://evil.example/payment/payment"), "other than Authorize.net"),
    (launcher(action="https://test.authorize.net/payment/payment?x=1"), "other than Authorize.net"),
    (launcher(fields=(("token", KEY), ("redirect", "https://evil.example"))), "unexpected field"),
    (launcher(fields=(("token", "short"),)), "unexpected shape"),
    (launcher(fields=(("token", KEY + "&quot;&gt;&lt;script&gt;"),)), "unexpected shape"),
    (launcher(fields=(("paymentProfileId", "1"),)), "no form key"),
    (launcher(fields=(("token", KEY), ("token", KEY))), "unexpected shape"),
    (launcher() + launcher(), "exactly one form"),
])
def test_parse_refuses_anything_unexpected(page, reason):
    with pytest.raises(PayLinkError, match=re.escape(reason)):
        parse_form(markdown(page))


def test_parse_refuses_an_answer_without_a_page():
    with pytest.raises(PayLinkError, match="no form page"):
        parse_form(json.dumps({"result": "Payment created"}))


def test_minutes_outside_1_to_15_fall_back_to_15():
    assert parse_form(markdown(minutes=99))[2] == 15


# ---- saving a link ------------------------------------------------------------------------------------------------


def test_make_link_saves_one_private_file_named_by_hash(folder):
    link = make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW, amount="5.00")
    link_id = link_id_of(link.url)
    files = list(folder.iterdir())
    assert [f.name for f in files] == [hashlib.sha256(link_id.encode()).hexdigest() + ".json"]
    assert link_id not in files[0].read_text()  # the folder alone never gives the link away
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o640
    record = json.loads(files[0].read_text())
    assert record == {"post_url": "https://test.authorize.net/payment/payment", "fields": {"token": KEY},
                      "expires_at": NOW + 15 * 60 - 60, "amount": "5.00"}
    assert link.expires_at == NOW + 14 * 60 and link.until_utc().endswith("UTC")


def test_each_link_is_new_and_unguessable(folder):
    urls = {make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW).url for _ in range(5)}
    assert len(urls) == 5


def test_a_strange_amount_is_left_out(folder):
    make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW, amount="5<b>")
    assert "amount" not in json.loads(next(folder.iterdir()).read_text())


def test_expired_links_are_removed_when_a_new_one_is_made(folder):
    old = make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW)
    make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW + 15 * 60)
    assert not (folder / file_name(link_id_of(old.url))).exists() and len(list(folder.iterdir())) == 1


@pytest.mark.parametrize("origin, made, reason", [
    ("http://plain.example", True, "not set up"), ("", True, "not set up"), (ORIGIN, False, "does not exist"),
])
def test_make_link_says_why_it_cannot(tmp_path, origin, made, reason):
    target = tmp_path / "paylinks"
    if made:
        target.mkdir()
    with pytest.raises(PayLinkError, match=reason):
        make_link(markdown(), folder=str(target), origin=origin, now=NOW)


# ---- what the executor gets ---------------------------------------------------------------------------------------


def row(args='{"amount": "5.00", "description": "Test"}'):
    return SimpleNamespace(park_id="P-1A2B3C4D", args_json=args, tool=FORM_TOOL)


class Log:
    def __init__(self):
        self.errors = []

    def error(self, msg, *args):
        self.errors.append(msg % args)


def test_form_link_success_gives_the_model_the_link_and_never_the_key(folder):
    fields, note = make_form_link(str(folder), ORIGIN, clock=lambda: NOW)(row(), markdown())
    assert set(fields) == {"pay_link", "message"}
    assert fields["pay_link"] in fields["message"] and fields["pay_link"] in note
    assert KEY not in json.dumps(fields) and KEY not in note
    assert note.startswith("Pay link: " + ORIGIN + "/pay/")
    assert json.loads(next(folder.iterdir()).read_text())["amount"] == "5.00"


@pytest.mark.parametrize("folder_setting, origin, reason", [
    (None, ORIGIN, "not set up"), ("x", None, "not set up"),
])
def test_form_link_failure_is_loud(folder_setting, origin, reason):
    log = Log()
    fields, note = make_form_link(folder_setting, origin, log=log)(row(), markdown())
    assert set(fields) == {"message"} and reason in fields["message"] and "admin" in fields["message"]
    assert note.startswith("No pay link: ") and reason in note and "admin" in note
    assert log.errors and reason in log.errors[0] and KEY not in log.errors[0]


def test_form_link_failure_on_a_cut_key(folder):
    page = launcher(fields=(("token", "abc.[hidden]" + "x" * 30),))
    fields, note = make_form_link(str(folder), ORIGIN, log=Log())(row(), markdown(page))
    assert "safety filter" in note and list(folder.iterdir()) == []


# ---- the outcome line -----------------------------------------------------------------------------------------------


def ended(**kw):
    base = dict(park_id="P-1A2B3C4D", tool=FORM_TOOL, status="executed", result_json=markdown(), note=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_outcome_line_shows_the_pay_link_never_the_key():
    text = outcome_text(ended(note="Pay link: https://x.example/pay/abc (works until 17:21 UTC)"))
    assert text == ("[durable-park] P-1A2B3C4D made an Authorize.net form (mcp__wh__authnet__authnet_create_payment_form)."
                    " Pay link: https://x.example/pay/abc (works until 17:21 UTC)")
    assert KEY not in outcome_text(ended(note=None))


def test_outcome_line_for_other_tools_is_unchanged():
    text = outcome_text(ended(tool="mcp__wh__quickbooks__create_payment", result_json='{"result": "ok"}'))
    assert text == "[durable-park] P-1A2B3C4D ran: mcp__wh__quickbooks__create_payment. Result: ok"


# ---- the service ----------------------------------------------------------------------------------------------------


async def get(app, path, method="GET"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path)


def saved(folder, now=NOW, **kw):
    link = make_link(markdown(), folder=str(folder), origin=ORIGIN, now=now, **kw)
    return link_id_of(link.url)


@pytest.mark.anyio
async def test_service_serves_a_page_that_posts_the_key_to_authorize_net(folder):
    link_id = saved(folder, amount="5.00")
    answer = await get(paylink_app.create_app(folder=str(folder), clock=lambda: NOW + 60), f"/pay/{link_id}")
    assert answer.status_code == 200
    page = answer.text
    assert '<form method="post" action="https://test.authorize.net/payment/payment">' in page
    assert f'<input type="hidden" name="token" value="{KEY}">' in page
    assert "<script" not in page and "Amount: <strong>$5.00</strong>" in page
    assert "Continue to payment" in page
    headers = answer.headers
    assert "script-src 'none'" in headers["content-security-policy"]
    assert "form-action https://test.authorize.net https://accept.authorize.net" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
    assert headers["x-robots-tag"].startswith("noindex") and headers["x-content-type-options"] == "nosniff"


def test_the_style_hash_in_the_policy_matches_the_style():
    import base64

    digest = base64.b64encode(hashlib.sha256(paylink_app.STYLE.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in paylink_app.CSP and f"<style>{paylink_app.STYLE}</style>" in paylink_app.gone_page()


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/pay/" + "A" * 32, "/pay/short", "/pay/" + "A" * 31 + "!"])
async def test_service_unknown_or_malformed_links_are_gone(folder, path):
    saved(folder)
    answer = await get(paylink_app.create_app(folder=str(folder), clock=lambda: NOW), path)
    assert answer.status_code in (404, 410)
    if answer.status_code == 410:
        assert "has expired" in answer.text and KEY not in answer.text


@pytest.mark.anyio
async def test_service_expired_link_is_gone(folder):
    link_id = saved(folder)
    answer = await get(paylink_app.create_app(folder=str(folder), clock=lambda: NOW + 14 * 60), f"/pay/{link_id}")
    assert answer.status_code == 410 and KEY not in answer.text


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"post_url": "https://evil.example/payment/payment"},
    {"fields": {"token": KEY, "next": "x"}},
    {"fields": {"token": "<b>"}},
    {"fields": {}},
    {"expires_at": "soon"},
])
async def test_service_refuses_a_tampered_file(folder, change):
    link_id = saved(folder)
    path = folder / file_name(link_id)
    record = json.loads(path.read_text())
    record.update(change)
    path.write_text(json.dumps(record))
    answer = await get(paylink_app.create_app(folder=str(folder), clock=lambda: NOW), f"/pay/{link_id}")
    assert answer.status_code == 410


@pytest.mark.anyio
async def test_service_answers_head_and_refuses_post(folder):
    link_id = saved(folder)
    app = paylink_app.create_app(folder=str(folder), clock=lambda: NOW)
    assert (await get(app, f"/pay/{link_id}", "HEAD")).status_code == 200
    assert (await get(app, f"/pay/{link_id}", "POST")).status_code == 405


def test_plugin_and_service_check_the_same_things():
    assert paylink.ALLOWED_POST_URLS == paylink_app.ALLOWED_POST_URLS
    assert {k: v.pattern for k, v in paylink.ALLOWED_FIELDS.items()} == \
        {k: v.pattern for k, v in paylink_app.ALLOWED_FIELDS.items()}
    assert paylink.LINK_ID.pattern == paylink_app.LINK_ID.pattern
    assert paylink.file_name("x" * 32) == paylink_app.file_name("x" * 32)


def test_stale_temp_files_are_removed_but_fresh_ones_kept(folder):
    stale, fresh = folder / ".new-old.tmp", folder / ".new-now.tmp"
    stale.write_text("x")
    fresh.write_text("x")
    os.utime(stale, (NOW - 7200, NOW - 7200))
    os.utime(fresh, (NOW - 10, NOW - 10))
    make_link(markdown(), folder=str(folder), origin=ORIGIN, now=NOW)
    assert not stale.exists() and fresh.exists()
