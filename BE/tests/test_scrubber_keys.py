"""Only real emulator account keys join the process-wide scrubber (review follow-up T22; AC-16).

A bot's `emulator_api_key` is free-form input. Registering any value would let one user's bot
mask words in every other user's events, logs and notifications.
"""
from __future__ import annotations

import pytest

from app.bot_engine.exchange import redaction
from app.bot_engine.exchange.bybit.adapter import BybitAdapter


@pytest.fixture()
def scrubber(monkeypatch):
    fresh = redaction.Scrubber()
    monkeypatch.setattr(redaction, "_shared", fresh)
    return fresh


@pytest.mark.parametrize("value", ["Buy", "1", "bot-12-g1-entry-1", "Trading bot not found"])
def test_a_free_form_value_that_is_no_emulator_key_never_joins_the_scrubber(scrubber, value):
    BybitAdapter("emulator", emulator_api_key=value)

    text = f"side Buy for bot-12-g1-entry-1 (1): Trading bot not found; value {value}"
    assert scrubber.scrub(text) == text


def test_an_emulator_issued_key_is_masked_everywhere(scrubber):
    key = "emu_Q3vX9kLm2PzR7tYw1aBcD4eF"
    BybitAdapter("emulator", emulator_api_key=key)

    assert scrubber.scrub(f"failed for {key}") == "failed for ****D4eF"


def test_the_configured_default_emulator_key_is_masked(scrubber):
    from app.core.config import get_settings

    default = get_settings().exchange_emulator_default_api_key
    BybitAdapter("emulator")

    assert default not in scrubber.scrub(f"failed for {default}")
