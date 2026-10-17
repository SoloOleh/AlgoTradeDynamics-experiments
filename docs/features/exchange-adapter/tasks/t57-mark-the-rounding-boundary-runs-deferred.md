---
id: T57
title: "Mark the rounding-boundary golden runs deferred in the test plan"
layer: "docs"
deps: ["T55"]
acs: ["AC-01"]
files_hint: ["docs/features/exchange-adapter/test-plan.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17-2"
status: "todo"
---

# T57 — Mark the rounding-boundary golden runs deferred in the test plan

Follow-up task from the independent review — [`_review/review-2026-10-17-2.md`](../_review/review-2026-10-17-2.md), finding(s) 2.

## Problem

test-plan.md still describes rounding-boundary golden runs that were never recorded (`:16`, `:37`, `:114`), while `spec.md:258` defers them.

## Acceptance

- AC: AC-01 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** test-plan.md no longer presents the rounding-boundary golden runs as recorded or run: the settled-decisions note, the AC-01 rounding-boundary row and the «On every PR» line say they are deferred, with a link to the spec §8 item. No code change.
