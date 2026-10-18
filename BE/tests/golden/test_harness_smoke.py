"""Smoke test for the golden-run harness: one short scenario recorded twice is canonically equal."""
import pytest

from tests.golden.canonical import canonicalise
from tests.golden.harness import Scenario, record_run

pytestmark = pytest.mark.golden

SMOKE = Scenario(
    name="smoke-dca",
    strategy_type="dca",
    symbol="SOLUSDT",
    start_price=100.0,
    bot={"order_qty": 1.0, "grid_orders_count": 2, "grid_step_percent": 2.0},
    settings={"dca_volume_multiplier": 1.5, "dca_step_multiplier": 1.0, "take_profit_percent": 1.0},
    prices=[100.0, 97.5, 96.0, 99.5],
)


def test_same_scenario_recorded_twice_is_canonically_equal(emulator, golden_db):
    first = record_run(SMOKE, emulator=emulator, db=golden_db)
    second = record_run(SMOKE, emulator=emulator, db=golden_db)

    first_canonical = canonicalise(first)
    second_canonical = canonicalise(second)

    # The scenario must actually trade, or equality proves nothing.
    assert any(item["op"] == "place" for item in first_canonical["orders"])
    assert any(event["event_type"] == "dca_entry_filled" for event in first_canonical["events"])
    assert first_canonical == second_canonical


def test_recording_carries_commit_and_wall_clock_timings(emulator, golden_db):
    recording = record_run(SMOKE, emulator=emulator, db=golden_db)

    assert recording["source_commit"]
    assert recording["requests"], "requests at the EmulatorHTTP boundary are recorded"
    assert all(item["duration_s"] >= 0 for item in recording["requests"])
    assert recording["timings"]["fill_reaction_s"], "a fill → reaction latency is measured"


def test_wording_and_read_requests_are_not_compared(emulator, golden_db):
    recording = record_run(SMOKE, emulator=emulator, db=golden_db)
    reworded = dict(recording)
    reworded["events"] = [dict(event, message="different wording") for event in recording["events"]]
    reworded["requests"] = [
        item for item in recording["requests"] if item["method"] != "GET"
    ]

    assert canonicalise(reworded) == canonicalise(recording)


def test_resume_from_mid_cycle_snapshot_matches_the_tail_of_the_full_run(emulator, golden_db, tmp_path):
    snapshot = tmp_path / "mid-cycle"
    full = record_run(SMOKE, emulator=emulator, db=golden_db, snapshot_after_step=1, snapshot_dir=snapshot)
    resumed = record_run(SMOKE, emulator=emulator, db=golden_db, resume_from=snapshot)

    assert resumed["resumed_from_step"] == 2
    assert (snapshot / "rows.json").exists() and (snapshot / "emulator.db.gz").exists()
    tail = canonicalise(resumed, from_step=2)
    assert tail["orders"], "the resumed tail must trade, or equality proves nothing"
    assert tail == canonicalise(full, from_step=2)
