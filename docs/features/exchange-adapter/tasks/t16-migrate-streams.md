---
id: T16
title: "Move live exchange streams behind the adapter and make the stream supervisor and UI live stream exchange-neutral"
layer: "infra"
deps: ["T07", "T09"]
blocks: ["T20"]
acs: ["AC-10", "AC-16"]
files_hint: ["BE/app/bot_engine/exchange/bybit/streams.py", "BE/app/bot_engine/exchange_streams.py", "BE/app/api/websockets.py", "BE/app/bot_engine/runtime.py", "BE/tests/test_exchange_streams.py"]
owner: "Oleh Solomko"
estimate: "L"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T16 — Move live exchange streams behind the adapter and make the stream supervisor and UI live stream exchange-neutral

## Place in the sequence

- **Blocked by:** T07 — guard, T09 — Guard in the worker · **Blocks:** T20 — Close the step · **Wave:** 7 (SAD migration step 7; independent of the strategy chain).
- **Lane:** shares `runtime.py` with T09 — serialized.

## Why (user story)

> **As an** Operator
> **I want** my exchange keys and secrets to stay out of bot events, logs, notifications and the connection check answer
> **So that** moving error handling into the exchange adapter does not open a new leak path
>
> — `spec.md §4, US-07, verbatim` · full text: [spec.md](../spec.md)

This task moves WS endpoints and credentials (incl. the emulator key in the URL) inside the adapter, so a live event both wakes the bot through the guard and never carries a secret.

## Inlined context

> - **Producer:** the venue's exchange adapter (`bot_engine/exchange/<name>/streams.py`), which translates exchange WS topics into neutral stream events (ADR-0001). Moves out of today's `bot_engine/exchange_streams.py`, which becomes exchange-neutral.
> - **Delivery:** at-most-once (a WS message can be lost on reconnect). The 15 s fallback reconciliation is the safety net.
> - Event `exchange.stream_event.v1`: `{bot_id, topic, data, received_at}`. **`topic`:** one of `order`, `execution`, `position`, `wallet` (private, capability `private_event_stream`) or `tickers.<SYMBOL>` (public). Same topics as today (`PRIVATE_TOPICS`). `order`, `execution` and `position` force a reconcile, as today (`FORCE_RECONCILE_TOPICS`). **`data`:** neutral types from `exchange/port.py` (`StreamEvent`). No Bybit field name crosses the adapter. The envelope is kept as is (`ExchangeStreamEvent`, `exchange_streams.py:40-45`), and only `data` becomes neutral.
> - **Secrets:** stream endpoints and credentials stay inside the adapter. The event never carries a key, secret or authenticated URL.
>
> — `contracts/events.md, Channel + Event exchange.stream_event.v1, abridged` · full text: [events.md](../contracts/events.md)

> The UI live stream (`api/websockets.py:247-261`) opens `/v5/` WS connections itself with `api_key` in the URL | Medium | Migrated in step (7): it takes its stream endpoints from the adapter through the guard, and its errors pass the scrubber (ADR-0008, ADR-0009)
>
> — `sad.md §11, risk row «UI live stream», verbatim` · full text: [sad.md](../sad.md)

> The emulator account key travels inside the WS URL query string (`exchange_streams.py`, `?api_key=…`), so a connection error can echo it.
>
> — `adr/0008, Context, abridged` · full text: [ADR-0008](../adr/0008-scrub-secrets-at-the-adapter-and-at-every-sink.md)

> EX->>BUS: publish order or execution event → BUS->>AD: deliver raw event → AD->>WK: neutral stream event wakes the bot → WK->>SEAM: resolve adapter for the bot with its required capabilities (refusal path in Flow 7)
>
> — `sad.md §6, Flow 1 steps 1–7, abridged` · full text: [sad.md](../sad.md)

> **Hard rule:** Only `exchange/<name>/` may […] open an exchange WS connection.
>
> — `sad.md §5, intro ¶, abridged` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([events.md](../contracts/events.md) · [sad.md](../sad.md)) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface. (The UI WS route in `api/websockets.py` keeps its current path and message shape; only its upstream source moves behind the adapter.)

## Acceptance criteria

### AC-10 — cross-context

> **Given** a bot is running and a deploy or configuration change has left its venue without an exchange adapter
> **When** the bot's next scheduled check or the next live exchange event for that bot arrives
> **Then** the bot is paused with the reason stated, the Operator is notified, and no request is sent to any exchange: the bot's existing orders are left on the exchange as they are, neither placed nor cancelled, and the Operator decides what to do with them
>
> — `spec.md §5, AC-10, verbatim` · full text: [spec.md](../spec.md)

### AC-16 — error

> **Given** a request to an exchange fails while the bot's keys are in use
> **When** the failure is recorded in bot events and logs, sent as a notification, or reported by the connection check
> **Then** none of them contains a key or a secret of any venue, including the emulator account key (also when it appears inside a connection address); at most a masked key showing only its last 4 characters (`****abcd`) is shown, and a secret is never shown even in part. This applies to everything recorded after the deploy; records stored before it are not rewritten
>
> — `spec.md §5, AC-16, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Bybit + emulator WS endpoints, auth, topic → neutral `StreamEvent` translation — `exchange/bybit/streams.py`
- [ ] Supervisor asks the bot's adapter (via the guard) for its streams; no Bybit URL, topic parsing or field names left; connection errors pass the scrubber — `exchange_streams.py`
- [ ] A live-event wake-up goes through the T09 guard path (pause on refusal) — `runtime.py`
- [ ] UI live stream takes its endpoints from the adapter through the guard; no `/v5/` or `api_key` in the module — `api/websockets.py`
- [ ] Tests: neutral events for each topic; emulator key in a failing WS URL never reaches a log line or event; venue removed → live event pauses the bot with zero requests — `BE/tests/test_exchange_streams.py` (mechanical edits to existing cases only)

## Edge cases

| Case | Behaviour |
|---|---|
| WS reconnect drops a message | 15 s fallback reconciliation catches up, as today |
| WS connect fails with the URL in the exception | Logged/recorded with the key masked `****abcd` |
| Public ticker stream | Same topics as today, neutral payload |

## Definition of Done

- [ ] `pytest BE/tests/test_exchange_streams.py` passes; `pytest -m golden` 12/12 and latency p95 ≤ baseline + 0.5 s
- [ ] Static search: no hits in `exchange_streams.py`, `api/websockets.py`
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
