---
id: T27
title: "Declare every capability each strategy actually uses"
layer: "domain"
deps: ["T20"]
acs: ["AC-15"]
files_hint: ["BE/app/bot_engine/strategies/grid.py", "BE/app/bot_engine/strategies/dca.py", "BE/tests/test_venue_guard.py", "docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T27 — Declare every capability each strategy actually uses

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 9.

## Problem

Grid places Market close orders and DCA reads positions every tick, but neither declares the capability, so AC-15 does not hold for a non-Bybit adapter.

## Acceptance

- AC: AC-15 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** Grid declares MARKET_ORDERS, DCA declares POSITION_READS; a venue lacking either refuses that strategy at start; every configuration accepted today still passes on Bybit; sad.md §5 catalogue matches.
