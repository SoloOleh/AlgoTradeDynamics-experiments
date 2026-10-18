---
id: T19
title: "Prove every strategy runs unchanged through a stand-in adapter on a test venue"
layer: "tests"
deps: ["T14"]
blocks: []
acs: ["AC-17"]
files_hint: ["BE/tests/golden/stand_in_adapter.py", "BE/tests/golden/test_stand_in_adapter.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "S"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T19 — Prove every strategy runs unchanged through a stand-in adapter on a test venue

## Place in the sequence

- **Blocked by:** T14 — Migrate Pattern Scalper (all four strategies on the port) · **Blocks:** — · **Wave:** 11 (parallel with T20).
- **Lane:** own lane (test-only files).

## Why (user story)

> **As an** Operator
> **I want** a new exchange to plug in through its own exchange adapter without changing my strategies
> **So that** Binance can arrive later without re-validating strategy logic
>
> — `spec.md §4, US-08, verbatim` · full text: [spec.md](../spec.md)

This task is the executable proof of US-08.

## Inlined context

> Precondition: a test venue is registered with a stand-in adapter that declares the same capabilities as the Bybit adapter → resolve adapter for a bot on the test venue → tick the bot with the stand-in adapter → the same platform-term calls it makes on a Bybit venue → requests in the stand-in's own way → neutral orders, executions and positions → save orders and bot state. alt the stand-in fails a request → exchange error with an error class → failure handled by the same reaction table as for any adapter.
>
> — `sad.md §6, Flow 10, abridged` · full text: [sad.md](../sad.md)

> **Stand-in test exchange (Flow 10).** Whether the stand-in adapter talks to the emulator or works in memory is left to `plan-tests`.
>
> — `sad.md §6, Flagged for design, verbatim` · full text: [sad.md](../sad.md)

> The harness doubles as the AC-17 stand-in-adapter runner. A stand-in adapter (AC-17) is a plain implementation of the same `Protocol`.
>
> — `adr/0007 + adr/0001, Consequences/Positive, abridged` · full text: [ADR-0007](../adr/0007-record-golden-runs-at-the-emulator-client-in-a-pytest-harness.md)

**Fallback:** insufficient or contradicted by the code → read [sad.md](../sad.md) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-17 — happy path

> **Given** a stand-in exchange adapter for a test venue that declares the same capabilities as the Bybit adapter
> **When** a bot of each of the four strategies runs a golden-run scenario on that test venue
> **Then** every strategy trades through the stand-in adapter with no change to the strategy itself
>
> — `spec.md §5, AC-17, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Stand-in adapter implementing `ExchangeAdapter` with `BYBIT_CAPABILITIES`, registered for a test venue via a test-only registry override (shape decided by `plan-tests`; default: in memory) — `BE/tests/golden/stand_in_adapter.py`
- [ ] Run each strategy's fresh golden scenario on the test venue; assert orders placed and state reached; assert `git diff` of `BE/app/bot_engine/strategies/` and runtimes is empty for this task — `BE/tests/golden/test_stand_in_adapter.py`
- [ ] One stand-in failure per strategy → same reaction-table decision as for Bybit

## Edge cases

| Case | Behaviour |
|---|---|
| Strategy reaches for a Bybit field name | Test fails (stand-in never produces one) |
| Stand-in raises `ExchangeError(network)` | Retry with back-off, as for Bybit |

## Definition of Done

- [ ] `pytest BE/tests/golden/test_stand_in_adapter.py` passes for all four strategies
- [ ] No production file changed by this task
- [ ] lint + vet clean
