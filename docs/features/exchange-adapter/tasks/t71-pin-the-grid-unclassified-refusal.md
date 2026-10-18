---
id: T71
title: "Pin the Grid position take-profit on an unclassified refusal and scope the T67 wording"
layer: "tests"
deps: ["T69"]
acs: ["AC-06"]
files_hint: ["BE/tests/test_bots_api.py", "docs/features/exchange-adapter/test-plan.md", "docs/features/exchange-adapter/tasks.json"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-3"
status: "todo"
---

# T71 — Pin the Grid position take-profit on an unclassified refusal and scope the T67 wording

Follow-up task from the independent review — [`_review/review-2026-10-18-3.md`](../_review/review-2026-10-18-3.md), finding(s) 2.

## Problem

The `isinstance` guard in `_is_duplicate_client_tag` (`grid_runtime.py:864-866`) keeps a Grid/DCA placement refused with an unmapped code from raising `AttributeError`, but no test covers it. The test-plan row (`test-plan.md:49`) and the T67 DoD say "a refused placement stops the bot as internal", which is true only for Momentum and Pattern Scalper. Grid/DCA record the refused order, as before the refactor.

## Acceptance

- AC: AC-06 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** `test_bots_api.py` places a Grid position take-profit that is refused with an unmapped code (170134): the tick does not crash, the order is recorded with no exchange order id, no duplicate-tag event is logged and the bot keeps running. Reverting the guard makes it fail. The test-plan row and the T67 DoD say "a refused Momentum / Pattern Scalper placement stops the bot as internal; Grid/DCA record the refused order as before".
