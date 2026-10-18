"""The Bybit exchange adapter over the existing `pybit` and `EmulatorHTTP` clients (ADR-0002).

Order-mutating requests (place, cancel) carry exactly the parameters, in exactly the order, the bot
sent before the adapter existed. Responses are translated into the port's neutral types; every
transport failure is re-raised as an `ExchangeError` via the Bybit classifier (ADR-0005).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.bot_engine.exchange.bybit.capabilities import BYBIT_CAPABILITIES
from app.bot_engine.exchange.bybit.client import create_session
from app.bot_engine.exchange.bybit.errors import classify_bybit_failure, extract_code
from app.bot_engine.exchange.capabilities import CapabilitySet
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError, UnclassifiedExchangeFailure
from app.bot_engine.exchange.port import (
    ACTIVE_ORDER_STATUSES,
    Balance,
    Candle,
    ConnectionStatus,
    Execution,
    InstrumentRules,
    NewOrder,
    Order,
    OrderLookup,
    PlacedOrder,
    Position,
    Ticker,
    Venue,
)
from app.bot_engine.exchange.redaction import get_scrubber, scrub

# The emulator issues `emu_` + a url-safe token (EXCHANGE_EMULATOR make_api_key); nothing else but
# the configured default key can authenticate there.
_EMULATOR_ISSUED_KEY = re.compile(r"emu_[A-Za-z0-9_-]{16,}")

EXECUTION_PAGE_LIMIT = 100
EXECUTION_MAX_PAGES = 100


def _float_or_none(value: Any) -> float | None:
    """`None` for an absent or empty field, else the field as a float (raises on garbage)."""
    if value is None or value == "":
        return None
    return float(value)


def _parsed_float(value: Any) -> float | None:
    """`None` for an absent, empty or non-numeric field."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usable_float(value: Any) -> float | None:
    """`None` for a value that is absent, falsy or not a number."""
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    """The exchange's number text as a Decimal; `None` for an absent, empty or non-numeric field."""
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _usable_decimal(value: Any) -> Decimal | None:
    """The exchange's number text as a Decimal; `None` where `_usable_float` gives `None`."""
    return None if _usable_float(value) is None else _decimal_or_none(value)


def _list(response: dict) -> list:
    return response.get("result", {}).get("list", []) or []


def _ret_code(response: dict) -> str | None:
    value = response.get("retCode")
    return None if value is None else str(value)


def _rejection(response: dict) -> ExchangeError | UnclassifiedExchangeFailure | None:
    code = _ret_code(response)
    if code in (None, "0"):
        return None
    message = str(response.get("retMsg") or "Bybit returned an error")
    classified = classify_bybit_failure(RuntimeError(f"{message} (ErrCode: {code})"))
    if classified is None:
        # An unmapped code is not interpreted: the engine treats it as internal (ADR-0005).
        return UnclassifiedExchangeFailure(scrub(message), raw_code=code)
    return ExchangeError(classified.error_class, raw_code=code, message=scrub(message))


def order_from_payload(payload: dict) -> Order:
    return Order(
        order_id=payload.get("orderId"),
        client_tag=payload.get("orderLinkId"),
        status=payload.get("orderStatus"),
        side=payload.get("side"),
        order_type=payload.get("orderType"),
        qty=_float_or_none(payload.get("qty")),
        filled_qty=_float_or_none(payload.get("cumExecQty")),
        leaves_qty=_parsed_float(payload.get("leavesQty")),
        price=float(payload["price"]) if payload.get("price") else None,
        avg_price=_usable_float(payload.get("avgPrice")),
        reduce_only=bool(payload.get("reduceOnly")),
        raw=dict(payload),
    )


def position_from_payload(item: dict) -> Position | None:
    """An open position, or `None` for a flat or unreadable one."""
    size_value = item.get("size") or item.get("qty") or "0"
    try:
        size = float(size_value)
    except (TypeError, ValueError):
        return None
    if size == 0:
        return None
    return Position(
        symbol=item.get("symbol"),
        side=str(item.get("side") or item.get("positionSide") or ""),
        size=size,
        avg_entry_price=_usable_float(item.get("avgPrice") or item.get("avgEntryPrice")),
        mark_price=_usable_decimal(item.get("markPrice")),
        liq_price=_usable_decimal(item.get("liqPrice")),
        leverage=_usable_decimal(item.get("leverage")),
        margin_mode=item.get("tradeMode") or item.get("marginMode"),
        position_value=_parsed_float(item.get("positionValue")),
        unrealised_pnl=_parsed_float(item.get("unrealisedPnl") or item.get("unrealizedPnl")),
    )


def balance_from_account(account: dict) -> Balance:
    coin = next(
        (item for item in account.get("coin") or [] if str(item.get("coin") or "").upper() == "USDT"),
        {},
    )
    return Balance(
        total_available=_usable_float(account.get("totalAvailableBalance")),
        total_equity=_usable_float(account.get("totalEquity")),
        total_wallet=_usable_float(account.get("totalWalletBalance")),
        wallet=_usable_float(account.get("walletBalance")),
        coin_available=_usable_float(coin.get("availableToWithdraw")),
        coin_equity=_usable_float(coin.get("equity")),
        coin_wallet=_usable_float(coin.get("walletBalance")),
    )


class BybitAdapter:
    """One Bybit venue (emulator, demo, testnet or live) bound to one exchange account."""

    exchange = "bybit"

    def __init__(self, environment: str, *, emulator_api_key: str | None = None) -> None:
        self.environment = environment
        self._emulator_api_key = emulator_api_key
        self._session = None
        if environment == "emulator":
            from app.core.config import get_settings

            default_key = get_settings().exchange_emulator_default_api_key
            self.account_key = str(emulator_api_key or default_key)
            # A bot's key is free-form input: only a key that can be a real account joins the
            # process-wide scrubber, so one bot cannot mask words in every user's texts.
            if self.account_key == default_key or _EMULATOR_ISSUED_KEY.fullmatch(self.account_key):
                get_scrubber().add_key(self.account_key)
        else:
            self.account_key = None

    # ------------------------------------------------------------------ transport

    @property
    def session(self):
        if self._session is None:
            self._session = create_session(self.environment, emulator_api_key=self.account_key)
        return self._session

    def _request(self, method: str, **params: Any) -> dict:
        try:
            return getattr(self.session, method)(**params)
        except Exception as exc:  # noqa: BLE001 - every transport failure is classified
            classified = classify_bybit_failure(exc)
            if classified is None:
                # Not chained: the original exception's text may carry a key (ADR-0008).
                raise UnclassifiedExchangeFailure(
                    scrub(str(exc).strip() or type(exc).__name__), raw_code=extract_code(exc),
                ) from None
            raise classified from exc

    # ------------------------------------------------------------------ identity

    def venue(self) -> Venue:
        return Venue(self.exchange, self.environment)

    def capabilities(self) -> CapabilitySet:
        return BYBIT_CAPABILITIES

    # ------------------------------------------------------------------ market data

    def instrument_rules(self, market_type: str, symbol: str) -> InstrumentRules:
        items = _list(self._request("get_instruments_info", category=market_type, symbol=symbol))
        if not items:
            raise ExchangeError(ErrorClass.INVALID_SYMBOL, message=f"Instrument info not found for {symbol}")
        info = items[0]
        lot_size_filter = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        return InstrumentRules(
            min_order_qty=float(lot_size_filter.get("minOrderQty") or 0),
            qty_step=float(lot_size_filter.get("qtyStep") or 0.000001),
            tick_size=float(price_filter.get("tickSize") or 0.000001),
            min_notional_value=float(lot_size_filter.get("minNotionalValue") or 0),
        )

    def ticker(self, market_type: str, symbol: str) -> Ticker:
        items = _list(self._request("get_tickers", category=market_type, symbol=symbol))
        if not items:
            raise ExchangeError(ErrorClass.INVALID_SYMBOL, message=f"Ticker not found for {symbol}")
        item = items[0]
        return Ticker(
            symbol=item.get("symbol") or symbol,
            last_price=_usable_decimal(item.get("lastPrice")),
            mark_price=_usable_decimal(item.get("markPrice")),
        )

    def candles(self, market_type: str, symbol: str, *, interval: str, end_ms: int, limit: int) -> list[Candle]:
        response = self._request(
            "get_kline", category=market_type, symbol=symbol, interval=interval, end=end_ms, limit=limit,
        )
        candles: list[Candle] = []
        for row in _list(response):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                candles.append(Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
            except (TypeError, ValueError):
                continue
        candles.sort(key=lambda item: item.open_time)
        return candles

    # ------------------------------------------------------------------ orders

    def place_order(self, market_type: str, symbol: str, order: NewOrder) -> PlacedOrder:
        payload: dict[str, Any] = {
            "category": market_type,
            "symbol": symbol,
            "side": order.side,
            "orderType": order.order_type,
            "qty": str(order.qty),
            "orderLinkId": order.client_tag,
        }
        if order.price is not None:
            payload["price"] = str(order.price)
        if order.reduce_only:
            payload["reduceOnly"] = True
        response = self._request("place_order", **payload)
        result = response.get("result", {}) or {}
        return PlacedOrder(
            order_id=result.get("orderId"),
            client_tag=result.get("orderLinkId") or order.client_tag,
            status=result.get("orderStatus"),
            raw=response,
            rejection=_rejection(response),
        )

    def cancel_order(self, market_type: str, symbol: str, *, order_id: str | None = None,
                     client_tag: str | None = None) -> dict[str, Any]:
        if order_id is not None:
            return self._request("cancel_order", category=market_type, symbol=symbol, orderId=order_id)
        return self._request("cancel_order", category=market_type, symbol=symbol, orderLinkId=client_tag)

    def open_orders(self, market_type: str, symbol: str) -> list[Order]:
        response = self._request("get_open_orders", category=market_type, symbol=symbol, openOnly=0)
        return [order_from_payload(item) for item in _list(response)]

    def order_history(self, market_type: str, symbol: str, *, limit: int) -> list[Order]:
        response = self._request("get_order_history", category=market_type, symbol=symbol, limit=limit)
        return [order_from_payload(item) for item in _list(response)]

    def _lookup_request(self, method: str, **params: Any) -> list:
        try:
            response = self._request(method, **params)
        except ExchangeError as exc:
            if exc.error_class is ErrorClass.UNKNOWN_ORDER:
                return []
            raise
        rejection = _rejection(response)
        if rejection is not None:
            if isinstance(rejection, ExchangeError) and rejection.error_class is ErrorClass.UNKNOWN_ORDER:
                return []
            raise rejection
        return _list(response)

    def lookup_order(self, market_type: str, symbol: str, client_tag: str) -> OrderLookup:
        """Find an order by client tag in open orders, then in order history (AC-08)."""
        items = self._lookup_request(
            "get_open_orders", category=market_type, symbol=symbol, orderLinkId=client_tag, openOnly=0,
        )
        if not items:
            items = self._lookup_request(
                "get_order_history", category=market_type, symbol=symbol, orderLinkId=client_tag, limit=1,
            )
        if not items:
            return OrderLookup.not_found()
        order = order_from_payload(items[0])
        return OrderLookup.open(order) if order.status in ACTIVE_ORDER_STATUSES else OrderLookup.final(order)

    # ------------------------------------------------------------------ account

    def executions(self, market_type: str, symbol: str, *, start_ms: int | None = None,
                   end_ms: int | None = None) -> list[Execution]:
        raw_items: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(EXECUTION_MAX_PAGES):
            params: dict[str, Any] = {"category": market_type, "symbol": symbol}
            if start_ms is not None:
                params["startTime"] = start_ms
            if end_ms is not None:
                params["endTime"] = end_ms
            params["limit"] = EXECUTION_PAGE_LIMIT
            if cursor:
                params["cursor"] = cursor
            result = self._request("get_executions", **params).get("result", {})
            raw_items.extend(result.get("list", []) or [])
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return [self._execution(item) for item in raw_items]

    @staticmethod
    def _execution(item: dict) -> Execution:
        def number(key: str) -> Decimal:
            return _decimal_or_none(item.get(key)) or Decimal(0)

        exec_time = item.get("execTime")
        exec_seq = item.get("execSeq")
        return Execution(
            exec_id=None if item.get("execId") is None else str(item.get("execId")),
            order_id=item.get("orderId"),
            client_tag=item.get("orderLinkId"),
            side=item.get("side"),
            qty=number("execQty"),
            price=number("execPrice"),
            fee=number("execFee"),
            realised_pnl=_decimal_or_none(item.get("closedPnl")),
            exec_time_ms=int(exec_time) if exec_time not in (None, "") else None,
            exec_seq=int(exec_seq) if exec_seq not in (None, "") else None,
        )

    def positions(self, market_type: str, symbol: str) -> list[Position]:
        positions = (
            position_from_payload(item)
            for item in _list(self._request("get_positions", category=market_type, symbol=symbol))
        )
        return [position for position in positions if position is not None]

    def balance(self) -> Balance:
        accounts = _list(self._request("get_wallet_balance", accountType="UNIFIED"))
        return balance_from_account(accounts[0]) if accounts else Balance()

    # ------------------------------------------------------------------ streams + connection

    def streams(self) -> Any:
        from app.bot_engine.exchange.bybit.streams import BybitStreams

        return BybitStreams(self.environment, account_key=self.account_key)

    def check_connection(self) -> ConnectionStatus:
        """An authenticated account request with the venue's keys (sad.md §6 Flow 8).

        No keys configured → `not_configured` without a request. The answer carries only the error
        class of a failure, never its raw message.
        """
        if self.environment != "emulator":
            from app.bot_engine.exchange.bybit.client import get_bybit_credentials

            api_key, api_secret = get_bybit_credentials(self.environment)
            if not api_key or not api_secret:
                return ConnectionStatus("not_configured")
            get_scrubber().add_key(api_key)
            get_scrubber().add_secret(api_secret)
        try:
            response = self._request("get_wallet_balance", accountType="UNIFIED")
        except ExchangeError as exc:
            return ConnectionStatus("failing", exc.error_class.value)
        except Exception:  # noqa: BLE001 - an unclassified failure still never leaks its text
            return ConnectionStatus("failing", ErrorClass.EXCHANGE_UNAVAILABLE.value)
        rejection = _rejection(response)
        if isinstance(rejection, UnclassifiedExchangeFailure):
            return ConnectionStatus("failing", ErrorClass.EXCHANGE_UNAVAILABLE.value)
        if rejection is not None:
            return ConnectionStatus("failing", rejection.error_class.value)
        return ConnectionStatus("connected")
