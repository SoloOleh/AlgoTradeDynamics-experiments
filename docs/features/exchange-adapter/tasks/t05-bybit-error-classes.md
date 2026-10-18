---
id: T05
title: "Move Bybit error classification into the adapter package and keep reactions in an engine class table"
layer: "infra"
deps: ["T02", "T03", "T04"]
blocks: ["T06", "T09"]
acs: ["AC-06", "AC-07"]
files_hint: ["BE/app/bot_engine/exchange/bybit/__init__.py", "BE/app/bot_engine/exchange/bybit/errors.py", "BE/app/bot_engine/error_handling.py", "BE/tests/golden/test_failure_table.py", "BE/tests/test_bybit_error_classes.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T05 — Move Bybit error classification into the adapter package and keep reactions in an engine class table

## Place in the sequence

- **Blocked by:** T02 — golden recordings (pre-refactor baseline must exist), T03 — reference failure table, T04 — seam core (`ErrorClass`, `ExchangeError`, scrubber) · **Blocks:** T06 — Bybit adapter, T09 — Guard in the worker · **Wave:** 3. First task that edits the existing exchange path.
- **Lane:** shares `BE/app/bot_engine/error_handling.py` with T09 and T17 — serialized.

## Why (user story)

> **As an** Operator
> **I want** every kind of exchange failure to trigger the same reaction (retry after back-off, resync, pause or stop) with the same severity as today
> **So that** my alerts and recovery habits stay valid
>
> — `spec.md §4, US-03, verbatim` · full text: [spec.md](../spec.md)

This task splits today's one-step classifier into «Bybit failure → error class» (adapter side) and «error class → reaction» (engine side) with identical results.

## Inlined context

> **Chosen:** Option 1. The adapter raises `ExchangeError(error_class, raw_code, message)` with the Bybit code and text tables moved into `exchange/bybit/errors.py`. The engine keeps one table, error class → (stored type, severity, action, retry delay), equal to today's. […] the failure table can test the two layers separately: Bybit code → class, then class → reaction.
> `duplicate_client_tag`, `unknown_order` and `order_state_conflict` all store `order_state` with `resync_and_retry`, as today.
> Non-exchange failures (configuration `ValueError`s, internal errors) keep today's classification in the engine unchanged.
> The `bot_error` event payload keeps `error_type` (stored type) and `error_code` (raw code), as AC-06 requires.
> `classify_bot_error`'s message-text matching moves into the Bybit adapter, and its order of checks must be preserved exactly (the failure table guards it).
> Two transports stay inside one adapter (`pybit` vs `EmulatorHTTP`), and their exception types differ, so error classification must handle both.
>
> — `adr/0005 Decision outcome + Consequences; adr/0002 Consequences/Negative, abridged` · full text: [ADR-0005](../adr/0005-classify-errors-in-the-adapter-and-react-in-the-engine.md) · [ADR-0002](../adr/0002-implement-the-bybit-adapter-over-the-existing-clients.md)

> AD->>AD: map code, HTTP status and text to an error class, keep the raw code, scrub the message
> AD-->>ST: exchange error with error class, raw code and scrubbed message
> WK->>WK: look up the error class in the reaction table - stored error type, severity, action, retry delay
> WK->>DB: record the bot event — persists bot event (event type, stored error type as today, raw exchange code, severity) and an operation log entry
> alt retry after back-off … retry up to the existing retry limit (default 5) … else retry limit exhausted → bot paused with the same severity as today
> else resync and retry … else pause … else stop
>
> — `sad.md §6, Flow 4, steps 6–11 + alt branches, abridged` · full text: [sad.md](../sad.md)

> **Hard rule:** Raw codes never drive behaviour outside the adapter (glossary «Error class»).
>
> — `sad.md §4, pillar 5, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** Guard refusals (`VenueNotSupported`, `CapabilityMissing`) are a separate path […], never retried.
>
> — `sad.md §8, Error handling, abridged` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bots.last_error_type` | VARCHAR(40) | NULL | read/write — **stored values unchanged**; `unknown_order` stores `order_state` |
| `trading_bots.last_error_code` | VARCHAR(40) | NULL | write — raw exchange code, kept for diagnosis |
| `trading_bots.last_error` | VARCHAR(500) | NULL | write — scrubbed message only |

— `data-model.md §Entities, trading_bots, abridged` · full text: [data-model.md](../data-model.md)

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-06 — error

> **Given** the reference failure table, recorded before the refactor, has one row for each error type the bot distinguishes today (at least one real Bybit failure per type, including rate limit, insufficient balance, rejected order, duplicate client tag, unknown order, rejected credentials and temporary network failure), plus one row for a retryable failure that exhausts the existing retry limit, each with the action and severity the bot took
> **When** each listed failure occurs for a running bot after a migration step
> **Then** the bot takes the same action with the same severity, and the recorded event keeps the same category (the same stored error type as today) and the raw exchange code — except for the failed-order-lookup row, whose expected reaction is recorded as the deliberate deviation of AC-08
>
> — `spec.md §5, AC-06, verbatim` · full text: [spec.md](../spec.md)

### AC-07 — error

> **Given** a running bot is placing an order
> **When** the request to the exchange times out because of a temporary network failure
> **Then** the bot retries after a back-off and keeps running, and the Operator sees a temporary-network event rather than a stopped bot; if the failure persists past the existing retry limit, the bot pauses as it does today
>
> — `spec.md §5, AC-07, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Move the Bybit code / HTTP-status / message-text tables from `classify_bot_error` into `classify_bybit_failure(exc) -> ExchangeError` for both `pybit` and `EmulatorHTTP` exception types, same check order, message passed through the T04 scrubber — `exchange/bybit/errors.py`
- [ ] Engine table `ErrorClass → (BotErrorType, severity, action, retry delay)` reproducing today's values — `bot_engine/error_handling.py`
- [ ] `classify_bot_error(exc)`: `ExchangeError` → table; a raw Bybit/emulator exception (un-migrated call sites) → `classify_bybit_failure` → table; anything else → today's non-exchange branch unchanged
- [ ] Repoint the failure-table check at the two layers (Bybit row → class, class → reaction) — `BE/tests/golden/test_failure_table.py`
- [ ] Unit tests for each class mapping, incl. `unknown_order → order_state / resync_and_retry` — `BE/tests/test_bybit_error_classes.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Exception message echoes a key or the emulator `api_key` URL | `ExchangeError.message` is scrubbed; raw code untouched |
| Retryable failure beyond `error_max_retries` | `pause` with severity `error`, as today |
| `VenueNotSupported` / `CapabilityMissing` reaching `classify_bot_error` | Not classified here — guard path (T09); must not become a retryable decision |
| Unknown Bybit code with no table match | Same fallback class/type as today's classifier |

## Definition of Done

- [ ] Failure-table check passes 100% of rows through the two layers
- [ ] `pytest -m golden` 12/12 replays pass; existing suites pass (incl. `test_bot_error_handling.py` unchanged)
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
