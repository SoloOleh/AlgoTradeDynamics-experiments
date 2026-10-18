"""Connection check per venue (T18; AC-12, AC-13, AC-13b, AC-14, AC-16).

`GET /api/venues/{exchange}/{environment}/connection` replaces the demo-only `/api/bybit/test`. It
names the venue, never carries a raw exchange message, a key or a secret, and persists nothing.
"""
from __future__ import annotations

import pytest

from tests.contract_schema import assert_response_matches
from tests.golden.failure_table.rows import build_exception

KEY = "VenueCheckKey00wxyz"
SECRET = "VenueCheckSecret-77aa"


@pytest.fixture()
def sessions(monkeypatch):
    """Exchange sessions the adapter opens, answering from a script; records every request."""
    calls: list[tuple[str, str]] = []
    answers: dict[str, object] = {}

    class Session:
        def __init__(self, environment):
            self.environment = environment

        def get_wallet_balance(self, **params):
            calls.append((self.environment, "get_wallet_balance"))
            answer = answers.get(self.environment, {"retCode": 0, "retMsg": "OK", "result": {"list": []}})
            if isinstance(answer, BaseException):
                raise answer
            return answer

    def create_session(environment, *, emulator_api_key=None):
        return Session(environment)

    monkeypatch.setattr("app.bot_engine.exchange.bybit.adapter.create_session", create_session)
    for environment in ("DEMO", "TESTNET", "LIVE"):
        monkeypatch.delenv(f"BYBIT_{environment}_API_KEY", raising=False)
        monkeypatch.delenv(f"BYBIT_{environment}_API_SECRET", raising=False)
    return calls, answers


def _check(client, headers, exchange, environment):
    """Every answer, whatever its status, is one the contract allows."""
    response = client.get(f"/api/venues/{exchange}/{environment}/connection", headers=headers)
    assert_response_matches("getVenueConnection", response)
    return response


def test_demo_with_keys_is_connected(client, auth_headers, sessions, monkeypatch):
    calls, _ = sessions
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", SECRET)

    response = _check(client, auth_headers, "bybit", "demo")

    assert response.status_code == 200
    assert response.json() == {
        "venue": {"exchange": "bybit", "environment": "demo"},
        "status": "connected",
        "error_class": None,
        "message": "Venue bybit/demo is connected",
    }
    assert calls == [("demo", "get_wallet_balance")]


def test_venue_without_keys_is_not_configured_and_sends_nothing(client, auth_headers, sessions):
    calls, _ = sessions

    response = _check(client, auth_headers, "bybit", "live")

    assert response.status_code == 200
    assert response.json() == {
        "venue": {"exchange": "bybit", "environment": "live"},
        "status": "not_configured",
        "error_class": None,
        "message": "Venue bybit/live has no exchange keys configured",
    }
    assert calls == []


def test_failing_exchange_answers_with_the_error_class_only(client, auth_headers, sessions, monkeypatch):
    calls, answers = sessions
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", SECRET)
    answers["testnet"] = build_exception({
        "kind": "pybit_invalid_request", "code": 10003,
        "message": f"API key is invalid. api_key={KEY} secret={SECRET}",
    })

    response = _check(client, auth_headers, "bybit", "testnet")

    assert response.status_code == 200
    assert response.json() == {
        "venue": {"exchange": "bybit", "environment": "testnet"},
        "status": "failing",
        "error_class": "rejected_credentials",
        "message": "Venue bybit/testnet is failing: rejected_credentials",
    }
    assert KEY not in response.text and SECRET not in response.text and "invalid" not in response.text
    assert calls == [("testnet", "get_wallet_balance")]


def test_refusal_in_the_answer_body_is_failing_too(client, auth_headers, sessions, monkeypatch):
    _, answers = sessions
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", SECRET)
    answers["demo"] = {"retCode": 10003, "retMsg": f"API key {KEY} is invalid", "result": {}}

    body = _check(client, auth_headers, "bybit", "demo").json()

    assert (body["status"], body["error_class"]) == ("failing", "rejected_credentials")
    assert KEY not in str(body)


def test_unrecognised_refusal_in_the_answer_body_is_failing_as_unavailable(client, auth_headers, sessions, monkeypatch):
    """An unmapped code is reported like an unclassified transport failure, never as an invalid order."""
    _, answers = sessions
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", SECRET)
    answers["demo"] = {"retCode": 170134, "retMsg": f"Unexpected answer for {KEY}", "result": {}}

    body = _check(client, auth_headers, "bybit", "demo").json()

    assert (body["status"], body["error_class"]) == ("failing", "exchange_unavailable")
    assert KEY not in str(body)


def test_network_failure_is_failing_with_its_class(client, auth_headers, sessions, monkeypatch):
    _, answers = sessions
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", SECRET)
    answers["demo"] = build_exception({
        "kind": "requests_timeout", "message": "HTTPSConnectionPool(host='api.bybit.com', port=443): Read timed out.",
    })

    body = _check(client, auth_headers, "bybit", "demo").json()

    assert (body["status"], body["error_class"]) == ("failing", "network")
    assert "Read timed out" not in body["message"]


def test_venue_without_adapter_is_not_supported_and_sends_nothing(client, auth_headers, sessions):
    calls, _ = sessions

    response = _check(client, auth_headers, "binance", "live")

    assert response.status_code == 200
    assert response.json() == {
        "venue": {"exchange": "binance", "environment": "live"},
        "status": "not_supported",
        "error_class": None,
        "message": "Venue binance/live has no exchange adapter",
    }
    assert calls == []


def test_emulator_venue_checks_its_default_account(client, auth_headers, sessions):
    calls, _ = sessions

    body = _check(client, auth_headers, "bybit", "emulator").json()

    assert body["status"] == "connected"
    assert calls == [("emulator", "get_wallet_balance")]


def test_anonymous_refusal_reveals_nothing_about_configured_venues(client, sessions, monkeypatch):
    calls, _ = sessions
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", SECRET)

    configured = _check(client, {}, "bybit", "demo")
    unconfigured = _check(client, {}, "bybit", "live")
    unsupported = _check(client, {}, "binance", "live")

    assert configured.status_code == unconfigured.status_code == unsupported.status_code == 401
    assert configured.content == unconfigured.content == unsupported.content
    assert calls == []


def test_too_long_environment_is_a_validation_error(client, auth_headers, sessions):
    assert _check(client, auth_headers, "bybit", "e" * 21).status_code == 422


def test_the_demo_only_route_is_gone(client, auth_headers):
    assert client.get("/api/bybit/test", headers=auth_headers).status_code == 404


# ------------------------------------------------ through the real client path, against the emulator

WRONG_EMULATOR_KEY = "emu_NotARealAccountKey0123456789"


@pytest.fixture(scope="module")
def emulator_process(tmp_path_factory):
    """A real exchange emulator subprocess on its own throwaway database (test-plan.md «Integration»)."""
    from tests.golden.harness import Emulator

    emulator = Emulator(tmp_path_factory.mktemp("venue-check") / "emulator.db")
    try:
        yield emulator
    finally:
        emulator.stop()


@pytest.fixture()
def emulator_venue(monkeypatch):
    """Point the backend's emulator venue at a URL and an account key, then call the check."""
    from app.core.config import get_settings

    settings = get_settings()

    def point(url: str, key: str):
        monkeypatch.setattr(settings, "exchange_emulator_url", url)
        monkeypatch.setattr(settings, "exchange_emulator_default_api_key", key)

    return point


def test_emulator_venue_with_its_real_account_is_connected(client, auth_headers, emulator_process, emulator_venue):
    emulator_venue(emulator_process.url, "emulator-default-key")

    body = _check(client, auth_headers, "bybit", "emulator").json()

    assert (body["status"], body["error_class"]) == ("connected", None)


def test_emulator_rejecting_the_key_is_failing_with_rejected_credentials(client, auth_headers, emulator_process, emulator_venue):
    emulator_venue(emulator_process.url, WRONG_EMULATOR_KEY)

    response = _check(client, auth_headers, "bybit", "emulator")

    assert response.json() == {
        "venue": {"exchange": "bybit", "environment": "emulator"},
        "status": "failing",
        "error_class": "rejected_credentials",
        "message": "Venue bybit/emulator is failing: rejected_credentials",
    }
    assert WRONG_EMULATOR_KEY not in response.text


def test_emulator_on_a_closed_port_is_failing_with_network(client, auth_headers, emulator_venue):
    from tests.golden.harness import _free_port

    emulator_venue(f"http://127.0.0.1:{_free_port()}", "emulator-default-key")

    response = _check(client, auth_headers, "bybit", "emulator")

    assert (response.json()["status"], response.json()["error_class"]) == ("failing", "network")
    assert "emulator-default-key" not in response.text and "127.0.0.1" not in response.text
