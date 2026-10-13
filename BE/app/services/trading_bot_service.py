"""Service layer for user-owned trading bot CRUD operations."""
from datetime import datetime, timezone

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.bot_engine.bot import run_bot_once, stop_bot_once, sync_bot_orders_once
from app.bot_engine.grid_runtime import sync_bot_orders
from app.bot_engine.error_handling import clear_bot_runtime_error
from app.bot_engine.exchange import registry
from app.bot_engine.performance import get_performance_summary
from app.bot_engine.strategies import get_strategy
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.models.user import User
from app.schemas.trading_bot import TradingBotCreate, TradingBotUpdate


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_strategy_configuration(strategy_type: str, category: str, grid_step_percent: float) -> None:
    get_strategy(strategy_type)
    if strategy_type == "grid" and grid_step_percent <= 0:
        raise ValueError("Grid bot step percent must be greater than zero")
    if strategy_type == "dca" and grid_step_percent <= 0:
        raise ValueError("DCA bot step percent must be greater than zero")
    if strategy_type == "pattern_scalper" and category != "linear":
        raise ValueError("Pattern Scalper currently supports linear perpetuals only")
    if strategy_type == "momentum" and category == "inverse":
        raise ValueError("Momentum currently supports linear and spot markets only")


def _serialize_trading_bot(db: Session, bot: TradingBot) -> dict:
    strategy = get_strategy(bot.strategy_type)
    runtime_state, last_risk_message = strategy.get_runtime_state(db, bot)
    return {
        "id": bot.id,
        "user_id": bot.user_id,
        "name": bot.name,
        "exchange": bot.exchange,
        "environment": bot.environment,
        "strategy_type": bot.strategy_type,
        "category": bot.category,
        "symbol": bot.symbol,
        "order_qty": bot.order_qty,
        "grid_orders_count": bot.grid_orders_count,
        "grid_step_percent": bot.grid_step_percent,
        "is_active": bot.is_active,
        "settings": strategy.get_effective_settings(bot),
        "order_link_generation": bot.order_link_generation,
        "runtime_status": bot.runtime_status,
        "runtime_state": runtime_state,
        "last_risk_message": last_risk_message,
        "created_at": bot.created_at,
        "updated_at": bot.updated_at,
        "started_at": bot.started_at,
        "stopped_at": bot.stopped_at,
        "last_run_at": bot.last_run_at,
        "last_error": bot.last_error,
        "last_error_type": bot.last_error_type,
        "last_error_severity": bot.last_error_severity,
        "last_error_action": bot.last_error_action,
        "last_error_code": bot.last_error_code,
        "last_error_at": bot.last_error_at,
        "error_retry_count": bot.error_retry_count,
        "next_retry_at": bot.next_retry_at,
    }


def serialize_trading_bot(db: Session, bot: TradingBot) -> dict:
    return _serialize_trading_bot(db, bot)


def list_trading_bots(db: Session, user_id: int, limit: int = 100) -> list[TradingBot]:
    bots = db.query(TradingBot).filter(
        TradingBot.user_id == user_id, TradingBot.is_backtest.is_(False)
    ).order_by(desc(TradingBot.updated_at), desc(TradingBot.id)).limit(limit).all()
    return [_serialize_trading_bot(db, bot) for bot in bots]


def get_trading_bot(db: Session, bot_id: int, user_id: int) -> TradingBot | None:
    return db.query(TradingBot).filter(
        TradingBot.id == bot_id,
        TradingBot.user_id == user_id,
        TradingBot.is_backtest.is_(False),
    ).first()


def create_trading_bot(db: Session, payload: TradingBotCreate, user_id: int) -> TradingBot:
    bot = TradingBot(user_id=user_id, **payload.model_dump())
    _validate_strategy_configuration(bot.strategy_type, bot.category, bot.grid_step_percent)
    db.add(bot); db.commit(); db.refresh(bot)
    return _serialize_trading_bot(db, bot)


def update_trading_bot(db: Session, bot: TradingBot, payload: TradingBotUpdate) -> TradingBot:
    updates = payload.model_dump(exclude_unset=True)
    strategy_type = updates.get("strategy_type", bot.strategy_type)
    category = updates.get("category", bot.category)
    grid_step_percent = updates.get("grid_step_percent", bot.grid_step_percent)
    _validate_strategy_configuration(strategy_type, category, grid_step_percent)
    for field, value in updates.items():
        setattr(bot, field, value)
    db.add(bot); db.commit(); db.refresh(bot)
    return _serialize_trading_bot(db, bot)


def delete_trading_bot(db: Session, bot: TradingBot) -> None:
    db.delete(bot); db.commit()


def list_trading_bot_orders(db: Session, bot_id: int, user_id: int) -> list[TradingBotOrder]:
    return db.query(TradingBotOrder).filter(
        TradingBotOrder.bot_id == bot_id, TradingBotOrder.user_id == user_id
    ).order_by(desc(TradingBotOrder.created_at), desc(TradingBotOrder.id)).all()


def list_trading_bot_events(db: Session, bot_id: int, user_id: int, limit: int = 100) -> list[TradingBotEvent]:
    return db.query(TradingBotEvent).filter(
        TradingBotEvent.bot_id == bot_id, TradingBotEvent.user_id == user_id
    ).order_by(desc(TradingBotEvent.created_at), desc(TradingBotEvent.id)).limit(limit).all()


def start_trading_bot_cycle(db: Session, bot: TradingBot, current_user: User) -> dict:
    result = run_bot_once(db, bot, current_user)
    return {**result, "bot": _serialize_trading_bot(db, result["bot"])}


def stop_trading_bot_cycle(db: Session, bot: TradingBot, current_user: User) -> TradingBot:
    return _serialize_trading_bot(db, stop_bot_once(db, bot, current_user))


def sync_trading_bot_orders(db: Session, bot: TradingBot, current_user: User) -> list[TradingBotOrder]:
    return sync_bot_orders_once(db, bot, current_user)


def cancel_trading_bot_orders(db: Session, bot: TradingBot, current_user: User) -> list[TradingBotOrder]:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    adapter = registry.exchange_for(bot)
    bot.runtime_status = "stopped"; bot.stopped_at = _utcnow(); clear_bot_runtime_error(bot)
    db.add(bot); db.flush()
    cancelled = get_strategy(bot.strategy_type).cancel_orders(db, bot, adapter=adapter)
    db.commit()
    return cancelled


def clear_trading_bot_history(db: Session, bot: TradingBot, current_user: User) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    exchange_cancel_error = None
    exchange_orders_cancelled = 0
    bot.runtime_status = "stopped"
    bot.stopped_at = _utcnow()
    bot.started_at = None
    bot.last_run_at = None
    clear_bot_runtime_error(bot)
    settings = dict(bot.settings or {})
    settings.pop("pattern_scalper_state", None)
    settings.pop("momentum_state", None)
    bot.settings = settings
    db.add(bot); db.flush()
    try:
        adapter = registry.exchange_for(bot)
        # Keep legacy reconciliation for old grid orders, then use the selected strategy.
        sync_bot_orders(db, bot, include_legacy=True)
        cancelled = get_strategy(bot.strategy_type).cancel_orders(db, bot, adapter=adapter)
        exchange_orders_cancelled = len(cancelled)
        db.flush()
    except Exception as exc:  # noqa: BLE001 - a guard refusal or exchange failure skips only the cancel
        exchange_cancel_error = str(exc)
    orders_deleted = db.query(TradingBotOrder).filter(
        TradingBotOrder.bot_id == bot.id, TradingBotOrder.user_id == current_user.id
    ).delete(synchronize_session=False)
    events_deleted = db.query(TradingBotEvent).filter(
        TradingBotEvent.bot_id == bot.id, TradingBotEvent.user_id == current_user.id
    ).delete(synchronize_session=False)
    bot.order_link_generation += 1
    db.add(bot); db.commit(); db.refresh(bot)
    return {
        "message": "Bot history cleared", "bot_id": bot.id,
        "orders_deleted": orders_deleted, "events_deleted": events_deleted,
        "exchange_orders_cancelled": exchange_orders_cancelled,
        "exchange_cancel_error": exchange_cancel_error,
    }


def get_trading_bot_position(db: Session, bot: TradingBot, current_user: User) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    adapter = registry.exchange_for(bot)
    return get_strategy(bot.strategy_type).get_position(db, bot, adapter=adapter)


def get_trading_bot_risk(db: Session, bot: TradingBot, current_user: User) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    adapter = registry.exchange_for(bot)
    return get_strategy(bot.strategy_type).get_risk(db, bot, adapter=adapter)


def get_trading_bot_performance(
    db: Session,
    bot: TradingBot,
    current_user: User,
    *,
    refresh_exchange: bool = False,
) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")

    # Grid/DCA performance is calculated from locally reconciled TradingBotOrder
    # rows. A freshly completed emulator scenario can fill exchange orders before
    # the background worker has persisted those statuses locally, which makes the
    # bot page briefly show an all-zero/stale performance summary. For explicit
    # reads (REST and the initial browser WS snapshot), do one best-effort
    # reconciliation first so the summary reflects exchange state immediately.
    if refresh_exchange and bot.strategy_type in {"grid", "dca"}:
        try:
            # A guard refusal skips the refresh; stored figures are still returned.
            registry.exchange_for(bot)
            sync_bot_orders(db, bot)
            db.commit()
        except Exception:  # noqa: BLE001 - keep performance readable if exchange refresh fails.
            db.rollback()

    return get_performance_summary(db, bot)


def close_trading_bot_position(db: Session, bot: TradingBot, current_user: User, *, confirm: bool) -> dict:
    if bot.user_id != current_user.id:
        raise PermissionError("Trading bot access denied")
    if not confirm:
        raise ValueError("Position close requires confirm=true")
    adapter = registry.exchange_for(bot)
    result = get_strategy(bot.strategy_type).close_position(db, bot, adapter=adapter)
    db.commit()
    return result
