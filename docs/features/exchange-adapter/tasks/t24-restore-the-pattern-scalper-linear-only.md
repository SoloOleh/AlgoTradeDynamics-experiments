---
id: T24
title: "Restore the Pattern Scalper linear-only start check"
layer: "domain"
deps: ["T20"]
acs: ["AC-15"]
files_hint: ["BE/app/bot_engine/strategies/pattern_scalper.py", "BE/tests/test_pattern_scalper.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "done"
---

# T24 — Restore the Pattern Scalper linear-only start check

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 4.

## Problem

`category != "linear"` was replaced by a SHORT_SELLING capability check; Bybit declares short selling on inverse, so inverse bots now start, and spot gets the capability body instead of today's text.

## Acceptance

- AC: AC-15 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** An inverse Pattern Scalper bot is refused at start with today's `{detail}` text and sends no request; the capability guard still runs first, so a spot Pattern Scalper bot gets the guard's `exchange.capability_missing` body (short_selling on spot, `StartRefused` in openapi.yaml) — corrected 2026-10-15 (review finding 3), pinned by T37.
