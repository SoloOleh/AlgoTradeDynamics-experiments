"""Venue registry and the fail-closed guard (T07, ADR-0004, ADR-0006)."""
from __future__ import annotations

import itertools
from typing import get_args

import pytest

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.bybit.adapter import BybitAdapter
from app.bot_engine.exchange.capabilities import Capability, CapabilitySet, MarketType
from app.bot_engine.exchange.errors import CapabilityMissing, VenueNotSupported
from app.bot_engine.exchange.registry import adapter_for_venue, exchange_for
from app.bot_engine.strategies import get_strategy
from app.models.trading_bot import TradingBot
from app.schemas.trading_bot import CategoryLiteral, EnvironmentLiteral, ExchangeLiteral, StrategyTypeLiteral


def _bot(**overrides) -> TradingBot:
    values = {
        "id": 1, "user_id": 1, "name": "guard", "exchange": "bybit", "environment": "emulator",
        "strategy_type": "grid", "category": "linear", "symbol": "BTCUSDT", "order_qty": 0.01,
        "grid_orders_count": 2, "grid_step_percent": 1.0, "settings": {},
    }
    values.update(overrides)
    return TradingBot(**values)


class RecordingStandIn:
    """A stand-in adapter that records every call — the guard must make none."""

    def __init__(self, declared: CapabilitySet) -> None:
        self.calls: list[str] = []
        self._declared = declared

    def capabilities(self) -> CapabilitySet:
        return self._declared

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
        return record


@pytest.fixture()
def no_network(monkeypatch):
    import httpx2

    def refuse(*args, **kwargs):
        raise AssertionError("the guard must not touch the network")

    monkeypatch.setattr(httpx2.Client, "send", refuse)


def test_bybit_venues_resolve_to_the_bybit_adapter(no_network):
    for environment in ("emulator", "demo", "testnet", "live"):
        adapter = exchange_for(_bot(environment=environment))
        assert isinstance(adapter, BybitAdapter)
        assert adapter.venue().environment == environment


@pytest.mark.parametrize("exchange,environment", [("binance", "live"), ("binance", "emulator"), ("bybit", "paper"), ("", "")])
def test_unknown_venue_is_refused_never_routed_to_a_default(exchange, environment, no_network):
    with pytest.raises(VenueNotSupported) as refused:
        exchange_for(_bot(exchange=exchange, environment=environment))
    assert (refused.value.exchange, refused.value.environment) == (exchange, environment)
    with pytest.raises(VenueNotSupported):
        adapter_for_venue(exchange, environment)


def test_missing_capability_is_refused_with_its_name_and_market_type_and_no_request(no_network):
    declared = CapabilitySet({MarketType.LINEAR: frozenset(Capability) - {Capability.CANDLES}})
    stand_in = RecordingStandIn(declared)
    with registry.registered_venue("standin", "test", lambda account: stand_in):
        with pytest.raises(CapabilityMissing) as refused:
            exchange_for(_bot(exchange="standin", environment="test", strategy_type="momentum"))
    assert refused.value.capability is Capability.CANDLES
    assert refused.value.market_type is MarketType.LINEAR
    assert (refused.value.exchange, refused.value.environment) == ("standin", "test")
    assert stand_in.calls == []


def test_explicit_needs_override_the_strategy_requirements(no_network):
    declared = CapabilitySet({MarketType.SPOT: frozenset({Capability.LIMIT_ORDERS})})
    with registry.registered_venue("standin", "test", lambda account: RecordingStandIn(declared)):
        exchange_for(_bot(exchange="standin", environment="test", category="spot"), needs=[Capability.LIMIT_ORDERS])
        with pytest.raises(CapabilityMissing):
            exchange_for(_bot(exchange="standin", environment="test", category="spot"), needs=[Capability.CANDLES])


@pytest.mark.parametrize("category,position_side,needs_short", [
    ("spot", "both", False),
    ("spot", "long", False),
    ("linear", "both", True),
    ("linear", "short", True),
    ("linear", "long", False),
])
def test_momentum_needs_short_selling_only_when_it_can_short(category, position_side, needs_short):
    bot = _bot(strategy_type="momentum", category=category, settings={"position_side": position_side})
    needs = get_strategy("momentum").required_capabilities(bot)
    assert (Capability.SHORT_SELLING in needs) == needs_short


@pytest.mark.parametrize("strategy_type,expected", [
    ("grid", {"limit_orders", "reduce_only_orders", "position_reads", "execution_pnl", "order_lookup_by_tag", "private_event_stream"}),
    ("dca", {"limit_orders", "market_orders", "reduce_only_orders", "execution_pnl", "order_lookup_by_tag", "private_event_stream"}),
    ("pattern_scalper", {"limit_orders", "market_orders", "reduce_only_orders", "position_reads", "short_selling",
                         "execution_pnl", "wallet_balance", "candles", "order_lookup_by_tag", "private_event_stream"}),
])
def test_strategy_requirements_follow_the_catalogue(strategy_type, expected):
    needs = get_strategy(strategy_type).required_capabilities(_bot(strategy_type=strategy_type))
    assert {item.value for item in needs} == expected


def test_every_configuration_accepted_today_passes_the_guard(no_network):
    from app.services.trading_bot_service import _validate_strategy_configuration

    accepted = 0
    for exchange, environment, strategy_type, category, position_side in itertools.product(
        get_args(ExchangeLiteral), get_args(EnvironmentLiteral), get_args(StrategyTypeLiteral),
        get_args(CategoryLiteral), ("both", "long", "short"),
    ):
        try:
            _validate_strategy_configuration(strategy_type, category, 1.0)
        except ValueError:
            continue
        bot = _bot(exchange=exchange, environment=environment, strategy_type=strategy_type,
                   category=category, settings={"position_side": position_side})
        exchange_for(bot)
        accepted += 1
    assert accepted > 0


def test_two_emulator_bots_with_their_own_keys_get_two_accounts(no_network):
    first = exchange_for(_bot(settings={"emulator_api_key": "key-a-0001"}))
    second = exchange_for(_bot(settings={"emulator_api_key": "key-b-0002"}))
    keyless = exchange_for(_bot(settings={}))
    assert (first.account_key, second.account_key) == ("key-a-0001", "key-b-0002")
    from app.core.config import get_settings
    assert keyless.account_key == get_settings().exchange_emulator_default_api_key


def test_strategies_accept_an_adapter_keyword():
    import inspect

    for strategy_type in get_args(StrategyTypeLiteral):
        strategy = get_strategy(strategy_type)
        for method in ("tick", "sync_orders", "cancel_orders", "get_position", "get_risk", "close_position"):
            assert "adapter" in inspect.signature(getattr(strategy, method)).parameters, (strategy_type, method)
