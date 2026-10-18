---
id: T61
title: "Amend ADR-0003 for the deferred neutral reduce-only source"
layer: "docs"
deps: ["T59"]
acs: []
files_hint: ["docs/features/exchange-adapter/adr/0003-keep-stored-order-and-error-vocabulary-as-platform-vocabulary.md", "docs/features/exchange-adapter/test-plan.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "done"
---

# T61 — Amend ADR-0003 for the deferred neutral reduce-only source

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 2.

## Problem

ADR-0003's consequence (`:46`) still says the code reading `raw_response['reduceOnly']` must move to a neutral source with a fallback, and `test-plan.md:88` describes a neutral accessor; both are deferred ([spec §8](../spec.md#8-open-questions), Bybit field names item) and the accessor reads only `reduceOnly`.

## Acceptance

- AC: n/a (ADR-0003 documentation fidelity).
- **Definition of Done:** ADR-0003 carries a dated Amendment: the move of `raw_response['reduceOnly']` readers to a neutral source with a legacy fallback is deferred to spec §8 (raw_response neutral keys), due before the roadmap step 1 spec; today's accessor `TradingBotOrder.is_reduce_only` reads the Bybit `reduceOnly` key. test-plan.md's legacy reduce-only edge case says the same. No code change.
