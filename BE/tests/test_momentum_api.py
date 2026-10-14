from math import sin

from app.bot_engine.bot import tick_bot_once
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder
from tests.remote_lookup import RemoteStateExchange, placed_from, positions_from, ticker_at


def _trend_candles(direction: int, count: int = 90):
    from app.bot_engine.strategies.momentum import Candle

    candles = []
    price = 100.0 if direction > 0 else 120.0
    for index in range(count):
        drift = (0.07 * direction) + (sin(index / 4.0) * 0.03)
        open_price = price + (sin(index / 6.0) * 0.02)
        close = open_price + drift
        wick = 0.08 + abs(sin(index / 5.0)) * 0.04
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        volume = 110.0 + (index % 7) * 4.0
        candles.append(Candle(index * 300_000, open_price, high, low, close, volume))
        price = close

    latest = candles[-1]
    impulse = 0.38
    candles[-1] = Candle(
        latest.open_time,
        latest.close - 0.10,
        latest.close + impulse + 0.08,
        latest.close - 0.18,
        latest.close + impulse,
        320.0,
    )
    return candles


def _create_momentum_bot(client, auth_headers, overrides=None):
    payload = {
        "name": "Momentum Runtime Bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "momentum",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 0.01,
        "grid_orders_count": 1,
        "grid_step_percent": 0,
        "is_active": True,
        "settings": {
            "timeframe": "15",
            "lookback_candles": 200,
            "position_side": "both",
            "fast_ema_period": 20,
            "slow_ema_period": 50,
            "rsi_period": 14,
            "rsi_long_threshold": 55,
            "rsi_short_threshold": 45,
            "volume_period": 20,
            "volume_multiplier": 1.2,
            "atr_period": 14,
            "atr_stop_loss_multiplier": 1.2,
            "atr_take_profit_multiplier": 2.0,
            "trailing_stop_enabled": True,
            "trailing_stop_atr_multiplier": 1.5,
            "minimum_signal_score": 70,
            "minimum_atr_percent": 0.0,
            "risk_per_trade_percent": 1.0,
            "cooldown_minutes": 0,
            "run_interval_seconds": 0,
            "max_position_qty": 0.05,
            "max_open_orders": 1,
            "stop_bot_on_error": True,
        },
    }
    if overrides:
        payload.update(overrides)
        if "settings" in overrides:
            payload["settings"] = {
                **payload["settings"],
                **overrides["settings"],
            }
    response = client.post("/api/bots", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _tick_bot(bot_id):
    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        return tick_bot_once(db, bot, force=True)
    finally:
        db.close()


def _mock_momentum_runtime(monkeypatch, *, remote_positions=None, current_price=100.0, order_log=None):
    if remote_positions is None:
        remote_positions = []
    if order_log is None:
        order_log = []

    monkeypatch.setattr("app.bot_engine.strategies.momentum.ensure_live_trading_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.bot_engine.strategies.momentum.sync_bot_orders", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.bot_engine.exchange.registry.exchange_for",
                        lambda _bot, needs=None: RemoteStateExchange({}, remote_positions=remote_positions,
                                                                    price=current_price))
    monkeypatch.setattr("app.bot_engine.strategies.momentum.get_last_price", lambda *args, **kwargs: current_price)
    monkeypatch.setattr(
        "app.bot_engine.strategies.momentum.get_ticker_snapshot",
        lambda *args, **kwargs: ticker_at(current_price),
    )
    monkeypatch.setattr(
        "app.bot_engine.strategies.momentum.get_instrument_rules",
        lambda *args, **kwargs: type("Rules", (), {
            "min_order_qty": 0.001,
            "qty_step": 0.001,
            "tick_size": 0.1,
            "min_notional_value": 0,
        })(),
    )
    monkeypatch.setattr(
        "app.bot_engine.strategies.momentum.get_open_positions",
        lambda *args, **kwargs: positions_from(remote_positions),
    )
    monkeypatch.setattr(
        "app.bot_engine.strategies.momentum._closed_candles",
        lambda *args, **kwargs: _trend_candles(1),
    )

    def _place_order(*args, **kwargs):
        order = kwargs["order"]
        order_log.append({
            "order_role": order.order_role,
            "side": order.side,
            "reduce_only": order.reduce_only,
            "qty": order.qty,
        })
        return placed_from({
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "orderId": f"oid-{order.order_link_id}",
                "orderStatus": "Filled" if order.reduce_only else "New",
            },
        }, order.order_link_id)

    monkeypatch.setattr("app.bot_engine.strategies.momentum.place_order", _place_order)
    return order_log


def test_create_momentum_bot_rejects_inverse_category(client, auth_headers):
    response = client.post(
        "/api/bots",
        json={
            "name": "Invalid Momentum",
            "exchange": "bybit",
            "environment": "demo",
            "strategy_type": "momentum",
            "category": "inverse",
            "symbol": "BTCUSDT",
            "order_qty": 0.01,
            "grid_orders_count": 1,
            "grid_step_percent": 0,
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Momentum currently supports linear and spot markets only"


def test_momentum_runtime_tick_creates_entry_order_and_persists_signal_state(client, auth_headers, monkeypatch):
    bot = _create_momentum_bot(client, auth_headers)
    order_log = _mock_momentum_runtime(monkeypatch)

    started = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert started.status_code == 200, started.text

    result = _tick_bot(bot["id"])
    assert result["message"] == "Momentum tick completed"

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers)
    assert orders.status_code == 200, orders.text
    payload = orders.json()
    assert len(payload) == 1
    assert payload[0]["order_role"] == "momentum_entry_long"
    assert payload[0]["side"] == "Buy"
    assert payload[0]["raw_response"]["reduceOnly"] is False
    assert any(item["order_role"] == "momentum_entry_long" for item in order_log)

    bot_detail = client.get(f"/api/bots/{bot['id']}", headers=auth_headers)
    assert bot_detail.status_code == 200, bot_detail.text
    detail = bot_detail.json()
    state = detail["settings"]["momentum_state"]
    assert state["current_signal"] == "long"
    assert state["last_analysis"]["type"] == "long"
    assert state["current_trade"]["side"] == "Buy"
    assert state["signal_history"]
    assert detail["runtime_state"] == "waiting_for_entry"


def test_momentum_close_position_creates_reduce_only_market_order(client, auth_headers, monkeypatch):
    bot = _create_momentum_bot(client, auth_headers)
    remote_positions = [{
        "side": "Buy",
        "size": "0.02",
        "avgPrice": "100.0",
        "positionValue": "2.0",
        "unrealisedPnl": "0.4",
    }]
    order_log = _mock_momentum_runtime(monkeypatch, remote_positions=remote_positions, current_price=102.0)

    response = client.post(
        f"/api/bots/{bot['id']}/close-position",
        json={"confirm": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "Position close order submitted"
    assert payload["order"]["order_role"] == "momentum_manual_close"
    assert payload["order"]["side"] == "Sell"
    assert payload["order"]["raw_response"]["reduceOnly"] is True
    assert any(item["order_role"] == "momentum_manual_close" and item["reduce_only"] is True for item in order_log)

    db = SessionLocal()
    try:
        stored = db.query(TradingBotOrder).filter(TradingBotOrder.bot_id == bot["id"]).all()
        assert any(order.order_role == "momentum_manual_close" for order in stored)
    finally:
        db.close()

