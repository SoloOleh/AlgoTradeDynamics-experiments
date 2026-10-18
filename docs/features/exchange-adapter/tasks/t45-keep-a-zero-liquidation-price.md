---
id: T45
title: "Keep a zero liquidation price through the stand-in adapter"
layer: "tests"
deps: ["T40"]
acs: ["AC-17"]
files_hint: ["BE/tests/golden/stand_in_adapter.py", "BE/tests/golden/test_stand_in_adapter.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "done"
---

# T45 — Keep a zero liquidation price through the stand-in adapter

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 5.

## Problem

The stand-in turns `liqPrice "0"` into `None`, while the Bybit adapter keeps `Decimal("0")`; the emulator sends `"0"` for a covered long, so the page shows `"0"` through Bybit and `null` through the stand-in.

## Acceptance

- AC: AC-17 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The stand-in applies the same usable-decimal rules as the Bybit adapter without importing the Bybit package; a `liqPrice "0"` case reads `Decimal("0")`, matching Bybit.
