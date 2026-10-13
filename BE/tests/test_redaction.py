"""The shared scrubber: keys at most as ****abcd, secrets never (AC-16, ADR-0008)."""
import pytest

from app.bot_engine.exchange.redaction import Scrubber, load_configured_scrubber, mask_key


def test_mask_key_keeps_only_the_last_four_characters():
    assert mask_key("AbCdEfGh1234wxyz") == "****wxyz"


@pytest.mark.parametrize("short", ["", "a", "abc", "abcd"])
def test_short_keys_are_masked_fully(short):
    assert mask_key(short) == "****"


def test_known_key_inside_a_ws_url_query_string_is_masked():
    scrubber = Scrubber(keys=["emu-account-key-9f3k"])
    text = "connect failed: ws://exchange-emulator:8001/v5/private?api_key=emu-account-key-9f3k&x=1"
    scrubbed = scrubber.scrub(text)
    assert "emu-account-key-9f3k" not in scrubbed
    assert "****9f3k" in scrubbed


def test_known_secret_is_removed_entirely():
    scrubber = Scrubber(keys=["KEY123456789"], secrets=["S3cr3tValueXYZ"])
    scrubbed = scrubber.scrub("signature for S3cr3tValueXYZ failed with key KEY123456789")
    assert "S3cr3t" not in scrubbed and "XYZ" not in scrubbed
    assert "****6789" in scrubbed


def test_secret_inside_a_longer_token_leaves_no_part():
    scrubber = Scrubber(secrets=["topsecret42"])
    scrubbed = scrubber.scrub("token=abc-topsecret42-def")
    assert "topsecret42" not in scrubbed and "secret42" not in scrubbed


def test_value_configured_as_key_and_secret_is_treated_as_secret():
    scrubber = Scrubber(keys=["SharedValue9876"], secrets=["SharedValue9876"])
    scrubbed = scrubber.scrub("value SharedValue9876")
    assert "9876" not in scrubbed


@pytest.mark.parametrize("text,leak,shown", [
    ("GET /v5/order?api_key=UnknownKey0000abcd&sign=1", "UnknownKey0000abcd", "****abcd"),
    ("headers: {'X-API-Key': 'otherKEYvalue5678'}", "otherKEYvalue5678", "****5678"),
    ("X-API-Key: headerkeyvalue4321", "headerkeyvalue4321", "****4321"),
])
def test_unknown_key_patterns_are_masked(text, leak, shown):
    scrubbed = Scrubber().scrub(text)
    assert leak not in scrubbed
    assert shown in scrubbed


def test_unknown_secret_pattern_is_removed():
    scrubbed = Scrubber().scrub("config api_secret=NeverShowMe123 loaded")
    assert "NeverShowMe123" not in scrubbed and "e123" not in scrubbed


def test_text_without_credentials_is_unchanged():
    text = "Order qty 0.0001 is below Bybit minimum 0.001 (code 110017)"
    assert Scrubber(keys=["abcdefgh"], secrets=["zyxwvuts"]).scrub(text) == text


def test_scrub_value_walks_nested_payloads():
    scrubber = Scrubber(keys=["nestedKey00001111"], secrets=["nestedSecret"])
    payload = {"error": "bad nestedKey00001111", "items": ["nestedSecret", 3, None]}
    scrubbed = scrubber.scrub_value(payload)
    assert scrubbed == {"error": "bad ****1111", "items": ["[REDACTED]", 3, None]}


def test_loader_reads_every_configured_venue_credential(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demoKey00001111")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demoSecretValue")
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "testnetKey2222")
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", "testnetSecretValue")
    monkeypatch.setenv("BYBIT_LIVE_API_KEY", "liveKey33334444")
    monkeypatch.setenv("BYBIT_LIVE_API_SECRET", "liveSecretValue")
    scrubber = load_configured_scrubber(emulator_keys=["emulator-account-key-5555"])

    text = ("demoKey00001111 demoSecretValue testnetKey2222 testnetSecretValue "
            "liveKey33334444 liveSecretValue emulator-account-key-5555 emulator-default-key")
    scrubbed = scrubber.scrub(text)
    for leaked in ("demoKey00001111", "demoSecretValue", "testnetKey2222", "testnetSecretValue",
                   "liveKey33334444", "liveSecretValue", "emulator-account-key-5555", "emulator-default-key"):
        assert leaked not in scrubbed
    assert "****1111" in scrubbed and "****5555" in scrubbed


def test_emulator_keys_can_be_registered_after_loading():
    scrubber = Scrubber()
    scrubber.add_key("late-emulator-key-7777")
    assert scrubber.scrub("x late-emulator-key-7777") == "x ****7777"
