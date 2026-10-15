---
id: T38
title: "Name no exchange in the engine's own order-rule refusals"
layer: "app"
deps: ["T34"]
acs: ["AC-17"]
files_hint: ["BE/app/bot_engine/orders.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/tests/test_engine_reaction.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "todo"
---

# T38 — Name no exchange in the engine's own order-rule refusals

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 4.

## Problem

`orders.py:102,105`, `momentum.py:1104`, `pattern_scalper.py:1530` say "below Bybit minimum" in exchange-neutral code; the T26 DoD rules that out.

## Acceptance

- AC: AC-17 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The live engine's minimum-quantity and minimum-notional refusals say "below the exchange minimum"; they still classify as invalid_order/pause. Backtest texts (a Bybit-only simulator) stay unchanged.
