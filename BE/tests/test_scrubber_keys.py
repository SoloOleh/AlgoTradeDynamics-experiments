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


KNOWN_KEY = "kQ7rT2vX9mZp4LwB"
KNOWN_SECRET = "s3cR3tV4lu3XyZ81qWe"


@pytest.mark.parametrize(
    "text",
    [
        "Bybit said: secret=abcdefgh123 rejected",
        "api_secret: 'abcdefgh123' & apiKey=AKIAXYZ123456",
        '{"api_key": "AKIAXYZ123456", "secret": "abcdefgh123"}',
        "x-api-key: AKIAXYZ123456; retCode=10003",
        f"signature check failed for {KNOWN_KEY} with {KNOWN_SECRET}",
        f"api_key={KNOWN_SECRET} secret={KNOWN_SECRET}",
    ],
)
def test_scrubbing_twice_changes_nothing_more(text):
    """ADR-0008 scrubs at the adapter, at each sink and in each log handler, so a pass over
    already-scrubbed text must leave it as it is (review 2026-10-17, finding 3)."""
    scrubber = redaction.Scrubber(keys=[KNOWN_KEY], secrets=[KNOWN_SECRET])

    once = scrubber.scrub(text)

    assert scrubber.scrub(once) == once
    assert scrubber.scrub(scrubber.scrub(once)) == once
    assert "abcdefgh123" not in once
    assert KNOWN_SECRET not in once
    assert KNOWN_KEY not in once
    assert "AKIAXYZ123456" not in once


def test_an_already_redacted_value_stays_exactly_redacted():
    scrubber = redaction.Scrubber()

    assert scrubber.scrub("secret=[REDACTED] api_key=[REDACTED]") == "secret=[REDACTED] api_key=[REDACTED]"


@pytest.mark.parametrize(
    ("text", "tail"),
    [
        ("Bybit said: secret=[REDACTED]abc123def rejected", "abc123def"),
        ("api_key=[REDACTED]AKIAXYZ123456 & retCode=10003", "AKIAXYZ123456"),
        ("secret=[redacted]abc123def", "abc123def"),
        ('{"api_secret": "[REDACTED]abc123def"}', "abc123def"),
        ("secret=[REDACTED][REDACTED]abc123def", "abc123def"),
        (f"secret={KNOWN_SECRET}tail9x7", "tail9x7"),
    ],
)
def test_a_value_that_only_starts_with_the_redaction_token_is_still_scrubbed(text, tail):
    """Only an exact `[REDACTED]` value is skipped; a tail after it is a secret too
    (review 2026-10-17-2, finding 4)."""
    scrubber = redaction.Scrubber(keys=[KNOWN_KEY], secrets=[KNOWN_SECRET])

    once = scrubber.scrub(text)

    assert tail not in once
    assert scrubber.scrub(once) == once


@pytest.mark.parametrize(
    "text",
    [
        '{"secret": "[REDACTED]", "api_key": "[REDACTED]"}',
        "secret=[REDACTED]&category=linear",
        "secret=[REDACTED]; api_secret=[REDACTED], x-api-key: [REDACTED]",
    ],
)
def test_an_exact_redaction_token_next_to_a_delimiter_is_left_as_it_is(text):
    assert redaction.Scrubber().scrub(text) == text


@pytest.mark.parametrize("text", ["api_key=[REDACTED]x", "x-api-key: [REDACTED]ab1", "apiKey=[redacted]Q"])
def test_a_short_key_tail_after_the_token_is_masked_without_the_token(text):
    once = redaction.Scrubber().scrub(text)

    assert once.endswith("****")
    assert redaction.Scrubber().scrub(once) == once
