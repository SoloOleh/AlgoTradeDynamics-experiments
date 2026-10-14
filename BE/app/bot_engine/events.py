"""Helpers for trading bot runtime event logging."""
from __future__ import annotations

from app.bot_engine.exchange.redaction import scrub, scrub_value
from app.core.clock import utcnow
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent


def log_bot_event(db, bot: TradingBot, event_type: str, message: str, payload: dict | None = None) -> TradingBotEvent:
    event = TradingBotEvent(
        created_at=utcnow(),
        bot_id=bot.id,
        user_id=bot.user_id,
        event_type=event_type,
        # Every event passes the scrubber: no key or secret is ever stored (ADR-0008).
        message=scrub(message),
        payload=scrub_value(payload),
    )
    db.add(event)
    db.flush()
    # Mirror non-backtest financial/runtime facts into the immutable audit ledger
    # in the same database transaction. Clearing ordinary bot history cannot remove
    # the regulatory record.
    from app.services.audit_service import record_bot_event_audit
    record_bot_event_audit(db, bot, event)
    # Queue external delivery in the same DB transaction. Telegram I/O is handled
    # asynchronously by the notification outbox worker, never inside bot runtime.
    from app.services.telegram_notifications import enqueue_event_notification
    enqueue_event_notification(db, event)
    return event
