"""Golden-run harness: drive a strategy against the real exchange emulator and record it.

The emulator runs as a local subprocess on its own SQLite file; the backend bot engine runs
in-process on a fresh SQLite file per run. Every request crossing the emulator client boundary
(`/v5/...` over httpx) is recorded with wall-clock timings, so the same hook records the code
before and after the exchange-adapter refactor (ADR-0007).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
EMULATOR_DIR = REPO_ROOT / "EXCHANGE_EMULATOR"
SIM_START = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_FILES = ("emulator.db", "emulator.db-wal", "emulator.db-shm", "backend.db")


@dataclass(frozen=True)
class Scenario:
    """A deterministic price path for one bot."""

    name: str
    strategy_type: str
    symbol: str
    start_price: float
    prices: list[float]
    bot: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    environment: str = "emulator"
    category: str = "linear"
    step_seconds: int = 60

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- emulator subprocess


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EmulatorError(RuntimeError):
    pass


class Emulator:
    """One running exchange emulator process bound to one SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._log = tempfile.NamedTemporaryFile(prefix="emulator-", suffix=".log", delete=False)
        env = {k: v for k, v in os.environ.items() if not k.startswith("EMULATOR_")}
        env["EMULATOR_DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=EMULATOR_DIR, env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        self.admin = httpx.Client(base_url=self.url, timeout=15.0)
        self._wait_ready()

    def _stderr(self) -> str:
        self._log.flush()
        return Path(self._log.name).read_text(errors="replace")[-4000:]

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise EmulatorError(f"emulator exited with {self.process.returncode}:\n{self._stderr()}")
            try:
                if self.admin.get("/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self.stop()
        raise EmulatorError(f"emulator did not become ready on port {self.port}:\n{self._stderr()}")

    def set_price(self, symbol: str, price: float, *, simulation_time: datetime) -> dict:
        response = self.admin.post(
            f"/api/admin/markets/{symbol}/price",
            json={"price": price, "simulation_time": int(simulation_time.timestamp() * 1000)},
        )
        response.raise_for_status()
        return response.json()

    def execution_count(self) -> int:
        return len(self.admin.get("/api/admin/executions", params={"limit": 100000}).raise_for_status().json())

    def positions(self) -> list[dict]:
        return self.admin.get("/api/admin/positions").raise_for_status().json()

    def accounts(self) -> list[dict]:
        return self.admin.get("/api/admin/accounts").raise_for_status().json()

    def stop(self) -> None:
        self.admin.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class EmulatorFactory:
    """Starts emulators for a test session and kills any left running."""

    def __init__(self) -> None:
        self._running: list[Emulator] = []

    def start(self, db_path: Path) -> Emulator:
        emulator = Emulator(db_path)
        self._running.append(emulator)
        return emulator

    def close(self) -> None:
        for emulator in self._running:
            emulator.stop()
        self._running.clear()


# ---------------------------------------------------------------- backend database


class GoldenDatabase:
    """Rebinds the backend session factory to a fresh SQLite file per run."""

    def __init__(self, client, workdir: Path) -> None:
        self.client = client
        self.workdir = Path(workdir)
        from app.db import session as db_session
        self._session_module = db_session
        self._original_engine = db_session.engine
        self._engines: list = []

    def bind(self, db_path: Path, *, create: bool) -> None:
        from sqlalchemy import create_engine
        from app.db.base import Base

        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, pool_pre_ping=True,
        )
        if create:
            Base.metadata.create_all(bind=engine)
        self._session_module.SessionLocal.configure(bind=engine)
        self._engines.append(engine)

    def dispose(self) -> None:
        for engine in self._engines:
            engine.dispose()

    def restore(self) -> None:
        self._session_module.SessionLocal.configure(bind=self._original_engine)
        self.dispose()
        self._engines.clear()

    def new_run_dir(self, name: str) -> Path:
        path = self.workdir / f"{name}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True)
        return path


# ---------------------------------------------------------------- request recording


class RequestRecorder:
    """Records every `/v5/` request sent through an httpx client (the EmulatorHTTP boundary)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.step: int | None = None
        self._original_send = None

    def _record(self, original_send):
        recorder = self

        def send(client, request, *args, **kwargs):
            path = request.url.path
            if not path.startswith("/v5/"):
                return original_send(client, request, *args, **kwargs)
            started = time.perf_counter()
            wall_start = time.time()
            response = original_send(client, request, *args, **kwargs)
            duration = time.perf_counter() - started
            body = None
            if request.content:
                try:
                    body = json.loads(request.content)
                except ValueError:
                    body = request.content.decode(errors="replace")
            try:
                result = json.loads(response.read())
            except ValueError:
                result = None
            recorder.requests.append({
                "step": recorder.step,
                "method": request.method,
                "path": path,
                "params": dict(request.url.params),
                "body": body,
                "status_code": response.status_code,
                "response": result,
                "wall_start": wall_start,
                "duration_s": duration,
            })
            return response

        return send

    @contextmanager
    def active(self):
        original = httpx.Client.send
        httpx.Client.send = self._record(original)
        try:
            yield self
        finally:
            httpx.Client.send = original


# ---------------------------------------------------------------- bot driving


def _source_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _point_backend_at(emulator: Emulator) -> None:
    from app.core.config import get_settings

    get_settings().exchange_emulator_url = emulator.url


def _auth_headers(client) -> dict:
    email = f"golden_{uuid.uuid4().hex[:10]}@test.dev"
    client.post("/api/auth/register", json={"email": email, "password": "secret123"})
    token = client.post("/api/auth/login", json={"email": email, "password": "secret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_and_start_bot(client, scenario: Scenario) -> int:
    headers = _auth_headers(client)
    payload = {
        "name": f"golden {scenario.name}",
        "exchange": "bybit",
        "environment": scenario.environment,
        "strategy_type": scenario.strategy_type,
        "category": scenario.category,
        "symbol": scenario.symbol,
        "is_active": True,
        "settings": {"run_interval_seconds": 0, **scenario.settings},
        **scenario.bot,
    }
    created = client.post("/api/bots", json=payload, headers=headers)
    if created.status_code != 201:
        raise AssertionError(f"bot create failed: {created.status_code} {created.text}")
    bot_id = created.json()["id"]
    started = client.post(f"/api/bots/{bot_id}/start", headers=headers)
    if started.status_code != 200:
        raise AssertionError(f"bot start failed: {started.status_code} {started.text}")
    return bot_id


def _tick(bot_id: int) -> None:
    """One worker tick for the bot, through the same entry point as the background worker."""
    from app.bot_engine.runtime import tick_running_bots

    app_stub = SimpleNamespace(state=SimpleNamespace(bot_worker_locks={}))
    asyncio.run(tick_running_bots(app_stub, bot_ids={bot_id}, force_bot_ids={bot_id}))


def _new_events(bot_id: int, after_id: int, step: int) -> list[dict]:
    from app.db.session import SessionLocal
    from app.models.trading_bot_event import TradingBotEvent

    db = SessionLocal()
    try:
        rows = (
            db.query(TradingBotEvent)
            .filter(TradingBotEvent.bot_id == bot_id, TradingBotEvent.id > after_id)
            .order_by(TradingBotEvent.id)
            .all()
        )
        return [
            {"id": row.id, "step": step, "event_type": row.event_type,
             "message": row.message, "payload": row.payload or {}}
            for row in rows
        ]
    finally:
        db.close()


def _bot_rows(bot_id: int) -> dict:
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot
    from app.models.trading_bot_order import TradingBotOrder

    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        orders = (
            db.query(TradingBotOrder)
            .filter(TradingBotOrder.bot_id == bot_id)
            .order_by(TradingBotOrder.id)
            .all()
        )
        return {
            "bot": {
                "id": bot.id,
                "runtime_status": bot.runtime_status,
                "order_link_generation": bot.order_link_generation,
                "last_error_type": bot.last_error_type,
                "last_error_code": bot.last_error_code,
                "last_error_action": bot.last_error_action,
                "error_retry_count": bot.error_retry_count,
                "settings": bot.settings,
            },
            "orders": [
                {
                    "id": order.id,
                    "order_link_id": order.order_link_id,
                    "order_role": order.order_role,
                    "side": order.side,
                    "order_type": order.order_type,
                    "status": order.status,
                    "qty": order.qty,
                    "filled_qty": order.filled_qty,
                    "price": order.price,
                    "parent_order_id": order.parent_order_id,
                    "cycle_completed": order.cycle_completed_at is not None,
                }
                for order in orders
            ],
        }
    finally:
        db.close()


def _exchange_state(emulator: Emulator) -> dict:
    account = next(item for item in emulator.accounts() if item["api_key"] == "emulator-default-key")
    return {
        "positions": [
            {"symbol": item["symbol"], "side": item["side"], "size": item["size"], "avg_price": item["avgPrice"]}
            for item in emulator.positions()
        ],
        "balance": account["balance"],
    }


def _snapshot(run_dir: Path, snapshot_dir: Path, bot_id: int, step: int) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for name in SNAPSHOT_FILES:
        source = run_dir / name
        if source.exists():
            shutil.copy2(source, snapshot_dir / name)
    (snapshot_dir / "rows.json").write_text(json.dumps(
        {"bot_id": bot_id, "after_step": step, **_bot_rows(bot_id)}, indent=2, default=str,
    ))


def _first_mutation_after(requests: list[dict], start_index: int) -> dict | None:
    for item in requests[start_index:]:
        if item["method"] == "POST":
            return item
    return None


def record_run(
    scenario: Scenario,
    *,
    emulator: EmulatorFactory,
    db: GoldenDatabase,
    snapshot_after_step: int | None = None,
    snapshot_dir: Path | None = None,
    resume_from: Path | None = None,
) -> dict:
    """Run a scenario end to end and return its recording.

    With `snapshot_after_step`, the emulator and backend databases are copied into `snapshot_dir`
    after that step (a mid-cycle state). With `resume_from`, the run starts from such a snapshot
    and plays only the steps after it.
    """
    run_dir = db.new_run_dir(scenario.name)
    first_step = 0
    if resume_from is not None:
        rows = json.loads((Path(resume_from) / "rows.json").read_text())
        bot_id = int(rows["bot_id"])
        first_step = int(rows["after_step"]) + 1
        for name in SNAPSHOT_FILES:
            source = Path(resume_from) / name
            if source.exists():
                shutil.copy2(source, run_dir / name)
        db.bind(run_dir / "backend.db", create=False)
    else:
        db.bind(run_dir / "backend.db", create=True)

    from app.core.clock import use_simulated_time

    recorder = RequestRecorder()
    events: list[dict] = []
    fill_reactions: list[float] = []
    running = emulator.start(run_dir / "emulator.db")
    try:
        _point_backend_at(running)
        with recorder.active():
            if resume_from is None:
                running.set_price(scenario.symbol, scenario.start_price, simulation_time=SIM_START)
                with use_simulated_time(SIM_START):
                    recorder.step = -1
                    bot_id = _create_and_start_bot(db.client, scenario)
                    _tick(bot_id)
                events.extend(_new_events(bot_id, 0, -1))
            last_event_id = max([event["id"] for event in events], default=_last_event_id(bot_id))

            for step in range(first_step, len(scenario.prices)):
                price = scenario.prices[step]
                moment = SIM_START + timedelta(seconds=scenario.step_seconds * (step + 1))
                executions_before = running.execution_count()
                running.set_price(scenario.symbol, price, simulation_time=moment)
                fill_published = time.time()
                filled = running.execution_count() > executions_before
                recorder.step = step
                index = len(recorder.requests)
                with use_simulated_time(moment):
                    _tick(bot_id)
                state_saved = time.time()
                if filled:
                    mutation = _first_mutation_after(recorder.requests, index)
                    reacted = min(state_saved, mutation["wall_start"]) if mutation else state_saved
                    fill_reactions.append(reacted - fill_published)
                new_events = _new_events(bot_id, last_event_id, step)
                events.extend(new_events)
                if new_events:
                    last_event_id = new_events[-1]["id"]
                if snapshot_after_step is not None and step == snapshot_after_step:
                    running.stop()
                    _snapshot(run_dir, Path(snapshot_dir), bot_id, step)
                    running = emulator.start(run_dir / "emulator.db")
                    _point_backend_at(running)

        final_state = {**_bot_rows(bot_id), "exchange": _exchange_state(running)}
    finally:
        running.stop()
        db.dispose()

    return {
        "scenario": scenario.to_dict(),
        "source_commit": _source_commit(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "resumed_from_step": first_step if resume_from is not None else None,
        "requests": recorder.requests,
        "events": events,
        "final_state": final_state,
        "timings": {
            "fill_reaction_s": fill_reactions,
            "request_durations_s": [item["duration_s"] for item in recorder.requests],
        },
    }


def _last_event_id(bot_id: int) -> int:
    from app.db.session import SessionLocal
    from app.models.trading_bot_event import TradingBotEvent

    db = SessionLocal()
    try:
        row = (
            db.query(TradingBotEvent.id)
            .filter(TradingBotEvent.bot_id == bot_id)
            .order_by(TradingBotEvent.id.desc())
            .first()
        )
        return row[0] if row else 0
    finally:
        db.close()


def save_recording(recording: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recording, indent=1, sort_keys=True, default=str))


def load_recording(path: Path) -> dict:
    return json.loads(Path(path).read_text())
