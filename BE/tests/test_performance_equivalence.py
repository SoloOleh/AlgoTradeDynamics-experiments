"""Performance equivalence (T15): every golden-run bot reports the same figures after the migration.

The snapshot (`recordings/<strategy>/performance.json`) was taken on the commit before performance
moved onto the exchange adapter, from each fresh golden run's final state:
    GOLDEN_CAPTURE_PERFORMANCE=1 pytest tests/test_performance_equivalence.py
"""
from __future__ import annotations

import json
import os

import pytest

from tests.golden.harness import EmulatorFactory, GoldenDatabase, record_run
from tests.golden.scenarios import SCENARIOS

RECORDINGS = __import__("pathlib").Path(__file__).parent / "golden" / "recordings"
CAPTURE = os.environ.get("GOLDEN_CAPTURE_PERFORMANCE") == "1"


@pytest.fixture(scope="module")
def emulator():
    factory = EmulatorFactory()
    try:
        yield factory
    finally:
        factory.close()


@pytest.fixture()
def golden_db(client, tmp_path):
    from app.core.config import get_settings

    original_url = get_settings().exchange_emulator_url
    database = GoldenDatabase(client, tmp_path)
    try:
        yield database
    finally:
        database.restore()
        get_settings().exchange_emulator_url = original_url


def _performance(bot_id: int) -> dict:
    from app.bot_engine.performance import get_performance_summary
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot

    db = SessionLocal()
    try:
        return get_performance_summary(db, db.get(TradingBot, bot_id))
    finally:
        db.close()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_performance_figures_are_unchanged(name, emulator, golden_db):
    run = record_run(SCENARIOS[name], emulator=emulator, db=golden_db, after_run=_performance)
    figures = run["after_run"]
    snapshot = RECORDINGS / name / "performance.json"

    if CAPTURE:
        snapshot.write_text(json.dumps(figures, indent=2, sort_keys=True) + "\n")
    expected = json.loads(snapshot.read_text())

    assert figures == expected
    assert int(expected["closed_cycles"]) >= 0 and expected["total_fees"] is not None
