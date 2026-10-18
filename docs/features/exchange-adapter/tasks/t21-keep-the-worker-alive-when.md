---
id: T21
title: "Keep the worker alive when the guard call fails with a non-guard error"
layer: "app"
deps: ["T20"]
acs: ["AC-10"]
files_hint: ["BE/app/bot_engine/runtime.py", "BE/tests/test_guard_pause.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "done"
---

# T21 — Keep the worker alive when the guard call fails with a non-guard error

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 1.

## Problem

`registry.exchange_for()` runs outside the per-bot handler and catches only `GuardRefusal`; a `ValueError` from a strategy's `required_capabilities()` (e.g. Momentum `lookback_candles: "abc"`) escapes `tick_running_bots` and `worker_loop` dies — every bot stops trading. Before the refactor the same error stopped only that bot (INTERNAL).

## Acceptance

- AC: AC-10 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** A bot whose guard call raises a non-guard error gets today's per-bot reaction (handle_bot_runtime_error) and the other bots still tick; worker_loop survives an unexpected tick failure.
