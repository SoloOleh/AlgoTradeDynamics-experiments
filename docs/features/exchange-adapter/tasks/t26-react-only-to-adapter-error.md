---
id: T26
title: "React only to adapter error classes in the engine and replay the failure table through the adapter on a tick"
layer: "app"
deps: ["T25"]
acs: ["AC-06", "AC-07"]
files_hint: ["BE/app/bot_engine/error_handling.py", "BE/app/bot_engine/strategies/momentum.py", "BE/app/bot_engine/strategies/pattern_scalper.py", "BE/app/bot_engine/grid_runtime.py", "BE/app/bot_engine/orders.py", "BE/app/bot_engine/exchange/bybit/streams.py", "BE/tests/golden/failure_table/", "BE/tests/golden/test_failure_table.py", "BE/tests/golden/stand_in_adapter.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "done"
---

# T26 — React only to adapter error classes in the engine and replay the failure table through the adapter on a tick

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 6, 14, 20.

## Problem

`_ensure_accepted` re-wraps the classified rejection as a raw-code `ExchangeOperationError` and `classify_bot_error` reclassifies every exception with Bybit's tables (ADR-0005). The failure table never runs through the adapter on a running bot (test-plan.md:48-51). `grid_runtime.py:1060` says "rejected by Bybit".

## Acceptance

- AC: AC-06, AC-07 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The engine imports nothing from exchange.bybit and reacts to `ExchangeError.error_class` only; rejected placements raise the adapter's classified error; every failure-table row, injected at the adapter's client transport during a real worker tick, yields the recorded stored type/severity/action/code and the persisted bot_error payload; a stand-in failure with a non-Bybit raw code gets the class's reaction; neutral runtime texts don't name Bybit.
