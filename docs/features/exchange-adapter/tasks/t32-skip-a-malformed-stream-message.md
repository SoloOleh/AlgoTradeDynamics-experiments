---
id: T32
title: "Skip a malformed stream message instead of dropping the connection"
layer: "infra"
deps: ["T20"]
acs: []
files_hint: ["BE/app/bot_engine/exchange/bybit/streams.py", "BE/tests/test_exchange_streams.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T32 — Skip a malformed stream message instead of dropping the connection

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 18.

## Problem

`translate()` parses numbers and a ValueError lands in the broad except that closes and reconnects.

## Acceptance

- AC: n/a (quality) — full text in [spec.md §5](../spec.md).
- **Definition of Done:** A message that fails translation is logged (scrubbed) and skipped; the connection stays open and the next valid message is delivered.
