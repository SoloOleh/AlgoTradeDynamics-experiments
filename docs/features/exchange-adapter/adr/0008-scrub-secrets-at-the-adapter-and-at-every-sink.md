---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0008 — Scrub secrets at the adapter and again at every sink

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

`str(exc)` flows unfiltered into `bot.last_error`, the `bot_error` event, the operations log, bot incidents (`error_handling.py:206-262`) and Telegram, and the connection check returns `str(exc)` to the caller (`BE/app/api/bybit.py:30`). The emulator account key travels inside the WS URL query string (`exchange_streams.py`, `?api_key=…`), so a connection error can echo it. Moving error handling into the adapter is a new place where secrets meet error text (spec US-07, §6.1 «Key leak through exchange error text») (SAD §4 pillar 8).

## Decision drivers

- Spec AC-16: no key or secret of any venue in events, logs, notifications or the connection-check answer. A key is at most masked `****abcd`, and a secret is never shown, even in part.
- Spec AC-13b: a failing connection check reports the error class, never the raw exchange message.
- Spec §6.1: data classification confidential; security review required.

## Considered options

1. **Scrub at the adapter and at every sink.** One shared scrubber, loaded with every configured key and secret of every venue plus the emulator account keys, is applied by the adapter to every `ExchangeError` it raises and again by each sink: `log_bot_event`, `record_operation_log`, `create_bot_incident`, Telegram, the connection check and a root logging filter.
2. **Scrub at the adapter only.**
3. **Scrub at the sinks only.**

## Decision outcome

**Chosen:** Option 1, defence in depth. Secrets can escape outside the adapter: the session factory failing, the stream supervisor logging a URL, an exception raised deep inside `pybit`. Option 2 misses all of those. Option 3 leaves every future sink, and every future adapter, unprotected by default.

## Consequences

**Positive**
- AC-16 is testable at two layers: adapter-level unit tests per error path, plus sink-level tests that inject a raw secret and assert it never lands.
- The Binance adapter inherits the protection without extra code.

**Negative**
- Small cost on every log or event write (string replacement over a known-secret set).
- Event and log wording changes where a key used to appear. This is allowed: equivalence covers the event category, not the wording (glossary «Trading equivalence»).

**Neutral**
- Records stored before the deploy are not rewritten (AC-16).

## Links

- Spec: [[../spec.md]] US-07, AC-13b, AC-16, §6.1
- SAD: [[../sad.md]] §4 pillar 8, §8
- Related ADR: [[0005-classify-errors-in-the-adapter-and-react-in-the-engine]]
