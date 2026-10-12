---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0005 — Classify errors in the adapter and decide reactions in the engine

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

`classify_bot_error` (`BE/app/bot_engine/error_handling.py:161-192`) maps Bybit codes, HTTP statuses and message texts directly to a `BotErrorDecision` (stored type, severity, action, retry delay). The glossary says the **error class** alone decides the reaction, and the raw exchange code is kept for diagnosis but «never drives behaviour outside the exchange adapter». AC-06 requires the same action, severity and stored type as today for every reference failure. AC-08 introduces one deliberate deviation for failed order lookups, which needs an explicit «unknown order» signal (SAD §4 pillar 5).

## Decision drivers

- Spec AC-06 / §6 «Error-reaction equivalence»: 100% of reference failure table rows give the recorded action and severity.
- Spec AC-08: an order is gone only on the unknown-order error class, or when it is absent from both open orders and history.
- SAD §1 QG-3: Binance must not be able to give a different reaction to the same situation.

## Considered options

1. **The adapter classifies and the engine reacts.** The adapter raises `ExchangeError(error_class, raw_code, message)` with the Bybit code and text tables moved into `exchange/bybit/errors.py`. The engine keeps one table, error class → (stored type, severity, action, retry delay), equal to today's.
2. **The adapter returns the full decision.** Each adapter decides stored type, severity and action itself, and the engine applies them.

## Decision outcome

**Chosen:** Option 1. The reaction policy stays single and exchange-neutral, as the glossary requires, and the failure table can test the two layers separately: Bybit code → class, then class → reaction. Option 2 spreads the recovery policy across adapters, so Binance could react differently to the same situation, and every new adapter would need its own policy review.

## Consequences

**Positive**
- Error classes for this step: `network`, `rate_limited`, `exchange_unavailable`, `time_sync`, `rejected_credentials`, `permission_denied`, `insufficient_balance`, `invalid_symbol`, `invalid_order`, `duplicate_client_tag`, `unknown_order`, `order_state_conflict`, `risk_limit`. Each maps to today's stored type and action. `duplicate_client_tag`, `unknown_order` and `order_state_conflict` all store `order_state` with `resync_and_retry`, as today.
- Non-exchange failures (configuration `ValueError`s, internal errors) keep today's classification in the engine unchanged.
- The `bot_error` event payload keeps `error_type` (stored type) and `error_code` (raw code), as AC-06 requires.

**Negative**
- Two vocabularies to keep straight: error class (behaviour) and stored error type (persistence, ADR-0003).
- `classify_bot_error`'s message-text matching moves into the Bybit adapter, and its order of checks must be preserved exactly (the failure table guards it).

**Neutral**
- The AC-08 deviation lives in the engine's order-lookup rule, not in the class table. Bybit's resync-and-retry policy for inconsistent order state is unchanged (spec §1).

## Links

- Spec: [[../spec.md]] US-03, AC-06, AC-07, AC-08
- SAD: [[../sad.md]] §4 pillar 5, §8
- Related ADR: [[0003-keep-stored-order-and-error-vocabulary-as-platform-vocabulary]], [[0001-define-a-typed-exchange-neutral-adapter-port]]
