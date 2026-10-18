---
id: T72
title: "Document how an unclassified failure is reported in the contract and the SAD"
layer: "docs"
deps: ["T69"]
acs: ["AC-13b", "AC-06"]
files_hint: ["docs/features/exchange-adapter/contracts/openapi.yaml", "docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-3"
status: "done"
---

# T72 — Document how an unclassified failure is reported in the contract and the SAD

Follow-up task from the independent review — [`_review/review-2026-10-18-3.md`](../_review/review-2026-10-18-3.md), finding(s) 3.

## Problem

The connection check reports an unclassified failure as `failing` / `exchange_unavailable`, and placements carry it as `PlacedOrder.rejection: UnclassifiedExchangeFailure`, but only `test-plan.md` and T67 say so. SAD §8 «Error handling» says the adapter raises only `ExchangeError`, and the §5 listing of `errors.py` leaves out `UnclassifiedExchangeFailure`.

## Acceptance

- AC: AC-13b, AC-06 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The `getVenueConnection` 200 description and the Flow 8 failure branch say that a failure the adapter cannot classify is reported as `failing` / `exchange_unavailable`. SAD §5 lists `UnclassifiedExchangeFailure` under `errors.py`, and the §8 «Error handling» row names it: an unclassified transport failure or body refusal, raw code kept, treated as internal by the engine (ADR-0005). No code change.
