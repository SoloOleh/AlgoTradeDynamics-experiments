"""Golden-run equivalence: 8 recordings (4 strategies × {fresh, mid-cycle}) replayed as 12.

Each mid-cycle recording is replayed from two starting states (AC-04): the snapshot the
pre-refactor version left, and the same state captured from the previous migration step
(`mid_cycle_previous_step/`, which falls back to the pre-refactor snapshot until a migration
step captures its own).

Recording (one-time, on the pre-refactor commit — ADR-0007):
    GOLDEN_RECORD=1 pytest tests/golden/test_golden_runs.py -k record
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from pathlib import Path

import pytest

from tests.golden.canonical import canonicalise
from tests.golden.harness import REPO_ROOT, load_recording, record_run, save_recording
from tests.golden.scenarios import MID_CYCLE_STEP, SCENARIOS

pytestmark = pytest.mark.golden

RECORDINGS = Path(__file__).parent / "recordings"
BASELINE = RECORDINGS / "baseline.json"
LATENCY_BUDGET_S = 0.5
OVERHEAD_BUDGET_S = 0.005
STARTING_STATES = ("pre_refactor", "previous_step")


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _mid_cycle_dir(name: str, starting_state: str) -> Path:
    path = RECORDINGS / name / f"mid_cycle_{starting_state}"
    if starting_state == "previous_step" and not (path / "rows.json").exists():
        return RECORDINGS / name / "mid_cycle_pre_refactor"
    return path


def duplicate_places(orders: list[dict]) -> list[str]:
    counts = Counter(item["order_link_id"] for item in orders if item["op"] == "place")
    return sorted(tag for tag, count in counts.items() if count > 1)


def unintended_cancels(replayed: list[dict], recorded: list[dict]) -> list[str]:
    expected = Counter(item["order_link_id"] for item in recorded if item["op"] == "cancel")
    actual = Counter(item["order_link_id"] for item in replayed if item["op"] == "cancel")
    return sorted((actual - expected).elements())


def assert_equivalent(replay: dict, recording: dict, *, from_step: int | None = None) -> None:
    replayed = canonicalise(replay, from_step=from_step)
    recorded = canonicalise(recording, from_step=from_step)
    duplicates = duplicate_places(replayed["orders"])
    assert not duplicates, f"deploy safety: duplicate orders placed for client tags {duplicates}"
    cancels = unintended_cancels(replayed["orders"], recorded["orders"])
    assert not cancels, f"deploy safety: unintended cancellations of {cancels}"
    assert replayed["orders"] == recorded["orders"]
    assert replayed["events"] == recorded["events"]
    assert replayed["state"] == recorded["state"]


def assert_within_baseline(replay: dict, name: str) -> None:
    baseline = json.loads(BASELINE.read_text())[name]
    latency = p95(replay["timings"]["fill_reaction_s"])
    assert latency <= baseline["fill_reaction_p95_s"] + LATENCY_BUDGET_S, (
        f"reaction latency p95 {latency:.4f}s > baseline {baseline['fill_reaction_p95_s']:.4f}s + {LATENCY_BUDGET_S}s"
    )
    overhead = p95(replay["timings"]["request_durations_s"])
    assert overhead <= baseline["request_p95_s"] + OVERHEAD_BUDGET_S, (
        f"request p95 {overhead * 1000:.2f}ms > baseline {baseline['request_p95_s'] * 1000:.2f}ms + 5ms"
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_fresh_run_is_equivalent_to_its_recording(name, emulator, golden_db):
    recording = load_recording(RECORDINGS / name / "fresh.json")
    replay = record_run(SCENARIOS[name], emulator=emulator, db=golden_db)

    assert_equivalent(replay, recording)
    assert_within_baseline(replay, name)


@pytest.mark.parametrize("starting_state", STARTING_STATES)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_mid_cycle_run_completes_equivalently(name, starting_state, emulator, golden_db):
    recording = load_recording(RECORDINGS / name / "mid_cycle.json")
    replay = record_run(
        SCENARIOS[name], emulator=emulator, db=golden_db,
        resume_from=_mid_cycle_dir(name, starting_state),
    )

    assert replay["resumed_from_step"] == MID_CYCLE_STEP[name] + 1
    assert_equivalent(replay, recording, from_step=MID_CYCLE_STEP[name] + 1)


# ---------------------------------------------------------------- one-time recording


def _assert_pre_refactor_commit() -> None:
    if (REPO_ROOT / "BE" / "app" / "bot_engine" / "exchange").exists():
        raise AssertionError(
            "refusing to record: this tree already contains exchange-adapter migration code; "
            "check out the pre-refactor commit (ADR-0007)"
        )


def _assert_mid_cycle(name: str, snapshot: Path) -> None:
    rows = json.loads((snapshot / "rows.json").read_text())
    open_orders = [order for order in rows["orders"] if order["status"] in {"New", "PartiallyFilled"}]
    state = rows["bot"]["settings"] or {}
    if name in {"grid", "dca"}:
        roles = {order["order_role"] for order in open_orders}
        assert "position_take_profit" in roles, f"{name}: no open take-profit at the mid-cycle point"
        assert roles - {"position_take_profit"}, f"{name}: no open entry orders at the mid-cycle point"
    else:
        trade_key = "momentum_state" if name == "momentum" else "pattern_scalper_state"
        trade = (state.get(trade_key) or {}).get("current_trade")
        assert trade, f"{name}: no open position with exit levels at the mid-cycle point"


@pytest.mark.skipif(os.environ.get("GOLDEN_RECORD") != "1", reason="one-time recording; set GOLDEN_RECORD=1")
def test_record_golden_runs(emulator, golden_db):
    _assert_pre_refactor_commit()
    baseline: dict[str, dict] = {}
    for name, scenario in sorted(SCENARIOS.items()):
        snapshot = RECORDINGS / name / "mid_cycle_pre_refactor"
        fresh = record_run(
            scenario, emulator=emulator, db=golden_db,
            snapshot_after_step=MID_CYCLE_STEP[name], snapshot_dir=snapshot,
        )
        _assert_mid_cycle(name, snapshot)
        mid_cycle = record_run(scenario, emulator=emulator, db=golden_db, resume_from=snapshot)
        save_recording(fresh, RECORDINGS / name / "fresh.json")
        save_recording(mid_cycle, RECORDINGS / name / "mid_cycle.json")
        durations = fresh["timings"]["request_durations_s"] + mid_cycle["timings"]["request_durations_s"]
        baseline[name] = {
            "source_commit": fresh["source_commit"],
            "fill_reaction_p95_s": p95(fresh["timings"]["fill_reaction_s"]),
            "request_p95_s": p95(durations),
            "requests": len(durations),
        }
    BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True))
