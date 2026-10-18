---
status: Draft
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-13"
feature_size: M
---

# Events — exchange-adapter

Async contract for the event-driven flows in `sad.md` §6 (Flow 1, Flow 3, Flow 7). Like the
OpenAPI contract, it is **derived** from the sequences: every event below maps to a message in a §6
diagram.

**There is no message broker.** The «live event stream» participant is the exchange's private
WebSocket, read by the stream supervisor inside the backend process. The bot worker is an asyncio
loop in the same process (`sad.md` §5, §7). So these are in-process contracts, plus the records
the worker writes (bot events, incidents) and the Telegram message it sends. They are written down
because the adapter boundary is new: the stream event is what every future adapter (Binance,
roadmap step 1) must produce, and the guard-pause records are what the bot page, the operations
log and Telegram read.

## Channel: exchange private stream → bot worker (in-process)

- **Producer:** the venue's exchange adapter (`bot_engine/exchange/<name>/streams.py`), which
  translates exchange WS topics into neutral stream events (ADR-0001). Moves out of today's
  `bot_engine/exchange_streams.py`, which becomes exchange-neutral.
- **Consumers:** the stream supervisor → the bot worker (`bot_engine/runtime.py`), which wakes the
  bot and ticks it.
- **Delivery:** at-most-once (a WS message can be lost on reconnect). The 15 s fallback
  reconciliation is the safety net: REST stays the source of truth for orders and state
  (`exchange_streams.py` module docstring).
- **Ordering:** per bot, in arrival order. Per-bot processing is serialised by the per-bot lock
  (`runtime.py:65`).

## Event: `exchange.stream_event.v1`

```json
{
  "bot_id": 42,
  "topic": "execution",
  "data": { "...": "neutral payload — order / execution / position / wallet / ticker in platform terms" },
  "received_at": 1760342400.123
}
```

- **Required fields:** `bot_id` (int, `trading_bots.id`), `topic`, `data`, `received_at`
  (wall-clock seconds, float).
- **`topic`:** one of `order`, `execution`, `position`, `wallet` (private, capability
  `private_event_stream`) or `tickers.<SYMBOL>` (public). Same topics as today
  (`exchange_streams.py` `PRIVATE_TOPICS`). `order`, `execution` and `position` force a reconcile,
  as today (`FORCE_RECONCILE_TOPICS`).
- **`data`:** neutral types from `exchange/port.py` (`StreamEvent`). No Bybit field name
  (`retCode`, `closedPnl`, …) crosses the adapter (ADR-0001). The exact typed fields are a
  `port.py` design detail settled in `tasks`. This contract fixes only the envelope and the rule.
- **Origin:** `sad.md` §6 Flow 1 `AD->>WK: neutral stream event wakes the bot`; Flow 3 (resume
  after deploy); Flow 7 `BUS->>WK: event wakes the bot`.
- **Envelope source:** today's `ExchangeStreamEvent(bot_id, topic, data, received_at)`
  (`bot_engine/exchange_streams.py:40-45`). The envelope is kept as is, and only `data` becomes
  neutral.
- **Secrets:** stream endpoints and credentials stay inside the adapter. The event never carries a
  key, secret or authenticated URL (ADR-0008; the emulator account key can appear in a connection
  address, AC-16).
- **Backwards-compat policy:** additive-only. A new optional field is fine. Removing or renaming a
  field is `v2`. Consumers ignore unknown fields.

## Event: `bot.guard_paused.v1`

A running bot whose venue lost its adapter, or whose adapter no longer declares a capability the
strategy needs on the bot's market type, is paused by the worker (AC-10, AC-15b). The fact is
recorded once in three sinks, all scrubbed (ADR-0008):

| Sink | Record | Values |
|---|---|---|
| Bot event feed | `trading_bot_events` row | `event_type` = `venue_unsupported` \| `capability_missing` (≤ 40); `message` = the reason (≤ 500); `payload` below |
| Incident | `incidents` row via `create_bot_incident` | `incident_type` stored as `VENUE_UNSUPPORTED` \| `CAPABILITY_MISSING` (`.upper()[:80]`, `operations_service.py:104`) |
| Telegram | pause notification, if connected and allowed by the preference | same reason text |

```json
{
  "event_type": "capability_missing",
  "message": "Bot paused: capability short_selling is not available on market type spot for venue bybit/demo",
  "payload": {
    "reason": "capability_missing",
    "exchange": "bybit",
    "environment": "demo",
    "capability": "short_selling",
    "market_type": "spot"
  }
}
```

- **Required payload fields:** `reason` (`venue_unsupported` | `capability_missing`), `exchange`
  (≤ 40), `environment` (≤ 20). With `capability_missing`, also `capability` (catalogue in
  `sad.md` §5 = `Capability` in `openapi.yaml`) and `market_type` (`spot` | `linear` | `inverse`).
  The payload mirrors the `GuardRefusal.details` of the HTTP contract, so a refused start and a
  guard pause name things the same way.
- **Bot state:** `trading_bots.runtime_status` / `is_active` take the paused state exactly as an
  error pause does today (data-model.md). Orders are left on the exchange untouched.
- **Origin:** `sad.md` §6 Flow 7 `WK->>DB: bot paused with the stated reason` +
  `WK->>TG: pause notification`; data-model.md `incidents` (the two new values).
- **Never fires** for a Bybit bot that runs today (`sad.md` §8 «Events»).
- **Backwards-compat policy:** additive-only on `payload`.

> `event_type` values `venue_unsupported` / `capability_missing` were proposed by this contract
> and **accepted** (`api-sync-report.md` F-3). `sad.md` §8 names the categories but not the stored
> strings. A guard pause also writes `last_error_type` / `last_error_severity` / `last_error_action`
> (`configuration` / `error` / `pause`), as data-model.md's guard-pause note now records. The event types, the incident types and these error values all fit the existing columns.

## Event: `bot_error` (existing, unchanged, invariants pinned)

Today's `log_bot_event(db, bot, "bot_error", …)` (`error_handling.py:254`). Its name and shape do
not change. This feature pins two invariants for the failure table (AC-06):

- `payload.error_type` keeps the **stored** error type, values unchanged (`network`, `rate_limit`,
  `order_state`, …, ADR-0003). The new error class `unknown_order` is stored as `order_state`
  (ADR-0005).
- `payload.error_code` keeps the raw exchange code. `message` and `payload` are scrubbed (AC-16).

## Idempotency & retry

Numbers come from the `sad.md` §6 retry notes and «Flagged for design: Async mapping».

- **Idempotency:** no event id. A duplicate or late stream event is harmless. The per-bot asyncio
  lock means a bot is never ticked twice at once. Deterministic client tags
  (`bot-{id}-g{gen}-…`, ADR-0003) plus an order lookup by tag before placing mean a re-tick never
  places a second copy of an intended order (AC-05). Flow 7 skips a bot the guard already paused.
- **Retry:** a failed exchange request is retried with back-off up to the existing
  `error_max_retries` (default 5, `trading_bots.error_retry_count` / `next_retry_at`). The AC-08
  lookup retry shares the same budget. A guard refusal is **never** retried: it goes straight to
  pause (Flow 7).
- **Dead-letter:** none. The system has no queue. After the retry limit, the bot is paused with
  today's severity, and that pause stands in for a dead-letter queue (Flow 1). The Operator drains
  it by resuming or stopping the bot.

## Schema registry

- None. The canonical shapes are `bot_engine/exchange/port.py` (stream event `data`) and
  `bot_engine/events.py` / `error_handling.py` (bot events).
- Validator: the existing pytest suites plus the golden-run harness (ADR-0007), which records event
  categories (event type + stored error type) for trading equivalence.
