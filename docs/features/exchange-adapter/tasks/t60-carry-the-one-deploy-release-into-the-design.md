---
id: T60
title: "Carry the one-deploy release into the SAD and the AC-04 wording"
layer: "docs"
deps: ["T59"]
acs: ["AC-04"]
files_hint: ["docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/spec.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "done"
---

# T60 — Carry the one-deploy release into the SAD and the AC-04 wording

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 1.

## Problem

`test-plan.md:100` and `tracker.md:70` say the branch ships as one deploy onto pre-refactor (confirmed by the Operator, 2026-10-18), while the SAD (`:63`, `:138`, `:825`, QG-1 `:894`) and the spec (US-02, AC-04) still describe a separate deploy per migration step.

## Acceptance

- AC: AC-04 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** sad.md §2 (delivery), §4 (migration order), §7 («Deploying a migration PR») and QG-1 say that the migration steps were built and gated one by one on one branch and that this release deploys them once onto the pre-refactor version, so the deploy's previous state is the `mid_cycle_pre_refactor` snapshot; a split into several deploys needs the previous-step snapshots recaptured first. spec.md AC-04 gets an added-by-fix clarification: when several migration steps ship in one deploy, «the previous migration step» is the version that deploy replaces. No code change.
