---
id: T62
title: "Pin the AC-15b capability pause on a scheduled tick"
layer: "tests"
deps: ["T59"]
acs: ["AC-15b"]
files_hint: ["BE/tests/test_guard_pause.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-18"
status: "done"
---

# T62 — Pin the AC-15b capability pause on a scheduled tick

Follow-up task from the independent review — [`_review/review-2026-10-18.md`](../_review/review-2026-10-18.md), finding(s) 3.

## Problem

test-plan.md's AC-15b row promises «paused on its next tick», but `test_guard_pause.py:120-149` drove only the live-event wake-up.

## Acceptance

- AC: AC-15b — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The AC-15b capability-missing test runs for both triggers, the scheduled fallback tick and the live-event wake-up, and on each the bot is paused with the capability and market type named, a capability-missing event and incident, the Operator notified and no adapter call made.
