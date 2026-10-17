---
id: T53
title: "Finish carrying clear-history into the design: Flow 9, coverage rows, api-sync report"
layer: "docs"
deps: ["T51"]
acs: ["AC-10", "AC-16"]
files_hint: ["docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/contracts/api-sync-report.md", "docs/features/exchange-adapter/contracts/events.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17"
status: "done"
---

# T53 — Finish carrying clear-history into the design: Flow 9, coverage rows, api-sync report

Follow-up task from the independent review — [`_review/review-2026-10-17.md`](../_review/review-2026-10-17.md), finding(s) 2.

## Problem

Flow 9's postcondition (`sad.md:735`) and the US-04 / AC-16 coverage rows (`sad.md:775,800`) leave out the Flow 2 clear-history branches; `api-sync-report.md:90,122,123` still shows F-3 and F-4 open with no resolution pointer; `events.md:108` «Both fit the existing columns» no longer has a clear antecedent.

## Acceptance

- AC: AC-10 — full text in [spec.md §5](../spec.md).
- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** sad.md Flow 9's postcondition names the clear-history answer as a sink. The US-04 and AC-16 coverage rows cite the Flow 2 clear-history branches. contracts/api-sync-report.md F-3 and F-4 carry resolution notes (data-model guard-pause note / T43; sad.md Flow 2, pillar 4, §8 / T49). contracts/events.md no longer says «Both fit» about an unclear antecedent. No code change.
