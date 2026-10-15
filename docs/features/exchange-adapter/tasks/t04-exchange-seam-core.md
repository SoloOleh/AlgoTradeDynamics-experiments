---
id: T04
title: "Add the exchange seam core: port, error vocabulary, capabilities and the shared scrubber"
layer: "domain"
deps: []
blocks: ["T05", "T06", "T17", "T18"]
acs: ["AC-16", "AC-17"]
files_hint: ["BE/app/bot_engine/exchange/__init__.py", "BE/app/bot_engine/exchange/port.py", "BE/app/bot_engine/exchange/errors.py", "BE/app/bot_engine/exchange/capabilities.py", "BE/app/bot_engine/exchange/redaction.py", "BE/tests/test_exchange_seam.py", "BE/tests/test_redaction.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T04 — Add the exchange seam core: port, error vocabulary, capabilities and the shared scrubber

## Place in the sequence

- **Blocked by:** — · **Blocks:** T05 — Bybit error classification, T06 — Bybit adapter, T17 — Scrub at every sink, T18 — Venue connection check · **Wave:** 1 (pure new code, no caller yet, so it can start in parallel with the harness).
- **Lane:** own lane (new package `BE/app/bot_engine/exchange/`).

## Why (user story)

> **As an** Operator
> **I want** a new exchange to plug in through its own exchange adapter without changing my strategies
> **So that** Binance can arrive later without re-validating strategy logic
>
> — `spec.md §4, US-08, verbatim` · full text: [spec.md](../spec.md)

This task defines the one interface every adapter implements and the vocabularies (error class, capability) plus the scrubber every adapter and sink shares.

## Inlined context

> ```
> exchange/                    NEW — the exchange seam
> ├── port.py                  ExchangeAdapter Protocol + neutral types: Order, OrderLookup
> │                            (open | final(filled/cancelled…) | not_found), Execution, Position,
> │                            Balance, InstrumentRules, Ticker, Candle, StreamEvent, ConnectionStatus
> ├── errors.py                ErrorClass, ExchangeError(error_class, raw_code, message),
> │                            VenueNotSupported, CapabilityMissing
> ├── capabilities.py          Capability enum, CapabilitySet, client_tag_max_length
> ├── registry.py              (T07)
> ├── redaction.py             Scrubber over every configured key/secret + emulator account keys;
> │                            mask_key() → ****abcd
> ```
>
> — `sad.md §5, Internal decomposition, abridged` · full text: [sad.md](../sad.md)

> Strategies, runtimes, the stream supervisor, performance and the connection check call one `ExchangeAdapter` interface in platform terms: place, cancel and look up orders, open orders, order history, executions, instrument rules, ticker, candles, balance, positions, live event streams and the connection check. They get back neutral types and `ExchangeError` with an error class. No Bybit field name (`lotSizeFilter`, `closedPnl`, `tradeMode`, `retCode`) survives outside the Bybit adapter.
>
> — `sad.md §4, pillar 1, abridged` · full text: [sad.md](../sad.md)

> **Chosen:** Option 1, the typed neutral port. […] Numeric fields keep today's float conversion (SAD §8). Neutral types must carry enough to rebuild today's numbers exactly (fees, realised PnL per execution).
>
> — `adr/0001, Decision outcome + Consequences, abridged` · full text: [ADR-0001](../adr/0001-define-a-typed-exchange-neutral-adapter-port.md)

> Error classes for this step: `network`, `rate_limited`, `exchange_unavailable`, `time_sync`, `rejected_credentials`, `permission_denied`, `insufficient_balance`, `invalid_symbol`, `invalid_order`, `duplicate_client_tag`, `unknown_order`, `order_state_conflict`, `risk_limit`.
>
> — `adr/0005, Consequences/Positive, abridged` · full text: [ADR-0005](../adr/0005-classify-errors-in-the-adapter-and-react-in-the-engine.md)

> Capabilities: `limit_orders`, `market_orders`, `reduce_only_orders`, `position_reads`, `short_selling`, `execution_pnl`, `wallet_balance`, `candles`, `order_lookup_by_tag`, `private_event_stream`; plus the value `client_tag_max_length` (declared, **not enforced by the guard in this step**). Market types: `spot`, `linear`, `inverse`.
>
> — `sad.md §5, Capability catalogue, abridged` · full text: [sad.md](../sad.md)

> One shared scrubber knows every configured key and secret of every venue, plus the emulator account keys in use. […] Keys appear at most as `****abcd`, and secrets never appear (AC-16, QG-2).
> The scrubber removes only secrets it knows. […] Pattern masking on top of known-value masking (`api_key=…`, `X-API-Key: …`, `api_secret`).
>
> — `sad.md §4 pillar 8 + §11 risk row «scrubber», abridged` · full text: [sad.md](../sad.md)

> **Hard rule:** Only `exchange/<name>/` may import an exchange client (`pybit`, `EmulatorHTTP`) or open an exchange WS connection.
>
> — `sad.md §5, intro ¶, abridged` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface. (`ErrorClass`, `Capability`, `MarketType` enums must match `contracts/openapi.yaml` `components.schemas.ErrorClass` (13 values), `Capability` (10 values, no `client_tag_max_length`), `MarketType` — `openapi.yaml, schemas ErrorClass/Capability/MarketType, abridged`.)

## Acceptance criteria

### AC-16 — error

> **Given** a request to an exchange fails while the bot's keys are in use
> **When** the failure is recorded in bot events and logs, sent as a notification, or reported by the connection check
> **Then** none of them contains a key or a secret of any venue, including the emulator account key (also when it appears inside a connection address); at most a masked key showing only its last 4 characters (`****abcd`) is shown, and a secret is never shown even in part. This applies to everything recorded after the deploy; records stored before it are not rewritten
>
> — `spec.md §5, AC-16, verbatim` · full text: [spec.md](../spec.md)

### AC-17 — happy path

> **Given** a stand-in exchange adapter for a test venue that declares the same capabilities as the Bybit adapter
> **When** a bot of each of the four strategies runs a golden-run scenario on that test venue
> **Then** every strategy trades through the stand-in adapter with no change to the strategy itself
>
> — `spec.md §5, AC-17, verbatim` · full text: [spec.md](../spec.md)

This task delivers the scrubber unit (AC-16) and the `Protocol` a stand-in implements (AC-17); T17 and T19 finish them.

## Checklist

- [ ] `ExchangeAdapter` `Protocol` with the methods listed above; neutral frozen dataclasses incl. `OrderLookup` with states `open` / `final(status)` / `not_found` and `InstrumentRules(qty_step, tick_size, min_order_qty, min_notional_value)` — `exchange/port.py`
- [ ] `ErrorClass` (13 values), `ExchangeError(error_class, raw_code, message)`, `VenueNotSupported(exchange, environment)`, `CapabilityMissing(exchange, environment, capability, market_type)` — **not** subclasses of `ValueError` (api-sync-report §D) — `exchange/errors.py`
- [ ] `Capability` enum, `CapabilitySet`, `client_tag_max_length` value slot — `exchange/capabilities.py`
- [ ] `Scrubber`: known-value masking (key → `****` + last 4, secret → removed entirely), pattern masking for `api_key=`, `X-API-Key:`, `api_secret`; `mask_key()`; a loader that reads every configured `BYBIT_{DEMO|TESTNET|LIVE}_API_KEY/SECRET` and accepts emulator account keys in use — `exchange/redaction.py`
- [ ] Unit tests: enum values match the contract; scrubber masks a key inside a WS URL query string, removes a secret, masks an unknown `api_key=` value — `BE/tests/test_exchange_seam.py`, `BE/tests/test_redaction.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Key shorter than 4 chars | Masked fully as `****`, never echoed |
| Secret appears as a substring of a longer token | Removed; no partial secret remains |
| Same value configured as key on one venue and secret on another | Treated as secret (never shown) |
| Text with no credential | Returned unchanged |

## Definition of Done

- [ ] `pytest BE/tests/test_exchange_seam.py BE/tests/test_redaction.py` passes
- [ ] The package imports no exchange client (`pybit`, `EmulatorHTTP`) outside `exchange/<name>/`
- [ ] No existing module changes behaviour; existing suites pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
