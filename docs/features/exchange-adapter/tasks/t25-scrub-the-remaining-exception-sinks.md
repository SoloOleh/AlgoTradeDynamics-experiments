---
id: T25
title: "Scrub the remaining exception sinks"
layer: "wiring"
deps: ["T22"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/exchange/bybit/adapter.py", "BE/app/bot_engine/exchange/errors.py", "BE/app/api/websockets.py", "BE/app/core/logging.py", "BE/tests/test_secret_leaks.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T25 — Scrub the remaining exception sinks

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 5.

## Problem

`_request` re-raises unclassified exceptions raw; `safe()` sends `str(exc)` to the browser; uvicorn's handlers (propagate=False) have no filter; `ScrubbingFilter.filter` calls `getMessage()` unguarded.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** An unclassified transport failure leaves the adapter scrubbed; the bot-page WS snapshot's stream_errors are scrubbed; uvicorn's handlers carry the scrubbing filter; a malformed log record does not raise into application code.
