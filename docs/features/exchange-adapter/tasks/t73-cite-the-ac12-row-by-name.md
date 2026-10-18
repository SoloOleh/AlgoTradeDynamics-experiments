---
id: T73
title: "Cite the AC-12 test-plan row by name in the ship precondition"
layer: "docs"
deps: ["T69"]
acs: ["AC-12"]
files_hint: ["docs/features/exchange-adapter/tasks/tracker.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-3"
status: "done"
---

# T73 — Cite the AC-12 test-plan row by name in the ship precondition

Follow-up task from the independent review — [`_review/review-2026-10-18-3.md`](../_review/review-2026-10-18-3.md), finding(s) 4.

## Problem

The ship-precondition line in the tracker cites `test-plan.md:66` for the AC-12 demo check. T67 inserted a row at `:49`, so `:66` is now an AC-11 row.

## Acceptance

- AC: AC-12 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The tracker's ship precondition cites the test-plan AC-12 row «connection check reports a configured demo venue as connected» by name, not by line. No code change.
