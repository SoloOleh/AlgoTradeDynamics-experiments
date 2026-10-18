"""Bybit failure → exchange-neutral error class (ADR-0005).

Bybit V5 codes, HTTP statuses and message texts, checked in exactly the order the engine checked
them before the refactor (the reference failure table guards it). Works for both transports:
`pybit` exceptions and the emulator client's `httpx` exceptions. Returns `None` for failures that
are not exchange failures, which the engine classifies itself.
"""
from __future__ import annotations

import re

from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
from app.bot_engine.exchange.redaction import scrub

RATE_LIMIT_CODES = {"429", "10006", "10429", "20003", "30035"}
EXCHANGE_UNAVAILABLE_CODES = {"10000", "10016", "10019"}
TIME_SYNC_CODES = {"-1", "10002"}
AUTH_CODES = {"10003", "10004", "10007", "-2015", "33004"}
PERMISSION_CODES = {"10005", "10008", "10009", "10010", "10024", "10027"}
INSUFFICIENT_FUNDS_CODES = {
    "110004", "110006", "110007", "110012", "110014", "110044", "110045",
    "110051", "110052", "110053", "3777040",
}
INVALID_SYMBOL_CODES = {"10029", "110050"}
DUPLICATE_CLIENT_TAG_CODES = {"110072"}
UNKNOWN_ORDER_CODES = {"110001"}
ORDER_STATE_CODES = {"10014", "110005", "110008", "110010", "40004", "20006"}
RISK_CODES = {
    "110011", "110013", "110021", "110022", "110023", "110031", "110039", "110040",
    "110047", "110048", "30256",
}
INVALID_ORDER_CODES = {
    "10001", "110003", "110009", "110017", "110020", "110032", "110049",
    "110057", "110058", "110059", "110060", "110061", "30208", "30209",
}

_CODE_PATTERNS = (
    re.compile(r"(?:errcode|retcode|code)\s*[:=]?\s*\(?(-?\d+)\)?", re.IGNORECASE),
    re.compile(r"\((-?\d+)\)"),
)


def extract_code(exc: BaseException) -> str | None:
    for attr in ("code", "status_code", "ret_code", "retCode"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    text = str(exc)
    for pattern in _CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if value is None:
        value = getattr(exc, "http_status", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_network_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    module = type(exc).__module__.lower()
    name = type(exc).__name__.lower()
    return (
        module.startswith("httpx")
        or module.startswith("requests")
        or module.startswith("urllib3")
    ) and any(token in name for token in ("timeout", "connect", "network", "request", "transport"))


def _order_state_class(code: str | None, lowered: str) -> ErrorClass:
    if code in DUPLICATE_CLIENT_TAG_CODES or "duplicate order" in lowered or "orderlinkid is duplicate" in lowered:
        return ErrorClass.DUPLICATE_CLIENT_TAG
    if code in UNKNOWN_ORDER_CODES or "order does not exist" in lowered:
        return ErrorClass.UNKNOWN_ORDER
    return ErrorClass.ORDER_STATE_CONFLICT


def _classify(exc: BaseException, lowered: str, code: str | None, status: int | None) -> ErrorClass | None:
    if code in RATE_LIMIT_CODES or status == 429:
        return ErrorClass.RATE_LIMITED
    if code in EXCHANGE_UNAVAILABLE_CODES or (status is not None and status >= 500):
        return ErrorClass.EXCHANGE_UNAVAILABLE
    if code in TIME_SYNC_CODES or "recv_window" in lowered or "timestamp" in lowered and "server" in lowered:
        return ErrorClass.TIME_SYNC
    if code in AUTH_CODES or status == 401 or "api credentials are not configured" in lowered or "authentication failed" in lowered:
        return ErrorClass.REJECTED_CREDENTIALS
    if code in PERMISSION_CODES or status == 403 or "permission denied" in lowered or "unmatched ip" in lowered:
        return ErrorClass.PERMISSION_DENIED
    if code in INSUFFICIENT_FUNDS_CODES or any(token in lowered for token in (
        "insufficient balance", "wallet balance is insufficient", "available balance is insufficient", "insufficient margin",
    )):
        return ErrorClass.INSUFFICIENT_BALANCE
    if code in INVALID_SYMBOL_CODES or any(token in lowered for token in (
        "ticker not found", "instrument info not found", "symbol is invalid", "invalid symbol",
    )):
        return ErrorClass.INVALID_SYMBOL
    if (
        code in DUPLICATE_CLIENT_TAG_CODES | UNKNOWN_ORDER_CODES | ORDER_STATE_CODES
        or any(token in lowered for token in (
            "order does not exist", "order has been completed or cancelled", "duplicate order", "orderlinkid is duplicate",
        ))
    ):
        return _order_state_class(code, lowered)
    if code in RISK_CODES or any(token in lowered for token in ("liquidation will be triggered", "risk limit", "position limit")):
        return ErrorClass.RISK_LIMIT
    if code in INVALID_ORDER_CODES or any(token in lowered for token in (
        "below bybit minimum", "order notional", "order qty", "order quantity", "price exceeds", "price is higher", "price is lower",
    )):
        return ErrorClass.INVALID_ORDER
    if is_network_exception(exc) or status in {408, 425, 502, 503, 504}:
        return ErrorClass.NETWORK
    return None


def classify_bybit_failure(exc: BaseException) -> ExchangeError | None:
    """Translate a Bybit/emulator failure into an `ExchangeError`, or `None` if it is not one."""
    if isinstance(exc, ExchangeError):
        return exc
    message = str(exc).strip() or type(exc).__name__
    code = extract_code(exc)
    status = http_status(exc)
    error_class = _classify(exc, message.lower(), code, status)
    if error_class is None:
        return None
    return ExchangeError(error_class, raw_code=code, message=scrub(message), http_status=status)
