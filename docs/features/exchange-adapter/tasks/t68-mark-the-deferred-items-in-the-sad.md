---
id: T68
title: "Mark the deferred rounding-boundary runs and latency check in the SAD"
layer: "docs"
deps: ["T65"]
acs: ["AC-01"]
files_hint: ["docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18-2"
status: "todo"
---

# T68 — Mark the deferred rounding-boundary runs and latency check in the SAD

Follow-up task from the independent review — [`_review/review-2026-10-18-2.md`](../_review/review-2026-10-18-2.md), finding(s) 3.

## Problem

SAD §11 (`sad.md:956`) lists the rounding-boundary scenario as the accepted default with no note that its recordings were never made, and QG-4 "How verify" (`:931`) describes the latency comparison with no note that it is not yet validated. Both are deferred in spec §8 and test-plan.md.

## Acceptance

- AC: AC-01 — full text in [spec.md §5](../spec.md); §6 NFR (reaction latency).
- **Definition of Done:** The SAD §11 golden-run scenarios row says the rounding-boundary recordings are deferred (spec §8), and QG-4 carries a "Deferred, see spec §8" line naming the missing fill samples. No code change.
