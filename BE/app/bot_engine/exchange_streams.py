"""Real-time exchange streams for running bots, exchange-neutral.

Each bot's venue adapter (through the guard) owns its streams: endpoints, authentication and the
translation of exchange topics into neutral events (ADR-0001). The runtime sees the same topics as
before:
  - tickers.{SYMBOL}
  - order
  - execution
  - position
  - wallet

REST remains the source of truth for commands and periodic reconciliation. WS messages wake the
strategy worker immediately instead of waiting for the fixed polling cadence.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.capabilities import Capability
from app.bot_engine.exchange.port import StreamEvent
from app.bot_engine.exchange.redaction import scrub
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot

logger = logging.getLogger(__name__)

PRIVATE_TOPICS = ("order", "execution", "position", "wallet")
FORCE_RECONCILE_TOPICS = {"order", "execution", "position"}


@dataclass(slots=True)
class ExchangeStreamEvent:
    bot_id: int
    topic: str
    data: Any
    received_at: float


def _envelope_topic(event: StreamEvent) -> str:
    return f"tickers.{event.symbol}" if event.kind == "ticker" else event.kind


def _wants_private_stream(adapter, bot: TradingBot) -> bool:
    return adapter.capabilities().supports(Capability.PRIVATE_EVENT_STREAM, bot.category)


def stream_signature(bot: TradingBot, adapter) -> tuple[Any, ...]:
    return (
        bot.exchange,
        bot.environment,
        _wants_private_stream(adapter, bot),
        adapter.streams().signature(bot.category, bot.symbol),
    )


class BotExchangeStream:
    def __init__(
        self,
        bot: TradingBot,
        adapter,
        on_event: Callable[[ExchangeStreamEvent], Awaitable[None]],
    ) -> None:
        self.bot_id = bot.id
        self.symbol = bot.symbol.upper()
        self.streams = adapter.streams()
        self.sources = self.streams.sources(bot.category, self.symbol, private=_wants_private_stream(adapter, bot))
        self._on_event = on_event
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(
                self.streams.run(
                    source,
                    self.symbol,
                    on_event=self._emit,
                    on_connected=self._connected,
                    context=f"bot={self.bot_id}",
                ),
                name=f"exchange-ws-{source.label}-{self.bot_id}",
            )
            for source in self.sources
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _emit(self, event: StreamEvent) -> None:
        await self._on_event(ExchangeStreamEvent(
            bot_id=self.bot_id,
            topic=_envelope_topic(event),
            data=event,
            received_at=time.time(),
        ))

    async def _connected(self, label: str) -> None:
        await self._on_event(ExchangeStreamEvent(
            bot_id=self.bot_id,
            topic="system.connected",
            data={"stream": label},
            received_at=time.time(),
        ))


class ExchangeStreamSupervisor:
    """Own per-bot streams and expose a deduplicated wake-up queue to the worker."""

    def __init__(self) -> None:
        self._streams: dict[int, BotExchangeStream] = {}
        self._signatures: dict[int, tuple[Any, ...]] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1000)
        self._queued: set[int] = set()
        self._last_ticker_wake: dict[int, float] = {}
        self._ticker_intervals: dict[int, float] = {}
        self._force_tick: set[int] = set()
        self._last_reconcile = 0.0

    async def stop(self) -> None:
        streams = list(self._streams.values())
        self._streams.clear()
        self._signatures.clear()
        self._force_tick.clear()
        if streams:
            await asyncio.gather(*(stream.stop() for stream in streams), return_exceptions=True)

    def _enqueue(self, bot_id: int) -> None:
        if bot_id in self._queued:
            return
        self._queued.add(bot_id)
        try:
            self._queue.put_nowait(bot_id)
        except asyncio.QueueFull:
            self._queued.discard(bot_id)

    async def on_event(self, event: ExchangeStreamEvent) -> None:
        if event.topic in FORCE_RECONCILE_TOPICS:
            self._force_tick.add(event.bot_id)
        if event.topic.startswith("tickers."):
            now = time.monotonic()
            interval = self._ticker_intervals.get(event.bot_id, 5.0)
            previous = self._last_ticker_wake.get(event.bot_id, 0.0)
            if now - previous < interval:
                return
            self._last_ticker_wake[event.bot_id] = now
        self._enqueue(event.bot_id)

    def consume_force_tick_ids(self, bot_ids: set[int]) -> set[int]:
        forced = bot_ids & self._force_tick
        self._force_tick.difference_update(forced)
        return forced

    async def reconcile_if_due(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_reconcile < 2.0:
            return
        self._last_reconcile = now
        db = SessionLocal()
        try:
            bots = db.query(TradingBot).filter(
                TradingBot.runtime_status.in_(("running", "retrying")),
                TradingBot.is_backtest.is_(False),
            ).all()
            active = {bot.id: bot for bot in bots}
            for bot in bots:
                run_interval = float((bot.settings or {}).get(
                    "run_interval_seconds",
                    5 if bot.strategy_type == "pattern_scalper" else 10,
                ))
                self._ticker_intervals[bot.id] = max(min(run_interval, 10.0), 0.5)
                try:
                    # The guard refuses an unknown venue or a missing capability in memory; the
                    # bot's next tick then pauses it (ADR-0004). No stream is opened meanwhile.
                    adapter = registry.exchange_for(bot)
                    signature = stream_signature(bot, adapter)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Cannot configure Exchange WS for bot=%s: %s", bot.id, scrub(str(exc)))
                    old = self._streams.pop(bot.id, None)
                    self._signatures.pop(bot.id, None)
                    if old is not None:
                        await old.stop()
                    continue
                if self._signatures.get(bot.id) == signature and bot.id in self._streams:
                    continue
                old = self._streams.pop(bot.id, None)
                if old is not None:
                    await old.stop()
                stream = BotExchangeStream(bot, adapter, self.on_event)
                self._streams[bot.id] = stream
                self._signatures[bot.id] = signature
                stream.start()
                self._enqueue(bot.id)  # initial reconciliation immediately after start

            stale = [bot_id for bot_id in self._streams if bot_id not in active]
            for bot_id in stale:
                stream = self._streams.pop(bot_id)
                self._signatures.pop(bot_id, None)
                self._ticker_intervals.pop(bot_id, None)
                self._last_ticker_wake.pop(bot_id, None)
                self._force_tick.discard(bot_id)
                await stream.stop()
        finally:
            db.close()

    async def wait_for_bot_ids(self, timeout: float) -> set[int] | None:
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        ids = {first}
        while True:
            try:
                ids.add(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for bot_id in ids:
            self._queued.discard(bot_id)
        return ids
