---
id: T15
title: "Build bot performance from neutral executions through the adapter"
layer: "app"
deps: ["T10"]
blocks: ["T20"]
acs: ["AC-02"]
files_hint: ["BE/app/bot_engine/performance.py", "BE/tests/test_performance_equivalence.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "S"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T15 — Build bot performance from neutral executions through the adapter

## Place in the sequence

- **Blocked by:** T10 — Helpers onto the port · **Blocks:** T20 — Close the step · **Wave:** 7 (part of SAD migration step 7; independent of the strategy chain, so it runs beside T11).
- **Lane:** own lane (`performance.py`). The service-side guard skip for `GET …/performance` is T08's.

## Why (user story)

> **As an** Operator
> **I want** my existing Bybit bots of all four strategies to place the same orders and reach the same state after the refactor
> **So that** I don't have to re-validate strategies I already trust
>
> — `spec.md §4, US-01, verbatim` · full text: [spec.md](../spec.md)

This task keeps the bot page's PnL, fees and cycle count identical while reading executions through the port.

## Inlined context

> opt open the bot page → API->>AD: read the bot's executions → AD-->>API: neutral executions with realised PnL and fee per fill → API-->>C: PnL, fees and closed cycles, same figures as before the step (AC-02)
>
> — `sad.md §6, Flow 2, «open the bot page» branch, abridged` · full text: [sad.md](../sad.md)

> | `execution_pnl` | Executions carry realised PnL and fee per fill | Grid, DCA (performance), Momentum, Pattern Scalper | spot, linear, inverse |
>
> — `sad.md §5, Capability catalogue row, verbatim` · full text: [sad.md](../sad.md)

> **Hard rule:** Prices, quantities, fees and PnL stay floats converted exactly as today. […] No move to Decimal in this step (it would change AC-02 figures)
>
> — `sad.md §8, Numbers, abridged` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read [sad.md](../sad.md) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

- `GET /api/bots/{bot_id}/performance` → `200 TradingBotPerformance` (unchanged body: `closed_cycles`, `closed_fees`, `net_realized_pnl`, …) — same figures as before the step.

— `contracts/openapi.yaml, operationId getBotPerformance, abridged` · full text: [openapi.yaml](../contracts/openapi.yaml)

## Acceptance criteria

### AC-02 — cross-context

> **Given** an Operator's Bybit bot has closed cycles, fees and realised profit recorded before a migration step
> **When** the Operator opens that bot's page after the migration step
> **Then** the profit and loss, fees and number of closed cycles shown are the same as before the migration step, because they are still built from the same exchange execution history
>
> — `spec.md §5, AC-02, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Before editing: snapshot `TradingBotPerformance` for each golden-run bot at its final state on the current `HEAD` — `BE/tests/test_performance_equivalence.py`
- [ ] Replace the 2 direct session sites with an adapter from `exchange_for(bot, {execution_pnl})`; read neutral executions; no `closedPnl` / Bybit field names — `performance.py`
- [ ] Equivalence test: the snapshot figures equal the migrated figures for every strategy

## Edge cases

| Case | Behaviour |
|---|---|
| Guard refuses for the bot | Refresh skipped, stored figures returned with 200 (T08) |
| Execution with zero fee | Same float result as today |

## Definition of Done

- [ ] `pytest BE/tests/test_performance_equivalence.py` passes — identical PnL, fees, closed cycles
- [ ] Static search: no hits left in `performance.py`; existing suites pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
