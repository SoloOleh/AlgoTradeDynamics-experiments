---
id: T64
title: "Align the remaining test-plan rows and bookkeeping with what runs"
layer: "docs"
deps: ["T59"]
acs: ["AC-05"]
files_hint: ["docs/features/exchange-adapter/test-plan.md", "docs/features/exchange-adapter/tasks/tracker.md", "docs/features/exchange-adapter/tasks/t58-state-the-ac04-previous-state-for-this-release.md", "docs/features/exchange-adapter/tasks.json"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "done"
---

# T64 — Align the remaining test-plan rows and bookkeeping with what runs

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 3, 4, 5, 6.

## Problem

The AC-05 row (`test-plan.md:46`) names a one-tick test that does not exist; the NFR section (`:105-109`, `:116`) describes 3 + 3 load runs that were not run and is not marked deferred; `tracker.md:70` cites a shifted line; T58 repeats the wrong snapshot fact.

## Acceptance

- AC: AC-05 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** test-plan.md's AC-05 «first tick» row points at the golden mid-cycle replays that prove it; the NFR validation section and the «On PRs that touch the adapter» line say the 3 + 3 load runs are deferred to spec §8 (`spec.md:259`); tracker.md's ship precondition cites the «Known gap» bullet by name; T58's description and DoD say all four previous-step snapshots were last captured at `394be42`. No code change.
