---
id: T56
title: "Pin AC-07 on an order-placement timeout"
layer: "tests"
deps: ["T55"]
acs: ["AC-07"]
files_hint: ["BE/tests/golden/failure_table/rows.py", "BE/tests/golden/test_failure_table.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17-2"
status: "todo"
---

# T56 — Pin AC-07 on an order-placement timeout

Follow-up task from the independent review — [`_review/review-2026-10-17-2.md`](../_review/review-2026-10-17-2.md), finding(s) 1.

## Problem

The failure-table session raises on every client method, so the `network_timeout` row fails on the tick's first call, `get_open_orders`. `test-plan.md:50-51` asks for an order-placement timeout that retries after back-off and then pauses past the limit, and no test does that. The code is right (`place_order` shares `_request` and the classifier); only the test is missing.

## Acceptance

- AC: AC-07 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** A failure-table tick where every read succeeds and only `place_order` raises the recorded `network_timeout` leaves the bot `retrying` with a `network` bot_error event and action `retry_backoff`; at the retry limit the same failure pauses the bot with severity `error`. The test fails if the placement path stops reaching the network reaction.
