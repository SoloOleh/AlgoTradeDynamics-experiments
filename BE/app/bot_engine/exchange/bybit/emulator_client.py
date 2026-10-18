"""Small Bybit-compatible HTTP client for the local exchange emulator."""
from __future__ import annotations

from typing import Any

import httpx2 as httpx


class EmulatorHTTP:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-API-Key"] = self.api_key
        response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_tickers(self, **params: Any) -> dict:
        return self._request("GET", "/v5/market/tickers", params=params)

    def get_instruments_info(self, **params: Any) -> dict:
        return self._request("GET", "/v5/market/instruments-info", params=params)

    def place_order(self, **payload: Any) -> dict:
        return self._request("POST", "/v5/order/create", json=payload)

    def cancel_order(self, **payload: Any) -> dict:
        return self._request("POST", "/v5/order/cancel", json=payload)

    def get_open_orders(self, **params: Any) -> dict:
        return self._request("GET", "/v5/order/realtime", params=params)

    def get_order_history(self, **params: Any) -> dict:
        return self._request("GET", "/v5/order/history", params=params)

    def get_positions(self, **params: Any) -> dict:
        return self._request("GET", "/v5/position/list", params=params)

    def set_leverage(self, **payload: Any) -> dict:
        return self._request("POST", "/v5/position/set-leverage", json=payload)

    def set_trading_stop(self, **payload: Any) -> dict:
        return self._request("POST", "/v5/position/trading-stop", json=payload)

    def get_wallet_balance(self, **params: Any) -> dict:
        return self._request("GET", "/v5/account/wallet-balance", params=params)

    def get_executions(self, **params: Any) -> dict:
        return self._request("GET", "/v5/execution/list", params=params)

    def get_kline(self, **params: Any) -> dict:
        return self._request("GET", "/v5/market/kline", params=params)
