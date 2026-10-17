---
id: T63
title: "Assert each emulator account's balance in the AC-03 test"
layer: "tests"
deps: ["T59"]
acs: ["AC-03"]
files_hint: ["BE/tests/test_bybit_adapter.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "todo"
---

# T63 — Assert each emulator account's balance in the AC-03 test

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 3.

## Problem

test-plan.md's AC-03 row promises «orders and balance changed only by its own bot», but the test asserted open orders only.

## Acceptance

- AC: AC-03 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The two-accounts test also shows each account's balance changing only with its own bot's trade: a fill on one account leaves the other account's balance unchanged, both ways. The test fails if both adapters share one account.
