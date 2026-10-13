"""Strategy-agnostic bot runtime orchestration."""
from __future__ import annotations

from datetime import datetime, timezone

from app.bot_engine.events import log_bot_event
from app.bot_engine.exchange import registry
from app.bot_engine.error_handling import clear_bot_runtime_error
from app.bot_engine.strategies import get_strategy
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder
from app.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_bot_once(db, bot: TradingBot, current_user: User) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    if not bot.is_active:
        raise ValueError("Trading bot is inactive")
    strategy = get_strategy(bot.strategy_type)
    # Fail closed before anything else: unknown venue or missing capability (ADR-0004).
    registry.exchange_for(bot)
    strategy.validate_start(db, bot)
    bot.runtime_status = "running"
    bot.started_at = bot.started_at or _utcnow()
    bot.stopped_at = None
    had_error = bool(bot.last_error_type)
    clear_bot_runtime_error(bot)
    if had_error:
        from app.services.operations_service import record_operation_log, resolve_bot_incidents
        resolved = resolve_bot_incidents(
            db, bot, resolution="User restarted the bot after intervention; runtime resumed",
        )
        if resolved:
            record_operation_log(
                db, level="INFO", service="BOT_WORKER",
                message=f"User restart resolved {resolved} incident(s); runtime resumed",
                correlation_id=f"bot:{bot.id}", user_id=bot.user_id, bot_id=bot.id, exchange=bot.exchange,
            )
    db.add(bot)
    log_bot_event(db, bot, "bot_started", f"{bot.strategy_type} bot marked as running")
    db.commit(); db.refresh(bot)
    return {"bot": bot, "orders": [], "action": "STARTED", "message": "Bot worker started"}


def stop_bot_once(db, bot: TradingBot, current_user: User, *, cancel_open: bool = True) -> TradingBot:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    strategy = get_strategy(bot.strategy_type)
    adapter = registry.exchange_for(bot)
    if bool(strategy.get_effective_settings(bot).get("close_position_on_stop", False)):
        strategy.close_position(db, bot, adapter=adapter)
    if cancel_open and bool(strategy.get_effective_settings(bot).get("cancel_orders_on_stop", True)):
        strategy.cancel_orders(db, bot, adapter=adapter)
    bot.runtime_status = "stopped"; bot.stopped_at = _utcnow(); clear_bot_runtime_error(bot)
    db.add(bot); log_bot_event(db, bot, "bot_stopped", "Bot stopped"); db.commit(); db.refresh(bot)
    return bot


def sync_bot_orders_once(db, bot: TradingBot, current_user: User) -> list[TradingBotOrder]:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    adapter = registry.exchange_for(bot)
    orders = get_strategy(bot.strategy_type).sync_orders(db, bot, adapter=adapter)
    bot.last_run_at = _utcnow(); bot.last_error = None; db.add(bot); db.commit()
    return orders


def tick_bot_once(db, bot: TradingBot, *, force: bool = False, adapter=None) -> dict:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    return get_strategy(bot.strategy_type).tick(db, bot, force=force, adapter=adapter)


run_grid_bot_once = run_bot_once
stop_grid_bot_once = stop_bot_once
sync_grid_bot_orders = sync_bot_orders_once
tick_grid_bot_once = tick_bot_once
