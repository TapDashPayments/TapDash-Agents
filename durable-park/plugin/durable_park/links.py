"""Links into withHuman's review queue, where a person approves or denies a request.

Standard library only: the sweep loads this file by path, without importing the plugin package.
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit

DEFAULT_APP_URL = "https://app.withhuman.ai"


def app_origin(base_url: str | None) -> str:
    """The withHuman app's address from the AAP base URL (``https://app.withhuman.ai/api/aap`` gives
    ``https://app.withhuman.ai``). Anything that is not an http(s) URL gives the hosted app."""
    parts = urlsplit(base_url or "")
    if parts.scheme in ("https", "http") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return DEFAULT_APP_URL


def approval_url(request_id: str, origin: str = DEFAULT_APP_URL) -> str:
    """The page of one request in withHuman's review queue. It only opens for a signed-in member, so it is
    safe to show in chats and alerts."""
    return f"{origin.rstrip('/')}/queue/{quote(str(request_id), safe='')}"
