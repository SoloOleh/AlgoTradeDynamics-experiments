---
id: T37
title: "Pin the spot Pattern Scalper start refusal and correct the T24 DoD"
layer: "tests"
deps: ["T34"]
acs: ["AC-15"]
files_hint: ["BE/tests/test_bots_guard_api.py", "docs/features/exchange-adapter/tasks/t24-restore-the-pattern-scalper-linear-only.md", "docs/features/exchange-adapter/tasks.json"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "done"
---

# T37 — Pin the spot Pattern Scalper start refusal and correct the T24 DoD

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 3.

## Problem

The T24 DoD says spot keeps today's `{detail}` text, but the guard refuses spot first with the capability body (correct per `StartRefused` and ux-flows US-06). No test asserts the spot body.

## Acceptance

- AC: AC-15 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** An API test proves a Bybit spot Pattern Scalper start is refused with 422, `exchange.capability_missing` for short_selling on spot, validated against `startBot` in openapi.yaml, sending no exchange request; the T24 DoD names inverse only and records that spot gets the guard body.
