---
id: T35
title: "Scrub a log record the handler cannot format"
layer: "wiring"
deps: ["T34"]
acs: ["AC-16"]
files_hint: ["BE/app/core/logging.py", "BE/tests/test_secret_leaks.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "done"
---

# T35 — Scrub a log record the handler cannot format

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 1.

## Problem

`ScrubbingFilter.filter` returns `True` untouched when `record.getMessage()` raises; `Handler.handleError` then prints `Message: %r / Arguments: %s` raw to stderr, so a secret passed as an argument leaks in clear (reproduced by all three reviewers). The existing test only checks the filter doesn't raise.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** A log record whose message cannot be formatted (format/argument mismatch) reaches no handler output with a key or secret in clear: msg, args, exc_text and stack_info are scrubbed and args cleared; the test emits through a real handler and asserts stdout and stderr clean.
