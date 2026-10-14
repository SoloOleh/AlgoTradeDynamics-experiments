---
id: T34
title: "Remove leftover dead code and give the pre-merge gate its own test database"
layer: "wiring"
deps: ["T26"]
acs: []
files_hint: ["BE/app/bot_engine/grid_runtime.py", "BE/app/bot_engine/error_handling.py", "scripts/premerge.sh"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T34 — Remove leftover dead code and give the pre-merge gate its own test database

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 21, 22.

## Problem

Dead code left by the migration; all stages share `$TMPDIR/algotrade_test.db`, which made the gate flaky.

## Acceptance

- AC: n/a (quality) — full text in [spec.md §5](../spec.md).
- **Definition of Done:** No uncalled `_mark_order_missing_from_exchange`, no unread `remote_by_link` write, no stale docstring; premerge.sh runs every stage on a fresh per-run temp dir so concurrent pytest runs can't collide.
