---
id: T17
title: "Scrub keys and secrets at every sink and in the root logging filter"
layer: "wiring"
deps: ["T04", "T08", "T09"]
blocks: ["T20"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/events.py", "BE/app/bot_engine/error_handling.py", "BE/app/services/operations_service.py", "BE/app/services/telegram_notifications.py", "BE/app/services/trading_bot_service.py", "BE/app/core/logging.py", "BE/tests/test_secret_leaks.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "S"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T17 — Scrub keys and secrets at every sink and in the root logging filter

## Place in the sequence

- **Blocked by:** T04 — scrubber, T08 — Guard at start and actions (same service file), T09 — Guard pause (same error-handling file) · **Blocks:** T20 — Close the step · **Wave:** 7.
- **Lane:** shares `error_handling.py` with T05, T09 and `trading_bot_service.py` with T08 — serialized.

## Why (user story)

> **As an** Operator
> **I want** my exchange keys and secrets to stay out of bot events, logs, notifications and the connection check answer
> **So that** moving error handling into the exchange adapter does not open a new leak path
>
> — `spec.md §4, US-07, verbatim` · full text: [spec.md](../spec.md)

This task adds the second scrubbing layer, so a secret that escapes outside the adapter still never lands.

## Inlined context

> `str(exc)` flows unfiltered into `bot.last_error`, the `bot_error` event, the operations log, bot incidents (`error_handling.py:206-262`) and Telegram […]. **Chosen:** Option 1, defence in depth. One shared scrubber […] is applied by the adapter to every `ExchangeError` it raises and again by each sink: `log_bot_event`, `record_operation_log`, `create_bot_incident`, Telegram, the connection check and a root logging filter. Secrets can escape outside the adapter: the session factory failing, the stream supervisor logging a URL, an exception raised deep inside `pybit`.
> Event and log wording changes where a key used to appear. This is allowed: equivalence covers the event category, not the wording.
>
> — `adr/0008, Context + Decision outcome + Consequences, abridged` · full text: [ADR-0008](../adr/0008-scrub-secrets-at-the-adapter-and-at-every-sink.md)

> `clearBotHistory`: `exchange_cancel_error` passes the shared scrubber in every case (AC-16): today it is `str(exc)` of a raw exchange failure.
>
> — `contracts/openapi.yaml, operationId clearBotHistory description, abridged` · full text: [openapi.yaml](../contracts/openapi.yaml)

> Leak tests inject the configured keys, secrets and an emulator key inside a WS URL into every error path and every sink, then assert absence.
>
> — `sad.md §10, QG-2 How verify, verbatim` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read [ADR-0008](../adr/0008-scrub-secrets-at-the-adapter-and-at-every-sink.md) and follow it. Do not guess.

## Data delta

| Column | Type | Change |
|---|---|---|
| `trading_bots.last_error`, `trading_bot_events.message` / `payload`, `incidents.title` / `description`, `operation_logs.message` | VARCHAR / JSON / TEXT | written scrubbed only; rows stored before the deploy are not rewritten |

— `data-model.md §Entities, abridged` · full text: [data-model.md](../data-model.md)

## API contract

- `POST /api/bots/{bot_id}/clear-history` → `200 ClearHistoryResult`; `exchange_cancel_error` is scrubbed in every case.

— `contracts/openapi.yaml, operationId clearBotHistory, abridged` · full text: [openapi.yaml](../contracts/openapi.yaml)

## Acceptance criteria

### AC-16 — error

> **Given** a request to an exchange fails while the bot's keys are in use
> **When** the failure is recorded in bot events and logs, sent as a notification, or reported by the connection check
> **Then** none of them contains a key or a secret of any venue, including the emulator account key (also when it appears inside a connection address); at most a masked key showing only its last 4 characters (`****abcd`) is shown, and a secret is never shown even in part. This applies to everything recorded after the deploy; records stored before it are not rewritten
>
> — `spec.md §5, AC-16, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Scrub in `log_bot_event` (message + payload strings) — `events.py`
- [ ] Scrub `bot.last_error` in `handle_bot_runtime_error` and the guard-pause path — `error_handling.py`
- [ ] Scrub in `record_operation_log`, `create_bot_incident` — `operations_service.py`; Telegram send — `telegram_notifications.py`
- [ ] Scrub `exchange_cancel_error` — `trading_bot_service.py`
- [ ] Root logging filter applying the scrubber to every record — `core/logging.py`
- [ ] Leak tests: inject each configured key/secret and an emulator key inside a WS URL through every sink → assert absent; key at most `****abcd` — `BE/tests/test_secret_leaks.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Secret inside a nested payload dict | Scrubbed (walk string values) |
| Pre-deploy rows containing a key | Not rewritten |
| Log record with `%s` args | Filter scrubs the formatted message |

## Definition of Done

- [ ] `pytest BE/tests/test_secret_leaks.py` passes for every sink
- [ ] `test_telegram_notifications.py`, `test_compliance_privacy_operations.py` pass (mechanical edits only); golden 12/12
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
