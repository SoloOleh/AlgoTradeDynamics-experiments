---
id: T07
title: "Add the venue registry, the fail-closed guard and each strategy's required capabilities"
layer: "domain"
deps: ["T06"]
blocks: ["T08", "T09", "T10", "T16", "T18"]
acs: ["AC-03", "AC-15"]
files_hint: ["BE/app/bot_engine/exchange/registry.py", "BE/app/bot_engine/strategies/base.py", "BE/app/bot_engine/strategies/grid.py", "BE/app/bot_engine/strategies/dca.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/tests/test_venue_guard.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T07 — Add the venue registry, the fail-closed guard and each strategy's required capabilities

## Place in the sequence

- **Blocked by:** T06 — Bybit adapter · **Blocks:** T08 — Guard at start and Operator actions, T09 — Guard in the worker, T10 — Helpers onto the port, T16 — Streams, T18 — Connection check · **Wave:** 5. The single choke point everything after it calls.
- **Lane:** shares `strategies/*.py` with T11–T14 (each runtime migration) — serialized.

## Why (user story)

> **As an** Operator
> **I want** a bot whose strategy needs something its venue cannot do on the bot's market type to be refused at start, with the missing capability named
> **So that** an incompatible setting never reaches the exchange
>
> — `spec.md §4, US-06, verbatim` · full text: [spec.md](../spec.md)

This task builds `exchange_for(bot, needs)` and the per-bot capability needs it compares, entirely in memory.

## Inlined context

> **Chosen:** Option 1. [Derive the venue from `exchange` + `environment`, with one central guard.] A registry maps each venue to an adapter factory, with no default. A single `exchange_for(bot, needs)` is the only way to obtain an adapter, and every entry point calls it before any request. […] one choke point that strategies cannot bypass, because they receive an adapter only from the guard.
> The guard raises typed exceptions (`VenueNotSupported`, `CapabilityMissing`).
> Venues for this step: `bybit` × {`emulator`, `demo`, `testnet`, `live`}, which covers every bot that runs today.
>
> — `adr/0004, Decision outcome + Consequences, abridged` · full text: [ADR-0004](../adr/0004-resolve-adapters-by-venue-through-a-fail-closed-guard.md)

> **Chosen:** Option 1, static declarations. […] Each strategy implements `required_capabilities(bot)`, derived from the bot's settings. The guard compares the two in memory. […] need no network call (so AC-15's «sends nothing» holds), can be unit-tested («every Bybit configuration accepted today passes»).
> The maximum client-tag length is a declared capability *value* […] the guard does **not** enforce it in this step.
> A capability whose requirement depends on settings (short selling only when `position_side` includes `short`) must be computed per bot, not per strategy type.
>
> — `adr/0006, Decision outcome + Consequences, abridged` · full text: [ADR-0006](../adr/0006-declare-capabilities-statically-per-market-type.md)

> | Capability | Required by (today's usage) |
> |---|---|
> | `limit_orders` | Grid, DCA, Momentum, Pattern Scalper |
> | `market_orders` | DCA (market entry), Momentum, Pattern Scalper |
> | `reduce_only_orders` | Grid, DCA (take-profit roles), Momentum, Pattern Scalper |
> | `position_reads` | Grid, Momentum, Pattern Scalper |
> | `short_selling` | Momentum when the market type is not spot **and** `position_side` ∈ {`both`, `short`} (exactly `_short_trading_supported`, `momentum.py:583-585`; a spot Momentum bot with the default `both` never shorts and does not require it), Pattern Scalper |
> | `execution_pnl` | Grid, DCA (performance), Momentum, Pattern Scalper |
> | `wallet_balance` | Momentum, Pattern Scalper (risk guards) |
> | `candles` | Momentum, Pattern Scalper |
> | `order_lookup_by_tag` | Grid, DCA, Momentum, Pattern Scalper |
> | `private_event_stream` | all (worker wake-ups) |
>
> An invariant test asserts that every configuration accepted by today's `_validate_strategy_configuration` passes the guard.
>
> — `sad.md §5, Capability catalogue, abridged` · full text: [sad.md](../sad.md)

> SEAM->>SEAM: look up the venue in the registry with no default, compare declared capabilities with no network call
>
> — `sad.md §6, Flow 6, step 9, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** A bot whose venue has no adapter is refused or paused, never routed to a default exchange.
>
> — `spec.md §6.1, Abuse cases «Misrouted orders», abridged` · full text: [spec.md](../spec.md)

**Tasks-level decision (this breakdown):** strategies receive the adapter as a keyword argument `adapter=` on `tick` / `sync_orders` / `cancel_orders` / `get_position` / `get_risk` / `close_position`; un-migrated strategies accept and ignore it until their migration task (T11–T14).

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bots.exchange` | VARCHAR(40) | NOT NULL, default `bybit` | read-only — venue key part 1 |
| `trading_bots.environment` | VARCHAR(20) | NOT NULL, default `demo` | read-only — venue key part 2 |
| `trading_bots.category` | VARCHAR(20) | NOT NULL, default `linear` | read-only — market type |
| `trading_bots.settings` | JSON | NOT NULL | read-only — e.g. Momentum `position_side` |

— `data-model.md §Entities, trading_bots, abridged` · full text: [data-model.md](../data-model.md)

## API contract

Internal — no API surface. (The exceptions carry exactly the fields `GuardRefusal.details` needs: `exchange`, `environment`, `capability`, `market_type` — `openapi.yaml, schema GuardRefusal, abridged`.)

## Acceptance criteria

### AC-03 — domain invariant

> **Given** an Operator runs two bots on the Bybit emulator venue, each configured with its own emulator account key
> **When** both bots trade
> **Then** each bot's orders and balance stay on its own emulator account only, because a bot only ever trades on the exchange account it is configured with (bots that have no key of their own share the default emulator account by design, as today)
>
> — `spec.md §5, AC-03, verbatim` · full text: [spec.md](../spec.md)

### AC-15 — domain invariant

> **Given** an Operator owns a bot whose strategy needs a capability that the exchange adapter for the bot's venue does not declare for the bot's market type
> **When** the Operator starts the bot
> **Then** the system refuses to start it, names the missing capability and the market type, and sends nothing to any exchange, because a strategy never relies on a capability its exchange adapter did not declare. A Bybit bot configuration that starts today is never refused by this check in this step
>
> — `spec.md §5, AC-15, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Registry `{(exchange, environment): factory}` with the four Bybit venues; factory binds the adapter to the bot's account (emulator key or default; server keys otherwise); no default entry — `exchange/registry.py`
- [ ] `exchange_for(bot, needs)` → adapter, or raises `VenueNotSupported` / `CapabilityMissing` (first missing capability, with market type) with zero network calls; a venue-only variant for the connection check (T18)
- [ ] `required_capabilities(bot)` on the strategy protocol + each of the four strategies per the catalogue — `strategies/base.py`, `grid.py`, `dca.py`, `momentum.py`, `pattern_scalper.py`
- [ ] Optional `adapter=` keyword on strategy methods (ignored until migrated)
- [ ] Tests: unknown venue refused; capability removed from a stand-in declaration refused with name + market type; zero requests on refusal (recording stand-in); invariant over every config `_validate_strategy_configuration` accepts today; two emulator bots get two accounts — `BE/tests/test_venue_guard.py`

## Edge cases

| Case | Behaviour |
|---|---|
| `exchange = "binance"` | `VenueNotSupported(binance, <env>)` — never a Bybit adapter |
| Spot Momentum, `position_side = both` | `short_selling` not required → passes |
| Linear Momentum, `position_side = short` | requires `short_selling` → Bybit declares it on linear → passes |
| Momentum tag ~43 chars vs `client_tag_max_length = 36` | Not enforced → passes |

## Definition of Done

- [ ] `pytest BE/tests/test_venue_guard.py` passes, incl. the invariant test
- [ ] Guard performs no network call (asserted)
- [ ] `pytest -m golden` 12/12 and existing suites pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
