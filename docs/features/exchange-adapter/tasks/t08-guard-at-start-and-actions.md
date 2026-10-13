---
id: T08
title: "Run the guard at start and every Operator bot action, after the ownership check"
layer: "ports"
deps: ["T07"]
blocks: ["T11", "T17"]
acs: ["AC-09", "AC-11", "AC-15"]
files_hint: ["BE/app/api/bots.py", "BE/app/services/trading_bot_service.py", "BE/app/bot_engine/bot.py", "BE/tests/test_bots_guard_api.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T08 — Run the guard at start and every Operator bot action, after the ownership check

## Place in the sequence

- **Blocked by:** T07 — Venue registry and guard · **Blocks:** T11 — Migrate DCA, T17 — Scrub at every sink · **Wave:** 6 (parallel with T09, T10, T18).
- **Lane:** shares `bot_engine/bot.py` with T09 and `services/trading_bot_service.py` with T15, T17 — serialized.

## Why (user story)

> **As an** Operator
> **I want** a bot whose venue has no exchange adapter to be refused at start, and a running one to be paused, with the reason stated
> **So that** an order can never land on an exchange I didn't choose
>
> — `spec.md §4, US-04, verbatim` · full text: [spec.md](../spec.md)

This task delivers the «refused at start» half and the same refusal on every other Operator action that reaches an exchange.

## Inlined context

> API->>DB: load the bot owned by the Operator
> alt bot belongs to another platform user or does not exist → trading bot not found, no venue or state revealed, nothing sent (AC-11)
> else own bot → API->>ST: required capabilities for this bot's settings and market type → API->>SEAM: resolve adapter for the venue with the capability set
> alt venue has no adapter → start refused, venue named, bot stays stopped (AC-09)
> else a capability is missing on the market type → start refused, capability and market type named, bot stays stopped (AC-15)
> else guard passes → bot marked running with a bot-started event
>
> — `sad.md §6, Flow 6, abridged` · full text: [sad.md](../sad.md)

> Bot ownership is checked by `get_trading_bot(db, bot_id, user_id)` → 404 «Trading bot not found» **before** any venue routing or guard, so another user's bot reveals no venue or state (AC-11).
>
> — `sad.md §8, Authentication / authorization, abridged` · full text: [sad.md](../sad.md)

> `closeBotPosition` maps `ValueError → 400` today. Guard refusals must be mapped to 422 explicitly (don't let them subclass `ValueError` into that branch). The same applies to `startBot`, which maps every `ValueError` to 422/400 by message text.
> `getBotRisk` reaches the exchange only for Momentum and Pattern Scalper, but the guard runs for every strategy so the rule stays one line.
>
> — `contracts/api-sync-report.md §D, verbatim` · full text: [api-sync-report.md](../contracts/api-sync-report.md)

> **Chosen:** Option 1 [superset `{detail, code?, details?}`], and keep the unversioned `/api/` prefix. […] The 404 «Trading bot not found» and 401 bodies stay byte-identical (AC-11 keeps revealing nothing).
>
> — `adr/0010, Decision outcome + Consequences, abridged` · full text: [ADR-0010](../adr/0010-extend-the-fastapi-error-body-and-keep-the-unversioned-api-prefix.md)

> **Hard rule:** Bot ownership checks stay where they are and run before any venue routing, so routing never reaches a bot the caller does not own (AC-11).
>
> — `spec.md §6.1, AuthZ/AuthN impact, verbatim` · full text: [spec.md](../spec.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [openapi.yaml](../contracts/openapi.yaml) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bots.user_id` | INTEGER | NOT NULL, FK → users, indexed | read-only — ownership before routing |
| `trading_bots.runtime_status` | VARCHAR(20) | NOT NULL | unchanged on refusal (bot stays as it was) |

— `data-model.md §Entities, trading_bots, abridged` · full text: [data-model.md](../data-model.md)

## API contract

- `POST /api/bots/{bot_id}/start` → `200 TradingBotRunResponse` (unchanged) · errors: `404 BotNotFound` (unchanged `{detail}`), `422 StartRefused` = `GuardRefusal` **or** today's `{detail}` validation.
- `POST …/stop`, `POST …/sync`, `POST …/cancel-orders`, `POST …/close-position`, `GET …/position`, `GET …/risk` → unchanged 200 bodies · errors: `404 BotNotFound`, `422 GuardRefused`. `close-position` keeps `400` for `confirm: false`.
- `GET …/performance` → **no refusal**: guard refuses → skip the best-effort refresh, return stored figures `200`.
- `POST …/clear-history` → **no refusal**: guard refuses → `exchange_orders_cancelled: 0`, `exchange_cancel_error` = refusal reason, local history still cleared.
- `GuardRefusal`: `{detail, code ∈ {exchange.venue_unsupported, exchange.capability_missing}, details: {exchange, environment, capability?, market_type?}}`, e.g. `detail: "Venue binance/live has no exchange adapter"`, `detail: "Capability short_selling is not available on market type spot for venue bybit/demo"`.

— `contracts/openapi.yaml, operationIds startBot/stopBot/syncBot/cancelBotOrders/closeBotPosition/getBotPosition/getBotRisk/getBotPerformance/clearBotHistory + responses GuardRefused/StartRefused, abridged` · full text: [openapi.yaml](../contracts/openapi.yaml)

## Acceptance criteria

### AC-09 — error

> **Given** an Operator owns a bot whose venue has no exchange adapter
> **When** the Operator starts the bot
> **Then** the system refuses to start it, tells the Operator that the bot's venue (named) has no exchange adapter, and sends nothing to any exchange
>
> — `spec.md §5, AC-09, verbatim` · full text: [spec.md](../spec.md)

### AC-11 — authorization

> **Given** another platform user owns a bot
> **When** the Operator tries any action on that bot that reaches the exchange through an exchange adapter (start, stop, sync, cancel orders, close the position, read its live position or orders)
> **Then** the system treats the bot as non-existent, sends nothing to any exchange, and no refusal message reveals that bot's venue or state
>
> — `spec.md §5, AC-11, verbatim` · full text: [spec.md](../spec.md)

### AC-15 — domain invariant

> **Given** an Operator owns a bot whose strategy needs a capability that the exchange adapter for the bot's venue does not declare for the bot's market type
> **When** the Operator starts the bot
> **Then** the system refuses to start it, names the missing capability and the market type, and sends nothing to any exchange, because a strategy never relies on a capability its exchange adapter did not declare. A Bybit bot configuration that starts today is never refused by this check in this step
>
> — `spec.md §5, AC-15, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Service entry points call `exchange_for(bot, strategy.required_capabilities(bot))` after `get_trading_bot` and before any strategy call; pass the adapter as `adapter=` — `services/trading_bot_service.py`, `bot_engine/bot.py` (`run_bot_once`, `stop_bot_once`, `sync_bot_orders_once`)
- [ ] Map `VenueNotSupported` / `CapabilityMissing` → `422` `GuardRefusal` explicitly in every route, ahead of the generic `ValueError` branches — `api/bots.py`
- [ ] Performance: guard refusal → skip refresh, return stored figures; clear-history: guard refusal → skip exchange cancel, set `exchange_cancel_error` to the reason — `services/trading_bot_service.py`
- [ ] Tests: AC-09/AC-15 refusals with `code` + `details`, bot stays stopped, zero exchange requests; AC-11 on every listed route returns byte-identical 404 with no guard call; performance/clear-history degrade paths; a Bybit bot that starts today still starts — `BE/tests/test_bots_guard_api.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Another user's bot on an unsupported venue | 404 «Trading bot not found» — the guard is never reached, venue not revealed |
| `close-position` with `confirm: false` on an unsupported venue | Today's `400` — confirm check runs first, as today |
| **Stop on an unsupported venue** | Contract says `422`, bot left as is. **ADR-0004 Consequences says stop moves the bot to `stopped` locally without touching the exchange.** Conflict — resolve with the owner before starting; default: follow `openapi.yaml` (the later api-stage decision) |
| Today's start validations (inactive bot, live trading disabled) | Unchanged `{detail}` body and status |

## Definition of Done

- [ ] `pytest BE/tests/test_bots_guard_api.py` passes; `test_bots_api.py`, `test_user_isolation.py` pass unchanged
- [ ] Handler returns the spec'd outcome for AC-09, AC-11, AC-15
- [ ] `pytest -m golden` 12/12 pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
