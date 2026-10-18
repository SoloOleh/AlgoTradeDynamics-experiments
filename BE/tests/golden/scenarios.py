"""The pinned golden-run scenarios (spec §8 default).

One trend-and-range scenario per strategy. Grid and DCA push manual prices; their prices sit on
tick-rounding boundaries (e.g. 100 × 0.985 and 1.5 % take-profits of fractional averages), which is
the rounding-boundary case. Momentum and Pattern Scalper read candles, so they replay seeded
1-minute candles from a historical dataset.

Mid-cycle snapshot points (`MID_CYCLE_STEP`) are where the pre-refactor run holds open entry
orders and a take-profit (Grid, DCA) or an open position with its exit levels (Momentum, Pattern
Scalper).
"""
from tests.golden.harness import Scenario

GRID = Scenario(
    name="grid",
    strategy_type="grid",
    symbol="SOLUSDT",
    start_price=100.0,
    bot={"order_qty": 1.0, "grid_orders_count": 3, "grid_step_percent": 1.5},
    settings={"take_profit_percent": 1.5},
    prices=[99.0, 98.5, 97.7, 97.0, 98.2, 99.4, 100.6, 101.8, 100.9, 99.2, 98.4, 99.9, 101.6, 102.4],
)

DCA = Scenario(
    name="dca",
    strategy_type="dca",
    symbol="SOLUSDT",
    start_price=100.0,
    bot={"order_qty": 1.0, "grid_orders_count": 3, "grid_step_percent": 1.5},
    settings={"dca_volume_multiplier": 1.5, "dca_step_multiplier": 1.2, "take_profit_percent": 1.5},
    prices=[99.2, 98.5, 97.9, 97.4, 96.4, 97.3, 98.1, 99.0, 99.8, 99.5, 98.6, 98.0, 99.7, 100.6],
)

MOMENTUM = Scenario(
    name="momentum",
    strategy_type="momentum",
    symbol="SOLUSDT",
    bot={"order_qty": 5.0, "grid_orders_count": 1, "grid_step_percent": 1.0},
    settings={"timeframe": "1", "cooldown_minutes": 0},
    candle_seed=0,
    candle_count=300,
    replay_from=200,
)

PATTERN_SCALPER = Scenario(
    name="pattern_scalper",
    strategy_type="pattern_scalper",
    symbol="SOLUSDT",
    bot={"order_qty": 5.0, "grid_orders_count": 1, "grid_step_percent": 1.0},
    settings={"timeframe": "1", "cooldown_minutes": 0},
    candle_seed=0,
    candle_count=300,
    replay_from=200,
)

SCENARIOS = {item.name: item for item in (GRID, DCA, MOMENTUM, PATTERN_SCALPER)}
# Step after which the mid-cycle state is captured (see the module docstring).
MID_CYCLE_STEP: dict[str, int] = {
    "grid": 1,              # entry 3 + position take-profit open, 2 entries filled
    "dca": 1,               # safety orders 3-4 + position take-profit open
    "momentum": 3,          # short position open, exit levels in bot state
    "pattern_scalper": 21,  # short position open right after entry
}
