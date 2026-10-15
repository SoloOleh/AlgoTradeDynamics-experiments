---
id: T40
title: "Skip a stream frame that is not JSON instead of reconnecting"
layer: "infra"
deps: ["T34"]
acs: []
files_hint: ["BE/app/bot_engine/exchange/bybit/streams.py", "BE/tests/test_exchange_streams.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "done"
---

# T40 — Skip a stream frame that is not JSON instead of reconnecting

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 7.

## Problem

`json.loads(raw)` at `streams.py:213` sits outside T32's per-message `try`; a non-JSON frame hits the reconnect `except`.

## Acceptance

- AC: n/a (quality follow-up).
- **Definition of Done:** A text frame that is not valid JSON is logged scrubbed and skipped; the connection is not reopened and the next event is delivered.
