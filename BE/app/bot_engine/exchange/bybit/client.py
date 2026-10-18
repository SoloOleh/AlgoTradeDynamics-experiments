"""Bybit session factory (private to the Bybit adapter package)."""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from pybit.unified_trading import HTTP
except ImportError:  # Allows emulator-only development without pybit installed.
    HTTP = None  # type: ignore[assignment]

from app.core.config import get_settings
from app.bot_engine.exchange.bybit.emulator_client import EmulatorHTTP


def get_bybit_credentials(environment: str) -> tuple[str | None, str | None]:
    env_key = environment.upper()
    api_key = os.getenv(f"BYBIT_{env_key}_API_KEY")
    api_secret = os.getenv(f"BYBIT_{env_key}_API_SECRET")

    if environment == "demo":
        api_key = api_key or os.getenv("BYBIT_DEMO_API_KEY")
        api_secret = api_secret or os.getenv("BYBIT_DEMO_API_SECRET")

    return api_key, api_secret


@lru_cache(maxsize=256)
def _get_emulator_session(base_url: str, api_key: str) -> EmulatorHTTP:
    return EmulatorHTTP(base_url=base_url, api_key=api_key)


def create_session(environment: str, *, emulator_api_key: str | None = None):
    if environment == "emulator":
        api_key = emulator_api_key or get_settings().exchange_emulator_default_api_key
        return _get_emulator_session(get_settings().exchange_emulator_url, str(api_key))

    if HTTP is None:
        raise RuntimeError("pybit is required for Bybit demo, testnet, or live environments")

    api_key, api_secret = get_bybit_credentials(environment)
    if not api_key or not api_secret:
        raise ValueError(
            f"Bybit {environment} API credentials are not configured")

    kwargs = {
        "api_key": api_key,
        "api_secret": api_secret,
    }
    if environment == "demo":
        kwargs["demo"] = True
    elif environment == "testnet":
        kwargs["testnet"] = True

    return HTTP(**kwargs)
