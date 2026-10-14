import uuid

from app.bot_engine.error_handling import handle_bot_runtime_error
from app.bot_engine.exchange.bybit.errors import classify_bybit_failure
from app.db.session import SessionLocal
from app.models.audit_event import AuditEvent
from app.models.operations import Incident, OperationLog
from app.models.trading_bot import TradingBot
from app.models.user import User


def _login(client):
    email = f"compliance_{uuid.uuid4().hex[:10]}@test.dev"
    password = "secret123"
    registered = client.post("/api/auth/register", json={"email": email, "password": password})
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    login = client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, email, password, user_id


def _create_bot(client, headers):
    response = client.post("/api/bots", headers=headers, json={
        "name": "Compliance Bot", "exchange": "bybit", "environment": "demo", "strategy_type": "grid",
        "category": "linear", "symbol": "BTCUSDT", "order_qty": 0.001,
        "grid_orders_count": 2, "grid_step_percent": 5, "is_active": True,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_compliance_overview_and_operations_log(client):
    headers, _, _, _ = _login(client)
    overview = client.get("/api/compliance/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["reference_jurisdiction"] == "EU"
    assert data["retention_matrix"]
    assert data["hosting_target_region"] == "EU/EEA"

    client.get("/api/auth/me", headers=headers)
    logs = client.get("/api/operations/logs?page_size=200", headers=headers)
    assert logs.status_code == 200, logs.text
    assert logs.json()["items"]
    assert any(item["request_id"] for item in logs.json()["items"])


def test_config_change_has_explicit_before_after_diff(client):
    headers, _, _, _ = _login(client)
    bot = _create_bot(client, headers)
    changed = client.put(f"/api/bots/{bot['id']}", headers=headers, json={"order_qty": 0.002, "grid_step_percent": 2.5})
    assert changed.status_code == 200, changed.text
    events = client.get(f"/api/audit/events?event_type=BOT_CONFIG_CHANGED&bot_id={bot['id']}", headers=headers).json()["items"]
    assert events
    payload = events[0]["payload"]
    assert payload["before_config_hash"] != payload["after_config_hash"]
    by_field = {item["field"]: item for item in payload["changes"]}
    assert by_field["order_qty"]["before"] == 0.001
    assert by_field["order_qty"]["after"] == 0.002
    assert by_field["grid_step_percent"]["before"] == 5.0
    assert by_field["grid_step_percent"]["after"] == 2.5


def test_error_incident_is_created_and_exposed(client):
    headers, _, _, user_id = _login(client)
    bot = _create_bot(client, headers)
    db = SessionLocal()
    try:
        item = db.get(TradingBot, bot["id"])
        assert item is not None and item.user_id == user_id
        handle_bot_runtime_error(db, item, classify_bybit_failure(RuntimeError("insufficient balance (ErrCode: 110007)")), source="test")
    finally:
        db.close()

    result = client.get(f"/api/operations/incidents?bot_id={bot['id']}", headers=headers)
    assert result.status_code == 200, result.text
    incidents = result.json()["items"]
    assert incidents
    assert incidents[0]["incident_type"] == "INSUFFICIENT_FUNDS"
    assert incidents[0]["status"] == "OPEN"
    assert incidents[0]["action_taken"] == "pause"


def test_data_export_excludes_credentials_and_account_delete_preserves_audit(client):
    headers, email, password, user_id = _login(client)
    _create_bot(client, headers)
    exported = client.get("/api/privacy/export", headers=headers)
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    assert payload["profile"]["email"] == email
    raw = exported.text.lower()
    assert "hashed_password" not in raw
    assert "token_hash" not in raw
    assert "secret123" not in raw
    assert payload["regulatory_audit"]

    deleted = client.post("/api/privacy/delete-account", headers=headers, json={"password": password, "confirmation": "DELETE"})
    assert deleted.status_code == 204, deleted.text

    db = SessionLocal()
    try:
        assert db.get(User, user_id) is None
        assert db.query(OperationLog).filter(OperationLog.user_id == user_id).count() == 0
        assert db.query(Incident).filter(Incident.user_id == user_id).count() == 0
        retained = db.query(AuditEvent).filter(AuditEvent.user_id == user_id).all()
        assert retained
        assert any(event.event_type == "ACCOUNT_DELETION_REQUESTED" for event in retained)
    finally:
        db.close()
