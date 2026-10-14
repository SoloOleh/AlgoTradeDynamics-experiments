"""In-process background worker for continuously running bots."""
from __future__ import annotations

import asyncio
import logging

from app.bot_engine.bot import tick_bot_once
from app.bot_engine.error_handling import clear_bot_runtime_error, handle_bot_runtime_error, pause_bot_for_guard
from app.bot_engine.exchange import registry
from app.bot_engine.exchange.errors import GuardRefusal
from app.bot_engine.exchange_streams import ExchangeStreamSupervisor
from app.core.clock import utcnow
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot
from app.services.ui_stream_service import bot_ui_stream_hub

WORKER_STREAM_CHECK_SECONDS = 2
WORKER_FALLBACK_SECONDS = 15

logger = logging.getLogger(__name__)


async def worker_loop(app) -> None:
    supervisor = ExchangeStreamSupervisor()
    app.state.exchange_stream_supervisor = supervisor
    try:
        await supervisor.reconcile_if_due(force=True)
        last_fallback = asyncio.get_running_loop().time()
        while getattr(app.state, "bot_worker_running", False):
            await supervisor.reconcile_if_due()
            bot_ids = await supervisor.wait_for_bot_ids(WORKER_STREAM_CHECK_SECONDS)
            if bot_ids is not None:
                force_bot_ids = supervisor.consume_force_tick_ids(bot_ids)
                await _tick_safely(app, bot_ids=bot_ids, force_bot_ids=force_bot_ids)
                continue
            now = asyncio.get_running_loop().time()
            if now - last_fallback >= WORKER_FALLBACK_SECONDS:
                # Slow fail-safe reconciliation if a WS feed is unavailable.
                await _tick_safely(app)
                last_fallback = now
    finally:
        await supervisor.stop()
        app.state.exchange_stream_supervisor = None


async def _tick_safely(app, **kwargs) -> None:
    # One failed pass must never end the worker: every other bot would stop trading.
    try:
        await tick_running_bots(app, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("Bot worker tick failed; continuing with the next pass")


def _retry_is_due(bot: TradingBot) -> bool:
    return bot.next_retry_at is None or bot.next_retry_at <= utcnow()


async def tick_running_bots(
    app,
    *,
    bot_ids: set[int] | None = None,
    force_bot_ids: set[int] | None = None,
) -> None:
    db = SessionLocal()
    try:
        query = db.query(TradingBot).filter(
            TradingBot.runtime_status.in_(("running", "retrying")),
            TradingBot.is_backtest.is_(False),
        )
        if bot_ids is not None:
            if not bot_ids:
                return
            query = query.filter(TradingBot.id.in_(bot_ids))
        bots = query.all()
        for bot in bots:
            if bot.runtime_status == "retrying" and not _retry_is_due(bot):
                continue
            lock = app.state.bot_worker_locks.setdefault(bot.id, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                bot_db = SessionLocal()
                try:
                    current_bot = bot_db.get(TradingBot, bot.id)
                    if current_bot is None or current_bot.runtime_status not in {"running", "retrying"}:
                        continue
                    if current_bot.runtime_status == "retrying" and not _retry_is_due(current_bot):
                        continue
                    # Fail closed before any request: unknown venue or missing capability
                    # pauses the bot and leaves its orders as they are (ADR-0004).
                    try:
                        adapter = registry.exchange_for(current_bot)
                    except GuardRefusal as refusal:
                        pause_bot_for_guard(bot_db, current_bot, refusal)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        # E.g. invalid strategy settings while reading its needs: this bot alone.
                        bot_db.rollback()
                        failed_bot = bot_db.get(TradingBot, bot.id)
                        if failed_bot is not None:
                            handle_bot_runtime_error(bot_db, failed_bot, exc, source="worker")
                        continue
                    if current_bot.runtime_status == "retrying":
                        if not _retry_is_due(current_bot):
                            continue
                        # Strategy ticks intentionally execute only in running state.
                        # We do not commit this transition until the tick succeeds.
                        current_bot.runtime_status = "running"
                        bot_db.add(current_bot)
                        bot_db.flush()
                    try:
                        tick_bot_once(
                            bot_db,
                            current_bot,
                            force=bool(force_bot_ids and bot.id in force_bot_ids),
                            adapter=adapter,
                        )
                        # A successful tick proves the transient problem is gone.
                        current_bot = bot_db.get(TradingBot, bot.id)
                        if current_bot is not None and current_bot.runtime_status == "running":
                            had_error = bool(current_bot.last_error_type)
                            clear_bot_runtime_error(current_bot)
                            if had_error:
                                from app.services.operations_service import record_operation_log, resolve_bot_incidents
                                resolved = resolve_bot_incidents(
                                    bot_db, current_bot,
                                    resolution="Bot completed a successful worker tick after recovery",
                                )
                                if resolved:
                                    record_operation_log(
                                        bot_db, level="INFO", service="BOT_WORKER",
                                        message=f"Bot recovered; resolved {resolved} incident(s)",
                                        correlation_id=f"bot:{current_bot.id}", user_id=current_bot.user_id,
                                        bot_id=current_bot.id, exchange=current_bot.exchange,
                                    )
                            bot_db.add(current_bot)
                            bot_db.commit()
                    except Exception as exc:  # noqa: BLE001
                        # Discard partially staged local changes before deciding whether
                        # this failure is safe to retry. Exchange state is reconciled on
                        # the next strategy tick when RESYNC_AND_RETRY is selected.
                        bot_db.rollback()
                        failed_bot = bot_db.get(TradingBot, bot.id)
                        if failed_bot is not None:
                            if isinstance(exc, GuardRefusal):
                                pause_bot_for_guard(bot_db, failed_bot, exc)
                            else:
                                handle_bot_runtime_error(bot_db, failed_bot, exc, source="worker")
                finally:
                    bot_ui_stream_hub.publish(bot.id, "worker.tick")
                    bot_db.close()
    finally:
        db.close()


def start_bot_worker(app) -> None:
    if getattr(app.state, "bot_worker_task", None) is not None:
        return
    app.state.bot_worker_running = True
    app.state.bot_worker_locks = {}
    app.state.bot_worker_task = asyncio.create_task(worker_loop(app))


async def stop_bot_worker(app) -> None:
    task = getattr(app.state, "bot_worker_task", None)
    app.state.bot_worker_running = False
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        app.state.bot_worker_task = None
