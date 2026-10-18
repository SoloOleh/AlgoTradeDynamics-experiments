---
id: T66
title: "Make the AC-07 cleared-retry-time check able to fail"
layer: "tests"
deps: ["T65"]
acs: ["AC-07"]
files_hint: ["BE/tests/golden/failure_table/rows.py", "BE/tests/golden/test_failure_table.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-2"
status: "done"
---

# T66 — Make the AC-07 cleared-retry-time check able to fail

Follow-up task from the independent review — [`_review/review-2026-10-18-2.md`](../_review/review-2026-10-18-2.md), finding(s) 1.

## Problem

The exhausted run of the placement-timeout test seeds a `running` bot with no `next_retry_at`, so `exhausted["next_retry_at"] is None` holds even if the pause path keeps a stale retry time (`test_failure_table.py:82,89`; `rows.py:163-171`). A real bot at retry 5 is `retrying` with a past `next_retry_at`.

## Acceptance

- AC: AC-07 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** `react_on_a_tick` seeds `runtime_status` and `next_retry_at` from the row; the placement-timeout test's exhausted case starts as a `retrying` bot with `next_retry_at` one minute in the past, so its `next_retry_at is None` assertion fails if the pause path keeps the stale retry time.
