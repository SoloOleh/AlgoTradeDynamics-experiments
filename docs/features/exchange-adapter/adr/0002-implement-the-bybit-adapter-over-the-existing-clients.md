---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0002 — Implement the Bybit adapter over the existing pybit and emulator clients

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Bybit demo, testnet and live are reached through `pybit` 5.17 (`pybit.unified_trading.HTTP`), and the emulator through the in-repo `EmulatorHTTP`. Both expose the same Bybit v5 method names (`BE/app/bot_engine/bybit/client.py`, `emulator_client.py`). Live streams use `websockets` against Bybit v5 WS or the emulator WS (`exchange_streams.py`). With the port fixed by ADR-0001, the Bybit adapter needs an inside (SAD §4 pillar 2).

## Decision drivers

- SAD §1 QG-1: trading equivalence, meaning the same orders sent in the same order with the same rounding and tags.
- Spec §6 «Adapter overhead»: ≤ 5 ms added to the p95 duration of a single exchange request.
- SAD §2: no CI, one developer. Every extra moving part on the money path costs review time.

## Considered options

1. **Wrap the existing clients.** `BybitAdapter` holds a `pybit` session or an `EmulatorHTTP` client as its transport, sends exactly today's parameters and translates responses into the neutral types.
2. **Rebuild on a unified exchange library.** `BybitAdapter` calls a multi-exchange library (ccxt-style) internally, and the port stays ours.

## Decision outcome

**Chosen:** Option 1, wrapping the existing clients. Request bytes stay identical to today, so the equivalence risk is confined to response translation. No new dependency and no new rounding or parameter defaults. Option 2 would put two refactorings in one step: a different request shape and different defaults are exactly what the golden runs would flag as non-equivalent, and the emulator's authentication would need custom work.

## Consequences

**Positive**
- The bytes on the wire to Bybit and the emulator do not change. Golden-run failures can only come from translation or orchestration changes.
- The overhead target is easy to meet: translation is in-memory work only.
- `get_bybit_credentials` and the `EmulatorHTTP` session cache move into `exchange/bybit/` unchanged.

**Negative**
- Two transports stay inside one adapter (`pybit` vs `EmulatorHTTP`), and their exception types differ, so error classification must handle both (ADR-0005).
- The Binance adapter in step 1 gets no head start from a shared library.

**Neutral**
- Switching the Bybit adapter's inside to a library later is local to `exchange/bybit/` and is guarded by the same golden runs.

## Links

- Spec: [[../spec.md]] AC-01, AC-04, §6 «Adapter overhead»
- SAD: [[../sad.md]] §4 pillar 2, §5
- Related ADR: [[0001-define-a-typed-exchange-neutral-adapter-port]]
