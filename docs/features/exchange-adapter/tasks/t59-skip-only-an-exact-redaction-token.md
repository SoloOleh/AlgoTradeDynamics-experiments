---
id: T59
title: "Skip only an exact redaction token in the scrubber"
layer: "wiring"
deps: ["T55"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/exchange/redaction.py", "BE/tests/test_scrubber_keys.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17-2"
status: "done"
---

# T59 — Skip only an exact redaction token in the scrubber

Follow-up task from the independent review — [`_review/review-2026-10-17-2.md`](../_review/review-2026-10-17-2.md), finding(s) 4.

## Problem

The T55 lookahead `(?!\[REDACTED\])` has no right boundary and `(?i)` makes it case-insensitive, so any value that merely starts with `[REDACTED]` is skipped and its tail stays in clear text. The pre-fix code leaked the same tails, so this is not a regression.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The scrubber skips a value only when it is exactly `[REDACTED]` (followed by the end of the value), so `secret=[REDACTED]abc123def`, `api_key=[REDACTED]AKIAXYZ123456` and `secret=[redacted]abc123def` have their tails scrubbed, while `scrub(scrub(x)) == scrub(x)` still holds. Tests pin both.
