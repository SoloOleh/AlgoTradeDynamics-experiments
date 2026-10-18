---
id: T42
title: "Record the guard-refused stop in ADR-0004 and carry the api-sync gaps forward"
layer: "docs"
deps: ["T40"]
acs: ["AC-10"]
files_hint: ["docs/features/exchange-adapter/adr/0004-resolve-adapters-by-venue-through-a-fail-closed-guard.md", "docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/spec.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "done"
---

# T42 — Record the guard-refused stop in ADR-0004 and carry the api-sync gaps forward

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 2.

## Problem

Accepted ADR-0004 says a stop on a guard-refused bot moves it to `stopped`; code and openapi refuse with 422 `GuardRefusal` and leave it `paused`. api-sync F-2 (Flow 2's page read should degrade) and F-5 (no AC for `not_supported` or for guard refusals on non-start actions) were never carried forward.

## Acceptance

- AC: AC-10 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** ADR-0004 carries a dated amendment matching openapi (stop refused with 422; the Operator clears history or deletes); sad.md Flow 2's page read has the degrade branch; F-5 is in spec §8 with owner and due date. No code change.
