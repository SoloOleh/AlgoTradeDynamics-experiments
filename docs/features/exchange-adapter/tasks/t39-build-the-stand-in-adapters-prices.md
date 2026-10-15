---
id: T39
title: "Build the stand-in adapter's prices as Decimals like the port declares"
layer: "tests"
deps: ["T34"]
acs: ["AC-17"]
files_hint: ["BE/tests/golden/stand_in_adapter.py", "BE/tests/golden/test_stand_in_adapter.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "done"
---

# T39 — Build the stand-in adapter's prices as Decimals like the port declares

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 6.

## Problem

`stand_in_adapter.py:147,244-245` still uses `_number()` (float) for fields the port declares `Decimal`; the second adapter therefore disagrees with Bybit on body formatting.

## Acceptance

- AC: AC-17 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The stand-in adapter builds Ticker last/mark price and Position mark/liq price and leverage with `_decimal()`, so a position page reads the same through the stand-in as through Bybit ("10", not "10.0").
