---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0001 — Define a typed exchange-neutral adapter port

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Strategies and runtimes reach Bybit directly. About 22 sites call `get_bybit_session(bot)` and then read Bybit v5 response shapes (`lotSizeFilter`, `priceFilter`, `closedPnl`, `tradeMode`, `retCode`, `orderLinkId`). The connection check, the stream supervisor and the error classifier carry Bybit-specific logic too (`docs/architecture-map.md` §Constraints). Binance (roadmap steps 1–8) would otherwise have to be translated into Bybit shapes or fork the strategies. Spec §1 commits to an adapter that «speaks platform terms». This ADR fixes what the port looks like (SAD §4 pillar 1).

## Decision drivers

- SAD §1 QG-3 / spec §7 KPI: 0 strategy files change when the Binance adapter is added.
- Spec §6: 0 places in strategies or runtimes reach an exchange other than through an exchange adapter.
- SAD §1 QG-1: trading equivalence. Any translation must be lossless for what the strategies use today.
- SAD §2: budget is size M with no hard deadline, so a full port is affordable.

## Considered options

1. **Typed neutral port.** A `Protocol` with platform-term methods and neutral dataclasses (`Order`, `OrderLookup`, `Execution`, `Position`, `Balance`, `InstrumentRules`, `Ticker`, `Candle`, `StreamEvent`, `ConnectionStatus`) and `ExchangeError`. Each adapter translates fully.
2. **Narrow port.** Neutral types only for the order lifecycle, instrument rules and errors. Positions, executions and balances stay raw exchange dicts until Binance needs them.
3. **A unified exchange library as the port.** Adopt a multi-exchange library's method set and data schema (ccxt-style) as the platform terms.

## Decision outcome

**Chosen:** Option 1, the typed neutral port. Only option 1 meets QG-3. Option 2 leaves Momentum and Pattern Scalper reading `closedPnl` / `tradeMode`, so Binance would force strategy edits. Option 3 imports a foreign schema into the domain, does not cover the emulator's `X-API-Key` authentication out of the box, and makes the step 1 guarantees (lookup before resend, fail-closed) depend on a library's semantics.

## Consequences

**Positive**
- Strategies, runtimes, performance, streams and the connection check depend only on `BE/app/bot_engine/exchange/`, and a new exchange is a new adapter package.
- The static search (spec §6) becomes a simple rule: no import of an exchange client outside `exchange/<name>/`.
- A stand-in adapter (AC-17) is a plain implementation of the same `Protocol`.

**Negative**
- The most translation work: every field the strategies read today needs a neutral counterpart, and a conversion slip (for example float vs string) breaks equivalence. The golden runs catch it, but only for the scenarios recorded.
- Neutral types must carry enough to rebuild today's numbers exactly (fees, realised PnL per execution), so the types are shaped by Bybit's richest response.

**Neutral**
- Numeric fields keep today's float conversion (SAD §8). Moving to `Decimal` is a later, separate decision.

**Amendment (2026-10-15).** Two parts of the port carry `Decimal` after all. Neither is a general move to `Decimal`; both keep today's numbers exactly. First, where the exchange's text reached the page unchanged (ticker and position prices, leverage), the port carries it as a `Decimal` built from that text, so the response strings stay the same (`"10"`, not `"10.0"`) — T31, review 2026-10-14 finding 16. Second, execution qty, price, fee and realised P&L are `Decimal`s from the exchange text, as performance read them before the adapter — T33, finding 19. Every other numeric field keeps the float conversion. See SAD §8 «Numbers».

## Links

- Spec: [[../spec.md]] US-08, AC-17, §6 «Exchange access outside adapters», §7 KPIs
- SAD: [[../sad.md]] §4 pillar 1, §5
- Related ADR: [[0002-implement-the-bybit-adapter-over-the-existing-clients]], [[0005-classify-errors-in-the-adapter-and-react-in-the-engine]]
