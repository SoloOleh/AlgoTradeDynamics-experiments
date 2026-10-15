"""Bybit and emulator live streams, behind the Bybit adapter (ADR-0001, ADR-0008).

Only this module opens exchange WebSocket connections. It owns the endpoints, the private-stream
authentication, the subscriptions and the translation of Bybit topics into neutral `StreamEvent`s.
Stream addresses can carry the emulator account key (`?api_key=…`), so every connection error is
scrubbed before it is logged or reported.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import websockets

from app.bot_engine.exchange.bybit.adapter import (
    _usable_decimal,
    balance_from_account,
    order_from_payload,
    position_from_payload,
    BybitAdapter,
)
from app.bot_engine.exchange.bybit.client import get_bybit_credentials
from app.bot_engine.exchange.port import StreamEvent, Ticker
from app.bot_engine.exchange.redaction import get_scrubber, scrub

logger = logging.getLogger("app.bot_engine.exchange_streams")

PRIVATE_TOPICS = ("order", "execution", "position", "wallet")
RECEIVE_TIMEOUT_S = 20
AUTH_TIMEOUT_S = 10


@dataclass(frozen=True, slots=True)
class StreamSource:
    """One WS connection: `public` (tickers) or `private` (orders, fills, positions, wallet)."""

    label: str
    url: str = field(repr=False)
    topics: tuple[str, ...]
    authenticate: bool


def _http_to_ws(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def auth_payload(api_key: str, api_secret: str) -> dict[str, Any]:
    expires = int(time.time() * 1000) + 10_000
    raw = f"GET/realtime{expires}"
    signature = hmac.new(api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {"op": "auth", "args": [api_key, expires, signature]}


def _items(data: Any) -> list[dict]:
    return [item for item in (data if isinstance(data, list) else [data]) if isinstance(item, dict)]


class BybitStreams:
    """The live streams of one Bybit venue bound to one exchange account."""

    def __init__(self, environment: str, *, account_key: str | None = None) -> None:
        self.environment = environment
        self._account_key = account_key

    # ------------------------------------------------------------------ endpoints

    def credentials(self) -> tuple[str, str]:
        """Server keys for the private stream of demo, testnet or live (registered with the scrubber)."""
        api_key, api_secret = get_bybit_credentials(self.environment)
        if not api_key or not api_secret:
            raise ValueError(f"Bybit {self.environment} API credentials are not configured")
        get_scrubber().add_key(api_key)
        get_scrubber().add_secret(api_secret)
        return api_key, api_secret

    def sources(self, market_type: str, symbol: str, *, private: bool = True) -> list[StreamSource]:
        symbol = symbol.upper()
        public_topics = (f"tickers.{symbol}",)
        if self.environment == "emulator":
            from app.core.config import get_settings

            base = _http_to_ws(get_settings().exchange_emulator_url)
            query = urlencode({"api_key": self._account_key})
            public = StreamSource("public", f"{base}/v5/public/{market_type}?{query}", public_topics, False)
            private_source = StreamSource("private", f"{base}/v5/private?{query}", PRIVATE_TOPICS, False)
        else:
            self.credentials()
            if self.environment == "testnet":
                public_host = private_host = "wss://stream-testnet.bybit.com"
            elif self.environment == "demo":
                # Demo has a private demo stream; public prices come from mainnet.
                public_host, private_host = "wss://stream.bybit.com", "wss://stream-demo.bybit.com"
            else:
                public_host = private_host = "wss://stream.bybit.com"
            public = StreamSource("public", f"{public_host}/v5/public/{market_type}", public_topics, False)
            private_source = StreamSource("private", f"{private_host}/v5/private", PRIVATE_TOPICS, True)
        return [public, private_source] if private else [public]

    def signature(self, market_type: str, symbol: str) -> tuple[Any, ...]:
        """What, when changed, needs the bot's streams reopened. Holds no key in clear."""
        sources = self.sources(market_type, symbol)
        digest = hashlib.sha256("|".join(source.url for source in sources).encode()).hexdigest()
        account = hashlib.sha256(str(self._account_key or self.credentials()[0]).encode()).hexdigest()
        return (self.environment, market_type, symbol.upper(), digest, account)

    async def emulator_account_key(self, account_id: int) -> str | None:
        """The key of an emulator account, from the emulator's admin plane (emulator UI only)."""
        import httpx2 as httpx

        from app.core.config import get_settings

        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{get_settings().exchange_emulator_url.rstrip('/')}/api/admin/accounts")
            response.raise_for_status()
            for account in response.json():
                if int(account.get("id") or 0) == account_id:
                    key = str(account.get("api_key") or "") or None
                    get_scrubber().add_key(key)
                    return key
        return None

    # ------------------------------------------------------------------ translation

    def translate(self, message: dict[str, Any], symbol: str) -> StreamEvent | None:
        """A Bybit WS message as a neutral event for `symbol`, or `None` when it is not one."""
        topic = str(message.get("topic") or "")
        if not topic:
            return None
        symbol = symbol.upper()
        data = message.get("data")
        if topic.startswith("tickers."):
            if topic != f"tickers.{symbol}":
                return None
            item = (_items(data) or [{}])[0]
            ticker = Ticker(
                symbol=str(item.get("symbol") or symbol),
                last_price=_usable_decimal(item.get("lastPrice")),
                mark_price=_usable_decimal(item.get("markPrice")),
            )
            return StreamEvent("ticker", symbol, {"ticker": ticker})
        if topic == "wallet":
            accounts = _items(data)
            balance = balance_from_account(accounts[0]) if accounts else None
            return StreamEvent("wallet", symbol, {"balance": balance})
        if topic not in PRIVATE_TOPICS:
            return None
        items = [item for item in _items(data) if str(item.get("symbol") or "").upper() in ("", symbol)]
        if not items:
            return None
        if topic == "order":
            # The raw payload stays here: a stream event is never stored as is (port.py).
            orders = [replace(order_from_payload(item), raw={}) for item in items]
            return StreamEvent("order", symbol, {"orders": orders})
        if topic == "execution":
            return StreamEvent("execution", symbol, {"executions": [BybitAdapter._execution(item) for item in items]})
        positions = [position_from_payload(item) for item in items]
        return StreamEvent("position", symbol, {"positions": [item for item in positions if item is not None]})

    # ------------------------------------------------------------------ connections

    async def _open(self, websocket, source: StreamSource) -> None:
        if source.authenticate:
            api_key, api_secret = self.credentials()
            await websocket.send(json.dumps(auth_payload(api_key, api_secret)))
            # Bybit sends an auth acknowledgement before subscriptions.
            acknowledgement = json.loads(await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_S))
            if not bool(acknowledgement.get("success")):
                raise RuntimeError(f"Bybit WebSocket auth failed: {acknowledgement}")
        await websocket.send(json.dumps({"op": "subscribe", "args": list(source.topics)}))

    async def _connect_forever(
        self,
        source: StreamSource,
        *,
        on_open: Callable[[], Awaitable[None]],
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_error: Callable[[str, float], Awaitable[None]],
        keepalive: bool,
        max_delay: float,
    ) -> None:
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    source.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=256,
                ) as websocket:
                    await self._open(websocket, source)
                    delay = 1.0
                    await on_open()
                    while True:
                        if keepalive:
                            try:
                                raw = await asyncio.wait_for(websocket.recv(), timeout=RECEIVE_TIMEOUT_S)
                            except asyncio.TimeoutError:
                                await websocket.send(json.dumps({"op": "ping"}))
                                continue
                        else:
                            raw = await websocket.recv()
                        message = json.loads(raw)
                        if isinstance(message, dict):
                            await on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect after any connection failure
                await on_error(str(scrub(str(exc))), delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def run(
        self,
        source: StreamSource,
        symbol: str,
        *,
        on_event: Callable[[StreamEvent], Awaitable[None]],
        on_connected: Callable[[str], Awaitable[None]],
        context: str = "",
    ) -> None:
        """Keep one bot stream open until cancelled, delivering neutral events for `symbol`."""

        async def opened() -> None:
            logger.info("Exchange WS connected: %s venue=bybit/%s stream=%s", context, self.environment, source.label)
            await on_connected(source.label)

        async def message(raw: dict[str, Any]) -> None:
            event = self.translate(raw, symbol)
            if event is not None:
                await on_event(event)

        async def failed(error: str, delay: float) -> None:
            logger.warning(
                "Exchange WS disconnected: %s venue=bybit/%s stream=%s error=%s; retrying in %.1fs",
                context, self.environment, source.label, error, delay,
            )

        await self._connect_forever(
            source, on_open=opened, on_message=message, on_error=failed, keepalive=True, max_delay=15.0,
        )

    async def relay(
        self,
        source: StreamSource,
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_status: Callable[[str, str | None], Awaitable[None]],
    ) -> None:
        """Forward every topic message of one stream as received (the emulator UI's live view)."""

        async def opened() -> None:
            await on_status("live", None)

        async def message(raw: dict[str, Any]) -> None:
            if raw.get("topic"):
                await on_message(raw)

        async def failed(error: str, delay: float) -> None:
            await on_status("reconnecting", error)

        await self._connect_forever(
            source, on_open=opened, on_message=message, on_error=failed, keepalive=False, max_delay=10.0,
        )
