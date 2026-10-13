"""The exchange seam core: neutral vocabularies match the API contract (T04)."""
from pathlib import Path

import pytest
import yaml

from app.bot_engine.exchange import capabilities as caps
from app.bot_engine.exchange import errors, port

CONTRACT = Path(__file__).resolve().parents[2] / "docs/features/exchange-adapter/contracts/openapi.yaml"


@pytest.fixture(scope="module")
def schemas():
    return yaml.safe_load(CONTRACT.read_text())["components"]["schemas"]


def test_error_classes_match_the_contract(schemas):
    assert [item.value for item in errors.ErrorClass] == schemas["ErrorClass"]["enum"]


def test_capabilities_match_the_contract(schemas):
    assert [item.value for item in caps.Capability] == schemas["Capability"]["enum"]
    assert "client_tag_max_length" not in {item.value for item in caps.Capability}


def test_market_types_match_the_contract(schemas):
    assert [item.value for item in caps.MarketType] == schemas["MarketType"]["enum"]


def test_guard_errors_are_not_value_errors():
    # The engine's classifier maps ValueError text to configuration errors; guard refusals
    # must never be reclassified that way (api-sync-report §D).
    assert not issubclass(errors.VenueNotSupported, ValueError)
    assert not issubclass(errors.CapabilityMissing, ValueError)
    assert not issubclass(errors.ExchangeError, ValueError)


def test_exchange_error_carries_class_and_raw_code():
    exc = errors.ExchangeError(errors.ErrorClass.RATE_LIMITED, raw_code=10006, message="Too many visits!")
    assert exc.error_class is errors.ErrorClass.RATE_LIMITED
    assert exc.raw_code == "10006"
    assert "Too many visits!" in str(exc)


def test_guard_errors_name_the_venue_and_capability():
    refused = errors.CapabilityMissing("bybit", "demo", caps.Capability.SHORT_SELLING, caps.MarketType.SPOT)
    assert (refused.exchange, refused.environment) == ("bybit", "demo")
    assert refused.capability is caps.Capability.SHORT_SELLING
    assert refused.market_type is caps.MarketType.SPOT
    unsupported = errors.VenueNotSupported("binance", "live")
    assert (unsupported.exchange, unsupported.environment) == ("binance", "live")


def test_capability_set_answers_per_market_type():
    declared = caps.CapabilitySet(
        {
            caps.MarketType.SPOT: frozenset({caps.Capability.LIMIT_ORDERS}),
            caps.MarketType.LINEAR: frozenset({caps.Capability.LIMIT_ORDERS, caps.Capability.SHORT_SELLING}),
        },
        client_tag_max_length=36,
    )
    assert declared.supports(caps.Capability.SHORT_SELLING, caps.MarketType.LINEAR)
    assert not declared.supports(caps.Capability.SHORT_SELLING, caps.MarketType.SPOT)
    assert not declared.supports(caps.Capability.LIMIT_ORDERS, caps.MarketType.INVERSE)
    assert declared.missing(
        [caps.Capability.LIMIT_ORDERS, caps.Capability.SHORT_SELLING], caps.MarketType.SPOT,
    ) == [caps.Capability.SHORT_SELLING]
    assert declared.client_tag_max_length == 36


def test_order_lookup_states():
    order = port.Order(
        order_id="1", client_tag="bot-1-g1-entry-1-1", status="Filled", side="Buy", order_type="Limit",
        qty=1.0, filled_qty=1.0, price=100.0, avg_price=100.0, reduce_only=False, raw={},
    )
    assert port.OrderLookup.open(order).is_open
    final = port.OrderLookup.final(order)
    assert final.is_final and final.order.status == "Filled"
    gone = port.OrderLookup.not_found()
    assert gone.is_not_found and gone.order is None


def test_instrument_rules_shape():
    rules = port.InstrumentRules(min_order_qty=0.001, qty_step=0.001, tick_size=0.1, min_notional_value=5.0)
    assert (rules.qty_step, rules.tick_size, rules.min_order_qty, rules.min_notional_value) == (0.001, 0.1, 0.001, 5.0)


def test_adapter_protocol_lists_the_port_methods():
    expected = {
        "venue", "capabilities", "instrument_rules", "ticker", "candles", "place_order", "cancel_order",
        "open_orders", "order_history", "lookup_order", "executions", "positions", "balance",
        "streams", "check_connection",
    }
    assert expected <= set(dir(port.ExchangeAdapter))


def test_seam_package_imports_no_exchange_client():
    import re

    client_import = re.compile(r"^\s*(from|import)\s+(pybit\b|.*\bEmulatorHTTP\b)", re.MULTILINE)
    package = Path(port.__file__).parent
    for path in package.glob("*.py"):
        assert not client_import.search(path.read_text()), path.name
