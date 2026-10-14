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
status: "todo"
---

# T24 — Restore the Pattern Scalper linear-only start check

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 4.

## Problem

`category != "linear"` was replaced by a SHORT_SELLING capability check; Bybit declares short selling on inverse, so inverse bots now start, and spot gets the capability body instead of today's text.

## Acceptance

- AC: AC-15 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** An inverse or spot Pattern Scalper bot is refused at start with today's `{detail}` text and sends no request; the capability guard still runs on top.
