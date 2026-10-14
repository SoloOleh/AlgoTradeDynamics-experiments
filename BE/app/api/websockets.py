"""Authenticated browser WebSocket endpoints for bots and the emulator UI."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.redaction import scrub
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.user import User
from app.schemas.backtest import BacktestRunDetail, BacktestRunSummary
from app.services.backtest_service import get_backtest_by_id, list_backtests
from app.services.trading_bot_service import (
    get_trading_bot,
    get_trading_bot_performance,
    get_trading_bot_position,
    get_trading_bot_risk,
    list_trading_bot_events,
    list_trading_bot_orders,
    serialize_trading_bot,
)
from app.services.ui_stream_service import backtest_ui_stream_hub, bot_ui_stream_hub

router = APIRouter(tags=["WebSocket"])
AUTH_TIMEOUT_SECONDS = 8
HEARTBEAT_SECONDS = 20


def _utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _authenticate_token(token: str) -> int | None:
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub"))
    except (jwt.PyJWTError, TypeError, ValueError):
        return None
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        return user.id if user is not None else None
    finally:
        db.close()


async def _authenticate_websocket(websocket: WebSocket) -> int | None:
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=AUTH_TIMEOUT_SECONDS)
    except WebSocketDisconnect:
        return None
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=4401, reason="Authentication required")
        return None

    if not isinstance(message, dict) or str(message.get("op") or "") != "auth" or not message.get("token"):
        await websocket.send_json({"type": "auth.error", "message": "Authentication required"})
        await websocket.close(code=4401, reason="Authentication required")
        return None

    user_id = await asyncio.to_thread(_authenticate_token, str(message["token"]))
    if user_id is None:
        await websocket.send_json({"type": "auth.error", "message": "Invalid or expired access token"})
        await websocket.close(code=4401, reason="Invalid or expired access token")
        return None

    await websocket.send_json({"type": "auth.ok", "ts": _utc_ms()})
    return user_id


def _bot_belongs_to_user(bot_id: int, user_id: int) -> bool:
    db = SessionLocal()
    try:
        return get_trading_bot(db, bot_id, user_id) is not None
    finally:
        db.close()


def _build_bot_snapshot(bot_id: int, user_id: int, *, refresh_exchange: bool = False) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        bot = get_trading_bot(db, bot_id, user_id)
        if user is None or bot is None:
            return None
        errors: dict[str, str] = {}

        def safe(name: str, func, default):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                errors[name] = scrub(str(exc))
                return default

        performance = safe(
            "performance",
            lambda: get_trading_bot_performance(
                db, bot, user, refresh_exchange=refresh_exchange
            ),
            None,
        )

        return jsonable_encoder({
            "bot": serialize_trading_bot(db, bot),
            # Performance refresh above reconciles Grid/DCA orders first, so the
            # initial browser snapshot cannot pair a fresh PnL with stale orders.
            "orders": list_trading_bot_orders(db, bot.id, user_id),
            "events": list_trading_bot_events(db, bot.id, user_id),
            "position": safe("position", lambda: get_trading_bot_position(db, bot, user), None),
            "risk": safe("risk", lambda: get_trading_bot_risk(db, bot, user), None),
            "performance": performance,
            "stream_errors": errors,
        })
    finally:
        db.close()


@router.websocket("/ws/bots/{bot_id}")
async def bot_browser_stream(websocket: WebSocket, bot_id: int) -> None:
    user_id = await _authenticate_websocket(websocket)
    if user_id is None:
        return

    if not await asyncio.to_thread(_bot_belongs_to_user, bot_id, user_id):
        await websocket.send_json({"type": "access.denied", "message": "Trading bot not found"})
        await websocket.close(code=4404, reason="Trading bot not found")
        return

    subscription_id, queue = bot_ui_stream_hub.subscribe(bot_id)
    try:
        snapshot = await asyncio.to_thread(_build_bot_snapshot, bot_id, user_id, refresh_exchange=True)
        if snapshot is None:
            await websocket.close(code=4404, reason="Trading bot not found")
            return
        await websocket.send_json({"type": "bot.snapshot", "reason": "initial", "ts": _utc_ms(), "data": snapshot})

        while True:
            try:
                reason = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                snapshot = await asyncio.to_thread(_build_bot_snapshot, bot_id, user_id)
                if snapshot is None:
                    await websocket.send_json({"type": "bot.deleted", "ts": _utc_ms()})
                    await websocket.close(code=4404, reason="Trading bot not found")
                    return
                await websocket.send_json({"type": "bot.snapshot", "reason": reason, "ts": _utc_ms(), "data": snapshot})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping", "ts": _utc_ms()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bot_ui_stream_hub.unsubscribe(bot_id, subscription_id)


async def _forward_emulator_source(browser: WebSocket, send_lock: asyncio.Lock, streams, source) -> None:
    """Relay one emulator stream to the browser; the adapter owns the connection (ADR-0001)."""

    async def forward(message: dict[str, Any]) -> None:
        async with send_lock:
            await browser.send_json({"type": "emulator.event", "source": source.label, "ts": _utc_ms(), "data": message})

    async def status(state: str, error: str | None) -> None:
        payload = {"type": "emulator.stream_status", "source": source.label, "status": state}
        if error is not None:
            payload["message"] = error
        payload["ts"] = _utc_ms()
        async with send_lock:
            await browser.send_json(payload)

    await streams.relay(source, on_message=forward, on_status=status)


@router.websocket("/ws/emulator")
async def emulator_browser_stream(
    websocket: WebSocket,
    account_id: int = Query(..., gt=0),
    symbol: str = Query(..., min_length=1, max_length=32),
) -> None:
    user_id = await _authenticate_websocket(websocket)
    if user_id is None:
        return

    # Emulator is a shared authenticated lab in the MVP. Bot ownership remains
    # strictly user-scoped; emulator account ownership is not modeled yet.
    # Stream endpoints come from the emulator venue's adapter, resolved through the registry.
    try:
        venue = registry.adapter_for_venue("bybit", "emulator")
        account_key = await venue.streams().emulator_account_key(account_id)
    except Exception as exc:  # noqa: BLE001
        await websocket.send_json({"type": "emulator.error", "message": f"Emulator unavailable: {scrub(str(exc))}"})
        await websocket.close(code=1011, reason="Emulator unavailable")
        return
    if not account_key:
        await websocket.send_json({"type": "access.denied", "message": "Emulator account not found"})
        await websocket.close(code=4404, reason="Emulator account not found")
        return

    adapter = registry.adapter_for_venue("bybit", "emulator", account=registry.Account(account_key))
    streams = adapter.streams()
    send_lock = asyncio.Lock()
    tasks = {
        asyncio.create_task(_forward_emulator_source(websocket, send_lock, streams, source))
        for source in streams.sources("linear", symbol.upper())
    }

    async def wait_for_browser_disconnect() -> None:
        while True:
            message = await websocket.receive_json()
            if str(message.get("op") or "") == "ping":
                async with send_lock:
                    await websocket.send_json({"type": "pong", "ts": _utc_ms()})

    browser_task = asyncio.create_task(wait_for_browser_disconnect())
    tasks.add(browser_task)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)



def _build_backtest_snapshot(run_id: int, user_id: int) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        run = get_backtest_by_id(db, run_id, user_id)
        return BacktestRunDetail.model_validate(run).model_dump(mode="json") if run is not None else None
    finally:
        db.close()


def _build_backtests_snapshot(user_id: int) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        return [
            BacktestRunSummary.model_validate(run).model_dump(mode="json")
            for run in list_backtests(db, user_id)
        ]
    finally:
        db.close()


@router.websocket("/ws/backtests")
async def backtests_browser_stream(
    websocket: WebSocket,
    run_id: int | None = Query(default=None, gt=0),
) -> None:
    user_id = await _authenticate_websocket(websocket)
    if user_id is None:
        return

    # Subscribe before reading the initial database state. This closes the race
    # where a very fast backtest can finish between page navigation and the WS
    # subscription: any change after this point is already queued.
    subscription_id, queue = backtest_ui_stream_hub.subscribe(user_id)
    try:
        if run_id is not None:
            snapshot = await asyncio.to_thread(_build_backtest_snapshot, run_id, user_id)
            if snapshot is None:
                await websocket.send_json({"type": "access.denied", "message": "Backtest run not found"})
                await websocket.close(code=4404, reason="Backtest run not found")
                return
            await websocket.send_json({
                "type": "backtest.updated",
                "run_id": run_id,
                "reason": "initial",
                "ts": _utc_ms(),
                "data": snapshot,
            })
        else:
            snapshots = await asyncio.to_thread(_build_backtests_snapshot, user_id)
            await websocket.send_json({
                "type": "backtests.snapshot",
                "reason": "initial",
                "ts": _utc_ms(),
                "data": snapshots,
            })

        while True:
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                event_run_id_text, _, reason = raw.partition(":")
                event_run_id = int(event_run_id_text)
                if run_id is not None and event_run_id != run_id:
                    continue
                snapshot = await asyncio.to_thread(_build_backtest_snapshot, event_run_id, user_id)
                if snapshot is None:
                    await websocket.send_json({
                        "type": "backtest.deleted",
                        "run_id": event_run_id,
                        "reason": reason,
                        "ts": _utc_ms(),
                    })
                else:
                    await websocket.send_json({
                        "type": "backtest.updated",
                        "run_id": event_run_id,
                        "reason": reason or "updated",
                        "ts": _utc_ms(),
                        "data": snapshot,
                    })
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping", "ts": _utc_ms()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        backtest_ui_stream_hub.unsubscribe(user_id, subscription_id)
