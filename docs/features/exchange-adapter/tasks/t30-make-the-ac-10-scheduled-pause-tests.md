---
id: T30
title: "Make the AC-10 scheduled-pause tests order-independent"
layer: "tests"
deps: ["T21"]
acs: ["AC-10", "AC-15b"]
files_hint: ["BE/app/bot_engine/runtime.py", "BE/tests/test_guard_pause.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T30 — Make the AC-10 scheduled-pause tests order-independent

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 15, 23.

## Problem

SQLite returns naive `next_retry_at`, compared with aware `utcnow()` → TypeError aborts the scheduled tick when other tests leave retrying bots.

## Acceptance

- AC: AC-10, AC-15b — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The scheduled-pause tests pass in any order alongside retrying bots; `_retry_is_due` tolerates a naive stored datetime; the misnamed notification-wiring test says what it checks.
