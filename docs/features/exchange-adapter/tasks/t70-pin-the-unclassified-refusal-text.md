---
id: T70
title: "Pin the stored text of an unclassified placement refusal"
layer: "tests"
deps: ["T69"]
acs: ["AC-06"]
files_hint: ["BE/tests/test_engine_reaction.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-3"
status: "done"
---

# T70 — Pin the stored text of an unclassified placement refusal

Follow-up task from the independent review — [`_review/review-2026-10-18-3.md`](../_review/review-2026-10-18-3.md), finding(s) 1.

## Problem

No test checks the stored text on the unclassified branch of `ensure_accepted` (`orders.py:119-121`). T67's DoD promises the `<action>: <message> (code N)` text, but replacing the message with `"x"` survives the whole suite and the failure table.

## Acceptance

- AC: AC-06 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** `test_engine_reaction.py` has an `UnclassifiedExchangeFailure` case (raw code 170134) for the Momentum and Pattern Scalper entries that asserts `internal / critical / stop`, the code `170134` and the full `<action>: Order price has too many decimals. (code 170134)` text. Replacing the message in `ensure_accepted`'s unclassified branch makes it fail.
