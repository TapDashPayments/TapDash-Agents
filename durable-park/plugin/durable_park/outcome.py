"""The fixed line that tells the chat how an approved run ended (GitHub #66).

The executor posts it into the chat itself, with no model in between, the moment the run ends; the wake handler
re-sends it when that did not happen (a crash in the gap, a refused post). Standard library only.
"""
from __future__ import annotations

import json
from typing import Any

from .ledger import ParkRow

MAX_RESULT_CHARS = 200
MAX_LINE_CHARS = 400

EXECUTED_TEXT = "[durable-park] {park_id} ran: {tool}."
EXECUTED_RESULT_TEXT = "[durable-park] {park_id} ran: {tool}. Result: {result}"
FAILED_TEXT = "[durable-park] {park_id} ({tool}) did NOT complete: {note}"
UNKNOWN_TEXT = "[durable-park] {park_id} ({tool}): outcome UNKNOWN. Check it before trying again."


def _flat(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _result(result_json: str | None) -> str:
    """The run's result as one short line: an MCP ``{"result": "..."}`` gives its text, other JSON its compact form,
    anything else the text itself."""
    if not result_json:
        return ""
    try:
        data = json.loads(result_json)
    except ValueError:
        return _flat(result_json, MAX_RESULT_CHARS)
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return _flat(data["result"], MAX_RESULT_CHARS)
    return _flat(json.dumps(data, ensure_ascii=False, default=str), MAX_RESULT_CHARS)


def outcome_text(row: ParkRow) -> str | None:
    """The outcome line for a run that ended (``executed``, ``failed``, ``unknown``); ``None`` otherwise."""
    if row.status == "executed":
        result = _result(row.result_json)
        text = (EXECUTED_RESULT_TEXT.format(park_id=row.park_id, tool=row.tool, result=result) if result
                else EXECUTED_TEXT.format(park_id=row.park_id, tool=row.tool))
    elif row.status == "failed":
        text = FAILED_TEXT.format(park_id=row.park_id, tool=row.tool, note=_flat(row.note or "", MAX_RESULT_CHARS))
    elif row.status == "unknown":
        text = UNKNOWN_TEXT.format(park_id=row.park_id, tool=row.tool)
    else:
        return None
    return _flat(text, MAX_LINE_CHARS)
