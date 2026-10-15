---
id: T50
title: "Sweep the stale cross-references and task statuses"
layer: "docs"
deps: ["T47"]
acs: []
files_hint: ["docs/features/exchange-adapter/contracts/events.md", "docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/tasks/"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-3"
status: "todo"
---

# T50 — Sweep the stale cross-references and task statuses

Follow-up task from the independent review — [`_review/review-2026-10-15-3.md`](../_review/review-2026-10-15-3.md), finding(s) 3.

## Problem

After T42/T43, events.md still points at the data-model TBD; sad.md's «Flagged for design» list still treats stop-on-refusal as undecided; pillar 4 and the §8 error-handling row mention only start/pause refusals; the §2 and §11 CI rows still call CI-vs-local open; t01–t20 say `status: "todo"` while tracker.md says done.

## Acceptance

- AC: n/a (documentation consistency).
- **Definition of Done:** Each stale reference points at its resolution (data-model guard-pause note, ADR-0004 amendment, test-plan «CI placement»); t01–t20 carry `status: "done"`. No code change.
