---
id: T69
title: "Cite the spec §8 items by name in T61 and T64"
layer: "docs"
deps: ["T65"]
acs: []
files_hint: ["docs/features/exchange-adapter/tasks/t61-amend-adr-0003-for-the-deferred-reduce-only-source.md", "docs/features/exchange-adapter/tasks/t64-align-the-remaining-test-plan-rows.md", "docs/features/exchange-adapter/tasks.json"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-2"
status: "todo"
---

# T69 — Cite the spec §8 items by name in T61 and T64

Follow-up task from the independent review — [`_review/review-2026-10-18-2.md`](../_review/review-2026-10-18-2.md), finding(s) 4.

## Problem

T60 shifted every spec §8 item down by 3 lines, so T61 (`spec.md:256`) and T64 (`spec.md:259`, in the task file and the `tasks.json` DoD) now point at the wrong §8 items.

## Acceptance

- AC: n/a (bookkeeping).
- **Definition of Done:** T61 and T64 (task files and `tasks.json`) cite the spec §8 items by name with the `#8-open-questions` anchor instead of line numbers. No code change.
