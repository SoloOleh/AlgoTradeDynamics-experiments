---
id: T46
title: "Check the position-read capability for the performance adapter"
layer: "app"
deps: ["T40"]
acs: ["AC-15"]
files_hint: ["BE/app/bot_engine/performance.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "todo"
---

# T46 — Check the position-read capability for the performance adapter

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 6.

## Problem

The Momentum and Pattern Scalper performance adapter is resolved checking only `EXECUTION_PNL`, but is then used for `get_position`.

## Acceptance

- AC: AC-15 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The performance adapter is resolved with `{EXECUTION_PNL, POSITION_READS}`; a test proves a venue without position reads is refused.
