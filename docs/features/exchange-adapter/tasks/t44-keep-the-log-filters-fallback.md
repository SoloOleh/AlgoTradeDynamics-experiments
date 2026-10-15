---
id: T44
title: "Keep the log filter's fallback from raising into the caller"
layer: "wiring"
deps: ["T40"]
acs: ["AC-16"]
files_hint: ["BE/app/core/logging.py", "BE/tests/test_secret_leaks.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15-2"
status: "todo"
---

# T44 — Keep the log filter's fallback from raising into the caller

Follow-up task from the independent review — [`_review/review-2026-10-15-2.md`](../_review/review-2026-10-15-2.md), finding(s) 4.

## Problem

Regression from T35: the fallback builds `f"{record.msg} … {record.args!r}"` outside any guard, so a msg or argument whose `__str__`/`__repr__` raises makes `filter()` raise out of `Handler.handle`, which `handleError` does not cover. `logger.warning(BadStr())` now raises.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** The fallback text is built in its own `try`; if that fails too the record carries a text holding neither msg nor args (`unformattable log record at {pathname}:{lineno}`). A test logs an object whose `__str__` raises, as msg and as an argument, through the real handler: no exception reaches the caller and no secret appears on stdout or stderr.
