"""The shared scrubber (ADR-0008): keys at most as `****abcd`, secrets never.

It masks every key and secret it knows (all configured venue credentials plus the emulator
account keys in use), then masks credential-looking patterns it does not know.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"
_ENVIRONMENTS = ("DEMO", "TESTNET", "LIVE")

_KEY_PATTERNS = (
    re.compile(r"""(?i)(\bapi[_-]?key["']?\s*[=:]\s*["']?)([^\s"'&,;}\]]+)"""),
    re.compile(r"""(?i)(\bx-api-key["']?\s*[=:]\s*["']?)([^\s"'&,;}\]]+)"""),
)
_SECRET_PATTERN = re.compile(r"""(?i)(\b(?:api[_-]?)?secret["']?\s*[=:]\s*["']?)([^\s"'&,;}\]]+)""")


def mask_key(key: str | None) -> str:
    value = key or ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


class Scrubber:
    def __init__(self, keys: Iterable[str] = (), secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self._secrets: set[str] = set()
        for key in keys:
            self.add_key(key)
        for secret in secrets:
            self.add_secret(secret)

    def add_key(self, key: str | None) -> None:
        if key:
            with self._lock:
                self._keys.add(str(key))

    def add_secret(self, secret: str | None) -> None:
        if secret:
            with self._lock:
                self._secrets.add(str(secret))

    def scrub(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
            keys = sorted(self._keys - self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        for key in keys:
            text = text.replace(key, mask_key(key))
        for pattern in _KEY_PATTERNS:
            text = pattern.sub(lambda match: match.group(1) + mask_key(match.group(2)), text)
        text = _SECRET_PATTERN.sub(lambda match: match.group(1) + REDACTED, text)
        return text

    def scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {key: self.scrub_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub_value(item) for item in value)
        return value


def load_configured_scrubber(emulator_keys: Iterable[str] = ()) -> Scrubber:
    """A scrubber that knows every configured venue key and secret plus the emulator keys."""
    from app.core.config import get_settings

    scrubber = Scrubber()
    for environment in _ENVIRONMENTS:
        scrubber.add_key(os.getenv(f"BYBIT_{environment}_API_KEY"))
        scrubber.add_secret(os.getenv(f"BYBIT_{environment}_API_SECRET"))
    scrubber.add_key(get_settings().exchange_emulator_default_api_key)
    for key in emulator_keys:
        scrubber.add_key(key)
    return scrubber


_shared: Scrubber | None = None
_shared_lock = threading.Lock()


def get_scrubber() -> Scrubber:
    """The process-wide scrubber every adapter and sink uses."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = load_configured_scrubber()
        return _shared


def scrub(text: Any) -> Any:
    return get_scrubber().scrub(text)


def scrub_value(value: Any) -> Any:
    return get_scrubber().scrub_value(value)
