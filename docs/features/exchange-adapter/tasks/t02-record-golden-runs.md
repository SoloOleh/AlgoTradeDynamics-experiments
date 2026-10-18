---
id: T02
title: "Record the 8 golden runs and the latency and overhead baselines on the pre-refactor code"
layer: "tests"
deps: ["T01"]
blocks: ["T05"]
acs: ["AC-01", "AC-04", "AC-05"]
files_hint: ["BE/tests/golden/recordings/", "BE/tests/golden/scenarios.py", "BE/tests/golden/test_golden_runs.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T02 — Record the 8 golden runs and the latency and overhead baselines on the pre-refactor code

## Place in the sequence

- **Blocked by:** T01 — Build the golden-run harness · **Blocks:** T05 — Bybit error classification + engine class table (the first task that edits the existing exchange path; every later one depends on it transitively) · **Wave:** 2. The recordings must come from the pre-refactor commit.
- **Lane:** own lane (`BE/tests/golden/recordings/`, `scenarios.py`, `test_golden_runs.py`).

## Why (user story)

> **As an** Operator
> **I want** bots that are mid-cycle when a new version is deployed to continue their cycle without duplicating or needlessly cancelling live orders
> **So that** I don't have to stop my bots for every release of this step
>
> — `spec.md §4, US-02, verbatim` · full text: [spec.md](../spec.md)

This task freezes the reference behaviour (fresh and mid-cycle) that every migration PR is compared against.

## Inlined context

> | Trading equivalence (golden runs) | 100% of golden-run replays equivalent after every migration PR: 8 recordings (4 strategies × {fresh, mid-cycle}), replayed as 12 (each mid-cycle recording from both starting states of AC-04) | golden-run suite, run before every merge |
> | Deploy safety | 0 duplicate orders and 0 unintended cancellations across the mid-cycle golden runs | golden-run diff |
>
> — `spec.md §6, NFR rows «Trading equivalence» + «Deploy safety», verbatim` · full text: [spec.md](../spec.md)

> Open question (spec §8): which price scenarios the golden runs use, and the mid-cycle starting state for Momentum and Pattern Scalper | Open question | Resolve before `/sdd:plan-tests exchange-adapter`. Spec default: one trend-and-range scenario per strategy plus one rounding-boundary scenario. For Momentum and Pattern Scalper, mid-cycle = an open position with its take-profit and stop orders placed
>
> — `sad.md §11, open-question row «price scenarios», verbatim` · full text: [sad.md](../sad.md)

> (0) Build the harness, take the 8 recordings, the reference failure table and the latency and overhead baselines on the pre-refactor code.
>
> — `sad.md §4, Migration order step (0), verbatim` · full text: [sad.md](../sad.md)

> Recordings are a one-time artifact of the pre-refactor commit. Re-recording means checking out that commit, so the recordings and their source commit are versioned with the harness.
>
> — `adr/0007, Consequences/Negative, verbatim` · full text: [ADR-0007](../adr/0007-record-golden-runs-at-the-emulator-client-in-a-pytest-harness.md)

> Note over DB,EX: Precondition: the previous version left entry orders, a take-profit and, where the strategy holds one, a position. The starting state is the pre-refactor recording or the same state captured from the previous migration step
>
> — `sad.md §6, Flow 3, precondition note, verbatim` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([spec.md](../spec.md) · [sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

No DB changes. (Mid-cycle snapshots are copies of the emulator SQLite file plus dumps of existing `trading_bots` / `trading_bot_orders` rows — `data-model.md §Test fixtures`.)

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

- [ ] Pin the scenarios per the spec §8 default (one trend-and-range scenario per strategy + one rounding-boundary scenario; Momentum/Pattern Scalper mid-cycle = open position with take-profit and stop placed) — `BE/tests/golden/scenarios.py`
- [ ] Record 4 fresh runs (Grid, DCA, Momentum, Pattern Scalper) on the emulator venue at the current commit — `BE/tests/golden/recordings/`
- [ ] Record 4 mid-cycle runs: snapshot the mid-cycle state, then record the completion of the cycle from it — `recordings/`
- [ ] Record the latency and overhead baselines (p95) from the same runs — `recordings/baseline.json`
- [ ] Store the source commit hash in every recording
- [ ] Replay test: 8 recordings → 12 replays (each mid-cycle from the pre-refactor snapshot; the «previous step» snapshot slot is the same file until T11 captures a new one); asserts canonical equality, 0 duplicate orders, 0 unintended cancellations, p95 latency ≤ baseline + 0.5 s, p95 request overhead ≤ +5 ms — `BE/tests/golden/test_golden_runs.py`

## Edge cases

| Case | Behaviour |
|---|---|
| A mid-cycle replay places a second ladder or re-places the take-profit | Replay fails, naming the duplicate client tag (Deploy safety = 0) |
| Recording taken on a commit that already contains migration code | Refuse: the recording's commit must be the pre-refactor base (`054a7a1` lineage, before T05 merges) |
| Latency p95 above baseline + 0.5 s | Replay fails with both numbers printed |
| Momentum/Pattern Scalper scenario OQ answered differently before `plan-tests` | Re-record per ADR-0007 (checkout the source commit), never hand-edit a recording |

## Definition of Done

- [ ] 8 recordings + baselines committed under `BE/tests/golden/recordings/`, each stamped with the pre-refactor commit
- [ ] `pytest -m golden` runs 12 replays and all pass on the unchanged code
- [ ] Existing suites still pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
