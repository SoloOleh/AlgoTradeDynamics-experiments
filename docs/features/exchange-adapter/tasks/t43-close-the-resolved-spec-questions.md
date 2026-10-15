---
id: T43
title: "Close the resolved spec §8 questions and the data-model guard-pause TBD"
layer: "docs"
deps: ["T40"]
acs: []
files_hint: ["docs/features/exchange-adapter/spec.md", "docs/features/exchange-adapter/data-model.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "todo"
---

# T43 — Close the resolved spec §8 questions and the data-model guard-pause TBD

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 3.

## Problem

Three spec §8 questions are past due and already resolved elsewhere (risk guards in SAD §11, CI vs local in test-plan.md, capability lists in SAD §5). The data-model `last_error_type` TBD for a guard pause was settled in code (`error_handling.py`) but never written back.

## Acceptance

- AC: n/a (quality follow-up).
- **Definition of Done:** Each of the three §8 items is ticked with its resolution and a link; data-model.md records the guard-pause values `configuration` / `error` / `pause`. No code change.
