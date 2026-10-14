"""Core API smoke tests."""


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"


def test_venue_connection_endpoint(client, auth_headers, monkeypatch):
    # The demo-only /api/bybit/test became the venue connection check (sad.md §5, spec §6).
    from app.bot_engine.exchange.port import ConnectionStatus

    monkeypatch.setattr(
        "app.bot_engine.exchange.bybit.adapter.BybitAdapter.check_connection",
        lambda self: ConnectionStatus("connected"),
    )
    response = client.get("/api/venues/bybit/demo/connection", headers=auth_headers)
    assert response.status_code == 200


def test_backtests_require_auth(client):
    assert client.get("/api/backtests").status_code == 401
    assert client.post("/api/backtests", json={}).status_code == 401


def test_unknown_backtest_returns_404(client, auth_headers):
    assert client.get("/api/backtests/999999", headers=auth_headers).status_code == 404
