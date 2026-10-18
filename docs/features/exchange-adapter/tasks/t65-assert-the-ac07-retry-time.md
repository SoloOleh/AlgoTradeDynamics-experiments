---
id: T65
title: "Assert the AC-07 retry time in the placement-timeout test"
layer: "tests"
deps: ["T59"]
acs: ["AC-07"]
files_hint: ["BE/tests/golden/failure_table/rows.py", "BE/tests/golden/test_failure_table.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "done"
---

# T65 — Assert the AC-07 retry time in the placement-timeout test

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 7.

## Problem

test-plan.md's AC-07 row expects «a next retry time is set», which the T56 test did not check; past the limit it also did not check the event payload.

## Acceptance

- AC: AC-07 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The placement-timeout test asserts a next retry time is set on the first failure and cleared after the pause, and that the exhausted `bot_error` event says `network` / `pause`.
