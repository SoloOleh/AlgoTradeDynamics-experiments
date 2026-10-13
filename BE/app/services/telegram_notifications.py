"""Telegram account linking, notification outbox, and background workers."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import quote

import httpx2 as httpx
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.telegram_notification import (
    TelegramLinkToken,
    TelegramNotificationChannel,
    TelegramNotificationDelivery,
)
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot import TradingBot

logger = get_logger(__name__)


class TelegramApiError(RuntimeError):
    """Telegram API failure that never embeds the secret bot token."""


def _telegram_payload(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {}
    if response.status_code >= 400 or not body.get("ok"):
        description = body.get("description") or f"Telegram API HTTP {response.status_code}"
        raise TelegramApiError(str(description))
    return body

STATUS_EVENTS = {"bot_started", "bot_stopped"}
RISK_EVENTS = {"risk_blocked"}
ERROR_EVENTS = {"error", "bot_error", "order_rejected", "venue_unsupported", "capability_missing"}
TRADE_EVENTS = {
    "grid_entry_created",
    "grid_entry_filled",
    "dca_entry_created",
    "dca_entry_filled",
    "position_take_profit_created",
    "position_take_profit_updated",
    "position_take_profit_filled",
    "grid_cycle_completed",
    "dca_cycle_completed",
    "position_close_requested",
    "position_closed",
    "scalper_signal_entered",
    "scalper_exit_submitted",
    "scalper_position_closed",
}
NOTIFIABLE_EVENTS = STATUS_EVENTS | RISK_EVENTS | ERROR_EVENTS | TRADE_EVENTS


def telegram_configured() -> bool:
    settings = get_settings()
    return bool(settings.telegram_bot_token.strip() and settings.telegram_bot_username.strip())


def _hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_link_token(db: Session, user_id: int) -> tuple[str, object]:
    settings = get_settings()
    now = utcnow()
    # Invalidate any previous unused links for this user.
    db.query(TelegramLinkToken).filter(
        TelegramLinkToken.user_id == user_id,
        TelegramLinkToken.used_at.is_(None),
    ).update({TelegramLinkToken.used_at: now}, synchronize_session=False)

    raw = secrets.token_urlsafe(32)
    expires_at = now + timedelta(minutes=settings.telegram_link_expire_minutes)
    db.add(TelegramLinkToken(
        user_id=user_id,
        token_hash=_hash_link_token(raw),
        expires_at=expires_at,
    ))
    db.commit()
    username = settings.telegram_bot_username.strip().lstrip("@")
    return f"https://t.me/{quote(username)}?start={quote(raw)}", expires_at


def get_channel(db: Session, user_id: int) -> TelegramNotificationChannel | None:
    return db.query(TelegramNotificationChannel).filter(
        TelegramNotificationChannel.user_id == user_id
    ).first()


def disconnect_channel(db: Session, user_id: int) -> None:
    channel = get_channel(db, user_id)
    if channel is not None:
        db.delete(channel)
    db.query(TelegramNotificationDelivery).filter(
        TelegramNotificationDelivery.user_id == user_id,
        TelegramNotificationDelivery.status.in_(["queued", "failed"]),
    ).update({TelegramNotificationDelivery.status: "skipped"}, synchronize_session=False)
    now = utcnow()
    db.query(TelegramLinkToken).filter(
        TelegramLinkToken.user_id == user_id,
        TelegramLinkToken.used_at.is_(None),
    ).update({TelegramLinkToken.used_at: now}, synchronize_session=False)
    db.commit()


def update_preferences(db: Session, user_id: int, **updates) -> TelegramNotificationChannel | None:
    channel = get_channel(db, user_id)
    if channel is None:
        return None
    for key, value in updates.items():
        if value is not None and hasattr(channel, key):
            setattr(channel, key, value)
    channel.updated_at = utcnow()
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def _is_naive_expired(value) -> bool:
    if value is None:
        return True
    now = utcnow()
    if value.tzinfo is None:
        return value <= now.replace(tzinfo=None)
    return value <= now


def link_from_start_message(db: Session, message: dict) -> tuple[bool, str]:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text = (message.get("text") or "").strip()

    if chat.get("type") != "private":
        return False, "Підключення доступне лише в приватному чаті з ботом."
    if not text.startswith("/start"):
        return False, "Відкрийте Telegram через кнопку Connect Telegram в AlgoTradeDynamics."

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return False, "Відкрийте Telegram через кнопку Connect Telegram в AlgoTradeDynamics."

    raw = parts[1].strip()
    token_row = db.query(TelegramLinkToken).filter(
        TelegramLinkToken.token_hash == _hash_link_token(raw)
    ).first()
    if token_row is None or token_row.used_at is not None or _is_naive_expired(token_row.expires_at):
        return False, "Посилання недійсне або протерміноване. Створіть нове в AlgoTradeDynamics."

    chat_id = int(chat["id"])
    existing_for_chat = db.query(TelegramNotificationChannel).filter(
        TelegramNotificationChannel.chat_id == chat_id
    ).first()
    if existing_for_chat is not None and existing_for_chat.user_id != token_row.user_id:
        token_row.used_at = utcnow()
        db.add(token_row)
        db.commit()
        return False, "Цей Telegram уже прив'язаний до іншого акаунта AlgoTradeDynamics."

    channel = get_channel(db, token_row.user_id)
    if channel is None:
        channel = TelegramNotificationChannel(user_id=token_row.user_id, chat_id=chat_id)
    channel.chat_id = chat_id
    channel.telegram_user_id = int(sender["id"]) if sender.get("id") is not None else None
    channel.username = sender.get("username")
    channel.enabled = True
    channel.connected_at = utcnow()
    channel.updated_at = utcnow()
    token_row.used_at = utcnow()
    db.add(channel)
    db.add(token_row)
    db.commit()
    return True, "✅ Telegram підключено до AlgoTradeDynamics. Сповіщення бота активні."


def _preference_allows(channel: TelegramNotificationChannel, event_type: str) -> bool:
    if not channel.enabled:
        return False
    if event_type in STATUS_EVENTS:
        return channel.notify_bot_status
    if event_type in RISK_EVENTS:
        return channel.notify_risk
    if event_type in ERROR_EVENTS:
        return channel.notify_errors
    if event_type in TRADE_EVENTS:
        return channel.notify_trades
    return False


def enqueue_event_notification(db: Session, event: TradingBotEvent) -> None:
    if not telegram_configured() or event.event_type not in NOTIFIABLE_EVENTS:
        return
    bot = db.get(TradingBot, event.bot_id)
    if bot is None or bot.is_backtest:
        return
    channel = get_channel(db, event.user_id)
    if channel is None or not _preference_allows(channel, event.event_type):
        return
    db.add(TelegramNotificationDelivery(event_id=event.id, user_id=event.user_id))
    db.flush()


def _fmt_value(value) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def format_event_message(event: TradingBotEvent) -> str:
    bot = event.bot
    payload = event.payload or {}
    headers = {
        "bot_started": "🤖 Bot started",
        "bot_stopped": "⏹ Bot stopped",
        "scalper_signal_entered": "🟢 Position opened" if payload.get("side") == "Buy" else "🔴 Position opened",
        "scalper_exit_submitted": "📤 Exit submitted",
        "scalper_position_closed": "✅ Position closed",
        "grid_entry_created": "🧾 Grid order created",
        "grid_entry_filled": "🟢 Grid order filled",
        "dca_entry_created": "🧾 DCA order created",
        "dca_entry_filled": "🟢 DCA order filled",
        "position_take_profit_created": "🎯 Take Profit created",
        "position_take_profit_updated": "🎯 Take Profit updated",
        "position_take_profit_filled": "✅ Take Profit filled",
        "grid_cycle_completed": "✅ Grid cycle completed",
        "dca_cycle_completed": "✅ DCA cycle completed",
        "position_close_requested": "📤 Position close requested",
        "position_closed": "✅ Position closed",
        "risk_blocked": "🛡 Risk limit blocked action",
        "venue_unsupported": "⏸ Bot paused",
        "capability_missing": "⏸ Bot paused",
        "order_rejected": "⚠️ Order rejected",
        "error": "🚨 Bot error",
        "bot_error": "🚨 Bot error / recovery action",
    }
    lines = [headers.get(event.event_type, "🔔 AlgoTradeDynamics"), ""]
    if bot is not None:
        lines.append(f"Bot: {bot.name}")
        lines.append(f"Symbol: {bot.symbol}")
        lines.append(f"Environment: {bot.environment}")

    field_labels = [
        ("side", "Side"), ("qty", "Qty"), ("entry_price", "Entry"),
        ("stop_loss", "SL"), ("take_profit", "TP"), ("score", "Score"),
        ("pattern", "Pattern"), ("price", "Price"), ("exit_reason", "Exit reason"),
        ("order_link_id", "Order"),
        ("error_type", "Error type"), ("severity", "Severity"),
        ("action", "Action"), ("error_code", "Code"),
        ("retry_count", "Retry"), ("next_retry_at", "Next retry"),
    ]
    for key, label in field_labels:
        if payload.get(key) is not None:
            lines.append(f"{label}: {_fmt_value(payload[key])}")

    if event.message and (
        event.event_type in ERROR_EVENTS | RISK_EVENTS
        or event.event_type in {"scalper_exit_submitted", "scalper_position_closed", "grid_cycle_completed"}
    ):
        lines.extend(["", event.message])
    return "\n".join(lines)[:4000]


def _telegram_url(method: str) -> str:
    settings = get_settings()
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def send_message_sync(chat_id: int, text: str) -> None:
    try:
        response = httpx.post(
            _telegram_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
            timeout=10.0,
        )
    except httpx.HTTPError:
        raise TelegramApiError("Telegram API request failed") from None
    _telegram_payload(response)


async def _send_message(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    try:
        response = await client.post(
            _telegram_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
        )
    except httpx.HTTPError:
        raise TelegramApiError("Telegram API request failed") from None
    _telegram_payload(response)


async def _process_delivery_batch(client: httpx.AsyncClient) -> None:
    db = SessionLocal()
    try:
        deliveries = db.query(TelegramNotificationDelivery).filter(
            TelegramNotificationDelivery.status.in_(["queued", "failed"]),
            TelegramNotificationDelivery.attempts < 3,
        ).order_by(TelegramNotificationDelivery.id.asc()).limit(20).all()
        for delivery in deliveries:
            event = db.get(TradingBotEvent, delivery.event_id)
            channel = get_channel(db, delivery.user_id)
            if event is None or channel is None or not _preference_allows(channel, event.event_type):
                delivery.status = "skipped"
                db.add(delivery)
                db.commit()
                continue
            try:
                await _send_message(client, channel.chat_id, format_event_message(event))
                delivery.status = "sent"
                delivery.sent_at = utcnow()
                delivery.last_error = None
            except Exception as exc:  # noqa: BLE001
                delivery.status = "failed"
                delivery.last_error = str(exc)[:1000]
                logger.warning("Telegram delivery failed: delivery_id=%s error=%s", delivery.id, exc)
            delivery.attempts += 1
            db.add(delivery)
            db.commit()
    finally:
        db.close()


async def _delivery_loop(app) -> None:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        while getattr(app.state, "telegram_workers_running", False):
            await _process_delivery_batch(client)
            await asyncio.sleep(settings.telegram_delivery_interval_seconds)


async def _polling_loop(app) -> None:
    settings = get_settings()
    offset: int | None = None
    timeout = max(1, settings.telegram_poll_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout + 10.0) as client:
        # Long polling cannot run while a webhook is configured. In MVP polling mode,
        # explicitly remove any old webhook but keep pending updates.
        try:
            response = await client.post(
                _telegram_url("deleteWebhook"),
                json={"drop_pending_updates": False},
            )
            _telegram_payload(response)
        except Exception:  # noqa: BLE001
            logger.warning("Unable to clear Telegram webhook; long polling may be unavailable")

        while getattr(app.state, "telegram_workers_running", False):
            try:
                params = {"timeout": timeout, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                try:
                    response = await client.get(_telegram_url("getUpdates"), params=params)
                except httpx.HTTPError:
                    raise TelegramApiError("Telegram API request failed") from None
                payload = _telegram_payload(response)
                for update in payload.get("result") or []:
                    offset = int(update["update_id"]) + 1
                    message = update.get("message")
                    if not message:
                        continue
                    db = SessionLocal()
                    try:
                        _, reply = link_from_start_message(db, message)
                        chat_id = (message.get("chat") or {}).get("id")
                        if chat_id is not None:
                            await _send_message(client, int(chat_id), reply)
                    finally:
                        db.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(3)


def start_telegram_workers(app) -> None:
    settings = get_settings()
    if not telegram_configured():
        logger.info("Telegram notifications disabled: TELEGRAM_BOT_TOKEN/USERNAME not configured")
        return
    if getattr(app.state, "telegram_delivery_task", None) is not None:
        return
    app.state.telegram_workers_running = True
    app.state.telegram_delivery_task = asyncio.create_task(_delivery_loop(app))
    app.state.telegram_polling_task = (
        asyncio.create_task(_polling_loop(app)) if settings.telegram_polling_enabled else None
    )
    logger.info("Telegram notification workers started; polling=%s", settings.telegram_polling_enabled)


async def stop_telegram_workers(app) -> None:
    app.state.telegram_workers_running = False
    tasks = [
        getattr(app.state, "telegram_delivery_task", None),
        getattr(app.state, "telegram_polling_task", None),
    ]
    for task in tasks:
        if task is not None:
            task.cancel()
    for task in tasks:
        if task is None:
            continue
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.state.telegram_delivery_task = None
    app.state.telegram_polling_task = None
