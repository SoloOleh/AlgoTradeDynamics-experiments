---
id: T48
title: "Align the design docs with the clarified AC-08 lookup reaction"
layer: "docs"
deps: ["T47"]
acs: ["AC-08"]
files_hint: ["docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/ux-flows.md", "docs/features/exchange-adapter/spec.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-3"
status: "done"
---

# T48 — Align the design docs with the clarified AC-08 lookup reaction

Follow-up task from the independent review — [`_review/review-2026-10-15-3.md`](../_review/review-2026-10-15-3.md), finding(s) 1.

## Problem

sad.md Flow 5, the sad.md §8 order-lookup row and ux-flows.md:68 still say every lookup failure other than unknown-order retries up to the limit and then pauses. The clarified AC-08 and the code stop at once on credentials, permission or an unclassified failure and pause at once on `invalid_symbol`. The clarification's retry list also leaves out `duplicate_client_tag` (`resync_and_retry`).

## Acceptance

- AC: AC-08 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** sad.md Flow 5 splits the "any other error class" branch: a retryable class retries up to the limit and then pauses; any other class takes its AC-06 reaction at once with the order kept active and nothing tied to it placed or cancelled. The sad.md §8 order-lookup row and ux-flows.md say the same. The AC-08 clarification lists `duplicate_client_tag` among the retried classes. No code change.
