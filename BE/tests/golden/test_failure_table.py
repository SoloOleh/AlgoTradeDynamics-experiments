"""Reference failure table check (AC-06, AC-07).

Every row is a real exchange failure shape (pybit / emulator client / engine exception) that the
bot distinguishes today. Each is driven through the engine's reaction entry point for a running
bot and must produce the recorded stored type, severity, action and raw code.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.golden.failure_table.rows import build_exception, react_to_failure

ROWS = json.loads((Path(__file__).parent / "failure_table" / "rows.json").read_text())
STORED_TYPES = {
    "network", "rate_limit", "exchange_unavailable", "time_sync", "authentication", "permission",
    "insufficient_funds", "invalid_order", "invalid_symbol", "order_state", "configuration",
    "risk", "internal",
}


@pytest.fixture(autouse=True)
def _schema(app):
    """Importing the app creates the SQLite schema the reaction entry point writes to."""
    yield


def test_table_covers_every_stored_type_plus_exhausted_retry_and_lookup_rows():
    types = {row["expected"]["error_type"] for row in ROWS}
    assert STORED_TYPES <= types
    assert any(row["id"] == "retry_limit_exhausted" for row in ROWS)
    lookup = [row for row in ROWS if row.get("deviation") == "AC-08"]
    assert len(lookup) == 1 and lookup[0]["id"] == "failed_order_lookup"
    for named in ("rate_limit", "insufficient_balance", "rejected_order", "duplicate_client_tag",
                  "unknown_order", "rejected_credentials", "network_timeout"):
        assert any(row["id"] == named for row in ROWS), named


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_failure_produces_the_recorded_reaction(row):
    exc = build_exception(row["exception"])
    outcome = react_to_failure(exc, retry_count=row.get("retry_count", 0))

    expected = row["expected"]
    assert outcome["error_type"] == expected["error_type"]
    assert outcome["severity"] == expected["severity"]
    assert outcome["action"] == expected["action"]
    assert outcome["code"] == expected["code"]
    assert outcome["runtime_status"] == expected["runtime_status"]


def test_temporary_network_failure_retries_then_pauses_past_the_limit():
    row = next(row for row in ROWS if row["id"] == "network_timeout")
    exc = build_exception(row["exception"])

    first = react_to_failure(exc, retry_count=0)
    assert (first["action"], first["runtime_status"]) == ("retry_backoff", "retrying")
    exhausted = react_to_failure(exc, retry_count=5)
    assert (exhausted["action"], exhausted["severity"], exhausted["runtime_status"]) == ("pause", "error", "paused")


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_row_passes_through_the_adapter_classifier_then_the_engine_table(row):
    """Layer 1: Bybit failure → error class (adapter). Layer 2: error class → reaction (engine)."""
    from app.bot_engine.error_handling import REACTION_TABLE
    from app.bot_engine.exchange.bybit.errors import classify_bybit_failure

    expected = row["expected"]
    classified = classify_bybit_failure(build_exception(row["exception"]))
    if expected["error_type"] in {"configuration", "internal"}:
        assert classified is None, "non-exchange failures stay with the engine"
        return
    assert classified is not None
    assert classified.raw_code == expected["code"]
    reaction = REACTION_TABLE[classified.error_class]
    assert reaction.error_type.value == expected["error_type"]
    if row.get("retry_count", 0) == 0:
        assert (reaction.severity.value, reaction.action.value) == (expected["severity"], expected["action"])
