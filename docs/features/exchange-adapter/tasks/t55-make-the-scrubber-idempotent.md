---
id: T55
title: "Make the scrubber idempotent"
layer: "wiring"
deps: ["T51"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/exchange/redaction.py", "BE/tests/test_scrubber_keys.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17"
status: "done"
---

# T55 — Make the scrubber idempotent

Follow-up task from the independent review — [`_review/review-2026-10-17.md`](../_review/review-2026-10-17.md), finding(s) 3.

## Problem

In `secret=[REDACTED]` the value class `[^\s"'&,;}\]]+` matches `[REDACTED`, so every extra pass adds a `]` (`scrub(scrub('secret=abcdefgh123'))` → `'secret=[REDACTED]]'`). ADR-0008 scrubs at several layers on purpose, so stored events, logs and Telegram texts can carry extra brackets. Nothing leaks.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** `scrub(scrub(x)) == scrub(x)` for key and secret shapes; an already-scrubbed `[REDACTED]` value is left as it is, so repeated passes (adapter, sink, each log handler) add no `]`. A test pins idempotency and that secrets are still scrubbed.
