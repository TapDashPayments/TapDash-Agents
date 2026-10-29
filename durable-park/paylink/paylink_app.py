"""durable-park pay-link service: ``GET /pay/<link id>`` -> a page that opens one Authorize.net hosted form.

Runs as the ``durable-park-paylink`` system service (user ``dppay``) on 127.0.0.1:8793; Tailscale Funnel publishes
``/pay``. The plugin writes one small file per link into the pay-link folder (``plugin/durable_park/paylink.py``);
this service only reads them.

- The page is built here from checked parts (Authorize.net address, hidden field names and value shapes); nothing
  from the connector is served as it is. No script runs: the customer presses one button, so link previews and
  mail scanners that open the link never send the form key on.
- An unknown, malformed or expired link gets the same plain "this link has expired" page.
- Every answer forbids caching, framing, referrers and indexing, and its Content-Security-Policy allows no script
  and posts only to Authorize.net. Links and form keys are never logged.

Standard library plus Starlette (the receiver's venv).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

log = logging.getLogger("durable_park.paylink")

DEFAULT_FOLDER = "/var/lib/durable-park-paylinks"
HOST = "127.0.0.1"
PORT = 8793
PATH = "/pay/{link_id}"

LINK_ID = re.compile(r"[A-Za-z0-9_-]{32}")
_HOSTS = ("https://test.authorize.net", "https://accept.authorize.net")
_PAGES = ("/payment/payment", "/customer/manage", "/customer/addPayment", "/customer/editPayment",
          "/customer/addShipping", "/customer/editShipping")
ALLOWED_POST_URLS = frozenset(host + page for host in _HOSTS for page in _PAGES)
_TOKEN = re.compile(r"[A-Za-z0-9+/=._-]{20,8192}")
_ID = re.compile(r"[0-9]{1,20}")
ALLOWED_FIELDS = {"token": _TOKEN, "paymentProfileId": _ID, "shippingAddressId": _ID}
_AMOUNT = re.compile(r"[0-9]{1,9}(\.[0-9]{1,2})?")
MAX_FILE_BYTES = 32768

STYLE = ("body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:28rem;margin:4rem auto;"
         "padding:0 1rem;text-align:center;color:#1a1a1a;background:#fff}"
         "h1{font-size:1.4rem}p{line-height:1.5}"
         "button{font-size:1.1rem;padding:.75rem 1.5rem;border:0;border-radius:.5rem;background:#1a56db;color:#fff;"
         "cursor:pointer;transition:background-color .15s ease}"
         "button:hover{background:#1d4ed8}"
         "button:focus-visible{outline:2px solid #1d4ed8;outline-offset:2px}"
         "button:active{background:#1e40af}")
_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
CSP = (f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; script-src 'none'; "
       f"form-action {' '.join(_HOSTS)}; base-uri 'none'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}



def _head(title: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>")

GONE_TITLE = "Payment link expired"
GONE_BODY = ("<h1>This payment link has expired</h1>"
             "<p>Payment links work for about 15 minutes. Please ask for a new one.</p>")


def file_name(link_id: str) -> str:
    return hashlib.sha256(link_id.encode("ascii")).hexdigest() + ".json"


def load_link(folder: str, link_id: str, now: float) -> dict[str, Any] | None:
    """The checked record for a live link, or None (unknown, malformed, expired or unreadable)."""
    if not LINK_ID.fullmatch(link_id):
        return None
    path = Path(folder) / file_name(link_id)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    expires_at, post_url, fields = record.get("expires_at"), record.get("post_url"), record.get("fields")
    if not isinstance(expires_at, (int, float)) or expires_at <= now:
        return None
    if post_url not in ALLOWED_POST_URLS or not isinstance(fields, dict) or "token" not in fields:
        return None
    for name, value in fields.items():
        rule = ALLOWED_FIELDS.get(name)
        if rule is None or not isinstance(value, str) or not rule.fullmatch(value):
            return None
    amount = record.get("amount")
    if amount is not None and (not isinstance(amount, str) or not _AMOUNT.fullmatch(amount)):
        amount = None
    return {"post_url": post_url, "fields": fields, "amount": amount, "expires_at": expires_at}


def pay_page(record: dict[str, Any]) -> str:
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    payment = record["post_url"].endswith("/payment/payment")
    title = "Secure payment" if payment else "Your saved payment details"
    if payment and record.get("amount"):
        lead = f"<p>Amount: <strong>${esc(record['amount'])}</strong></p>"
    else:
        lead = ""
    inputs = "".join(f'<input type="hidden" name="{esc(n)}" value="{esc(v)}">' for n, v in record["fields"].items())
    button = "Continue to payment" if payment else "Continue"
    return (_head(title) + f"<h1>{esc(title)}</h1>{lead}"
            "<p>You will enter your card on Authorize.net's secure page, not here.</p>"
            f'<form method="post" action="{esc(record["post_url"])}">{inputs}'
            f'<button type="submit">{button}</button></form></body></html>')


def gone_page() -> str:
    return _head(GONE_TITLE) + GONE_BODY + "</body></html>"


def create_app(*, folder: str = DEFAULT_FOLDER, clock=time.time) -> Starlette:
    async def pay(request: Request) -> HTMLResponse:
        record = load_link(folder, request.path_params["link_id"], clock())
        if record is None:
            log.info("pay link: not found or expired")
            return HTMLResponse(gone_page(), status_code=410, headers=HEADERS)
        log.info("pay link: served")
        return HTMLResponse(pay_page(record), headers=HEADERS)

    return Starlette(routes=[Route(PATH, pay, methods=["GET", "HEAD"])])


def main() -> None:  # pragma: no cover - exercised by the systemd unit
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(folder=os.environ.get("DURABLE_PARK_PAYLINK_DIR", DEFAULT_FOLDER)),
                host=HOST, port=PORT, proxy_headers=False, server_header=False, date_header=False,
                log_config=None, access_log=False)


if __name__ == "__main__":
    sys.exit(main())
