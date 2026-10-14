"""The engine reacts to the adapter's error class, never to a raw exchange code (T26; ADR-0005, AC-06).

A raw code is meaningful only inside the adapter that produced it: another venue's codes must
never be matched against Bybit's tables.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.bot_engine.error_handling import classify_bot_error
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
from app.bot_engine.exchange.port import PlacedOrder

ENGINE = Path(__file__).resolve().parents[1] / "app" / "bot_engine"
ADAPTER_PACKAGE = "app.bot_engine.exchange.bybit"


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_the_engine_imports_nothing_from_the_bybit_adapter():
    offenders = []
    for path in ENGINE.rglob("*.py"):
        relative = path.relative_to(ENGINE).as_posix()
        if relative.startswith("exchange/bybit/") or relative == "exchange/registry.py":
            continue  # the adapter itself, and the registry's venue factories
        if any(name == ADAPTER_PACKAGE or name.startswith(ADAPTER_PACKAGE + ".") for name in _imports(path)):
            offenders.append(relative)
    assert offenders == []


@pytest.mark.parametrize("strategy, action", [("momentum", "Momentum entry"), ("pattern_scalper", "Pattern Scalper entry")])
def test_a_rejected_placement_reacts_by_its_error_class_whatever_the_raw_code(strategy, action):
    import importlib

    module = importlib.import_module(f"app.bot_engine.strategies.{strategy}")
    rejection = ExchangeError(ErrorClass.INSUFFICIENT_BALANCE, raw_code="-2019", message="Margin is not enough")
    placed = PlacedOrder(order_id=None, client_tag="bot-1-x", status=None, raw={}, rejection=rejection)

    with pytest.raises(ExchangeError) as raised:
        module._ensure_accepted(placed, action)

    decision = classify_bot_error(raised.value)
    assert (decision.error_type.value, decision.severity.value, decision.action.value) == (
        "insufficient_funds", "error", "pause",
    )
    assert decision.code == "-2019"
    # The stored text keeps today's shape: operation prefix and raw code.
    assert decision.message == f"{action}: Margin is not enough (code -2019)"


def test_a_raw_exception_that_only_bybit_would_recognise_is_internal_to_the_engine():
    decision = classify_bot_error(RuntimeError("Insufficient available balance (ErrCode: 110007)"))
    assert (decision.error_type.value, decision.action.value) == ("internal", "stop")


@pytest.mark.parametrize("message", [
    "Order qty 0.0001 is below Bybit minimum 0.001",
    "Order notional 4.00000000 is below Bybit minimum 5",
    "Close position order quantity exceeds bot-owned or exchange position size",
])
def test_the_engines_own_order_rule_refusals_pause_as_an_invalid_order(message):
    decision = classify_bot_error(ValueError(message))
    assert (decision.error_type.value, decision.severity.value, decision.action.value) == (
        "invalid_order", "error", "pause",
    )
