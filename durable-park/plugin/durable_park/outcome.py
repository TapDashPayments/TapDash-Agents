"""The fixed line that tells the chat how an approved run ended (GitHub #66).

The executor posts it into the chat itself, with no model in between, the moment the run ends; the wake handler
re-sends it when that did not happen (a crash in the gap, a refused post). Standard library only.
"""
from __future__ import annotations

import json
from typing import Any

from .ledger import ParkRow
from .paylink import is_form_tool

MAX_RESULT_CHARS = 200
MAX_LINE_CHARS = 400

EXECUTED_TEXT = "[durable-park] {park_id} ran: {tool}."
EXECUTED_RESULT_TEXT = "[durable-park] {park_id} ran: {tool}. Result: {result}"
FAILED_TEXT = "[durable-park] {park_id} ({tool}) did NOT complete: {note}"
UNKNOWN_TEXT = "[durable-park] {park_id} ({tool}): outcome UNKNOWN. Check it before trying again."
# An Authorize.net form tool: never its raw answer (it holds the form key), only the pay link note (paylink.py).
FORM_TEXT = "[durable-park] {park_id} made an Authorize.net form ({tool}). {note}"


def _flat(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# Fields worth showing from a tool's JSON answer, in order, with the word shown for each (the rest is left out so
# the line stays short; nothing here is an argument the agent sent, only what the server answered).
_KEY_FIELDS = (("Id", "id"), ("id", "id"), ("DocNumber", "number"), ("transactionId", "transaction"),
               ("transId", "transaction"), ("TotalAmt", "amount"), ("Amount", "amount"), ("amount", "amount"),
               ("status", "status"))
_MAX_KEY_FIELDS = 4


def _key_fields(data: Any) -> str:
    """``"id 159, amount 5"`` from the top level of a JSON object; empty when it has none of ``_KEY_FIELDS``."""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for key, word in _KEY_FIELDS:
        value = data.get(key)
        if word in seen or value is None or isinstance(value, (dict, list)):
            continue
        parts.append(f"{word} {value}")
        seen.add(word)
        if len(parts) == _MAX_KEY_FIELDS:
            break
    return ", ".join(parts)


def _summarise_text(text: str) -> str:
    """An MCP answer like ``"Payment created successfully:\n{...json...}"`` becomes
    ``"Payment created successfully (id 159, amount 5)"``; anything else stays the text itself."""
    head, sep, rest = text.strip().partition("\n")
    if sep:
        try:
            fields = _key_fields(json.loads(rest))
        except ValueError:
            fields = ""
        if fields:
            return f"{head.strip().rstrip(':').strip()} ({fields})"
    return text


def _result(result_json: str | None) -> str:
    """The run's result as one short line: the key fields of a JSON answer when it has them, else its text."""
    if not result_json:
        return ""
    try:
        data = json.loads(result_json)
    except ValueError:
        return _flat(result_json, MAX_RESULT_CHARS)
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return _flat(_summarise_text(data["result"]), MAX_RESULT_CHARS)
    fields = _key_fields(data)
    if fields:
        return _flat(fields, MAX_RESULT_CHARS)
    return _flat(json.dumps(data, ensure_ascii=False, default=str), MAX_RESULT_CHARS)


def outcome_text(row: ParkRow) -> str | None:
    """The outcome line for a run that ended (``executed``, ``failed``, ``unknown``); ``None`` otherwise."""
    if row.status == "executed" and is_form_tool(row.tool):
        text = FORM_TEXT.format(park_id=row.park_id, tool=row.tool, note=row.note or "No pay link was recorded.")
    elif row.status == "executed":
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
