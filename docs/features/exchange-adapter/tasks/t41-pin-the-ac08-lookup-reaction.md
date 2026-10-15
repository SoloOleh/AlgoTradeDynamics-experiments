---
id: T41
title: "Pin the AC-08 lookup reaction for every failure class"
layer: "tests"
deps: ["T40"]
acs: ["AC-08", "AC-06"]
files_hint: ["docs/features/exchange-adapter/spec.md", "BE/tests/test_order_lookup_rule.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "done"
---

# T41 — Pin the AC-08 lookup reaction for every failure class

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 1.

## Problem

AC-08 says a lookup that fails "for any reason other than unknown-order" retries then pauses, but `resolve_order_state` re-raises non-retryable classes, so they take their AC-06 reaction (credentials/permission → stop, invalid_symbol → pause, unclassified → internal → stop). Only the 5 retryable classes are tested.

## Acceptance

- AC: AC-08, AC-06 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** AC-08 carries an `<!-- added-by-fix -->` clarification: retryable classes retry up to the limit then pause, other classes take their AC-06 reaction, and the order is never marked gone. Parametrised tests for REJECTED_CREDENTIALS, INVALID_SYMBOL and UnclassifiedExchangeFailure assert the reaction, that the order row stays active and that nothing is placed or cancelled. No production code change.
