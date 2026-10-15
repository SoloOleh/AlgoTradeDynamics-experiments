---
id: T49
title: "Carry clear-history into the design (api-sync F-4)"
layer: "docs"
deps: ["T47"]
acs: ["AC-10", "AC-16"]
files_hint: ["docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-3"
status: "todo"
---

# T49 — Carry clear-history into the design (api-sync F-4)

Follow-up task from the independent review — [`_review/review-2026-10-15-3.md`](../_review/review-2026-10-15-3.md), finding(s) 2.

## Problem

api-sync F-4 was dropped: `clearBotHistory` reaches the exchange but has no §6 flow and is not in the pillar 4 guard entry-point list or the §8 Secrets sink list, while the ADR-0004 amendment relies on clear-history as the Operator's way out of a guard-refused bot.

## Acceptance

- AC: AC-10, AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** sad.md Flow 2 has a clear-history branch matching `trading_bot_service.py` (guard refused → local history cleared, exchange cancel skipped, scrubbed reason returned); clear-history is in the pillar 4 guard entry-point list and in the §8 Secrets sink list. No code change.
