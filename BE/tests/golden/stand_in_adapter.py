"""A stand-in exchange adapter for a test venue (AC-17, sad.md §6 Flow 10).

A separate implementation of `ExchangeAdapter` in its own code: its own HTTP client to the exchange
emulator's REST API, its own response parsing and its own failure classification. It declares the
same capabilities as the Bybit adapter. Strategies receive it through the guard like any adapter.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.bybit.capabilities import BYBIT_CAPABILITIES
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
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

STAND_IN_VENUE = ("standin", "emulator")

# The emulator's refusal codes this stand-in understands; anything else is an invalid order.
_REFUSALS = {
    "10006": ErrorClass.RATE_LIMITED,
    "110001": ErrorClass.UNKNOWN_ORDER,
    "110007": ErrorClass.INSUFFICIENT_BALANCE,
    "110072": ErrorClass.DUPLICATE_CLIENT_TAG,
}


def _number(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except InvalidOperation:
        return None


def _usable_decimal(value: Any) -> Decimal | None:
    """A price or leverage as the Bybit adapter reads it: "0" stays Decimal("0"); an absent, empty,
    non-numeric or non-finite text is None."""
    number = _decimal(value.strip() if isinstance(value, str) else value)
    return number if number is not None and number.is_finite() else None


class StandInAdapter:
    requests: list[tuple[str, str]] = []
    failure: ExchangeError | None = None

    def __init__(self, account_key: str | None) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self._key = account_key or settings.exchange_emulator_default_api_key
        self._http = httpx2.Client(base_url=settings.exchange_emulator_url.rstrip("/"), timeout=15.0)

    @classmethod
    def factory(cls, account: registry.Account) -> "StandInAdapter":
        return cls(account.emulator_api_key)

    # ------------------------------------------------------------------ transport

    def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        StandInAdapter.requests.append((method, path))
        if StandInAdapter.failure is not None:
            raise StandInAdapter.failure
        try:
            response = self._http.request(method, path, headers={"X-API-Key": self._key}, **kwargs)
        except httpx2.TimeoutException as exc:
            raise ExchangeError(ErrorClass.NETWORK, message="stand-in request timed out") from exc
        except httpx2.HTTPError as exc:
            raise ExchangeError(ErrorClass.EXCHANGE_UNAVAILABLE, message="stand-in request failed") from exc
        if response.status_code >= 500:
            raise ExchangeError(ErrorClass.EXCHANGE_UNAVAILABLE, message=f"stand-in HTTP {response.status_code}")
        return response.json()

    def _get(self, path: str, **params: Any) -> list:
        body = self._call("GET", path, params=params)
        refusal = self._refusal(body)
        if refusal is not None:
            raise refusal
        return (body.get("result") or {}).get("list") or []

    @staticmethod
    def _refusal(body: dict) -> ExchangeError | None:
        code = str(body.get("retCode", 0))
        if code == "0":
            return None
        return ExchangeError(_REFUSALS.get(code, ErrorClass.INVALID_ORDER), raw_code=code, message="stand-in refusal")

    @staticmethod
    def _order(item: dict) -> Order:
        return Order(
            order_id=item.get("orderId"),
            client_tag=item.get("orderLinkId"),
            status=item.get("orderStatus"),
            side=item.get("side"),
            order_type=item.get("orderType"),
            qty=_number(item.get("qty")),
            filled_qty=_number(item.get("cumExecQty")),
            leaves_qty=_number(item.get("leavesQty")),
            price=_number(item.get("price")) or None,
            avg_price=_number(item.get("avgPrice")) or None,
            reduce_only=bool(item.get("reduceOnly")),
            raw=dict(item),
        )

    # ------------------------------------------------------------------ identity

    def venue(self) -> Venue:
        return Venue(*STAND_IN_VENUE)

    def capabilities(self):
        return BYBIT_CAPABILITIES

    # ------------------------------------------------------------------ market data

    def instrument_rules(self, market_type: str, symbol: str) -> InstrumentRules:
        items = self._get("/v5/market/instruments-info", category=market_type, symbol=symbol)
        if not items:
            raise ExchangeError(ErrorClass.INVALID_SYMBOL, message=f"stand-in has no instrument {symbol}")
        lot, price = items[0].get("lotSizeFilter") or {}, items[0].get("priceFilter") or {}
        return InstrumentRules(
            min_order_qty=_number(lot.get("minOrderQty")) or 0.0,
            qty_step=_number(lot.get("qtyStep")) or 0.000001,
            tick_size=_number(price.get("tickSize")) or 0.000001,
            min_notional_value=_number(lot.get("minNotionalValue")) or 0.0,
        )

    def ticker(self, market_type: str, symbol: str) -> Ticker:
        items = self._get("/v5/market/tickers", category=market_type, symbol=symbol)
        if not items:
            raise ExchangeError(ErrorClass.INVALID_SYMBOL, message=f"stand-in has no ticker {symbol}")
        return Ticker(symbol, _usable_decimal(items[0].get("lastPrice")), _usable_decimal(items[0].get("markPrice")))

    def candles(self, market_type: str, symbol: str, *, interval: str, end_ms: int, limit: int) -> list[Candle]:
        rows = self._get("/v5/market/kline", category=market_type, symbol=symbol, interval=interval, end=end_ms, limit=limit)
        candles = [
            Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
            for row in rows if isinstance(row, (list, tuple)) and len(row) >= 6
        ]
        return sorted(candles, key=lambda candle: candle.open_time)

    # ------------------------------------------------------------------ orders

    def place_order(self, market_type: str, symbol: str, order: NewOrder) -> PlacedOrder:
        body: dict[str, Any] = {
            "category": market_type, "symbol": symbol, "side": order.side, "orderType": order.order_type,
            "qty": str(order.qty), "orderLinkId": order.client_tag,
        }
        if order.price is not None:
            body["price"] = str(order.price)
        if order.reduce_only:
            body["reduceOnly"] = True
        answer = self._call("POST", "/v5/order/create", json=body)
        result = answer.get("result") or {}
        return PlacedOrder(
            order_id=result.get("orderId"),
            client_tag=result.get("orderLinkId") or order.client_tag,
            status=result.get("orderStatus"),
            raw=answer,
            rejection=self._refusal(answer),
        )

    def cancel_order(self, market_type: str, symbol: str, *, order_id: str | None = None,
                     client_tag: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"category": market_type, "symbol": symbol}
        if order_id is not None:
            body["orderId"] = order_id
        else:
            body["orderLinkId"] = client_tag
        return self._call("POST", "/v5/order/cancel", json=body)

    def open_orders(self, market_type: str, symbol: str) -> list[Order]:
        return [self._order(item) for item in self._get("/v5/order/realtime", category=market_type, symbol=symbol, openOnly=0)]

    def order_history(self, market_type: str, symbol: str, *, limit: int) -> list[Order]:
        return [self._order(item) for item in self._get("/v5/order/history", category=market_type, symbol=symbol, limit=limit)]

    def lookup_order(self, market_type: str, symbol: str, client_tag: str) -> OrderLookup:
        for path in ("/v5/order/realtime", "/v5/order/history"):
            try:
                items = self._get(path, category=market_type, symbol=symbol, orderLinkId=client_tag)
            except ExchangeError as exc:
                if exc.error_class is not ErrorClass.UNKNOWN_ORDER:
                    raise
                items = []
            if items:
                order = self._order(items[0])
                return OrderLookup.open(order) if order.status in ACTIVE_ORDER_STATUSES else OrderLookup.final(order)
        return OrderLookup.not_found()

    # ------------------------------------------------------------------ account

    def executions(self, market_type: str, symbol: str, *, start_ms: int | None = None,
                   end_ms: int | None = None) -> list[Execution]:
        found: list[Execution] = []
        cursor, seen = None, set()
        while True:
            params: dict[str, Any] = {"category": market_type, "symbol": symbol, "limit": 100}
            if start_ms is not None:
                params["startTime"] = start_ms
            if end_ms is not None:
                params["endTime"] = end_ms
            if cursor:
                params["cursor"] = cursor
            body = self._call("GET", "/v5/execution/list", params=params)
            result = body.get("result") or {}
            for item in result.get("list") or []:
                found.append(Execution(
                    exec_id=item.get("execId"), order_id=item.get("orderId"), client_tag=item.get("orderLinkId"),
                    side=item.get("side"), qty=_decimal(item.get("execQty")) or Decimal(0),
                    price=_decimal(item.get("execPrice")) or Decimal(0), fee=_decimal(item.get("execFee")) or Decimal(0),
                    realised_pnl=_decimal(item.get("closedPnl")),
                    exec_time_ms=int(item["execTime"]) if item.get("execTime") else None,
                    exec_seq=int(item["execSeq"]) if item.get("execSeq") not in (None, "") else None,
                ))
            cursor = result.get("nextPageCursor")
            if not cursor or cursor in seen:
                return found
            seen.add(cursor)

    def positions(self, market_type: str, symbol: str) -> list[Position]:
        positions = []
        for item in self._get("/v5/position/list", category=market_type, symbol=symbol):
            size = _number(item.get("size"))
            if not size:
                continue
            positions.append(Position(
                symbol=item.get("symbol"), side=str(item.get("side") or ""), size=size,
                avg_entry_price=_number(item.get("avgPrice")) or None, mark_price=_usable_decimal(item.get("markPrice")),
                liq_price=_usable_decimal(item.get("liqPrice")), leverage=_usable_decimal(item.get("leverage")),
                margin_mode=item.get("tradeMode") or item.get("marginMode"),
                position_value=_number(item.get("positionValue")),
                unrealised_pnl=_number(item.get("unrealisedPnl")),
            ))
        return positions

    def balance(self) -> Balance:
        accounts = self._get("/v5/account/wallet-balance", accountType="UNIFIED")
        if not accounts:
            return Balance()
        account = accounts[0]
        coin = next((item for item in account.get("coin") or [] if str(item.get("coin")).upper() == "USDT"), {})
        return Balance(
            total_available=_number(account.get("totalAvailableBalance")) or None,
            total_equity=_number(account.get("totalEquity")) or None,
            total_wallet=_number(account.get("totalWalletBalance")) or None,
            wallet=_number(account.get("walletBalance")) or None,
            coin_available=_number(coin.get("availableToWithdraw")) or None,
            coin_equity=_number(coin.get("equity")) or None,
            coin_wallet=_number(coin.get("walletBalance")) or None,
        )

    # ------------------------------------------------------------------ streams + connection

    def streams(self) -> Any:
        raise NotImplementedError("the stand-in has no live streams; its bots run on worker ticks")

    def check_connection(self) -> ConnectionStatus:
        try:
            self.balance()
        except ExchangeError as exc:
            return ConnectionStatus("failing", exc.error_class.value)
        return ConnectionStatus("connected")


@contextmanager
def stand_in_venue():
    with registry.registered_venue(*STAND_IN_VENUE, StandInAdapter.factory):
        yield
