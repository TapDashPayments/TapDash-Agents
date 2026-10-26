"""GitHub #66: the fixed line that tells the chat how an approved run ended."""
import json
from dataclasses import replace

from durable_park.ledger import Ledger
from durable_park.outcome import MAX_LINE_CHARS, outcome_text


def _row(tmp_path, status, *, result_json=None, note=None):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    row = ledger.park(session_key="agent:main:teams:dm:1", session_id="s", tool_call_id="c", tool="mcp__wh__qb__pay",
                      args={"amount": 1}, call_key="k", server="park:qb")
    return replace(row, status=status, result_json=result_json, note=note)


def test_outcome_text_per_status(tmp_path):
    ran = _row(tmp_path, "executed", result_json=json.dumps({"result": "Payment created successfully: Id 158"}))
    assert outcome_text(ran) == (f"[durable-park] {ran.park_id} ran: mcp__wh__qb__pay. "
                                 "Result: Payment created successfully: Id 158")
    failed = _row(tmp_path, "failed", note="tool error: customer not found")
    assert outcome_text(failed) == (f"[durable-park] {failed.park_id} (mcp__wh__qb__pay) did NOT complete: "
                                    "tool error: customer not found")
    unknown = _row(tmp_path, "unknown", note="dispatch raised TimeoutError")
    assert outcome_text(unknown) == (f"[durable-park] {unknown.park_id} (mcp__wh__qb__pay): outcome UNKNOWN. "
                                     "Check it before trying again.")
    for status in ("parked", "approved", "executing", "denied", "expired", "cancelled", "allowed", "refused"):
        assert outcome_text(_row(tmp_path, status)) is None


def test_outcome_text_is_one_line_and_capped(tmp_path):
    big = "line one\n" + "x" * 5000 + "\n\tend"
    for row in (_row(tmp_path, "executed", result_json=json.dumps({"result": big})),
                _row(tmp_path, "executed", result_json=big),
                _row(tmp_path, "executed", result_json=json.dumps([1, {"a": big}])),
                _row(tmp_path, "failed", note=big)):
        text = outcome_text(row)
        assert "\n" not in text and "\t" not in text and len(text) <= MAX_LINE_CHARS


def test_outcome_text_without_a_result(tmp_path):
    row = _row(tmp_path, "executed", result_json=None)
    assert outcome_text(row) == f"[durable-park] {row.park_id} ran: mcp__wh__qb__pay."
