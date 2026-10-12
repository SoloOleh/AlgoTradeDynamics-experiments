---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0004 — Resolve adapters by venue through one fail-closed guard

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Today `get_bybit_session(bot)` keys only on `bot.environment`. An emulator bot gets `EmulatorHTTP`, and every other value gets a `pybit` session (`client.py:34`). `bot.exchange` is ignored, so a bot with any other exchange value would trade on Bybit. The glossary defines **venue** as exchange plus environment, with exactly one adapter per venue or a refusal. AC-09 and AC-10 (no adapter) and AC-15 and AC-15b (missing capability) need a refusal at start and a pause while running, with nothing sent to any exchange. Exchange access happens at many entry points: the start, stop, sync, cancel, close and position service calls, the worker tick, and live-event wake-ups (SAD §4 pillar 4).

## Decision drivers

- SAD §1 QG-2: fail-closed safety. Never routed to a default exchange (spec §6.1 «Misrouted orders»).
- Spec AC-09, AC-10, AC-15, AC-15b: name the venue or the capability, send nothing to any exchange, and leave existing orders untouched.
- Spec AC-11 / §6.1: ownership checks run before any venue routing.
- ADR-0003: no schema change in this step.

## Considered options

1. **Derive the venue from `exchange` + `environment`, with one central guard.** A registry maps each venue to an adapter factory, with no default. A single `exchange_for(bot, needs)` is the only way to obtain an adapter, and every entry point calls it before any request.
2. **Add a `venue` column.** A new `trading_bots.venue` with a backfill migration, and the same central guard.
3. **Check inside each strategy.** The venue comes from the existing columns, but each strategy calls the check itself in `validate_start` and `tick`.

## Decision outcome

**Chosen:** Option 1. It needs no migration and gives one choke point that strategies cannot bypass, because they receive an adapter only from the guard. Option 2 duplicates data that can drift from `exchange` / `environment`, and adds a backfill at deploy time with bots running. Option 3 copies the check four times, misses live-event wake-ups (handled before any strategy runs), and fails open the day someone adds an entry point and forgets the check.

## Consequences

**Positive**
- One place enforces AC-09, AC-10, AC-15 and AC-15b, and the static search can assert that nothing else constructs an adapter.
- The guard raises typed exceptions (`VenueNotSupported`, `CapabilityMissing`). A start is refused with the venue or capability named. A running bot is moved to `paused` with the reason recorded as a bot event and a notification, without entering the retry path.
- Venues for this step: `bybit` × {`emulator`, `demo`, `testnet`, `live`}, which covers every bot that runs today.

**Negative**
- The pause-by-guard path is a new runtime branch beside `handle_bot_runtime_error`. It must not be classified as a retryable error, or it would retry instead of pausing.
- Every service entry point (`trading_bot_service.py`, `bot.py`) must route through the guard, and that is a code-review checklist item until the static search enforces it.

**Neutral**
- A `venue` column can still be added later (for example for per-operator keys) without changing the guard's contract.

## Links

- Spec: [[../spec.md]] US-04, US-06, AC-09, AC-10, AC-11, AC-15, AC-15b
- SAD: [[../sad.md]] §4 pillar 4, §5, §6
- Related ADR: [[0006-declare-capabilities-statically-per-market-type]]
