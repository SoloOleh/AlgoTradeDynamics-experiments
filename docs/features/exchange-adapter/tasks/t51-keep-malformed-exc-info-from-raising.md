---
id: T51
title: "Keep malformed exception info from raising out of the log filter"
layer: "wiring"
deps: ["T47"]
acs: ["AC-16"]
files_hint: ["BE/app/core/logging.py", "BE/tests/test_secret_leaks.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-3"
status: "todo"
---

# T51 — Keep malformed exception info from raising out of the log filter

Follow-up task from the independent review — [`_review/review-2026-10-15-3.md`](../_review/review-2026-10-15-3.md), finding(s) 4.

## Problem

`formatException(record.exc_info)` in the log filter has no guard: a malformed tuple such as `exc_info=(ValueError, "not-an-exception", None)` raises `AttributeError` out of `filter()` into the caller, where without the filter `Handler.handleError` would swallow it.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The `formatException` call is in its own `try`; on failure the record carries `exc_text = "unformattable exception info"` and `exc_info = None`. A test logs a malformed `exc_info` through the real handler: no exception reaches the caller and the message is still emitted.
