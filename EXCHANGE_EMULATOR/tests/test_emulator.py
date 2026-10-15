from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = Path(tempfile.gettempdir()) / "algotrade_emulator_pytest.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["EMULATOR_DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_manual_matching_scenario_and_replay():
    with TestClient(app) as client:
        account = client.get("/api/admin/accounts").json()[0]
        headers = {"X-API-Key": account["api_key"]}

        created = client.post(
            "/v5/order/create",
            headers=headers,
            json={
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": "Buy",
                "orderType": "Limit",
                "qty": "0.01",
                "price": "3400",
                "orderLinkId": "pytest-entry-1",
            },
        ).json()
        assert created["retCode"] == 0

        client.post("/api/admin/markets/ETHUSDT/price", json={"price": 3300}).raise_for_status()
        positions = client.get(
            "/v5/position/list",
            headers=headers,
            params={"category": "linear", "symbol": "ETHUSDT"},
        ).json()["result"]["list"]
        assert positions[0]["size"] == "0.01"

        scenario = client.post(
            "/api/admin/scenarios",
            json={
                "name": "pytest scenario",
                "symbol": "ETHUSDT",
                "steps": [
                    {"price": 3200, "duration_seconds": 0},
                    {"price": 3500, "duration_seconds": 0},
                ],
            },
        ).json()
        client.post(f"/api/admin/scenarios/{scenario['id']}/start").raise_for_status()
        time.sleep(0.5)
        dashboard = client.get(
            f"/api/admin/dashboard?account_id={account['id']}&symbol=ETHUSDT"
        ).json()
        assert dashboard["market"]["status"] == "completed"

        csv_content = (
            "date,open,high,low,close,volume\n"
            "2024-01-01,3000,3100,2900,3050,10\n"
            "2024-01-02,3050,3200,3000,3150,12\n"
        )
        imported = client.post(
            "/api/admin/historical/import-csv?symbol=ETHUSDT&interval=D",
            files={"file": ("history.csv", csv_content, "text/csv")},
        ).json()
        assert imported["inserted"] == 2

        datasets = client.get("/api/admin/historical/datasets").json()
        dataset = next(item for item in datasets if item["symbol"] == "ETHUSDT" and item["interval"] == "D")
        replay = client.post(
            "/api/admin/markets/ETHUSDT/replay/start",
            json={
                "dataset_id": dataset["id"],
                "start_time": dataset["from_time"],
                "end_time": dataset["to_time"],
                "speed": 100,
                "path_mode": "ohlc",
            },
        ).json()
        assert replay["total"] == 2
        time.sleep(0.5)
        dashboard = client.get(
            f"/api/admin/dashboard?account_id={account['id']}&symbol=ETHUSDT"
        ).json()
        assert dashboard["market"]["status"] == "completed"


def test_execution_sequence_preserves_same_timestamp_order():
    with TestClient(app) as client:
        account = client.post(
            "/api/admin/accounts",
            json={"name": "Sequence test", "initial_balance": 10000},
        ).json()
        headers = {"X-API-Key": account["api_key"]}
        simulation_time = 1_704_067_200_000

        client.post(
            "/api/admin/markets/BTCUSDT/price",
            json={
                "price": 50000,
                "mark_price": 50000,
                "simulation_time": simulation_time,
                "account_id": account["id"],
            },
        ).raise_for_status()

        buy = client.post(
            "/v5/order/create",
            headers=headers,
            json={
                "category": "linear",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderType": "Limit",
                "qty": "0.001",
                "price": "50000",
                "orderLinkId": "sequence-buy",
            },
        ).json()
        assert buy["retCode"] == 0

        sell = client.post(
            "/v5/order/create",
            headers=headers,
            json={
                "category": "linear",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "orderType": "Limit",
                "qty": "0.001",
                "price": "50000",
                "reduceOnly": True,
                "orderLinkId": "sequence-sell",
            },
        ).json()
        assert sell["retCode"] == 0

        ascending = client.get(
            "/api/admin/executions",
            params={
                "account_id": account["id"],
                "symbol": "BTCUSDT",
                "after_sequence": 0,
                "limit": 10,
            },
        ).json()
        assert [item["side"] for item in ascending] == ["Buy", "Sell"]
        assert ascending[0]["execTime"] == ascending[1]["execTime"] == simulation_time
        assert ascending[0]["execSeq"] < ascending[1]["execSeq"]

        descending = client.get(
            "/api/admin/executions",
            params={"account_id": account["id"], "symbol": "BTCUSDT", "limit": 10},
        ).json()
        assert [item["side"] for item in descending] == ["Sell", "Buy"]


def test_short_position_profit_and_reduce_only_close():
    with TestClient(app) as client:
        account = client.post(
            "/api/admin/accounts",
            json={"name": "Short position test", "initial_balance": 10000},
        ).json()
        headers = {"X-API-Key": account["api_key"]}

        client.post(
            "/api/admin/markets/ETHUSDT/price",
            json={"price": 2000, "mark_price": 2000, "account_id": account["id"]},
        ).raise_for_status()

        opened = client.post(
            "/v5/order/create",
            headers=headers,
            json={
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": "Sell",
                "orderType": "Market",
                "qty": "0.01",
                "orderLinkId": "short-entry",
            },
        ).json()
        assert opened["retCode"] == 0

        client.post(
            "/api/admin/markets/ETHUSDT/price",
            json={"price": 1900, "mark_price": 1900, "account_id": account["id"]},
        ).raise_for_status()

        position = client.get(
            "/v5/position/list",
            headers=headers,
            params={"category": "linear", "symbol": "ETHUSDT"},
        ).json()["result"]["list"][0]
        assert position["side"] == "Sell"
        assert position["size"] == "0.01"
        assert float(position["unrealisedPnl"]) > 0

        closed = client.post(
            "/v5/order/create",
            headers=headers,
            json={
                "category": "linear",
                "symbol": "ETHUSDT",
                "side": "Buy",
                "orderType": "Market",
                "qty": "0.01",
                "reduceOnly": True,
                "orderLinkId": "short-close",
            },
        ).json()
        assert closed["retCode"] == 0

        position = client.get(
            "/v5/position/list",
            headers=headers,
            params={"category": "linear", "symbol": "ETHUSDT"},
        ).json()["result"]["list"][0]
        assert position["size"] == "0"

        wallet = client.get(
            "/v5/account/wallet-balance",
            headers=headers,
            params={"accountType": "UNIFIED"},
        ).json()["result"]["list"][0]
        assert float(wallet["totalWalletBalance"]) > 10000

        first_page = client.get(
            "/v5/execution/list",
            headers=headers,
            params={"category": "linear", "symbol": "ETHUSDT", "limit": 1},
        ).json()["result"]
        assert len(first_page["list"]) == 1
        assert first_page["nextPageCursor"]
        second_page = client.get(
            "/v5/execution/list",
            headers=headers,
            params={
                "category": "linear", "symbol": "ETHUSDT", "limit": 1,
                "cursor": first_page["nextPageCursor"],
            },
        ).json()["result"]
        assert len(second_page["list"]) == 1
        assert second_page["list"][0]["execId"] != first_page["list"][0]["execId"]



def test_datasets_with_same_symbol_interval_are_isolated():
    with TestClient(app) as client:
        account = client.post(
            "/api/admin/accounts",
            json={"name": "Dataset isolation", "initial_balance": 10000},
        ).json()
        headers = {"X-API-Key": account["api_key"]}
        csv_a = (
            "open_time,open,high,low,close,volume\n"
            "1704067200000,100,111,99,110,10\n"
            "1704067500000,110,121,109,120,12\n"
        )
        csv_b = (
            "open_time,open,high,low,close,volume\n"
            "1704067200000,200,211,199,210,20\n"
            "1704067500000,210,221,209,220,22\n"
        )
        first = client.post(
            "/api/admin/historical/import-csv?symbol=BTCUSDT&interval=5&name=Dataset%20A",
            files={"file": ("a.csv", csv_a, "text/csv")},
        ).json()["dataset"]
        second = client.post(
            "/api/admin/historical/import-csv?symbol=BTCUSDT&interval=5&name=Dataset%20B",
            files={"file": ("b.csv", csv_b, "text/csv")},
        ).json()["dataset"]
        assert first["id"] != second["id"]
        assert first["candles"] == second["candles"] == 2

        client.post(
            "/api/admin/markets/BTCUSDT/price",
            json={
                "price": 220,
                "account_id": account["id"],
                "simulation_time": 1704067800000,
                "dataset_id": second["id"],
            },
        ).raise_for_status()
        selected_rows = client.get(
            "/v5/market/kline",
            headers=headers,
            params={"category": "linear", "symbol": "BTCUSDT", "interval": "5", "end": 1704067800000},
        ).json()["result"]["list"]
        assert [float(row[4]) for row in selected_rows] == [220.0, 210.0]

        client.post(
            "/api/admin/markets/BTCUSDT/price",
            json={
                "price": 120,
                "account_id": account["id"],
                "simulation_time": 1704067800000,
                "dataset_id": first["id"],
            },
        ).raise_for_status()
        selected_rows = client.get(
            "/v5/market/kline",
            headers=headers,
            params={"category": "linear", "symbol": "BTCUSDT", "interval": "5", "end": 1704067800000},
        ).json()["result"]["list"]
        assert [float(row[4]) for row in selected_rows] == [120.0, 110.0]


def test_dataset_range_quality_detects_missing_candles():
    with TestClient(app) as client:
        csv_content = (
            "open_time,open,high,low,close,volume\n"
            "1704067200000,100,101,99,100,10\n"
            "1704067800000,100,102,99,101,11\n"
        )
        dataset = client.post(
            "/api/admin/historical/import-csv?symbol=SOLUSDT&interval=5&name=Gap%20dataset",
            files={"file": ("gap.csv", csv_content, "text/csv")},
        ).json()["dataset"]
        assert dataset["missing_candles"] == 1
        stats = client.get(
            f"/api/admin/historical/datasets/{dataset['id']}/range-stats",
            params={"start_time": dataset["from_time"], "end_time": dataset["to_time"]},
        ).json()
        assert stats["count"] == 2
        assert stats["missing_candles"] == 1
        assert stats["valid"] is False


def test_dataset_range_quality_detects_missing_boundary_candle():
    with TestClient(app) as client:
        csv_content = (
            "open_time,open,high,low,close,volume\n"
            "1704070800000,100,101,99,100,10\n"  # 01:00
            "1704071100000,100,101,99,100,10\n"  # 01:05
            # 01:10 is intentionally missing
            "1704071700000,100,101,99,100,10\n"  # 01:15
            "1704072000000,100,101,99,100,10\n"  # 01:20
        )
        dataset = client.post(
            "/api/admin/historical/import-csv?symbol=XRPUSDT&interval=5&name=Boundary%20gap",
            files={"file": ("boundary-gap.csv", csv_content, "text/csv")},
        ).json()["dataset"]

        # The selected sub-range begins exactly on the missing 01:10 candle.
        stats = client.get(
            f"/api/admin/historical/datasets/{dataset['id']}/range-stats",
            params={"start_time": 1704071400000, "end_time": 1704072000000},
        ).json()
        assert stats["count"] == 2
        assert stats["expected_candles"] == 3
        assert stats["missing_candles"] == 1
        assert stats["valid"] is False


def test_bybit_like_websocket_public_and_private_streams():
    with TestClient(app) as client:
        account = client.post(
            "/api/admin/accounts",
            json={"name": "WebSocket test", "initial_balance": 10000},
        ).json()
        api_key = account["api_key"]
        headers = {"X-API-Key": api_key}

        with client.websocket_connect(f"/v5/public/linear?api_key={api_key}") as public_ws:
            public_ws.send_json({"op": "subscribe", "args": ["tickers.BTCUSDT"]})
            ack = public_ws.receive_json()
            assert ack["success"] is True
            snapshot = public_ws.receive_json()
            assert snapshot["topic"] == "tickers.BTCUSDT"
            assert snapshot["data"]["symbol"] == "BTCUSDT"

            client.post(
                "/api/admin/markets/BTCUSDT/price",
                json={"price": 60000, "mark_price": 60000},
            ).raise_for_status()
            update = public_ws.receive_json()
            assert update["topic"] == "tickers.BTCUSDT"
            assert update["data"]["lastPrice"] == "60000"

        with client.websocket_connect(f"/v5/private?api_key={api_key}") as private_ws:
            private_ws.send_json({"op": "subscribe", "args": ["order", "execution", "position", "wallet"]})
            ack = private_ws.receive_json()
            assert ack["success"] is True

            created = client.post(
                "/v5/order/create",
                headers=headers,
                json={
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "orderType": "Market",
                    "qty": "0.001",
                    "orderLinkId": "websocket-market-entry",
                },
            ).json()
            assert created["retCode"] == 0

            topics = set()
            for _ in range(8):
                message = private_ws.receive_json()
                if message.get("topic"):
                    topics.add(message["topic"])
                if {"order", "execution", "position", "wallet"}.issubset(topics):
                    break
            assert {"order", "execution", "position", "wallet"}.issubset(topics)
