---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0003 — Keep the stored order and error vocabulary as the platform vocabulary

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

`trading_bot_orders.status` holds Bybit order statuses (`New`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected`, `Deactivated`, `Untriggered`…, `BE/app/bot_engine/orders.py:8-10`). `trading_bots.last_error_type` and the `bot_error` event payload hold `BotErrorType` values (`network`, `rate_limit`, `order_state`…, `error_handling.py:26-39`). Client tags follow `bot-{id}-g{generation}-…`. Bots that are mid-cycle across a deploy have rows written by the old code, and the new code must recognise them (spec AC-05; sharpest failure mode in spec §1: a second ladder and doubled exposure). AC-06 requires the stored error type to stay the same.

## Decision drivers

- SAD §1 QG-1, the top goal: trading equivalence across a deploy, with 0 duplicate orders and 0 unintended cancellations (spec §6 «Deploy safety»).
- Spec AC-06 / glossary «Trading equivalence»: the stored error type values stay the same as today.
- Spec AC-02: profit and loss, fees and closed cycles are still built from the same execution history.
- Migrations auto-apply on backend start (`README.md`), that is, while bots are mid-cycle.

## Considered options

1. **Freeze the stored vocabulary.** Today's stored order statuses, error types and tag format become the platform vocabulary. Every adapter maps into them. No data migration.
2. **New neutral enums plus a data migration.** Introduce platform-owned statuses and types, and rewrite existing rows with an Alembic migration in the same deploy.
3. **New neutral enums in code plus read-side translation.** The code uses new values, and a translation layer reads legacy values alongside new ones indefinitely.

## Decision outcome

**Chosen:** Option 1, freezing the vocabulary. It is the only option where a pre-refactor row is read exactly as it was written, so AC-05 and AC-06 hold by construction. Option 2 runs a row rewrite at backend start while bots are mid-cycle, and changing a stored error type breaks AC-06 outright. Option 3 leaves two vocabularies in every query on `trading_bot_orders` forever.

## Consequences

**Positive**
- No schema or data change for this step. `data-model` is likely N/A (no new entity, column or migration).
- A pre-refactor row and a post-refactor row are indistinguishable to the strategies.
- The reference failure table compares stored error types directly.

**Negative**
- The platform's order-status words look like Bybit's. The Binance adapter must map `NEW` / `PARTIALLY_FILLED` / `EXPIRED`… into them, and each mapping is a step 1 decision.
- `trading_bot_orders.raw_response` stays a raw exchange payload. Code that reads Bybit keys from it today (`raw_response['reduceOnly']` in `momentum.py:632,926`) must move to a neutral source with a fallback for legacy rows (SAD §11).

**Neutral**
- The error-class vocabulary (ADR-0005) is separate from the stored error type. Several error classes may store the same type (`unknown_order` → `order_state`).

**Amendment (2026-10-18).** The move of the `raw_response['reduceOnly']` readers to a neutral source with a fallback for legacy rows (Negative, above) is **deferred**, not done: it is the [spec.md](../spec.md) §8 item on Bybit field names in `raw_response`, due before the roadmap step 1 spec. Today's accessor, `TradingBotOrder.is_reduce_only` (`BE/app/models/trading_bot_order.py`), reads the Bybit `reduceOnly` key, and Grid still reads the key directly. Bybit is the only venue, so stored rows and the accessor agree. Review 2026-10-18, finding 2 (T61).

## Links

- Spec: [[../spec.md]] AC-02, AC-04, AC-05, AC-06
- SAD: [[../sad.md]] §4 pillar 3, §8, §11
- Related ADR: [[0005-classify-errors-in-the-adapter-and-react-in-the-engine]]
