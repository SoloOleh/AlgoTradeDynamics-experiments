---
id: T31
title: "Keep position response bodies and stored market-order prices unchanged"
layer: "app"
deps: ["T20"]
acs: ["AC-01"]
files_hint: ["BE/app/bot_engine/grid_runtime.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/app/bot_engine/orders.py", "BE/app/bot_engine/exchange/port.py", "BE/tests/test_bots_api.py", "BE/tests/test_order_lookup_rule.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "done"
---

# T31 — Keep position response bodies and stored market-order prices unchanged

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 16, 17.

## Problem

`_format_optional_number` now gets floats ("10" → "10.0"); Momentum/Scalper empty liqPrice gives null; `if order.price:` skips 0.0.

## Acceptance

- AC: AC-01 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** Position endpoints return the same strings as before the refactor (leverage "10", mark "65000.50", empty liqPrice → "0"); a market order acknowledged with price "0" is stored as 0.0.
