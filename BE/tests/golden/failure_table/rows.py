"""Build the reference failure table's exceptions and drive them through the engine's reaction.

Rows hold the failure as data (`exception`) so the same input can be replayed after the
classification moves into the Bybit adapter (T05). `react_to_failure` is the one entry point the
table is checked through.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

ROWS_PATH = Path(__file__).parent / "rows.json"
_PYBIT_TIME = "12:00:00"


def build_exception(spec: dict) -> BaseException:
    kind = spec["kind"]
    message = spec.get("message", "")
    if kind == "pybit_invalid_request":
        from pybit.exceptions import InvalidRequestError

        return InvalidRequestError(
            request=spec.get("request", "POST /v5/order/create: {}"), message=message,
            status_code=spec["code"], time=_PYBIT_TIME, resp_headers={},
        )
    if kind == "pybit_failed_request":
        from pybit.exceptions import FailedRequestError

        return FailedRequestError(
            request=spec.get("request", "POST /v5/order/create: {}"), message=message,
            status_code=spec["http_status"], time=_PYBIT_TIME, resp_headers={},
        )
    if kind == "requests_timeout":
        import requests

        return requests.exceptions.ReadTimeout(message)
    if kind in {"emulator_timeout", "emulator_connect_error"}:
        import httpx2 as httpx

        request = httpx.Request("POST", "http://exchange-emulator:8001/v5/order/create")
        cls = httpx.ReadTimeout if kind == "emulator_timeout" else httpx.ConnectError
        return cls(message, request=request)
    if kind == "emulator_http_status":
        import httpx2 as httpx

        request = httpx.Request("GET", "http://exchange-emulator:8001/v5/order/realtime")
        response = httpx.Response(spec["http_status"], request=request)
        return httpx.HTTPStatusError(message, request=request, response=response)
    if kind == "exchange_operation_error":
        from app.bot_engine.error_handling import ExchangeOperationError

        return ExchangeOperationError(message, code=spec.get("code"), operation=spec.get("operation"))
    if kind == "value_error":
        return ValueError(message)
    if kind == "runtime_error":
        return RuntimeError(message)
    raise ValueError(f"unknown failure kind {kind}")


def react_to_failure(exc: BaseException, *, retry_count: int = 0) -> dict:
    """Drive one failure through today's reaction entry point for a running bot."""
    from app.bot_engine.error_handling import handle_bot_runtime_error
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot
    from app.models.user import User

    db = SessionLocal()
    try:
        user = User(email=f"failure_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        bot = TradingBot(
            user_id=user.id, name="failure table", exchange="bybit", environment="emulator",
            strategy_type="grid", category="linear", symbol="BTCUSDT", order_qty=0.001,
            grid_orders_count=2, grid_step_percent=1.0, runtime_status="running",
            settings={}, error_retry_count=retry_count,
        )
        db.add(bot)
        db.commit()
        decision = handle_bot_runtime_error(db, bot, exc, source="worker")
        db.refresh(bot)
        return {
            "error_type": decision.error_type.value,
            "severity": decision.severity.value,
            "action": decision.action.value,
            "code": decision.code,
            "runtime_status": bot.runtime_status,
        }
    finally:
        db.close()


# One-time recording inputs (pre-refactor). Expected reactions are computed from today's code.
_INPUTS = [
    ("network_timeout", "bybit", {"kind": "requests_timeout", "message": "HTTPSConnectionPool(host='api.bybit.com', port=443): Read timed out. (read timeout=10)"}),
    ("emulator_network_timeout", "emulator", {"kind": "emulator_timeout", "message": "timed out"}),
    ("emulator_connect_error", "emulator", {"kind": "emulator_connect_error", "message": "[Errno 111] Connection refused"}),
    ("rate_limit", "bybit", {"kind": "pybit_invalid_request", "code": 10006, "message": "Too many visits!"}),
    ("exchange_unavailable", "bybit", {"kind": "pybit_invalid_request", "code": 10016, "message": "Server error."}),
    ("emulator_http_503", "emulator", {"kind": "emulator_http_status", "http_status": 503, "message": "Server error '503 Service Unavailable' for url 'http://exchange-emulator:8001/v5/order/realtime'"}),
    ("time_sync", "bybit", {"kind": "pybit_invalid_request", "code": 10002, "message": "invalid request, please check your server timestamp or recv_window param. req_timestamp[1736078400000],server_timestamp[1736078406000],recv_window[5000]"}),
    ("rejected_credentials", "bybit", {"kind": "pybit_invalid_request", "code": 10003, "message": "API key is invalid."}),
    ("missing_credentials", "synthetic", {"kind": "value_error", "message": "Bybit demo API credentials are not configured"}),
    ("permission", "bybit", {"kind": "pybit_invalid_request", "code": 10005, "message": "Permission denied, please check your API key permissions."}),
    ("ip_rate_limit_http_403", "bybit", {"kind": "pybit_failed_request", "http_status": 403, "message": "You have breached the IP rate limit or your IP is from the USA."}),
    ("insufficient_balance", "bybit", {"kind": "pybit_invalid_request", "code": 110007, "message": "ab not enough for new order"}),
    ("emulator_insufficient_balance", "emulator", {"kind": "exchange_operation_error", "code": 110007, "operation": "Momentum entry", "message": "Insufficient available balance"}),
    ("rejected_order", "bybit", {"kind": "pybit_invalid_request", "code": 110017, "message": "Reduce-only rule not satisfied"}),
    ("order_below_minimum", "synthetic", {"kind": "value_error", "message": "Order qty 0.0001 is below Bybit minimum 0.001"}),
    ("invalid_symbol", "bybit", {"kind": "value_error", "message": "Instrument info not found for FOOUSDT"}),
    ("duplicate_client_tag", "bybit", {"kind": "pybit_invalid_request", "code": 110072, "message": "OrderLinkedID is duplicate"}),
    ("unknown_order", "bybit", {"kind": "pybit_invalid_request", "code": 110001, "message": "Order does not exist."}),
    ("risk_limit", "bybit", {"kind": "pybit_invalid_request", "code": 110013, "message": "Due to risk limit, cannot set leverage"}),
    ("configuration", "synthetic", {"kind": "value_error", "message": "DCA volume multiplier must be at least 1"}),
    ("internal", "synthetic", {"kind": "runtime_error", "message": "unexpected state while building the ladder"}),
]


def record_rows() -> list[dict]:
    rows: list[dict] = []
    for row_id, source, spec in _INPUTS:
        outcome = react_to_failure(build_exception(spec))
        rows.append({"id": row_id, "source": source, "exception": spec, "expected": outcome})

    exhausted_spec = dict(_INPUTS[0][2])
    rows.append({
        "id": "retry_limit_exhausted",
        "source": "bybit",
        "exception": exhausted_spec,
        "retry_count": 5,
        "expected": react_to_failure(build_exception(exhausted_spec), retry_count=5),
    })

    lookup_spec = {
        "kind": "pybit_invalid_request", "code": 10006, "message": "Too many visits!",
        "request": "GET /v5/order/realtime: {'orderLinkId': 'bot-1-g1-entry-2-1'}",
    }
    rows.append({
        "id": "failed_order_lookup",
        "source": "bybit",
        "deviation": "AC-08",
        "note": (
            "Expected reaction is the AC-08 deviation: a lookup that fails for any reason other than "
            "the unknown-order class keeps the order active, places and cancels nothing tied to it "
            "and retries up to the limit, then pauses. Before the adapter, a failed lookup could "
            "leave the order marked missing. Enforced by the engine's order-lookup rule "
            "(orders.resolve_order_state), not by the class table (ADR-0005)."
        ),
        "exception": lookup_spec,
        "keeps_order_active": True,
        "expected": react_to_failure(build_exception(lookup_spec)),
    })
    ROWS_PATH.write_text(json.dumps(rows, indent=2) + "\n")
    return rows
