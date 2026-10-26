"""Pay links: a short https link that opens an Authorize.net hosted form (TAP2-82).

Authorize.net opens a hosted form only when a page POSTs the form key to it, so the connector answers with a tiny
page (``launcher_html``), not a link. A chat can't click a page. When an approved form tool has run, the executor
calls ``make_link``: it reads the post address and the hidden fields out of that page, checks each one against a
fixed list, and saves them in one small file in the pay-link folder. The pay-link service
(``paylink/paylink_app.py``) serves ``<origin>/pay/<link id>`` from that file until the form expires.

- The link id is 24 random bytes (URL-safe). The file is named by its SHA-256, so the folder never holds the link.
- Nothing from the connector's page is served as it is: the service builds its own page from the checked fields.
- A file holds a live form key for at most the form's 15 minutes; expired files are removed the next time a link
  is made, and the service refuses them anyway.

Standard library only.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The two Authorize.net tools whose answer is a hosted form page (``authorizenet-mcp`` ``tools/customers.py``).
FORM_TOOL_SUFFIXES = ("__authnet_create_payment_form", "__authnet_create_profile_form")

# Where a form page may post: Authorize.net's own hosted pages, sandbox and production, nothing else.
_HOSTS = ("https://test.authorize.net", "https://accept.authorize.net")
_PAGES = ("/payment/payment", "/customer/manage", "/customer/addPayment", "/customer/editPayment",
          "/customer/addShipping", "/customer/editShipping")
ALLOWED_POST_URLS = frozenset(host + page for host in _HOSTS for page in _PAGES)
FORM_ACTION_ORIGINS = _HOSTS

# Hidden fields a form page may carry, and what each value may look like.
_TOKEN = re.compile(r"[A-Za-z0-9+/=._-]{20,8192}")
_ID = re.compile(r"[0-9]{1,20}")
ALLOWED_FIELDS = {"token": _TOKEN, "paymentProfileId": _ID, "shippingAddressId": _ID}

LINK_ID = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24) is exactly 32 characters
DEFAULT_MINUTES = 15
SAFETY_SECONDS = 60  # the link stops a minute before Authorize.net's form does
MAX_FILE_BYTES = 32768

_FORM_RE = re.compile(r'<form method="post" action="([^"]*)">')
_INPUT_RE = re.compile(r'<input type="hidden" name="([^"]*)" value="([^"]*)">')
_LAUNCHER_LINE = re.compile(r"^\s*-\s*launcher_html:\s*(<!doctype html>.*)$", re.MULTILINE)
_MINUTES_LINE = re.compile(r"^\s*-\s*expires_in_minutes:\s*([0-9]{1,3})\s*$", re.MULTILINE)


class PayLinkError(Exception):
    """The pay link could not be made. The message is short and plain, and never holds the form key."""


@dataclass(frozen=True)
class PayLink:
    url: str
    expires_at: float  # Unix seconds

    def until_utc(self) -> str:
        return time.strftime("%H:%M UTC", time.gmtime(self.expires_at))


def is_form_tool(tool: str) -> bool:
    return any(str(tool).endswith(suffix) for suffix in FORM_TOOL_SUFFIXES)


def file_name(link_id: str) -> str:
    """The file that holds one link: the SHA-256 of the link id, so the folder alone never gives the link away."""
    return hashlib.sha256(link_id.encode("ascii")).hexdigest() + ".json"


def _tool_text(raw: str) -> str:
    """The connector's own answer text from what ``dispatch_tool`` returned (``{"result": "<text>", ...}``)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return data["result"]
    return raw if isinstance(raw, str) else json.dumps(data)


def parse_form(raw: str) -> tuple[str, dict[str, str], int]:
    """``(post_url, fields, minutes)`` from a form tool's answer, every part checked; ``PayLinkError`` otherwise.

    The answer is the connector's markdown (``- launcher_html: <!doctype html>...``) or its JSON
    (``{"launcher_html": ..., "expires_in_minutes": ...}``)."""
    text = _tool_text(raw)
    page, minutes = None, DEFAULT_MINUTES
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        page = data.get("launcher_html")
        if isinstance(data.get("expires_in_minutes"), int):
            minutes = data["expires_in_minutes"]
    else:
        match = _LAUNCHER_LINE.search(text)
        page = match.group(1).strip() if match else None
        found = _MINUTES_LINE.search(text)
        if found:
            minutes = int(found.group(1))
    if not isinstance(page, str) or not page:
        raise PayLinkError("the answer had no form page")
    if "[hidden]" in page:
        raise PayLinkError("the form key in the answer was cut by the connector's safety filter")
    forms = _FORM_RE.findall(page)
    if len(forms) != 1:
        raise PayLinkError("the form page did not have exactly one form")
    post_url = html.unescape(forms[0])
    if post_url not in ALLOWED_POST_URLS:
        raise PayLinkError("the form page posts somewhere other than Authorize.net's hosted pages")
    fields: dict[str, str] = {}
    for name, value in _INPUT_RE.findall(page):
        name, value = html.unescape(name), html.unescape(value)
        rule = ALLOWED_FIELDS.get(name)
        if rule is None or name in fields or not rule.fullmatch(value):
            raise PayLinkError(f"the form page had an unexpected field ({name[:30]!r})"
                               if rule is None else "the form page had a field in an unexpected shape")
        fields[name] = value
    if "token" not in fields:
        raise PayLinkError("the form page had no form key")
    if not 1 <= minutes <= DEFAULT_MINUTES:
        minutes = DEFAULT_MINUTES
    return post_url, fields, minutes


STALE_TMP_SECONDS = 3600


def _remove_expired(folder: Path, now: float) -> None:
    """Expired link files, and temp files a crash left behind more than an hour ago."""
    for path in folder.glob(".new-*.tmp"):
        try:
            if path.stat().st_mtime < now - STALE_TMP_SECONDS:
                path.unlink()
        except OSError:
            continue
    for path in folder.glob("*.json"):
        try:
            if json.loads(path.read_text()).get("expires_at", 0) <= now:
                path.unlink()
        except (OSError, ValueError, AttributeError):
            continue  # another process may be removing it; never fail a new link over an old one


def make_link(raw: str, *, folder: str, origin: str, now: float | None = None,
              amount: str | None = None) -> PayLink:
    """Save one pay link for a form tool's answer and return it. ``PayLinkError`` (plain reason) when it can't."""
    if not origin.startswith("https://"):
        raise PayLinkError("the pay-link address is not set up (paylink_origin)")
    post_url, fields, minutes = parse_form(raw)
    now = time.time() if now is None else now
    expires_at = now + minutes * 60 - SAFETY_SECONDS
    record = {"post_url": post_url, "fields": fields, "expires_at": expires_at}
    if amount is not None and re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", str(amount)):
        record["amount"] = str(amount)
    body = json.dumps(record, separators=(",", ":"))
    if len(body.encode()) > MAX_FILE_BYTES:
        raise PayLinkError("the form was too large to save")
    target = Path(folder)
    if not target.is_dir():
        raise PayLinkError("the pay-link folder does not exist (paylink_dir)")
    _remove_expired(target, now)
    link_id = secrets.token_urlsafe(24)
    try:
        fd, tmp = tempfile.mkstemp(dir=target, prefix=".new-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
            os.chmod(tmp, 0o640)  # the service's group reads it (the folder is setgid dppay)
            os.replace(tmp, target / file_name(link_id))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise PayLinkError(f"the pay link could not be saved ({type(exc).__name__})") from None
    return PayLink(url=f"{origin.rstrip('/')}/pay/{link_id}", expires_at=expires_at)


# ---- what the executor uses -------------------------------------------------------------------------------------

LINK_MESSAGE = ("The payment page is ready. Give the user this pay link exactly as written: {url} . It works until "
                "{until}. The customer types their card on Authorize.net's page; never ask for card details in chat.")
NO_LINK_MESSAGE = ("The form was made in Authorize.net, but the pay link could not be made ({reason}). Tell the user "
                   "plainly that there is no link to send and that an admin must check it. Nothing was charged.")
LINK_NOTE = "Pay link: {url} (works until {until})"
NO_LINK_NOTE = "No pay link: {reason}. Tell an admin."


def make_form_link(folder: str | None, origin: str | None, *, log: Any = None, clock: Any = None):
    """``form_link(row, raw) -> (reply fields, ledger note)`` for an executed form tool.

    Success: the reply carries ``pay_link`` and a message for the model, never the form key or the page. Failure
    is never silent: the reply tells the model to say so, the note (shown in the chat's outcome line) says why,
    and the gateway log gets an error."""

    def form_link(row: Any, raw: str) -> tuple[dict[str, Any], str]:
        amount = None
        try:
            args = json.loads(row.args_json)
            if isinstance(args, dict) and args.get("amount") is not None:
                amount = str(args["amount"])
        except (TypeError, ValueError):
            pass
        try:
            if not folder or not origin:
                raise PayLinkError("pay links are not set up (paylink_dir, paylink_origin)")
            link = make_link(raw, folder=folder, origin=origin, amount=amount,
                             now=None if clock is None else clock())
        except PayLinkError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - never break the executor's reply over the link
            reason = f"unexpected {type(exc).__name__}"
        else:
            until = link.until_utc()
            return ({"pay_link": link.url, "message": LINK_MESSAGE.format(url=link.url, until=until)},
                    LINK_NOTE.format(url=link.url, until=until))
        if log is not None:
            log.error("durable-park: no pay link for %s: %s", row.park_id, reason)
        return {"message": NO_LINK_MESSAGE.format(reason=reason)}, NO_LINK_NOTE.format(reason=reason)

    return form_link
