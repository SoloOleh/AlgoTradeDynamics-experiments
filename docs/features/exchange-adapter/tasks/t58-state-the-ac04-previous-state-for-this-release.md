---
id: T58
title: "State which previous state the AC-04 replays cover for this release"
layer: "docs"
deps: ["T55"]
acs: ["AC-04"]
files_hint: ["docs/features/exchange-adapter/test-plan.md", "docs/features/exchange-adapter/tasks/tracker.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17-2"
status: "done"
---

# T58 — State which previous state the AC-04 replays cover for this release

Follow-up task from the independent review — [`_review/review-2026-10-17-2.md`](../_review/review-2026-10-17-2.md), finding(s) 3.

## Problem

Each `mid_cycle_previous_step` snapshot was captured at that strategy's own migration commit, not after the later steps and fix rounds, so it is older than the tree that will ship.

## Acceptance

- AC: AC-04 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** test-plan.md (AC-04 previous-step row and test data) and the tracker's ship precondition say the previous-step snapshots were captured at each strategy's own migration commit, that this release ships as one deploy from pre-refactor so `mid_cycle_pre_refactor` is the deploy's real previous state, and that splitting the branch into several deploys needs each previous-step snapshot recaptured (`GOLDEN_CAPTURE_PREVIOUS=1`) on the tree deployed before it. No code change.
