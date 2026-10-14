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
