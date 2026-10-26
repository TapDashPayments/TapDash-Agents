"""Links into withHuman's review queue, where a person approves or denies a request.

Standard library only: the sweep loads this file by path, without importing the plugin package.
"""
from __future__ import annotations

from urllib.parse import quote

DEFAULT_APP_URL = "https://app.withhuman.ai"


def approval_url(request_id: str, origin: str = DEFAULT_APP_URL) -> str:
    """The page of one request in withHuman's review queue. It opens only for a signed-in member, and withHuman
    still checks who may decide. Send it only to approvers (never to the chat that asked: the asker must not be
    handed a way to approve their own request)."""
    return f"{origin.rstrip('/')}/queue/{quote(str(request_id), safe='')}"
