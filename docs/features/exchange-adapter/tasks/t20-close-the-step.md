---
id: T20
title: "Route backtest instrument reads through the emulator adapter, delete the Bybit session factory and drive the static search to 0"
layer: "wiring"
deps: ["T14", "T15", "T16", "T17", "T18"]
blocks: []
acs: ["AC-17"]
files_hint: ["BE/app/services/backtest_service.py", "BE/app/services/fast_momentum_backtest.py", "BE/app/services/fast_scalper_backtest.py", "BE/app/services/backtest_emulator.py", "BE/app/bot_engine/bybit/", "BE/app/bot_engine/orders.py", "BE/tests/golden/test_static_search.py", "BE/tests/test_backtest_equivalence.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "S"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T20 — Route backtest instrument reads through the emulator adapter, delete the Bybit session factory and drive the static search to 0

## Place in the sequence

- **Blocked by:** T14 — Pattern Scalper, T15 — performance, T16 — streams, T17 — sinks, T18 — connection check · **Blocks:** — · **Wave:** 11 (last; SAD migration step 8).
- **Lane:** shares `orders.py` with T10 (removes its session wrappers) — serialized.

## Why (user story)

> **As an** Operator
> **I want** a new exchange to plug in through its own exchange adapter without changing my strategies
> **So that** Binance can arrive later without re-validating strategy logic
>
> — `spec.md §4, US-08, verbatim` · full text: [spec.md](../spec.md)

This task removes the last way around the seam, so «0 places outside adapters» becomes an asserted fact.

## Inlined context

> (8) The connection check, then delete `get_bybit_session`, with the static search at 0.
>
> — `sad.md §4, Migration order, abridged` · full text: [sad.md](../sad.md)

> The backtests' real exchange calls (the three `instrument_info` reads) move to the Bybit emulator adapter's `instrument_rules`, and the UI live stream takes its endpoints from the adapter, so the backtest reaches the exchange like any other emulator bot, as spec §3 states. […] `BacktestEmulatorClient` keeps one HTTP client that talks to the emulator for admin calls. […] `backtest_service.py`, `fast_momentum_backtest.py` and `fast_scalper_backtest.py` must change their instrument-info reads. Results must stay identical (spec §3).
>
> — `adr/0009, Decision outcome + Consequences, abridged` · full text: [ADR-0009](../adr/0009-scope-the-static-search-to-the-exchange-trading-dialect.md)

> The backtests' instrument-info reads (`backtest_service.py:208`, `fast_momentum_backtest.py:832`, `fast_scalper_backtest.py:1010`) move to the Bybit emulator adapter (ADR-0009). Backtest results must not change (spec §3) | Low | Existing backtest tests, plus one recorded result per backtest engine compared before and after
> `docs/architecture-map.md` §Conventions goes stale: exchange clients move from `bot_engine/bybit/` to `bot_engine/exchange/<name>/` | Low | Re-run `/sdd:survey` after the last PR of the step
> One manual mid-cycle smoke run on the `docker compose` stack before the last PR of the step
>
> — `sad.md §11, risk rows «backtests», «architecture-map stale», «SQLite harness», abridged` · full text: [sad.md](../sad.md)

> **Hard rule:** Changing backtest behaviour [is a non-goal]. […] Their results, their Bybit label and their own replay logic do not change, and no backtest path is exempt from the §6 static search.
>
> — `spec.md §3, Non-goals, abridged` · full text: [spec.md](../spec.md)

> **Hard rule:** | Exchange access outside adapters | 0 places in strategies or runtimes that reach an exchange other than through an exchange adapter, at the end of the step |
>
> — `spec.md §6, NFR row, abridged` · full text: [spec.md](../spec.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([ADR-0009](../adr/0009-scope-the-static-search-to-the-exchange-trading-dialect.md) · [sad.md](../sad.md)) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface. (Backtest routes unchanged.)

## Acceptance criteria

### AC-17 — happy path

> **Given** a stand-in exchange adapter for a test venue that declares the same capabilities as the Bybit adapter
> **When** a bot of each of the four strategies runs a golden-run scenario on that test venue
> **Then** every strategy trades through the stand-in adapter with no change to the strategy itself
>
> — `spec.md §5, AC-17, verbatim` · full text: [spec.md](../spec.md)

This task guarantees no strategy or runtime can bypass the adapter (precondition for AC-17 holding for Binance).

## Checklist

- [ ] Before editing: record one result per backtest engine on the current `HEAD` — `BE/tests/test_backtest_equivalence.py`
- [ ] Replace the three `instrument_info` reads with the Bybit emulator adapter's `instrument_rules`; keep admin-plane calls on `BacktestEmulatorClient`; remove its `/v5/` method — `backtest_service.py`, `fast_momentum_backtest.py`, `fast_scalper_backtest.py`, `backtest_emulator.py`
- [ ] Delete `get_bybit_session`, the `bot_engine/bybit/` re-export shims and T10's session-taking wrappers — `BE/app/bot_engine/bybit/`, `orders.py`
- [ ] Flip the static search to assert `== 0` — `BE/tests/golden/test_static_search.py`
- [ ] Full gate (`scripts/premerge.sh`) + one manual mid-cycle smoke run on `docker compose` (PostgreSQL 18)

## Edge cases

| Case | Behaviour |
|---|---|
| Backtest admin-plane payload parsing with Bybit field names | Exempt from the field-name rule only (ADR-0009) |
| A leftover import of `app.bot_engine.bybit` | Import error → caught by the suites; fix the caller, never restore the shim |

## Definition of Done

- [ ] Static search reports 0 and asserts it
- [ ] Backtest results identical before/after; existing backtest tests pass
- [ ] Full gate passes: golden 12/12, failure table 100%, existing suites 100%
- [ ] Manual docker-compose mid-cycle smoke run recorded in the PR description
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
