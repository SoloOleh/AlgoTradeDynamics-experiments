---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0006 — Declare capabilities statically per adapter and market type

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Strategies differ in what they need from an exchange. Momentum and Pattern Scalper hold positions and send reduce-only exits. Momentum shorts only off spot. Strategies read realised PnL per execution (`closedPnl`). Client tags have a length the exchange must accept. Today these are implicit, or checked by exchange or category name (`trading_bot_service.py:29-32`, `pattern_scalper.py:1317`). The glossary defines a **capability** as something an adapter declares per market type, which strategies consult instead of the exchange's name. AC-15 refuses a start with the missing capability named and nothing sent. AC-15b pauses a running bot after a deploy removes a capability (SAD §4 pillar 6).

## Decision drivers

- Spec AC-15: refuse without sending anything to any exchange. A Bybit configuration that starts today is never refused in this step.
- Spec AC-15b: a capability removed by a deploy pauses the bot at its next check.
- SAD §1 QG-1: no extra exchange requests, which would change the recorded request sequence.

## Considered options

1. **Static declarations in code.** Each adapter has a constant `{market_type: frozenset[Capability]}`. Each strategy implements `required_capabilities(bot)`, derived from the bot's settings. The guard compares the two in memory.
2. **Probe the exchange at runtime.** The adapter asks the exchange (instrument or account info) what is available and caches it.
3. **A configuration file per venue.** Capability sets live in YAML or env next to the venue's keys.

## Decision outcome

**Chosen:** Option 1, static declarations. They are deterministic, need no network call (so AC-15's «sends nothing» holds), can be unit-tested («every Bybit configuration accepted today passes»), and a deploy that changes a set is visible in the diff, which is exactly AC-15b's trigger. Option 2 sends requests during start, which breaks AC-15 and changes golden-run request sequences. Option 3 lets a typo silently pause bots, or worse, declare something the adapter cannot do (fail-open).

## Consequences

**Positive**
- Settles spec §8 OQ «Which capabilities does each strategy need, and which does the Bybit adapter declare»: both lists are derived from today's usage and recorded in SAD §5.
- The maximum client-tag length is a declared capability *value* (spec §8 OQ on tags). Bybit declares 36, and step 1 decides about Binance.

**Negative**
- If an exchange withdraws a feature without a deploy on our side, we learn about it from an order error (an error class), not from the guard.
- A capability whose requirement depends on settings (short selling only when `position_side` includes `short`) must be computed per bot, not per strategy type.

**Neutral**
- The strategy × market × mode compatibility matrix (decision D2, spec §3 non-goal) is built on these declarations later.

## Links

- Spec: [[../spec.md]] US-06, AC-15, AC-15b, §8 OQ on capabilities and tag length
- SAD: [[../sad.md]] §4 pillar 6, §5 capability catalogue
- Related ADR: [[0004-resolve-adapters-by-venue-through-a-fail-closed-guard]]
