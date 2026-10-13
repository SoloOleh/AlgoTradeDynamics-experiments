"""Two-layer error handling (ADR-0005): Bybit failure → error class, error class → reaction."""
import pytest

from app.bot_engine.error_handling import (
    BotErrorAction,
    BotErrorSeverity,
    BotErrorType,
    REACTION_TABLE,
    classify_bot_error,
)
from app.bot_engine.exchange.bybit.errors import classify_bybit_failure
from app.bot_engine.exchange.errors import CapabilityMissing, ErrorClass, ExchangeError, VenueNotSupported
from tests.golden.failure_table.rows import build_exception


def _pybit(code, message):
    return build_exception({"kind": "pybit_invalid_request", "code": code, "message": message})


@pytest.mark.parametrize("code,message,expected", [
    (10006, "Too many visits!", ErrorClass.RATE_LIMITED),
    (10016, "Server error.", ErrorClass.EXCHANGE_UNAVAILABLE),
    (10002, "invalid request, please check your server timestamp", ErrorClass.TIME_SYNC),
    (10003, "API key is invalid.", ErrorClass.REJECTED_CREDENTIALS),
    (10005, "Permission denied", ErrorClass.PERMISSION_DENIED),
    (110007, "ab not enough for new order", ErrorClass.INSUFFICIENT_BALANCE),
    (110050, "Invalid symbol", ErrorClass.INVALID_SYMBOL),
    (110072, "OrderLinkedID is duplicate", ErrorClass.DUPLICATE_CLIENT_TAG),
    (110001, "Order does not exist.", ErrorClass.UNKNOWN_ORDER),
    (110010, "Order has been cancelled", ErrorClass.ORDER_STATE_CONFLICT),
    (110013, "Due to risk limit, cannot set leverage", ErrorClass.RISK_LIMIT),
    (110017, "Reduce-only rule not satisfied", ErrorClass.INVALID_ORDER),
])
def test_bybit_codes_map_to_error_classes(code, message, expected):
    mapped = classify_bybit_failure(_pybit(code, message))
    assert isinstance(mapped, ExchangeError)
    assert mapped.error_class is expected
    assert mapped.raw_code == str(code)


def test_emulator_transport_failures_are_classified():
    timeout = build_exception({"kind": "emulator_timeout", "message": "timed out"})
    assert classify_bybit_failure(timeout).error_class is ErrorClass.NETWORK
    unavailable = build_exception({"kind": "emulator_http_status", "http_status": 503, "message": "503"})
    assert classify_bybit_failure(unavailable).error_class is ErrorClass.EXCHANGE_UNAVAILABLE


def test_non_exchange_failures_are_left_to_the_engine():
    assert classify_bybit_failure(ValueError("DCA volume multiplier must be at least 1")) is None
    assert classify_bybit_failure(RuntimeError("unexpected")) is None


def test_classified_message_is_scrubbed(monkeypatch):
    from app.bot_engine.exchange import redaction

    monkeypatch.setattr(redaction, "_shared", redaction.Scrubber(keys=["leakykey12345678"]))
    exc = build_exception({
        "kind": "pybit_invalid_request", "code": 10003,
        "message": "API key is invalid. api_key=leakykey12345678",
    })
    mapped = classify_bybit_failure(exc)
    assert "leakykey12345678" not in mapped.message
    assert mapped.raw_code == "10003"


@pytest.mark.parametrize("error_class,stored,severity,action", [
    (ErrorClass.NETWORK, "network", "warning", "retry_backoff"),
    (ErrorClass.RATE_LIMITED, "rate_limit", "warning", "retry_backoff"),
    (ErrorClass.EXCHANGE_UNAVAILABLE, "exchange_unavailable", "error", "retry_backoff"),
    (ErrorClass.TIME_SYNC, "time_sync", "warning", "retry_backoff"),
    (ErrorClass.REJECTED_CREDENTIALS, "authentication", "critical", "stop"),
    (ErrorClass.PERMISSION_DENIED, "permission", "critical", "stop"),
    (ErrorClass.INSUFFICIENT_BALANCE, "insufficient_funds", "error", "pause"),
    (ErrorClass.INVALID_SYMBOL, "invalid_symbol", "error", "pause"),
    (ErrorClass.INVALID_ORDER, "invalid_order", "error", "pause"),
    (ErrorClass.DUPLICATE_CLIENT_TAG, "order_state", "warning", "resync_and_retry"),
    (ErrorClass.UNKNOWN_ORDER, "order_state", "warning", "resync_and_retry"),
    (ErrorClass.ORDER_STATE_CONFLICT, "order_state", "warning", "resync_and_retry"),
    (ErrorClass.RISK_LIMIT, "risk", "critical", "pause"),
])
def test_engine_reaction_table_reproduces_todays_reactions(error_class, stored, severity, action):
    reaction = REACTION_TABLE[error_class]
    assert reaction.error_type == BotErrorType(stored)
    assert reaction.severity == BotErrorSeverity(severity)
    assert reaction.action == BotErrorAction(action)
    assert set(REACTION_TABLE) == set(ErrorClass)


def test_exchange_error_goes_straight_through_the_table():
    decision = classify_bot_error(ExchangeError(ErrorClass.UNKNOWN_ORDER, raw_code=110001, message="Order does not exist."))
    assert decision.error_type == BotErrorType.ORDER_STATE
    assert decision.action == BotErrorAction.RESYNC_AND_RETRY
    assert decision.code == "110001"
    assert decision.retry_after_seconds == 5


@pytest.mark.parametrize("refusal", [
    VenueNotSupported("binance", "live"),
    CapabilityMissing("bybit", "demo", "short_selling", "spot"),
])
def test_guard_refusals_never_become_retryable(refusal):
    assert not classify_bot_error(refusal).retryable
