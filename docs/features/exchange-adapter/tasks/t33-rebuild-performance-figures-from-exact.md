---
id: T33
title: "Rebuild performance figures from exact decimal execution values"
layer: "app"
deps: ["T20"]
acs: ["AC-02"]
files_hint: ["BE/app/bot_engine/exchange/port.py", "BE/app/bot_engine/exchange/bybit/adapter.py", "BE/app/bot_engine/performance.py", "BE/tests/golden/stand_in_adapter.py", "BE/tests/test_performance_equivalence.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T33 — Rebuild performance figures from exact decimal execution values

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 19.

## Problem

`Execution` fields are floats and `_to_decimal(float)` replaced `Decimal(raw string)`.

## Acceptance

- AC: AC-02 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** Execution qty/price/fee/realised P&L carry the exchange's decimal text exactly; a value with more than 15 significant digits gives the same Decimal as the raw string; AC-02 recordings stay byte-identical.
