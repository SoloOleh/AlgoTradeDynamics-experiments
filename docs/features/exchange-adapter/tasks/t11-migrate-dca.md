---
id: T11
title: "Migrate the DCA strategy and runtime onto the exchange adapter"
layer: "app"
deps: ["T08", "T09", "T10"]
blocks: ["T12"]
acs: ["AC-01", "AC-04", "AC-05"]
files_hint: ["BE/app/bot_engine/dca_runtime.py", "BE/app/bot_engine/strategies/dca.py", "BE/tests/golden/recordings/", "BE/tests/test_dca_strategy.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T11 — Migrate the DCA strategy and runtime onto the exchange adapter

## Place in the sequence

- **Blocked by:** T08 — Guard at start and actions, T09 — Guard in the worker, T10 — Helpers onto the port · **Blocks:** T12 — Migrate Grid · **Wave:** 7 (SAD migration step 3 — DCA is the smallest runtime, so it goes first).
- **Lane:** shares `strategies/dca.py` with T07 and `BE/tests/golden/recordings/` with T02, T12–T14 — serialized.

## Why (user story)

> **As an** Operator
> **I want** bots that are mid-cycle when a new version is deployed to continue their cycle without duplicating or needlessly cancelling live orders
> **So that** I don't have to stop my bots for every release of this step
>
> — `spec.md §4, US-02, verbatim` · full text: [spec.md](../spec.md)

This task moves the first strategy behind the adapter and proves it on the fresh and mid-cycle golden runs.

## Inlined context

> (3) DCA (the smallest runtime). […] Every step is its own PR and passes the golden runs, the failure table and the existing suites before it merges.
>
> — `sad.md §4, Migration order, abridged` · full text: [sad.md](../sad.md)

> WK->>ST: tick the bot with its adapter
> ST->>DB: read own order rows by client tag and stored status → rows written by the previous version, read natively
> ST->>AD: look up each stored order by client tag → open, final (filled or cancelled) or not found
> alt the stored entry orders and the take-profit are still open → orders recognised as own, no second ladder, take-profit kept as is
> else some orders changed state while the backend was down → the next order actions of the cycle, the same ones the pre-refactor version sends from this state
> else a lookup fails → the order stays active, nothing tied to it is placed or cancelled (Flow 5)
>
> — `sad.md §6, Flow 3, abridged` · full text: [sad.md](../sad.md)

> Rows written by the pre-refactor code are read natively by the migrated code, so a mid-cycle bot recognises its own orders after a deploy (AC-05, QG-1). […] client order tags (`bot-{id}-g{gen}-…`).
>
> — `sad.md §4, pillar 3, abridged` · full text: [sad.md](../sad.md)

> Client order tags are `bot-{id}-g{generation}-…` plus a deterministic suffix (`grid_runtime.py:41-47`, `dca_runtime.py:213`).
>
> — `sad.md §2, Conventions, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** Order-mutating requests (place, cancel) stay byte-identical on the wire.
>
> — `sad.md §4, pillar 2, abridged` · full text: [sad.md](../sad.md)

> **Hard rule:** No Bybit field name (`lotSizeFilter`, `closedPnl`, `tradeMode`, `retCode`) survives outside the Bybit adapter.
>
> — `sad.md §4, pillar 1, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** Only mechanical test edits are allowed (signing in before a call, pointing a stub at the exchange adapter instead of the removed session factory); expected results do not change.
>
> — `spec.md §6, NFR «Existing test suites», abridged` · full text: [spec.md](../spec.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [spec.md](../spec.md)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bot_orders.status` | VARCHAR(30) | NOT NULL | read/write — frozen stored vocabulary |
| `trading_bot_orders.order_link_id` | VARCHAR(160) | NULL, indexed | read/write — frozen tag format |
| `trading_bot_orders.raw_response` | JSON | NOT NULL | write — raw exchange payload, unchanged |

— `data-model.md §Entities, trading_bot_orders, abridged` · full text: [data-model.md](../data-model.md)

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-01 — happy path

> **Given** a golden run from a fresh bot was recorded with the pre-refactor version for each of Grid, DCA, Momentum and Pattern Scalper on the Bybit emulator venue
> **When** the same price scenario is replayed after any migration step
> **Then** each strategy's run shows trading equivalence with its recording: the same orders in the same order, the same bot state, and the same event categories (event type plus the recorded error type, whose stored values stay the same as today)
>
> — `spec.md §5, AC-01, verbatim` · full text: [spec.md](../spec.md)

### AC-04 — happy path

> **Given** a golden run from a mid-cycle state was recorded for each strategy, where the pre-refactor version left open entry orders, a take-profit order and (where the strategy holds one) an open position
> **When** the bot resumes on the migrated version from each of two starting states: the one recorded by the pre-refactor version, and the same state freshly captured from the previous migration step
> **Then** it completes the cycle with trading equivalence to the recording from both starting states
>
> — `spec.md §5, AC-04, verbatim` · full text: [spec.md](../spec.md)

### AC-05 — domain invariant

> **Given** a Grid or DCA bot is mid-cycle with entry orders and a take-profit order placed by the previous version
> **When** the migrated version is deployed and the bot's next check runs
> **Then** the bot recognises those orders as its own, places no second ladder, does not cancel and re-place the take-profit, and continues the cycle, because a bot never holds two live copies of the same intended order
>
> — `spec.md §5, AC-05, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Before editing: capture the «previous migration step» mid-cycle snapshot for every strategy from the current `HEAD` — `BE/tests/golden/recordings/`
- [ ] Replace every `get_bybit_session` / raw-session call with the `adapter=` passed by the guard and the T10 helpers; use `resolve_order_state` for lookups; no Bybit field names — `dca_runtime.py`, `strategies/dca.py`
- [ ] Mechanical edits only in `BE/tests/test_dca_strategy.py` (stub the adapter instead of the session factory)
- [ ] Run the gate (`scripts/premerge.sh`)

## Edge cases

| Case | Behaviour |
|---|---|
| Mid-cycle DCA with take-profit still open after deploy | Recognised; take-profit not cancelled or re-placed |
| An entry order filled while the backend was down | Same next actions as the pre-refactor recording |
| Lookup fails during resume | Order kept active; nothing tied to it placed/cancelled (AC-08) |

## Definition of Done

- [ ] `pytest -m golden`: DCA fresh + both mid-cycle replays equivalent, 0 duplicate orders, 0 unintended cancellations; all 12 replays pass
- [ ] Static search: no hits left in `dca_runtime.py` / `strategies/dca.py`
- [ ] Failure table and existing suites pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
