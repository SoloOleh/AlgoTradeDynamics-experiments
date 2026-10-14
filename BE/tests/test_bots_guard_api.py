"""The venue guard at start and at every Operator bot action (T08; AC-09, AC-11, AC-15)."""
from __future__ import annotations

import uuid

import pytest

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.capabilities import Capability, CapabilitySet, MarketType
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot

GUARDED_ROUTES = [
    ("post", "stop", None),
    ("post", "sync", None),
    ("post", "cancel-orders", None),
    ("post", "close-position", {"confirm": True}),
    ("get", "position", None),
    ("get", "risk", None),
]
ALL_EXCHANGE_ROUTES = GUARDED_ROUTES + [("post", "start", None), ("get", "performance", None), ("post", "clear-history", None)]


class StandIn:
    def __init__(self, declared: CapabilitySet) -> None:
        self.calls: list[str] = []
        self._declared = declared

    def capabilities(self) -> CapabilitySet:
        return self._declared

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return []
        return record


@pytest.fixture()
def no_exchange_traffic(monkeypatch):
    import httpx2

    original = httpx2.Client.send

    def guarded(client, request, *args, **kwargs):
        if "/v5/" in request.url.path:
            raise AssertionError(f"exchange request sent: {request.url.path}")
        return original(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx2.Client, "send", guarded)


def _headers(client) -> dict:
    email = f"guard_{uuid.uuid4().hex[:10]}@test.dev"
    client.post("/api/auth/register", json={"email": email, "password": "secret123"})
    token = client.post("/api/auth/login", json={"email": email, "password": "secret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _bot(client, headers, **venue) -> int:
    payload = {
        "name": "guarded", "exchange": "bybit", "environment": "emulator", "strategy_type": "grid",
        "category": "linear", "symbol": "BTCUSDT", "order_qty": 0.01, "grid_orders_count": 2,
        "grid_step_percent": 1, "is_active": True, "settings": {},
    }
    created = client.post("/api/bots", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    bot_id = created.json()["id"]
    if venue:
        db = SessionLocal()
        try:
            bot = db.get(TradingBot, bot_id)
            for key, value in venue.items():
                setattr(bot, key, value)
            db.commit()
        finally:
            db.close()
    return bot_id


def _status(bot_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(TradingBot, bot_id).runtime_status
    finally:
        db.close()


def _call(client, method, bot_id, action, body, headers):
    url = f"/api/bots/{bot_id}/{action}"
    return client.post(url, json=body, headers=headers) if method == "post" else client.get(url, headers=headers)


def test_start_on_a_venue_without_an_adapter_is_refused_and_names_it(client, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="binance", environment="live")

    response = client.post(f"/api/bots/{bot_id}/start", headers=headers)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Venue binance/live has no exchange adapter",
        "code": "exchange.venue_unsupported",
        "details": {"exchange": "binance", "environment": "live"},
    }
    assert _status(bot_id) == "stopped"


def test_start_with_a_missing_capability_names_capability_and_market_type(client, no_exchange_traffic):
    stand_in = StandIn(CapabilitySet({MarketType.LINEAR: frozenset(Capability) - {Capability.POSITION_READS}}))
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="standin", environment="test")

    with registry.registered_venue("standin", "test", lambda account: stand_in):
        response = client.post(f"/api/bots/{bot_id}/start", headers=headers)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Capability position_reads is not available on market type linear for venue standin/test",
        "code": "exchange.capability_missing",
        "details": {"exchange": "standin", "environment": "test", "capability": "position_reads", "market_type": "linear"},
    }
    assert _status(bot_id) == "stopped"
    assert stand_in.calls == []


@pytest.mark.parametrize("method,action,body", GUARDED_ROUTES)
def test_every_operator_action_on_an_unsupported_venue_is_refused(client, method, action, body, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="binance", environment="live", runtime_status="running")

    response = _call(client, method, bot_id, action, body, headers)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "exchange.venue_unsupported"
    assert _status(bot_id) == "running", "a refused action leaves the bot as it was"


def test_close_position_without_confirm_keeps_todays_400(client, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="binance", environment="live")
    response = client.post(f"/api/bots/{bot_id}/close-position", json={"confirm": False}, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"detail": "Position close requires confirm=true"}


@pytest.mark.parametrize("method,action,body", ALL_EXCHANGE_ROUTES)
def test_another_users_bot_is_not_found_and_the_guard_is_never_reached(client, monkeypatch, method, action, body, no_exchange_traffic):
    owner, intruder = _headers(client), _headers(client)
    bot_id = _bot(client, owner, exchange="binance", environment="live")

    def guard_must_not_run(*args, **kwargs):
        raise AssertionError("guard reached for a bot the caller does not own")

    monkeypatch.setattr(registry, "exchange_for", guard_must_not_run)
    response = _call(client, method, bot_id, action, body, intruder)

    assert response.status_code == 404
    assert response.content == b'{"detail":"Trading bot not found"}'


@pytest.mark.parametrize("strategy", ["grid", "dca", "momentum", "pattern_scalper"])
def test_performance_on_an_unsupported_venue_returns_stored_figures(client, no_exchange_traffic, strategy):
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="binance", environment="live", strategy_type=strategy)
    response = client.get(f"/api/bots/{bot_id}/performance", headers=headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("strategy", ["grid", "dca", "momentum", "pattern_scalper"])
def test_a_running_bot_whose_venue_was_removed_keeps_a_readable_bot_page(
    client, no_exchange_traffic, monkeypatch, strategy,
):
    """Before the next tick pauses it, the bot page and the bot list still answer (AC-10)."""
    headers = _headers(client)
    bot_id = _bot(client, headers, strategy_type=strategy, runtime_status="running")
    monkeypatch.delitem(registry._VENUES, ("bybit", "emulator"))
    page = client.get(f"/api/bots/{bot_id}", headers=headers)
    assert page.status_code == 200, page.text
    listing = client.get("/api/bots", headers=headers)
    assert listing.status_code == 200, listing.text
    assert [bot["id"] for bot in listing.json()] == [bot_id]


def test_clear_history_on_an_unsupported_venue_still_clears_local_history(client, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers, exchange="binance", environment="live")
    response = client.post(f"/api/bots/{bot_id}/clear-history", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exchange_orders_cancelled"] == 0
    assert body["exchange_cancel_error"] == "Venue binance/live has no exchange adapter"


def test_a_bybit_bot_that_starts_today_still_starts(client, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers)
    response = client.post(f"/api/bots/{bot_id}/start", headers=headers)
    assert response.status_code == 200, response.text
    assert _status(bot_id) == "running"


def test_start_validations_keep_todays_bodies(client, no_exchange_traffic):
    headers = _headers(client)
    bot_id = _bot(client, headers, is_active=False)
    response = client.post(f"/api/bots/{bot_id}/start", headers=headers)
    assert response.status_code == 422
    assert response.json() == {"detail": "Trading bot is inactive"}
