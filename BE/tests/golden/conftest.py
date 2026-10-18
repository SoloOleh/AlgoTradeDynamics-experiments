"""Fixtures for the golden-run suite (`pytest -m golden`)."""
import pytest

from tests.golden.harness import EmulatorFactory, GoldenDatabase


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "golden: golden-run equivalence suite against the real exchange emulator",
    )


@pytest.fixture(scope="session")
def emulator():
    factory = EmulatorFactory()
    try:
        yield factory
    finally:
        factory.close()


@pytest.fixture()
def golden_db(client, tmp_path):
    from app.core.config import get_settings

    original_url = get_settings().exchange_emulator_url
    database = GoldenDatabase(client, tmp_path)
    try:
        yield database
    finally:
        database.restore()
        get_settings().exchange_emulator_url = original_url
