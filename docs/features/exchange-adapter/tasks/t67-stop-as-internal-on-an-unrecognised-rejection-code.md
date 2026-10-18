---
id: T67
title: "Stop the bot as internal on an unrecognised exchange rejection code"
layer: "infra"
deps: ["T65"]
acs: ["AC-06", "AC-08"]
files_hint: ["BE/app/bot_engine/exchange/bybit/adapter.py", "BE/app/bot_engine/exchange/port.py", "BE/app/bot_engine/orders.py", "BE/app/bot_engine/grid_runtime.py", "BE/tests/golden/failure_table/rows.json", "BE/tests/golden/failure_table/rows.py", "BE/tests/test_bybit_adapter.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-2"
status: "done"
---

# T67 — Stop the bot as internal on an unrecognised exchange rejection code

Follow-up task from the independent review — [`_review/review-2026-10-18-2.md`](../_review/review-2026-10-18-2.md), finding(s) 2.

## Problem

When `classify_bybit_failure` returns None, `_rejection` (`adapter.py:101-102`) falls back to `ExchangeError(INVALID_ORDER)`, so an answer body with an unrecognised non-zero `retCode` pauses the bot as an invalid order. The pre-refactor classifier, ADR-0005 and the AC-08 clarification (spec.md §5 AC-08, "an unclassified failure stops it as internal") expect `internal / critical / stop`. The same fallback reaches lookups (`_lookup_request`) and the connection check. Latent today; live once the emulator returns other codes (roadmap step 2).

## Acceptance

- AC: AC-06, AC-08 — full text in [spec.md §5](../spec.md); ADR-0005.
- **Definition of Done:** An unrecognised non-zero `retCode` in an answer body is carried as an `UnclassifiedExchangeFailure` (scrubbed message, raw code kept). A placement refused that way stops the bot as `internal / critical / stop` with the raw code stored and the `<action>: <message> (code N)` text; a lookup answered that way raises it (the order stays active, the bot stops as internal); the connection check reports it as `failing` / `exchange_unavailable`, like its unclassified transport branch. A failure-table row `emulator_unknown_rejection` (origin `rejection`) pins the placement reaction.
