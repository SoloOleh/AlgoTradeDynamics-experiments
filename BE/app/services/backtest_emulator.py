"""Admin client used by the backend backtest runner."""
from __future__ import annotations

from typing import Any, Iterator

import httpx2 as httpx


class BacktestEmulatorClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    def datasets(self) -> list[dict]:
        return self._request("GET", "/api/admin/historical/datasets")

    def dataset(self, dataset_id: int) -> dict:
        return self._request("GET", f"/api/admin/historical/datasets/{dataset_id}")

    def range_stats(self, *, dataset_id: int, start_time: int, end_time: int) -> dict:
        return self._request(
            "GET", f"/api/admin/historical/datasets/{dataset_id}/range-stats",
            params={"start_time": start_time, "end_time": end_time},
        )

    def candle_count(self, *, dataset_id: int, start_time: int, end_time: int) -> int:
        payload = self._request(
            "GET", "/api/admin/historical/count",
            params={"dataset_id": dataset_id, "start_time": start_time, "end_time": end_time},
        )
        return int(payload.get("count", 0))

    def first_candle(self, *, dataset_id: int, start_time: int, end_time: int) -> dict | None:
        payload = self._request(
            "GET", "/api/admin/historical/candles",
            params={
                "dataset_id": dataset_id,
                "start_time": start_time,
                "end_time": end_time,
                "limit": 1,
            },
        )
        items = payload.get("items", [])
        return items[0] if items else None

    def candles(self, *, dataset_id: int, start_time: int, end_time: int) -> Iterator[dict]:
        after_time = None
        while True:
            params: dict[str, Any] = {
                "dataset_id": dataset_id,
                "start_time": start_time,
                "end_time": end_time,
                "limit": 5000,
            }
            if after_time is not None:
                params["after_time"] = after_time
            payload = self._request("GET", "/api/admin/historical/candles", params=params)
            items = payload.get("items", [])
            for item in items:
                yield item
            after_time = payload.get("next_after_time")
            if after_time is None or not items:
                break

    def create_account(
        self, name: str, initial_balance: float, *, fee_rate: float, slippage_percent: float
    ) -> dict:
        return self._request(
            "POST", "/api/admin/accounts", json={
                "name": name,
                "initial_balance": initial_balance,
                "maker_fee_rate": fee_rate,
                "taker_fee_rate": fee_rate,
                "slippage_percent": slippage_percent,
            }
        )

    def delete_account(self, account_id: int) -> None:
        self._request("DELETE", f"/api/admin/accounts/{account_id}")

    def reset_account(self, account_id: int, balance: float) -> dict:
        return self._request(
            "POST", f"/api/admin/accounts/{account_id}/reset", json={"balance": balance}
        )

    def set_price(
        self, account_id: int, symbol: str, price: float, simulation_time: int, *, dataset_id: int
    ) -> dict:
        return self._request(
            "POST",
            f"/api/admin/markets/{symbol}/price",
            json={
                "price": price,
                "mark_price": price,
                "simulation_time": simulation_time,
                "account_id": account_id,
                "dataset_id": dataset_id,
            },
        )

    def dashboard(self, account_id: int, symbol: str) -> dict:
        return self._request(
            "GET", "/api/admin/dashboard", params={"account_id": account_id, "symbol": symbol}
        )

    def orders(self, account_id: int, symbol: str, limit: int = 1000) -> list[dict]:
        return self._request(
            "GET", "/api/admin/orders", params={"account_id": account_id, "symbol": symbol, "limit": limit}
        )

    def executions(
        self,
        account_id: int,
        symbol: str,
        limit: int = 1000,
        *,
        after_sequence: int | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"account_id": account_id, "symbol": symbol, "limit": limit}
        if after_sequence is not None:
            params["after_sequence"] = after_sequence
        return self._request(
            "GET", "/api/admin/executions", params=params
        )


def emulator_instrument_rules(adapter, *, category: str, symbol: str, missing: str):
    """Instrument rules from the Bybit emulator venue's adapter (the admin plane stays above).

    `missing` keeps each engine's message for a symbol the emulator does not list.
    """
    from app.bot_engine.exchange.errors import ErrorClass, ExchangeError

    try:
        return adapter.instrument_rules(category, symbol)
    except ExchangeError as exc:
        if exc.error_class is ErrorClass.INVALID_SYMBOL:
            raise ValueError(missing) from exc
        raise


def emulator_venue():
    from app.bot_engine.exchange import registry

    return registry.adapter_for_venue("bybit", "emulator")
