---
id: T06
title: "Build the Bybit adapter over the existing pybit and EmulatorHTTP clients"
layer: "infra"
deps: ["T04", "T05"]
blocks: ["T07", "T10"]
acs: ["AC-01", "AC-03", "AC-08"]
files_hint: ["BE/app/bot_engine/exchange/bybit/adapter.py", "BE/app/bot_engine/exchange/bybit/client.py", "BE/app/bot_engine/exchange/bybit/emulator_client.py", "BE/app/bot_engine/exchange/bybit/capabilities.py", "BE/app/bot_engine/bybit/", "BE/tests/test_bybit_adapter.py"]
owner: "Oleh Solomko"
estimate: "L"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T06 — Build the Bybit adapter over the existing pybit and EmulatorHTTP clients

## Place in the sequence

- **Blocked by:** T04 — seam core, T05 — Bybit error classification · **Blocks:** T07 — Venue registry and guard, T10 — Helpers onto the port · **Wave:** 4.
- **Lane:** own lane in `exchange/bybit/`; leaves `BE/app/bot_engine/bybit/` as a re-export shim (deleted by T20).

## Why (user story)

> **As an** Operator
> **I want** my existing Bybit bots of all four strategies to place the same orders and reach the same state after the refactor
> **So that** I don't have to re-validate strategies I already trust
>
> — `spec.md §4, US-01, verbatim` · full text: [spec.md](../spec.md)

This task builds the one Bybit implementation of the port, sending byte-identical order requests and translating responses into neutral types.

## Inlined context

> **Chosen:** Option 1, wrapping the existing clients. Order-mutating request bytes (place, cancel) stay identical to today (read requests may change only where SAD §8 says so), so the equivalence risk is confined to response translation. No new dependency and no new rounding or parameter defaults.
> `get_bybit_credentials` and the `EmulatorHTTP` session cache move into `exchange/bybit/` unchanged.
>
> — `adr/0002, Decision outcome + Consequences, abridged` · full text: [ADR-0002](../adr/0002-implement-the-bybit-adapter-over-the-existing-clients.md)

> ```
> └── bybit/                   Bybit adapter (moved from bot_engine/bybit/)
>     ├── adapter.py           BybitAdapter — pybit / EmulatorHTTP transport, response → neutral types
>     ├── client.py            session factory + get_bybit_credentials (moved, private to the package)
>     ├── emulator_client.py   EmulatorHTTP (moved)
>     ├── errors.py            (T05)
>     ├── streams.py           (T16)
>     └── capabilities.py      BYBIT_CAPABILITIES per market type
> ```
>
> — `sad.md §5, Internal decomposition, abridged` · full text: [sad.md](../sad.md)

> | Order lookup (AC-08) | The adapter's `lookup_order(tag)` answers `open`, `final(filled / cancelled / …)` or `not_found`, searching the exchange's open orders and order history **by client tag** (not today's «last 50 history items», `orders.py:39-50`). […] An order is gone only on `unknown_order` or `not_found`, and a `final` answer gives its final state.
> | Numbers | Prices, quantities, fees and PnL stay floats converted exactly as today. Rounding stays `round_to_step` (Decimal internally) over neutral `InstrumentRules` (`qty_step`, `tick_size`, `min_order_qty`, `min_notional_value`). No move to Decimal in this step |
>
> — `sad.md §8, rows «Order lookup», «Numbers», abridged` · full text: [sad.md](../sad.md)

> Note over SEAM,AD: emulator venue uses the bot's own emulator account key, or the default emulator account when it has none. Demo, testnet and live use the server keys of that venue
>
> — `sad.md §6, Flow 1 note after step 9, verbatim` · full text: [sad.md](../sad.md)

> Bybit declares: `limit_orders`, `market_orders`, `reduce_only_orders`, `position_reads`, `execution_pnl`, `wallet_balance`, `candles`, `order_lookup_by_tag`, `private_event_stream` on spot, linear, inverse; `short_selling` on linear, inverse (not spot); `client_tag_max_length` = 36. The Bybit declaration **mirrors today's behaviour**, including cases that are semantically loose (for example `position_reads` on spot).
>
> — `sad.md §5, Capability catalogue, abridged` · full text: [sad.md](../sad.md)

> The emulator session cache (`lru_cache(maxsize=256)`, keyed by emulator account key) moves into the Bybit adapter unchanged.
>
> — `sad.md §7, Scaling thresholds, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** No Bybit field name (`lotSizeFilter`, `closedPnl`, `tradeMode`, `retCode`) survives outside the Bybit adapter.
>
> — `sad.md §4, pillar 1, verbatim` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-01 — happy path

> **Given** a golden run from a fresh bot was recorded with the pre-refactor version for each of Grid, DCA, Momentum and Pattern Scalper on the Bybit emulator venue
> **When** the same price scenario is replayed after any migration step
> **Then** each strategy's run shows trading equivalence with its recording: the same orders in the same order, the same bot state, and the same event categories (event type plus the recorded error type, whose stored values stay the same as today)
>
> — `spec.md §5, AC-01, verbatim` · full text: [spec.md](../spec.md)

### AC-03 — domain invariant

> **Given** an Operator runs two bots on the Bybit emulator venue, each configured with its own emulator account key
> **When** both bots trade
> **Then** each bot's orders and balance stay on its own emulator account only, because a bot only ever trades on the exchange account it is configured with (bots that have no key of their own share the default emulator account by design, as today)
>
> — `spec.md §5, AC-03, verbatim` · full text: [spec.md](../spec.md)

### AC-08 — domain invariant

> **Given** a running bot has an order it believes is active
> **When** the bot asks the exchange about that order and the lookup fails for any reason other than the unknown-order error class
> **Then** the bot keeps the order as active and asks again later, up to the existing retry limit, after which it pauses as it does today; while the order's state is unknown, the bot places and cancels no orders tied to it. The bot treats the order as gone only when the exchange answers with the unknown-order error class, or when the order is in neither the exchange's open orders nor its order history; if the order history has it, the bot takes its final state (filled or cancelled) from there. A failed lookup is never proof that an order is gone
>
> — `spec.md §5, AC-08, verbatim` · full text: [spec.md](../spec.md)

This task provides `lookup_order` (open / final / not_found, errors raised); the engine-side rule is T10.

## Checklist

- [ ] Move `client.py` and `emulator_client.py` into `exchange/bybit/` unchanged; leave `bot_engine/bybit/{client,emulator_client}.py` as re-export shims so un-migrated callers and the golden-run hook keep working
- [ ] `BybitAdapter(bot_account)` holding a `pybit` session or `EmulatorHTTP` (per-bot emulator key, default emulator account otherwise); every REST port method sends today's parameters in today's order and maps the response to neutral types; every transport failure is re-raised via `classify_bybit_failure` (T05) — `exchange/bybit/adapter.py`
- [ ] `lookup_order(tag)`: open orders by tag, then order history by tag → `open` / `final(status)` / `not_found`; `unknown_order` error → `not_found`; any other error raises `ExchangeError`
- [ ] `BYBIT_CAPABILITIES` per market type per the catalogue — `exchange/bybit/capabilities.py`
- [ ] Contract tests against the emulator: each method's request equals today's call (recorded via the harness hook), translation round-trips fees/PnL/rounding exactly — `BE/tests/test_bybit_adapter.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Two bots with different emulator keys | Two adapters bound to two accounts; no shared session (AC-03) |
| Bot with no emulator key | Default emulator account, as today |
| Order in history with status `Cancelled` | `final(Cancelled)` — stored vocabulary unchanged (ADR-0003) |
| Lookup times out | Raises `ExchangeError(network)`, never `not_found` |
| Float vs string fields from pybit | Converted exactly as today's call sites do |

## Definition of Done

- [ ] `pytest BE/tests/test_bybit_adapter.py` passes against the emulator subprocess
- [ ] Place/cancel requests are byte-identical to today's (harness diff empty)
- [ ] `pytest -m golden` 12/12 and existing suites pass (no caller migrated yet)
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
