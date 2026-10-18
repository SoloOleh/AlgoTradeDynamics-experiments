---
id: T52
title: "Finish aligning the AC-08 docs: test plan, coverage row, stop shown as Error"
layer: "docs"
deps: ["T51"]
acs: ["AC-08"]
files_hint: ["docs/features/exchange-adapter/test-plan.md", "docs/features/exchange-adapter/sad.md", "docs/features/exchange-adapter/ux-flows.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17"
status: "done"
---

# T52 — Finish aligning the AC-08 docs: test plan, coverage row, stop shown as Error

Follow-up task from the independent review — [`_review/review-2026-10-17.md`](../_review/review-2026-10-17.md), finding(s) 1.

## Problem

`test-plan.md:53` still says every lookup failure other than unknown-order means «still active, retry later»; `sad.md:791` says Flow 5 has four branches (it has five); `ux-flows.md:54,61,69` shows a stop (rejected credentials) as Paused, while the code sets `runtime_status = "error"` (`error_handling.py:197-199`), which the bot page shows as Error.

## Acceptance

- AC: AC-08 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** test-plan.md AC-08 domain-invariant row says a retried class asks again later and any other class takes its AC-06 reaction at once, with the order kept active. The sad.md §6 coverage row says Flow 5 has all five branches. ux-flows.md routes the stop outcomes (rejected credentials, permission denied, unclassified) to an «SCR-02 status Error» node and its prose says a pause shows as Paused, a stop as Error. No code change.
