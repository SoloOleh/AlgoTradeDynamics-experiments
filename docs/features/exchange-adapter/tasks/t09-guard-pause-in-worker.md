---
id: T09
title: "Run the guard before every worker tick and pause the bot on a refusal, without retrying"
layer: "app"
deps: ["T05", "T07"]
blocks: ["T11", "T16", "T17"]
acs: ["AC-10", "AC-15b"]
files_hint: ["BE/app/bot_engine/runtime.py", "BE/app/bot_engine/bot.py", "BE/app/bot_engine/error_handling.py", "BE/tests/test_guard_pause.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T09 — Run the guard before every worker tick and pause the bot on a refusal, without retrying

## Place in the sequence

- **Blocked by:** T05 — engine class table (same file), T07 — guard · **Blocks:** T11 — Migrate DCA, T16 — Streams, T17 — Scrub at every sink · **Wave:** 6.
- **Lane:** shares `error_handling.py` with T05, T17 and `bot.py` with T08; `runtime.py` with T16 — serialized.

## Why (user story)

> **As an** Operator
> **I want** a bot whose venue has no exchange adapter to be refused at start, and a running one to be paused, with the reason stated
> **So that** an order can never land on an exchange I didn't choose
>
> — `spec.md §4, US-04, verbatim` · full text: [spec.md](../spec.md)

This task delivers the «running one paused» half, for both missing venues and missing capabilities.

## Inlined context

> alt live exchange event for the bot → BUS->>WK: event wakes the bot · else scheduled check → 15 s fallback reconciliation is due
> WK->>WK: check idempotency key - take the per-bot lock, skip a bot the guard already paused
> WK->>SEAM: resolve adapter for the bot with its required capabilities
> alt venue has no adapter → venue not supported, venue named · else a capability is missing on the market type → capability missing, capability and market type named
> Note: no retry - a guard refusal bypasses the error-class reaction table and goes straight to pause
> WK->>DB: bot paused with the stated reason, orders left as they are — persists bot status, bot event and bot incident (venue unsupported or capability missing)
> WK->>TG: pause notification with the reason, if connected and allowed
> Postcondition: no request reached any exchange, existing orders stay untouched, and the Operator decides what to do with them (AC-10, AC-15b)
>
> — `sad.md §6, Flow 7, abridged` · full text: [sad.md](../sad.md)

> A guard pause (no adapter or missing capability) could be classified as a retryable error and retried instead of pausing | Medium | Guard exceptions bypass `classify_bot_error` and take their own pause path (ADR-0004). A test asserts the bot reaches `paused` with zero exchange requests
>
> — `sad.md §11, risk row «guard pause retried», verbatim` · full text: [sad.md](../sad.md)

> | Bot event feed | `trading_bot_events` row | `event_type` = `venue_unsupported` \| `capability_missing` (≤ 40); `message` = the reason (≤ 500); `payload` below |
> | Incident | `incidents` row via `create_bot_incident` | `incident_type` stored as `VENUE_UNSUPPORTED` \| `CAPABILITY_MISSING` |
> | Telegram | pause notification, if connected and allowed by the preference | same reason text |
>
> `"message": "Bot paused: capability short_selling is not available on market type spot for venue bybit/demo"`, `"payload": {"reason": "capability_missing", "exchange": "bybit", "environment": "demo", "capability": "short_selling", "market_type": "spot"}`
> **Required payload fields:** `reason`, `exchange` (≤ 40), `environment` (≤ 20). With `capability_missing`, also `capability` and `market_type`.
> **Bot state:** `trading_bots.runtime_status` / `is_active` take the paused state exactly as an error pause does today.
>
> — `contracts/events.md, Event bot.guard_paused.v1, abridged` · full text: [events.md](../contracts/events.md)

> While the steps are in progress, the guard already protects start and the worker tick from step (1) onward.
>
> — `sad.md §4, Migration order, verbatim` · full text: [sad.md](../sad.md)

**Tasks-level decision (closes the data-model TBD):** a guard pause writes `last_error` = scrubbed reason, `last_error_type = "configuration"` (an existing stored value — ADR-0003 freezes the set), `last_error_severity = "error"`, `last_error_action = "pause"`, `last_error_code = NULL`, `error_retry_count = 0`, `next_retry_at = NULL`.

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [events.md](../contracts/events.md) · [data-model.md](../data-model.md)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bots.runtime_status`, `is_active` | VARCHAR(20), BOOLEAN | NOT NULL | write — paused, as an error pause today |
| `trading_bots.last_error*` | VARCHAR(500/40/20/40/40) | NULL | write — per the decision above |
| `trading_bot_events.event_type` | VARCHAR(40) | NOT NULL | new **values** `venue_unsupported`, `capability_missing` |
| `incidents.incident_type` | VARCHAR(80) | NOT NULL, indexed | new **values** stored as `VENUE_UNSUPPORTED`, `CAPABILITY_MISSING` (`.upper()[:80]`) |

— `data-model.md §Entities, trading_bots + trading_bot_events + incidents, abridged` · full text: [data-model.md](../data-model.md)

## API contract

Internal — no API surface. (Event contract `bot.guard_paused.v1` — inlined above.)

## Acceptance criteria

### AC-10 — cross-context

> **Given** a bot is running and a deploy or configuration change has left its venue without an exchange adapter
> **When** the bot's next scheduled check or the next live exchange event for that bot arrives
> **Then** the bot is paused with the reason stated, the Operator is notified, and no request is sent to any exchange: the bot's existing orders are left on the exchange as they are, neither placed nor cancelled, and the Operator decides what to do with them
>
> — `spec.md §5, AC-10, verbatim` · full text: [spec.md](../spec.md)

### AC-15b — cross-context

> **Given** a bot is running and, after a deploy, the exchange adapter for its venue no longer declares a capability the bot's strategy needs on the bot's market type
> **When** the bot's next scheduled check or the next live exchange event for that bot arrives
> **Then** the bot is paused with the missing capability and market type named, the Operator is notified, and no request is sent to any exchange, as in AC-10
>
> — `spec.md §5, AC-15b, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] In the worker tick path, inside the per-bot lock: skip bots already guard-paused, call `exchange_for(bot, required_capabilities)`, pass the adapter as `adapter=` to the strategy — `bot_engine/runtime.py`, `bot_engine/bot.py` (`tick_bot_once`)
- [ ] `pause_bot_for_guard(db, bot, refusal)` beside `handle_bot_runtime_error`, never through `classify_bot_error`: bot state, `log_bot_event` with the `bot.guard_paused.v1` shape, `create_bot_incident`, operation log, Telegram via the existing notification path — `bot_engine/error_handling.py`
- [ ] Tests: remove the bot's venue from the registry / a capability from a stand-in declaration, run one scheduled tick and one live-event wake-up → `paused`, event + incident + notification, **zero** exchange requests, orders untouched; a second tick does not duplicate the event — `BE/tests/test_guard_pause.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Live event and fallback tick arrive together | Per-bot lock — one pause, one event, one incident |
| Bot already paused by the guard | Skipped; no repeated notification |
| Telegram not connected or event type not allowed | Event + incident recorded, no notification sent |
| Guard passes for every Bybit bot running today | No pause, no new event category fires |

## Definition of Done

- [ ] `pytest BE/tests/test_guard_pause.py` passes, asserting zero exchange requests on both triggers
- [ ] `pytest -m golden` 12/12 and existing suites pass (`test_bot_error_handling.py` unchanged)
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
