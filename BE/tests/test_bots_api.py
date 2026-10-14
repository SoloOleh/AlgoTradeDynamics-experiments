"""API integration tests for trading bot CRUD endpoints."""

import pytest

from app.bot_engine.orders import ACTIVE_ORDER_STATUSES, OrderRequest, place_order
from app.bot_engine.grid_runtime import tick_grid_bot
from app.db.session import SessionLocal
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder
from tests.remote_lookup import RemoteStateLookup, orders_from, placed_from, positions_from, ticker_at


def test_bots_require_auth(client):
    assert client.get("/api/bots").status_code == 401
    assert client.post("/api/bots", json={}).status_code == 401


def test_create_list_update_and_delete_bot(client, auth_headers):
    payload = {
        "name": "BTC Demo Grid Bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "grid",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 0.001,
        "grid_orders_count": 2,
        "grid_step_percent": 5,
        "is_active": True,
    }

    create = client.post("/api/bots", json=payload, headers=auth_headers)
    assert create.status_code == 201, create.text
    created_bot = create.json()
    assert created_bot["name"] == payload["name"]
    assert created_bot["environment"] == "demo"

    listed = client.get("/api/bots", headers=auth_headers)
    assert listed.status_code == 200
    assert any(bot["id"] == created_bot["id"] for bot in listed.json())

    fetched = client.get(
        f"/api/bots/{created_bot['id']}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["symbol"] == "BTCUSDT"

    update = client.put(
        f"/api/bots/{created_bot['id']}",
        json={
            "name": "ETH Testnet Grid Bot",
            "environment": "testnet",
            "symbol": "ETHUSDT",
            "order_qty": 0.01,
            "grid_orders_count": 6,
            "grid_step_percent": 1.25,
            "is_active": False,
        },
        headers=auth_headers,
    )
    assert update.status_code == 200, update.text
    updated_bot = update.json()
    assert updated_bot["name"] == "ETH Testnet Grid Bot"
    assert updated_bot["environment"] == "testnet"
    assert updated_bot["is_active"] is False

    deleted = client.delete(
        f"/api/bots/{created_bot['id']}", headers=auth_headers)
    assert deleted.status_code == 204

    refetch = client.get(
        f"/api/bots/{created_bot['id']}", headers=auth_headers)
    assert refetch.status_code == 404


def test_start_sync_stop_and_list_bot_orders(client, auth_headers, monkeypatch):
    create = client.post(
        "/api/bots",
        json={
            "name": "BTC Runtime Bot",
            "exchange": "bybit",
            "environment": "demo",
            "strategy_type": "grid",
            "category": "linear",
            "symbol": "BTCUSDT",
            "order_qty": 0.001,
            "grid_orders_count": 2,
            "grid_step_percent": 5,
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    bot = create.json()

    _mock_runtime(monkeypatch)

    started = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert started.status_code == 200, started.text
    started_body = started.json()
    assert started_body["action"] == "STARTED"
    assert started_body["bot"]["runtime_status"] == "running"
    assert started_body["orders"] == []

    _tick_bot(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers)
    assert orders.status_code == 200
    assert len(orders.json()) == 2

    synced = client.post(f"/api/bots/{bot['id']}/sync", headers=auth_headers)
    assert synced.status_code == 200, synced.text
    assert all(order["status"] in ACTIVE_ORDER_STATUSES for order in synced.json())

    stopped = client.post(f"/api/bots/{bot['id']}/stop", headers=auth_headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["runtime_status"] == "stopped"


def test_inactive_bot_cannot_start(client, auth_headers):
    create = client.post(
        "/api/bots",
        json={
            "name": "Inactive Bot",
            "exchange": "bybit",
            "environment": "demo",
            "strategy_type": "grid",
            "category": "linear",
            "symbol": "BTCUSDT",
            "order_qty": 0.001,
            "grid_orders_count": 2,
            "grid_step_percent": 5,
            "is_active": False,
        },
        headers=auth_headers,
    )
    bot = create.json()

    start = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert start.status_code == 422


def test_live_environment_cannot_start_when_disabled(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {
        "environment": "live",
        "settings": {"allow_live_trading": False},
    })
    _mock_runtime(monkeypatch)

    start = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert start.status_code == 400
    assert start.json()["detail"] == "Live trading is disabled for this bot"


def test_worker_tick_creates_grid_orders_without_duplicates(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    _mock_runtime(monkeypatch)

    start = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert start.status_code == 200

    _tick_bot(bot["id"])
    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entry_orders = [
        order for order in orders if order["order_role"].startswith("grid_entry_")]
    assert len(entry_orders) == 2
    assert len({order["order_link_id"] for order in entry_orders}) == 2


def test_filled_entry_creates_position_take_profit_order(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state, remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "100.0",
    }]

    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    tp_orders = [
        order for order in orders if order["order_role"] == "position_take_profit"]
    assert len(tp_orders) == 1
    assert tp_orders[0]["side"] == "Sell"
    assert tp_orders[0]["order_link_id"].startswith(
        f"bot-{bot['id']}-g1-position-tp-",
    )
    assert tp_orders[0]["raw_response"]["reduceOnly"] is True
    assert not any(order["order_role"].startswith("take_profit_")
                   for order in orders)


def test_existing_exchange_position_tp_is_synced_without_duplicate_creation(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]
    existing_tp_link_id = f"bot-{bot['id']}-g1-position-tp-1700000000000"
    remote_state[existing_tp_link_id] = {
        "orderLinkId": existing_tp_link_id,
        "orderId": f"oid-{existing_tp_link_id}",
        "orderStatus": "New",
        "side": "Sell",
        "orderType": "Limit",
        "qty": str(bot["order_qty"]),
        "cumExecQty": "0",
        "price": "101.5",
        "reduceOnly": True,
    }

    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    position_tp_orders = [
        order for order in orders if order["order_role"] == "position_take_profit"
    ]
    assert len(position_tp_orders) == 1
    assert position_tp_orders[0]["order_link_id"] == existing_tp_link_id
    assert position_tp_orders[0]["status"] == "New"


def test_risk_blocks_new_buy_when_max_position_qty_exceeded(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {
        "settings": {"max_position_qty": 0.015},
    })
    remote_positions = [{
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "100.0",
    }]
    _mock_runtime(monkeypatch, remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick_bot(bot["id"])

    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["event_type"] == "risk_blocked" for event in events)
    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entry_orders = [order for order in orders if order["order_role"].startswith("grid_entry_")]
    assert len(entry_orders) == 1


def test_risk_blocks_new_buy_when_max_open_orders_exceeded(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {
        "settings": {"max_open_orders": 1},
    })
    _mock_runtime(monkeypatch, remote_state={}, remote_positions=[])
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick_bot(bot["id"])
    _tick_bot(bot["id"])

    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["event_type"] == "risk_blocked" for event in events)


def test_risk_blocks_new_buy_when_max_notional_exceeded(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {
        "settings": {"max_notional_usdt": 0.5},
    })
    _mock_runtime(monkeypatch, current_price=100.0)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick_bot(bot["id"])

    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["message"] ==
               "Max notional exposure reached" for event in events)
    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert orders == []


def test_second_filled_entry_replaces_position_take_profit_order(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "100.0",
    }]
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=2)

    remote_positions[:] = [{
        "side": "Buy",
        "size": "0.02",
        "avgPrice": "97.5",
    }]
    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    position_tp_orders = [
        order for order in orders if order["order_role"] == "position_take_profit"
    ]
    active_position_tps = [
        order for order in position_tp_orders if order["status"] in ACTIVE_ORDER_STATUSES
    ]

    assert len(active_position_tps) == 1
    assert active_position_tps[0]["qty"] == 0.02
    assert active_position_tps[0]["price"] == 98.9
    assert active_position_tps[0]["raw_response"]["reduceOnly"] is True
    assert any(order["status"] == "Cancelled" for order in position_tp_orders)
    assert len({order["order_link_id"] for order in position_tp_orders}) == 2


def test_position_take_profit_fill_cancels_old_grid_and_rebuilds_from_current_price(
    client, auth_headers, monkeypatch
):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(
        monkeypatch,
        remote_state=remote_state,
        remote_positions=remote_positions,
        current_price=100.0,
    )
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    old_entry_one = next(
        order for order in orders if order["order_role"] == "grid_entry_1"
    )
    old_entry_two = next(
        order for order in orders if order["order_role"] == "grid_entry_2"
    )

    remote_state[old_entry_one["order_link_id"]]["orderStatus"] = "Filled"
    remote_state[old_entry_one["order_link_id"]]["cumExecQty"] = str(
        old_entry_one["qty"]
    )
    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]
    _tick_bot(bot["id"])

    position_tp = next(
        order
        for order in client.get(
            f"/api/bots/{bot['id']}/orders", headers=auth_headers
        ).json()
        if order["order_role"] == "position_take_profit"
    )
    remote_state[position_tp["order_link_id"]]["orderStatus"] = "Filled"
    remote_state[position_tp["order_link_id"]]["cumExecQty"] = str(
        position_tp["qty"]
    )
    remote_positions.clear()

    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_last_price",
        lambda *args, **kwargs: 105.0,
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_ticker_snapshot",
        lambda *args, **kwargs: ticker_at(105.0),
    )

    result = _tick_bot(bot["id"])
    assert result["message"] == "Tick completed; take-profit cycle rolled over"

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    old_entry_two_after = next(
        order for order in orders if order["order_link_id"] == old_entry_two["order_link_id"]
    )
    assert old_entry_two_after["status"] == "Cancelled"

    new_entries = [
        order
        for order in orders
        if order["order_role"].startswith("grid_entry_")
        and order["order_link_id"] not in {
            old_entry_one["order_link_id"],
            old_entry_two["order_link_id"],
        }
    ]
    assert len(new_entries) == 2
    assert {order["order_role"] for order in new_entries} == {
        "grid_entry_1",
        "grid_entry_2",
    }
    assert next(
        order["price"] for order in new_entries if order["order_role"] == "grid_entry_1"
    ) == 105.0
    assert next(
        order["price"] for order in new_entries if order["order_role"] == "grid_entry_2"
    ) == 99.7

    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers
    ).json()
    assert any(event["event_type"] == "grid_cycle_completed" for event in events)


def test_new_grid_waits_until_old_grid_cancellation_is_confirmed(
    client, auth_headers, monkeypatch
):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(
        monkeypatch,
        remote_state=remote_state,
        remote_positions=remote_positions,
    )
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    entry_one = next(
        order for order in orders if order["order_role"] == "grid_entry_1"
    )
    entry_two = next(
        order for order in orders if order["order_role"] == "grid_entry_2"
    )
    remote_state[entry_one["order_link_id"]]["orderStatus"] = "Filled"
    remote_state[entry_one["order_link_id"]]["cumExecQty"] = str(entry_one["qty"])
    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]
    _tick_bot(bot["id"])

    position_tp = next(
        order
        for order in client.get(
            f"/api/bots/{bot['id']}/orders", headers=auth_headers
        ).json()
        if order["order_role"] == "position_take_profit"
    )
    remote_state[position_tp["order_link_id"]]["orderStatus"] = "Filled"
    remote_state[position_tp["order_link_id"]]["cumExecQty"] = str(
        position_tp["qty"]
    )
    remote_positions.clear()

    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.cancel_order_by_link_id",
        lambda *args, **kwargs: {
            "result": {
                "orderLinkId": kwargs["order_link_id"],
                "orderStatus": "Cancelled",
            }
        },
    )

    result = _tick_bot(bot["id"])
    assert result["message"] == "Waiting for previous grid cancellation confirmation"

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    grid_entries = [
        order for order in orders if order["order_role"].startswith("grid_entry_")
    ]
    assert len(grid_entries) == 2
    assert next(
        order for order in grid_entries if order["order_link_id"] == entry_two["order_link_id"]
    )["status"] == "New"



def test_exchange_reset_reconciles_missing_orders_and_rebuilds_clean_grid(
    client, auth_headers, monkeypatch
):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(
        monkeypatch,
        remote_state=remote_state,
        remote_positions=remote_positions,
        current_price=100.0,
    )
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    original_orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    original_entries = [
        order for order in original_orders
        if order["order_role"].startswith("grid_entry_")
    ]
    original_link_ids = {order["order_link_id"] for order in original_entries}
    assert len(original_link_ids) == 2
    assert len(remote_state) == 2

    # The emulator reset removes the account's order and position records entirely.
    remote_state.clear()
    remote_positions.clear()
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_last_price",
        lambda *args, **kwargs: 120.0,
    )

    result = _tick_bot(bot["id"])
    assert result["message"] == "Tick completed"

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    stale_orders = [
        order for order in orders if order["order_link_id"] in original_link_ids
    ]
    assert len(stale_orders) == 2
    assert all(order["status"] == "Cancelled" for order in stale_orders)
    assert all(
        order["raw_response"]["reconciliation"]["reason"]
        == "missing_from_exchange"
        for order in stale_orders
    )

    replacement_entries = [
        order
        for order in orders
        if order["order_role"].startswith("grid_entry_")
        and order["order_link_id"] not in original_link_ids
        and order["status"] in ACTIVE_ORDER_STATUSES
    ]
    assert len(replacement_entries) == 2
    assert next(
        order["price"]
        for order in replacement_entries
        if order["order_role"] == "grid_entry_1"
    ) == 120.0
    assert next(
        order["price"]
        for order in replacement_entries
        if order["order_role"] == "grid_entry_2"
    ) == 114.0
    assert set(remote_state) == {
        order["order_link_id"] for order in replacement_entries
    }

    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers
    ).json()
    reconciled_events = [
        event for event in events
        if event["event_type"] == "order_reconciled_missing"
    ]
    assert len(reconciled_events) == 2

    refreshed_bot = client.get(
        f"/api/bots/{bot['id']}", headers=auth_headers
    ).json()
    assert refreshed_bot["runtime_state"] == "waiting_for_entry"


def test_position_take_profit_fill_logs_once(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]

    _tick_bot(bot["id"])

    position_tp = next(
        order for order in client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
        if order["order_role"] == "position_take_profit"
    )
    remote_state[position_tp["order_link_id"]]["orderStatus"] = "Filled"
    remote_state[position_tp["order_link_id"]
                 ]["cumExecQty"] = str(position_tp["qty"])
    remote_positions.clear()

    _tick_bot(bot["id"])
    _tick_bot(bot["id"])

    db = SessionLocal()
    try:
        count = db.query(TradingBotEvent).filter(
            TradingBotEvent.bot_id == bot["id"],
            TradingBotEvent.event_type == "position_take_profit_filled",
        ).count()
        assert count == 1
    finally:
        db.close()


def test_position_size_zero_cancels_active_position_take_profit(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]

    _tick_bot(bot["id"])

    remote_positions.clear()

    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    position_tp_orders = [
        order for order in orders if order["order_role"] == "position_take_profit"
    ]
    assert position_tp_orders
    assert all(order["status"] == "Cancelled" for order in position_tp_orders)


def test_position_take_profit_orders_must_be_reduce_only():
    class FakeSession:
        def place_order(self, **kwargs):
            raise AssertionError("place_order should not be called")

    with pytest.raises(ValueError):
        place_order(
            FakeSession(),
            category="linear",
            symbol="BTCUSDT",
            order=OrderRequest(
                side="Sell",
                order_type="Limit",
                order_role="position_take_profit",
                order_link_id="bot-1-g1-position-tp-1700000000000",
                qty=0.1,
                price=101.5,
                reduce_only=False,
            ),
        )


def test_position_endpoint_returns_no_position_response(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    _mock_runtime(monkeypatch, remote_positions=[])

    response = client.get(
        f"/api/bots/{bot['id']}/position", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["symbol"] == bot["symbol"]
    assert payload["side"] is None
    assert payload["size"] == "0"
    assert payload["take_profit"] is None


def test_position_endpoint_returns_position_and_tp_data(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {
        f"bot-{bot['id']}-g1-position-tp-1700000000000": {
            "orderLinkId": f"bot-{bot['id']}-g1-position-tp-1700000000000",
            "orderId": "oid-position-tp",
            "orderStatus": "New",
            "side": "Sell",
            "orderType": "Limit",
            "qty": "0.01",
            "cumExecQty": "0",
            "price": "101.5",
            "reduceOnly": True,
        }
    }
    remote_positions = [{
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "100.0",
        "liqPrice": "80.0",
        "unrealisedPnl": "1.2",
        "leverage": "10",
        "tradeMode": "Cross",
        "positionValue": "100.0",
    }]
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions, current_price=101.0)
    _seed_filled_bot_entry(bot, qty=0.01, price=100.0, level=1)

    response = client.get(
        f"/api/bots/{bot['id']}/position", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["side"] == "Buy"
    assert payload["size"] == "0.01"
    assert payload["avg_entry_price"] == "100.0"
    assert payload["mark_price"] == "101.0"
    assert payload["take_profit"]["reduce_only"] is True
    assert payload["take_profit"]["price"] == "101.5"


def test_risk_endpoint_returns_calculated_exposure(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {
        f"bot-{bot['id']}-g1-entry-1-1": {
            "orderLinkId": f"bot-{bot['id']}-g1-entry-1-1",
            "orderId": "oid-entry-1",
            "orderStatus": "New",
            "side": "Buy",
            "orderType": "Limit",
            "qty": "0.01",
            "cumExecQty": "0",
            "price": "95.0",
            "reduceOnly": False,
        },
        f"bot-{bot['id']}-g1-position-tp-1700000000000": {
            "orderLinkId": f"bot-{bot['id']}-g1-position-tp-1700000000000",
            "orderId": "oid-tp",
            "orderStatus": "New",
            "side": "Sell",
            "orderType": "Limit",
            "qty": "0.01",
            "cumExecQty": "0",
            "price": "101.5",
            "reduceOnly": True,
        },
    }
    remote_positions = [{
        "side": "Buy",
        "size": "0.02",
        "avgPrice": "100.0",
    }]
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions, current_price=100.0)
    _seed_filled_bot_entry(bot, qty=0.01, price=100.0, level=1, suffix="owned-1")
    _seed_filled_bot_entry(bot, qty=0.01, price=100.0, level=2, suffix="owned-2")

    response = client.get(f"/api/bots/{bot['id']}/risk", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["current_position_qty"] == "0.02"
    assert payload["pending_buy_qty"] == "0.01"
    assert payload["potential_total_qty"] == "0.03"
    assert payload["current_open_orders"] == 2
    assert payload["estimated_notional_usdt"] == "3.0"


def test_close_position_requires_confirm_true(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    _mock_runtime(monkeypatch)

    response = client.post(
        f"/api/bots/{bot['id']}/close-position",
        json={"confirm": False},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Position close requires confirm=true"


def test_close_position_creates_reduce_only_market_sell(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_positions = [{
        "side": "Buy",
        "size": "0.03",
        "avgPrice": "100.0",
    }]
    order_log = []
    _mock_runtime(monkeypatch, remote_positions=remote_positions,
                  order_log=order_log)
    _seed_filled_bot_entry(bot, qty=0.03, price=100.0, level=1)

    response = client.post(
        f"/api/bots/{bot['id']}/close-position",
        json={"confirm": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["message"] == "Position close order submitted"
    assert payload["order"]["order_role"] == "position_close"
    assert payload["order"]["side"] == "Sell"
    assert payload["order"]["order_type"] == "Market"
    assert payload["order"]["raw_response"]["reduceOnly"] is True
    assert any(order["order_role"] == "position_close" and order["reduce_only"]
               is True for order in order_log)


def test_old_per_entry_take_profit_roles_are_not_created(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state, remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    _fill_remote_grid_entry(remote_state, bot["id"], level=1)

    remote_positions[:] = [{
        "side": "Buy",
        "size": str(bot["order_qty"]),
        "avgPrice": "100.0",
    }]

    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert any(order["order_role"] ==
               "position_take_profit" for order in orders)
    assert not any(order["order_role"].startswith("take_profit_")
                   for order in orders)
    assert not any(order["order_role"].startswith("tp_") for order in orders)


def test_instrument_rules_reject_too_small_qty(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {"order_qty": 0.001})
    _mock_runtime(monkeypatch, min_qty=1.0)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert orders == []
    events = client.get(
        f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["event_type"] == "error" for event in events)


def test_performance_refreshes_exchange_state_before_returning_summary(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(
        monkeypatch,
        remote_state=remote_state,
        remote_positions=remote_positions,
        current_price=100.0,
    )

    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    entry_link = next(
        link for link in remote_state
        if "-entry-1-" in link and "position-tp" not in link
    )
    entry = remote_state[entry_link]
    entry_price = float(entry["price"])
    entry_qty = float(entry["qty"])
    entry.update({
        "orderStatus": "Filled",
        "cumExecQty": entry["qty"],
        "avgPrice": entry["price"],
        "cumExecFee": "0.00001",
    })
    remote_positions[:] = [{
        "side": "Buy",
        "size": entry["qty"],
        "avgPrice": entry["price"],
        "unrealisedPnl": "0",
        "positionValue": str(entry_price * entry_qty),
    }]

    # This worker tick sees the entry fill and creates the managed position TP.
    _tick_bot(bot["id"])
    tp_link = next(link for link in remote_state if "position-tp" in link)
    tp = remote_state[tp_link]
    tp.update({
        "orderStatus": "Filled",
        "cumExecQty": tp["qty"],
        "avgPrice": tp["price"],
        "cumExecFee": "0.00001",
    })
    remote_positions.clear()

    # Intentionally do NOT tick the worker again. The exchange is already filled
    # while the local TP row is still New -- the exact stale-summary race seen
    # after a fast emulator scenario. The performance read must reconcile first.
    response = client.get(
        f"/api/bots/{bot['id']}/performance", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["closed_cycles"] == 1
    assert float(payload["gross_realized_pnl"]) > 0
    assert float(payload["net_realized_pnl"]) > 0

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers
    ).json()
    refreshed_tp = next(order for order in orders if order["order_link_id"] == tp_link)
    assert refreshed_tp["status"] == "Filled"


def test_stop_cancels_active_orders_when_configured(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers, {
                              "settings": {"cancel_orders_on_stop": True}})
    _mock_runtime(monkeypatch)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    stopped = client.post(f"/api/bots/{bot['id']}/stop", headers=auth_headers)
    assert stopped.status_code == 200
    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert all(order["status"] in {"Cancelled", "Filled"} for order in orders)


def test_clear_history_increments_generation_and_blocks_old_remote_history(client, auth_headers, monkeypatch):
    bot = _create_runtime_bot(client, auth_headers)
    remote_state = {}
    remote_positions = []
    _mock_runtime(monkeypatch, remote_state=remote_state,
                  remote_positions=remote_positions)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    _tick_bot(bot["id"])

    old_link_id = f"bot-{bot['id']}-g1-entry-1-1"
    remote_state[old_link_id]["orderStatus"] = "Filled"
    remote_state[old_link_id]["cumExecQty"] = remote_state[old_link_id]["qty"]

    cleared = client.post(
        f"/api/bots/{bot['id']}/clear-history", headers=auth_headers)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["orders_deleted"] >= 1

    refreshed_bot = client.get(
        f"/api/bots/{bot['id']}", headers=auth_headers).json()
    assert refreshed_bot["order_link_generation"] == 2
    assert refreshed_bot["runtime_status"] == "stopped"

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert orders == []

    restarted = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert restarted.status_code == 200, restarted.text
    _tick_bot(bot["id"])

    orders = client.get(
        f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert orders
    assert all(order["order_link_id"].startswith(
        f"bot-{bot['id']}-g2-") for order in orders)
    assert not any(order["order_link_id"] == old_link_id for order in orders)


def _create_runtime_bot(client, auth_headers, overrides=None):
    payload = {
        "name": "Grid Runtime Bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "grid",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 0.01,
        "grid_orders_count": 2,
        "grid_step_percent": 5,
        "is_active": True,
        "settings": {
            "take_profit_percent": 1.5,
            "run_interval_seconds": 0,
            "max_open_orders": 10,
            "max_position_qty": 1,
            "cancel_orders_on_stop": True,
        },
    }
    if overrides:
        payload.update(overrides)
        if "settings" in overrides:
            payload["settings"] = {
                **{
                    "take_profit_percent": 1.5,
                    "run_interval_seconds": 0,
                    "max_open_orders": 10,
                    "max_position_qty": 1,
                    "cancel_orders_on_stop": True,
                },
                **overrides["settings"],
            }
    response = client.post("/api/bots", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _tick_bot(bot_id):
    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        result = tick_grid_bot(db, bot)
        return result
    finally:
        db.close()


def _mock_runtime(monkeypatch, *, min_qty=0.001, remote_state=None, remote_positions=None, current_price=100.0, order_log=None):
    if remote_state is None:
        remote_state = {}
    if remote_positions is None:
        remote_positions = []
    if order_log is None:
        order_log = []
    monkeypatch.setattr(
        "app.bot_engine.exchange.registry.exchange_for",
        lambda _bot, needs=None: RemoteStateLookup(remote_state),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_orders_by_prefix",
        lambda *args, **kwargs: orders_from(_mock_get_orders_by_prefix(
            remote_state, kwargs["order_link_prefix"])),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_last_price",
        lambda *args, **kwargs: current_price,
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_ticker_snapshot",
        lambda *args, **kwargs: ticker_at(current_price),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_instrument_rules",
        lambda *args, **kwargs: type("Rules", (), {
            "min_order_qty": min_qty,
            "qty_step": 0.001,
            "tick_size": 0.1,
            "min_notional_value": 0,
        })(),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_open_positions",
        lambda *args, **kwargs: positions_from(remote_positions),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.get_open_orders",
        lambda *args, **kwargs: orders_from(_mock_get_open_orders(remote_state)),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.place_order",
        lambda *args, **kwargs: placed_from(_mock_place_order(
            remote_state, kwargs["order"], order_log), kwargs["order"].order_link_id),
    )
    monkeypatch.setattr(
        "app.bot_engine.grid_runtime.cancel_order_by_link_id",
        lambda *args, **kwargs: _mock_cancel_order(
            remote_state, kwargs["order_link_id"]),
    )


def _mock_place_order(remote_state, order, order_log):
    order_log.append({
        "order_role": order.order_role,
        "side": order.side,
        "order_type": order.order_type,
        "reduce_only": order.reduce_only,
        "qty": order.qty,
    })
    remote_state[order.order_link_id] = {
        "orderLinkId": order.order_link_id,
        "orderId": f"oid-{order.order_link_id}",
        "orderStatus": "New",
        "side": order.side,
        "orderType": order.order_type,
        "qty": str(order.qty),
        "cumExecQty": "0",
        "price": str(order.price) if order.price is not None else None,
        "reduceOnly": order.reduce_only,
    }
    return {"result": {
        "orderId": f"oid-{order.order_link_id}",
        "orderStatus": "New",
    }}


def _mock_get_orders_by_prefix(remote_state, order_link_prefix):
    return [
        remote_order
        for remote_order in remote_state.values()
        if (remote_order.get("orderLinkId") or "").startswith(order_link_prefix)
    ]


def _mock_get_open_orders(remote_state):
    return [
        remote_order
        for remote_order in remote_state.values()
        if remote_order.get("orderStatus") in ACTIVE_ORDER_STATUSES
    ]


def _mock_cancel_order(remote_state, order_link_id):
    remote_state.pop(order_link_id, None)
    return {"result": {
        "orderLinkId": order_link_id,
        "orderStatus": "Cancelled",
    }}


def test_new_grid_does_not_adopt_position_left_by_previous_bot(client, auth_headers, monkeypatch):
    """Regression: a new bot must manage only fills created by its own bot id."""
    bot = _create_runtime_bot(client, auth_headers, {
        "settings": {"run_interval_seconds": 0},
    })
    remote_state = {}
    # Simulate 0.01 BTC left in the shared emulator account by a stopped DCA bot.
    remote_positions = [{
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "90.0",
    }]
    _mock_runtime(
        monkeypatch,
        remote_state=remote_state,
        remote_positions=remote_positions,
        current_price=100.0,
    )

    started = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert started.status_code == 200, started.text
    _tick_bot(bot["id"])

    # The foreign 0.01 must not cause an immediate TP for the new grid.
    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    assert not any(order["order_role"] == "position_take_profit" for order in orders)
    assert any(order["order_role"] == "grid_entry_1" for order in orders)

    position = client.get(f"/api/bots/{bot['id']}/position", headers=auth_headers).json()
    assert position["size"] == "0"

    # Now fill only this grid bot's first entry. The shared exchange position is
    # 0.02, but the bot owns only its new 0.01 and must create TP for 0.01 only.
    first_entry_link = next(
        link for link in remote_state if f"bot-{bot['id']}-g1-entry-1-" in link
    )
    remote_state[first_entry_link]["orderStatus"] = "Filled"
    remote_state[first_entry_link]["cumExecQty"] = "0.01"
    remote_state[first_entry_link]["price"] = "100.0"
    remote_positions[:] = [{
        "side": "Buy",
        "size": "0.02",
        "avgPrice": "95.0",
    }]

    _tick_bot(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    active_tps = [
        order for order in orders
        if order["order_role"] == "position_take_profit"
        and order["status"] in ACTIVE_ORDER_STATUSES
    ]
    assert len(active_tps) == 1
    assert active_tps[0]["qty"] == pytest.approx(0.01)
    assert active_tps[0]["price"] == pytest.approx(101.5)

    position = client.get(f"/api/bots/{bot['id']}/position", headers=auth_headers).json()
    assert float(position["size"]) == pytest.approx(0.01)
    assert float(position["avg_entry_price"]) == pytest.approx(100.0)


def _seed_filled_bot_entry(bot, *, qty, price, level=1, suffix="seed"):
    db = SessionLocal()
    try:
        db.add(TradingBotOrder(
            bot_id=bot["id"],
            user_id=bot["user_id"],
            exchange=bot["exchange"],
            environment=bot["environment"],
            category=bot["category"],
            symbol=bot["symbol"],
            side="Buy",
            order_type="Limit",
            order_role=f"grid_entry_{level}",
            order_link_id=f"bot-{bot['id']}-g1-entry-{level}-{suffix}",
            qty=qty,
            filled_qty=qty,
            price=price,
            status="Filled",
            raw_response={"orderStatus": "Filled", "cumExecQty": str(qty), "price": str(price)},
        ))
        db.commit()
    finally:
        db.close()


def _fill_remote_grid_entry(remote_state, bot_id, *, level=1):
    link = next(
        link for link in remote_state
        if f"bot-{bot_id}-g1-entry-{level}-" in link
    )
    remote = remote_state[link]
    remote["orderStatus"] = "Filled"
    remote["cumExecQty"] = remote["qty"]
    return remote
