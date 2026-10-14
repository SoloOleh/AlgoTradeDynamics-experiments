"""Tests for the DCA strategy: ladder math, validation, and the cycle lifecycle."""

import pytest

from app.bot_engine.dca_runtime import (
    build_dca_ladder,
    get_effective_dca_settings,
    get_planned_ladder_qty,
    validate_dca_configuration,
)
from app.bot_engine.orders import ACTIVE_ORDER_STATUSES
from app.bot_engine.bot import tick_bot_once
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder
from tests.remote_lookup import RemoteStateExchange


def _make_bot(**overrides) -> TradingBot:
    values = {
        "user_id": 1,
        "name": "DCA test bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "dca",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 10.0,
        "grid_orders_count": 3,
        "grid_step_percent": 2.0,
        "settings": {},
    }
    values.update(overrides)
    return TradingBot(**values)


def test_ladder_quantities_follow_volume_multiplier():
    bot = _make_bot(settings={"dca_volume_multiplier": 1.5, "dca_step_multiplier": 1.0})
    ladder = build_dca_ladder(bot, anchor_price=100.0)

    assert [level["level"] for level in ladder] == [1, 2, 3, 4]
    assert [level["qty"] for level in ladder] == [10.0, 15.0, 22.5, 33.75]
    assert ladder[0]["price"] is None


def test_ladder_prices_follow_step_multiplier():
    bot = _make_bot(settings={"dca_volume_multiplier": 1.0, "dca_step_multiplier": 1.3})
    ladder = build_dca_ladder(bot, anchor_price=100.0)

    # Deviations: 2%, 2% + 2.6% = 4.6%, 4.6% + 3.38% = 7.98%.
    prices = [level["price"] for level in ladder[1:]]
    assert prices == pytest.approx([98.0, 95.4, 92.02])


def test_ladder_with_neutral_multipliers_matches_plain_grid_spacing():
    bot = _make_bot(settings={"dca_volume_multiplier": 1.0, "dca_step_multiplier": 1.0})
    ladder = build_dca_ladder(bot, anchor_price=100.0)

    assert [level["qty"] for level in ladder] == [10.0, 10.0, 10.0, 10.0]
    assert [level["price"] for level in ladder[1:]] == pytest.approx([98.0, 96.0, 94.0])


def test_planned_ladder_qty_drives_default_max_position():
    bot = _make_bot(settings={"dca_volume_multiplier": 1.5, "dca_step_multiplier": 1.0})
    settings = get_effective_dca_settings(bot)

    assert get_planned_ladder_qty(bot) == pytest.approx(81.25)
    assert settings["max_position_qty"] == pytest.approx(81.25)
    assert settings["max_open_orders"] == 5  # 3 safety + TP + base order


def test_validation_rejects_broken_configuration():
    with pytest.raises(ValueError):
        validate_dca_configuration(_make_bot(settings={"dca_volume_multiplier": 0.5}))
    with pytest.raises(ValueError):
        validate_dca_configuration(_make_bot(settings={"dca_step_multiplier": 0.9}))
    with pytest.raises(ValueError):
        validate_dca_configuration(_make_bot(settings={"take_profit_percent": 0}))
    # A 40% first step drives the third order below zero.
    with pytest.raises(ValueError):
        validate_dca_configuration(_make_bot(grid_step_percent=40.0))
    validate_dca_configuration(_make_bot())


# --- Cycle lifecycle through the shared runtime with a mocked exchange ---


def _mock_dca_runtime(monkeypatch, *, remote_state=None, remote_positions=None, current_price=100.0, order_log=None):
    if remote_state is None:
        remote_state = {}
    if remote_positions is None:
        remote_positions = []
    if order_log is None:
        order_log = []

    def fake_cancel(remote_link_id):
        entry = remote_state.get(remote_link_id)
        if entry is not None:
            entry["orderStatus"] = "Cancelled"
        return {"result": {}}

    rules = type("Rules", (), {
        "min_order_qty": 0.001,
        "qty_step": 0.001,
        "tick_size": 0.01,
        "min_notional_value": 0,
    })()

    def fake_place_neutral(order):
        order_log.append({
            "client_tag": order.client_tag,
            "side": order.side,
            "order_type": order.order_type,
            "qty": order.qty,
            "price": order.price,
        })
        status = "Filled" if order.order_type == "Market" else "New"
        remote_state[order.client_tag] = {
            "orderLinkId": order.client_tag,
            "orderId": f"oid-{order.client_tag}",
            "orderStatus": status,
            "side": order.side,
            "orderType": order.order_type,
            "qty": str(order.qty),
            "cumExecQty": str(order.qty) if status == "Filled" else "0",
            "price": str(order.price) if order.price is not None else None,
            "reduceOnly": order.reduce_only,
        }
        return status

    adapter = RemoteStateExchange(
        remote_state, remote_positions=remote_positions, price=current_price, rules=rules,
        place=fake_place_neutral, cancel=fake_cancel,
    )
    monkeypatch.setattr("app.bot_engine.exchange.registry.exchange_for", lambda _bot, needs=None: adapter)
    return remote_state, remote_positions, order_log


def _create_dca_bot(client, auth_headers, extra=None):
    payload = {
        "name": "BTC DCA Bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "dca",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 0.01,
        "grid_orders_count": 2,
        "grid_step_percent": 2,
        "is_active": True,
        "settings": {"dca_volume_multiplier": 1.5, "dca_step_multiplier": 1.0, "take_profit_percent": 1.5, "run_interval_seconds": 0},
    }
    payload.update(extra or {})
    response = client.post("/api/bots", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _tick(bot_id):
    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        tick_bot_once(db, bot)
    finally:
        db.close()


def test_create_dca_bot_via_api(client, auth_headers):
    bot = _create_dca_bot(client, auth_headers)
    assert bot["strategy_type"] == "dca"
    assert bot["settings"]["dca_volume_multiplier"] == 1.5
    assert bot["settings"]["max_position_qty"] == pytest.approx(0.01 + 0.015 + 0.0225)


def test_dca_cycle_places_base_market_order_and_safety_ladder(client, auth_headers, monkeypatch):
    remote_state, remote_positions, order_log = _mock_dca_runtime(monkeypatch)
    bot = _create_dca_bot(client, auth_headers)

    start = client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)
    assert start.status_code == 200, start.text

    _tick(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entries = sorted(
        (order for order in orders if order["order_role"].startswith("dca_entry_")),
        key=lambda order: order["order_role"],
    )
    assert len(entries) == 3
    base, safety_1, safety_2 = entries
    assert base["order_type"] == "Market"
    assert base["status"] == "Filled"
    assert safety_1["order_type"] == "Limit"
    assert safety_1["price"] == pytest.approx(98.0)
    assert safety_1["qty"] == pytest.approx(0.015)
    assert safety_2["price"] == pytest.approx(96.0)
    assert safety_2["qty"] == pytest.approx(0.022)  # 0.0225 rounded down to qty_step

    remote_positions[:] = [{"side": "Buy", "size": "0.01", "avgPrice": "100.0"}]
    _tick(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    tps = [order for order in orders if order["order_role"] == "position_take_profit"]
    assert len(tps) == 1
    assert tps[0]["price"] == pytest.approx(101.5)
    assert tps[0]["qty"] == pytest.approx(0.01)


def test_dca_ladder_is_not_recreated_inside_a_cycle(client, auth_headers, monkeypatch):
    remote_state, remote_positions, _ = _mock_dca_runtime(monkeypatch)
    bot = _create_dca_bot(client, auth_headers)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick(bot["id"])
    remote_positions[:] = [{"side": "Buy", "size": "0.01", "avgPrice": "100.0"}]
    _tick(bot["id"])
    _tick(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entries = [order for order in orders if order["order_role"].startswith("dca_entry_")]
    assert len(entries) == 3
    assert len({order["order_link_id"] for order in entries}) == 3


def test_filled_take_profit_rolls_the_cycle_over(client, auth_headers, monkeypatch):
    remote_state, remote_positions, _ = _mock_dca_runtime(monkeypatch)
    bot = _create_dca_bot(client, auth_headers)
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick(bot["id"])
    remote_positions[:] = [{"side": "Buy", "size": "0.01", "avgPrice": "100.0"}]
    _tick(bot["id"])

    for remote in remote_state.values():
        if remote.get("reduceOnly"):
            remote["orderStatus"] = "Filled"
            remote["cumExecQty"] = remote["qty"]
    remote_positions[:] = []

    _tick(bot["id"])  # sync fill + cancel stale safety orders
    _tick(bot["id"])  # confirm cancellation + open the next cycle

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entries = [order for order in orders if order["order_role"].startswith("dca_entry_")]
    active_entries = [order for order in entries if order["status"] in ACTIVE_ORDER_STATUSES]
    cancelled_entries = [order for order in entries if order["status"] == "Cancelled"]
    filled_bases = [order for order in entries if order["order_type"] == "Market" and order["status"] == "Filled"]

    assert len(entries) == 6
    assert len(cancelled_entries) == 2
    assert len(filled_bases) == 2
    assert len(active_entries) == 2

    events = client.get(f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["event_type"] == "dca_cycle_completed" for event in events)


def test_risk_limit_blocks_ladder_orders(client, auth_headers, monkeypatch):
    remote_state, _, _ = _mock_dca_runtime(monkeypatch)
    bot = _create_dca_bot(
        client,
        auth_headers,
        extra={"settings": {
            "dca_volume_multiplier": 1.5,
            "dca_step_multiplier": 1.0,
            "take_profit_percent": 1.5,
            "run_interval_seconds": 0,
            # Fits the base and the first safety order only.
            "max_position_qty": 0.025,
        }},
    )
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    entries = [order for order in orders if order["order_role"].startswith("dca_entry_")]
    assert len(entries) == 2  # the last safety order exceeds the limit

    events = client.get(f"/api/bots/{bot['id']}/events", headers=auth_headers).json()
    assert any(event["event_type"] == "risk_blocked" for event in events)


def test_grid_strategy_still_works_after_dca_changes(client, auth_headers, monkeypatch):
    """A regression guard: the shared rollover keeps its grid defaults."""
    remote_state, remote_positions, _ = _mock_dca_runtime(monkeypatch)
    payload = {
        "name": "BTC Grid Bot",
        "exchange": "bybit",
        "environment": "demo",
        "strategy_type": "grid",
        "category": "linear",
        "symbol": "BTCUSDT",
        "order_qty": 0.01,
        "grid_orders_count": 2,
        "grid_step_percent": 5,
        "is_active": True,
        "settings": {"run_interval_seconds": 0},
    }
    response = client.post("/api/bots", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    bot = response.json()
    client.post(f"/api/bots/{bot['id']}/start", headers=auth_headers)

    _tick(bot["id"])

    orders = client.get(f"/api/bots/{bot['id']}/orders", headers=auth_headers).json()
    grid_entries = [order for order in orders if order["order_role"].startswith("grid_entry_")]
    assert len(grid_entries) == 2
    assert all(order["order_type"] == "Limit" for order in grid_entries)
