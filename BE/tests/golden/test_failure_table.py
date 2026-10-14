"""Reference failure table check (AC-06, AC-07).

Every row is a real exchange failure shape (pybit / emulator client / engine exception) that the
bot distinguished before the adapter existed. Each enters where it really happens - the Bybit
adapter's client transport, an exchange answer, a placement refusal or the strategy itself -
during a worker tick of a running bot, and must store the recorded type, severity, action and raw
code on the bot and in its bot_error event.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.golden.failure_table.rows import build_exception, react_on_a_tick, react_to_failure

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
def test_failure_on_a_running_bot_produces_the_recorded_reaction(row, monkeypatch):
    """Each row enters where it really happens, during a worker tick (test-plan.md AC-06/AC-07 rows)."""
    outcome = react_on_a_tick(row, monkeypatch)

    expected = row["expected"]
    assert (outcome["last_error_type"], outcome["last_error_severity"], outcome["last_error_action"]) == (
        expected["error_type"], expected["severity"], expected["action"],
    )
    assert outcome["last_error_code"] == expected["code"]
    assert outcome["runtime_status"] == expected["runtime_status"]
    [payload] = outcome["events"]
    assert (payload["error_type"], payload["severity"], payload["action"], payload["error_code"]) == (
        expected["error_type"], expected["severity"], expected["action"], expected["code"],
    )


def test_temporary_network_failure_retries_then_pauses_past_the_limit(monkeypatch):
    row = next(row for row in ROWS if row["id"] == "network_timeout")

    first = react_on_a_tick({**row, "retry_count": 0}, monkeypatch)
    assert (first["last_error_action"], first["runtime_status"]) == ("retry_backoff", "retrying")
    exhausted = react_on_a_tick({**row, "retry_count": 5}, monkeypatch)
    assert (exhausted["last_error_action"], exhausted["last_error_severity"], exhausted["runtime_status"]) == (
        "pause", "error", "paused",
    )


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_row_passes_through_the_adapter_classifier_then_the_engine_table(row):
    """Layer 1: Bybit failure → error class (adapter). Layer 2: error class → reaction (engine)."""
    from app.bot_engine.error_handling import REACTION_TABLE
    from app.bot_engine.exchange.bybit.errors import classify_bybit_failure

    expected = row["expected"]
    if row["origin"] != "transport":
        pytest.skip("only client-transport failures pass through the classifier")
    classified = classify_bybit_failure(build_exception(row["exception"]))
    if expected["error_type"] in {"configuration", "internal"}:
        assert classified is None, "unclassified failures stay internal to the engine"
        return
    assert classified is not None
    assert classified.raw_code == expected["code"]
    reaction = REACTION_TABLE[classified.error_class]
    assert reaction.error_type.value == expected["error_type"]
    if row.get("retry_count", 0) == 0:
        assert (reaction.severity.value, reaction.action.value) == (expected["severity"], expected["action"])


def test_failed_lookup_row_keeps_the_order_active_through_the_lookup_rule():
    """AC-08 row: the classified lookup failure leaves the order active and reaches the reaction."""
    import uuid

    from app.bot_engine.exchange.bybit.errors import classify_bybit_failure
    from app.bot_engine.exchange.errors import ExchangeError
    from app.bot_engine.orders import resolve_order_state
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot
    from app.models.trading_bot_order import TradingBotOrder
    from app.models.user import User

    row = next(row for row in ROWS if row.get("deviation") == "AC-08")
    assert row["keeps_order_active"] is True
    failure = classify_bybit_failure(build_exception(row["exception"]))

    class FailingLookup:
        def lookup_order(self, market_type, symbol, client_tag):
            raise failure

    db = SessionLocal()
    try:
        user = User(email=f"table_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        bot = TradingBot(
            user_id=user.id, name="lookup row", strategy_type="grid", category="linear", symbol="BTCUSDT",
            order_qty=0.01, grid_orders_count=2, grid_step_percent=1.0, runtime_status="running",
            exchange="bybit", environment="emulator", settings={},
        )
        db.add(bot)
        db.flush()
        order = TradingBotOrder(
            bot_id=bot.id, user_id=user.id, exchange="bybit", environment="emulator", category="linear",
            symbol="BTCUSDT", side="Buy", order_type="Limit", order_role="grid_entry_2",
            order_link_id="bot-1-g1-entry-2-1", qty=0.01, price=60000.0, status="New", raw_response={},
        )
        db.add(order)
        db.commit()

        with pytest.raises(ExchangeError) as raised:
            resolve_order_state(db, bot, FailingLookup(), order)
        db.rollback()
        assert db.get(TradingBotOrder, order.id).status == "New"
        outcome = react_to_failure(raised.value)
        assert (outcome["error_type"], outcome["action"], outcome["runtime_status"]) == (
            row["expected"]["error_type"], row["expected"]["action"], row["expected"]["runtime_status"],
        )
    finally:
        db.rollback()
        db.close()
