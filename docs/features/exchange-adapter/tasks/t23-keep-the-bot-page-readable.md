---
id: T23
title: "Keep the bot page readable for guard-refused Momentum and Pattern Scalper bots"
layer: "app"
deps: ["T20"]
acs: ["AC-02", "AC-10"]
files_hint: ["BE/app/bot_engine/performance.py", "BE/app/services/trading_bot_service.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/tests/test_guard_pause.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T23 — Keep the bot page readable for guard-refused Momentum and Pattern Scalper bots

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 3.

## Problem

`_get_scalper_performance` and `get_runtime_state` call `exchange_for` without handling `GuardRefusal`, so the global handler answers 422 — the contract allows 200/401/404/429 and says a paused bot keeps a readable bot page.

## Acceptance

- AC: AC-02, AC-10 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** GET /bots/{id}/performance and GET /bots answer 200 for a Momentum or Pattern Scalper bot on a venue the guard refuses (stored figures, no exchange request), for all four strategies.
