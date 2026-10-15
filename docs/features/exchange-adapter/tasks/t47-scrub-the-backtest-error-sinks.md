---
id: T47
title: "Scrub the backtest error sinks"
layer: "wiring"
deps: ["T40"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/grid_runtime.py", "BE/app/bot_engine/dca_runtime.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/app/services/backtest_service.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "todo"
---

# T47 — Scrub the backtest error sinks

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 7.

## Problem

The backtest-bot and backtest-run error sinks store `str(exc)` without the sink scrub ADR-0008 asks for (grid_runtime, dca_runtime, momentum, pattern_scalper, backtest_service, fast_momentum_backtest, fast_scalper_backtest).

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** Each of the seven sinks stores `scrub(str(exc))`; a test proves a secret in the exception text does not reach the stored error.
