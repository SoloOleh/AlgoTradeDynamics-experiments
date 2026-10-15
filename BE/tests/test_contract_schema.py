"""The contract checker reads contracts/openapi.yaml itself and rejects what the file rejects (T28)."""
from __future__ import annotations

import pytest

from tests.contract_schema import ContractViolation, assert_body_matches

VENUE_UNSUPPORTED = {
    "detail": "Venue binance/live has no exchange adapter",
    "code": "exchange.venue_unsupported",
    "details": {"exchange": "binance", "environment": "live"},
}
CONNECTED = {
    "venue": {"exchange": "bybit", "environment": "demo"},
    "status": "connected",
    "error_class": None,
    "message": "Venue bybit/demo is connected",
}


def test_conforming_bodies_pass():
    assert_body_matches("startBot", 422, VENUE_UNSUPPORTED)
    assert_body_matches("stopBot", 422, VENUE_UNSUPPORTED)
    assert_body_matches("getVenueConnection", 200, CONNECTED)
    assert_body_matches("getVenueConnection", 200, {**CONNECTED, "status": "failing", "error_class": "network"})
    assert_body_matches("getVenueConnection", 401, {"detail": "Не автентифіковано"})


@pytest.mark.parametrize("operation,status,body", [
    ("stopBot", 422, {**VENUE_UNSUPPORTED, "code": "exchange.something_else"}),
    ("stopBot", 422, {**VENUE_UNSUPPORTED, "details": {"exchange": "binance"}}),
    ("stopBot", 422, {**VENUE_UNSUPPORTED, "details": {**VENUE_UNSUPPORTED["details"], "api_key": "k"}}),
    ("stopBot", 422, {**VENUE_UNSUPPORTED, "details": {**VENUE_UNSUPPORTED["details"], "market_type": "margin"}}),
    ("stopBot", 422, {"detail": "Trading bot is inactive"}),
    ("startBot", 422, {"detail": 7}),
    ("getVenueConnection", 200, {**CONNECTED, "status": "up"}),
    ("getVenueConnection", 200, {**CONNECTED, "error_class": "Invalid api key"}),
    ("getVenueConnection", 200, {**CONNECTED, "raw": "x"}),
    ("getVenueConnection", 200, {key: value for key, value in CONNECTED.items() if key != "error_class"}),
    ("getVenueConnection", 200, {**CONNECTED, "venue": {"exchange": "bybit", "environment": "e" * 21}}),
])
def test_bodies_the_contract_rejects_fail(operation, status, body):
    with pytest.raises(ContractViolation):
        assert_body_matches(operation, status, body)


def test_a_status_the_operation_does_not_declare_fails():
    with pytest.raises(ContractViolation):
        assert_body_matches("getBotPerformance", 422, {"detail": "x"})
