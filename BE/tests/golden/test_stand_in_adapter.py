"""AC-17: every strategy trades through a stand-in exchange adapter with no change to the strategy.

The stand-in (`stand_in_adapter.py`) is a separate implementation of the port, registered for a
test venue, that talks to the same exchange emulator in its own code (test-plan.md). Each
strategy's fresh golden scenario on that venue must give the canonical records of the Bybit
recording; the Bybit adapter is never constructed during the run.
"""
from __future__ import annotations

import pytest

from tests.golden import harness
from tests.golden.harness import load_recording, record_run
from tests.golden.scenarios import SCENARIOS
from tests.golden.stand_in_adapter import STAND_IN_VENUE, StandInAdapter, stand_in_venue
from tests.golden.test_golden_runs import RECORDINGS, assert_equivalent

pytestmark = pytest.mark.golden


@pytest.fixture()
def no_bybit_adapter(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError("the Bybit adapter was constructed during a stand-in run")

    monkeypatch.setattr("app.bot_engine.exchange.bybit.adapter.BybitAdapter.__init__", refuse)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_strategy_trades_through_the_stand_in_adapter_unchanged(name, emulator, golden_db, no_bybit_adapter):
    StandInAdapter.requests.clear()
    with stand_in_venue():
        replay = record_run(SCENARIOS[name], emulator=emulator, db=golden_db, venue=STAND_IN_VENUE)

    assert_equivalent(replay, load_recording(RECORDINGS / name / "fresh.json"))
    assert any(method == "POST" for method, _ in StandInAdapter.requests), "no order went through the stand-in"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_stand_in_failure_gets_the_same_reaction_as_on_bybit(name, emulator, golden_db, tmp_path):
    from app.bot_engine.error_handling import REACTION_TABLE
    from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
    from app.core.clock import use_simulated_time

    scenario = SCENARIOS[name]
    golden_db.bind(tmp_path / "backend.db", create=True)
    running = emulator.start(tmp_path / "emulator.db")
    try:
        harness._point_backend_at(running)
        moment = harness.prepare_scenario(running, scenario)
        with use_simulated_time(moment), stand_in_venue():
            bot_id = harness._create_and_start_bot(golden_db.client, scenario, venue=STAND_IN_VENUE)
            StandInAdapter.failure = ExchangeError(ErrorClass.NETWORK, message="stand-in read timed out")
            try:
                harness._tick(bot_id)
            finally:
                StandInAdapter.failure = None
        bot = harness._bot_rows(bot_id)["bot"]
    finally:
        running.stop()
        golden_db.dispose()

    reaction = REACTION_TABLE[ErrorClass.NETWORK]
    assert bot["runtime_status"] == "retrying"
    assert (bot["last_error_type"], bot["last_error_action"]) == (reaction.error_type.value, reaction.action.value)


def test_the_stand_in_carries_the_exchange_text_as_decimals_like_the_port_declares(monkeypatch):
    """Ticker and position prices are Decimals built from the exchange text (port.py; T31), so a page
    shows "10" through the stand-in exactly as through Bybit, never "10.0"."""
    from decimal import Decimal

    rows = {
        "/v5/market/tickers": [{"symbol": "BTCUSDT", "lastPrice": "65000.50", "markPrice": "65001.00"}],
        "/v5/position/list": [{
            "symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "64000",
            "markPrice": "65001.00", "liqPrice": "", "leverage": "10",
        }],
    }
    monkeypatch.setattr(StandInAdapter, "_get", lambda self, path, **params: rows[path])
    adapter = StandInAdapter(None)

    ticker = adapter.ticker("linear", "BTCUSDT")
    (position,) = adapter.positions("linear", "BTCUSDT")

    assert (ticker.last_price, ticker.mark_price) == (Decimal("65000.50"), Decimal("65001.00"))
    assert all(isinstance(value, Decimal) for value in (ticker.last_price, ticker.mark_price))
    assert (str(position.mark_price), position.liq_price, str(position.leverage)) == ("65001.00", None, "10")
    assert isinstance(position.mark_price, Decimal) and isinstance(position.leverage, Decimal)


@pytest.mark.parametrize(("text", "expected"), [
    ("0", "0"), ("12.50", "12.50"), ("", None), (None, None), ("abc", None), ("NaN", None),
])
def test_the_stand_in_reads_a_position_price_like_the_bybit_adapter(monkeypatch, text, expected):
    """The emulator sends `liqPrice "0"` for a covered long: Bybit keeps Decimal("0"), so the page
    shows "0" either way; only an absent, empty or non-numeric text reads as None."""
    rows = {"/v5/position/list": [{
        "symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "64000",
        "markPrice": text, "liqPrice": text, "leverage": text,
    }]}
    monkeypatch.setattr(StandInAdapter, "_get", lambda self, path, **params: rows[path])

    (position,) = StandInAdapter(None).positions("linear", "BTCUSDT")

    for value in (position.mark_price, position.liq_price, position.leverage):
        assert (None if value is None else str(value)) == expected
