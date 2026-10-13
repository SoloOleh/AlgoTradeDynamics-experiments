"""Static search for exchange access outside the adapters (QG-3, ADR-0009)."""
from tests.golden.static_search import scan

# Hits on the pre-refactor code. Migration tasks may only lower it; T20 flips the check to == 0.
BASELINE_HITS = 75


def test_static_search_does_not_grow_past_its_baseline():
    hits = scan()
    report = "\n".join(f"{hit.path}:{hit.line}: [{hit.rule}] {hit.text}" for hit in hits)
    assert BASELINE_HITS is not None, f"record the baseline ({len(hits)} hits):\n{report}"
    assert len(hits) <= BASELINE_HITS, f"{len(hits)} hits > baseline {BASELINE_HITS}:\n{report}"


def test_static_search_flags_each_rule(tmp_path):
    from tests.golden import static_search

    engine = tmp_path / "BE" / "app" / "bot_engine"
    (engine / "exchange").mkdir(parents=True)
    (engine / "strategies").mkdir()
    (engine / "strategies" / "leaky.py").write_text(
        "from pybit.unified_trading import HTTP\n"
        "from app.bot_engine.bybit.emulator_client import EmulatorHTTP\n"
        "session = get_bybit_session(bot)\n"
        "url = '/v5/order/create'\n"
        "step = info['lotSizeFilter']\n"
    )
    (engine / "exchange" / "inside.py").write_text("from pybit.unified_trading import HTTP\nx = '/v5/order'\n")
    services = tmp_path / "BE" / "app" / "services"
    services.mkdir(parents=True)
    (services / "backtest_service.py").write_text("x = row['lotSizeFilter']\ny = '/v5/market/instruments-info'\n")

    hits = static_search.scan(root=tmp_path)
    rules = sorted({(hit.path, hit.rule) for hit in hits})
    assert rules == sorted({
        ("BE/app/bot_engine/strategies/leaky.py", "pybit-import"),
        ("BE/app/bot_engine/strategies/leaky.py", "emulator-client"),
        ("BE/app/bot_engine/strategies/leaky.py", "session-factory"),
        ("BE/app/bot_engine/strategies/leaky.py", "v5-path"),
        ("BE/app/bot_engine/strategies/leaky.py", "bybit-field"),
        ("BE/app/services/backtest_service.py", "v5-path"),
    })
