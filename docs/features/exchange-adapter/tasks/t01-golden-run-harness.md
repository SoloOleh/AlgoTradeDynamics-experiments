---
id: T01
title: "Build the golden-run harness around the emulator client"
layer: "tests"
deps: []
blocks: ["T02", "T03"]
acs: ["AC-01", "AC-04"]
files_hint: ["BE/tests/golden/conftest.py", "BE/tests/golden/harness.py", "BE/tests/golden/canonical.py", "BE/tests/golden/test_harness_smoke.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T01 — Build the golden-run harness around the emulator client

## Place in the sequence

- **Blocked by:** — · **Blocks:** T02 — Record the 8 golden runs and the baselines, T03 — Record the reference failure table and add the pre-merge gate · **Wave:** 1 (a root: it must exist on the **pre-refactor** code, before any migration task touches the exchange path).
- **Lane:** own lane (new directory `BE/tests/golden/`). Touches no production code.

## Why (user story)

> **As an** Operator
> **I want** my existing Bybit bots of all four strategies to place the same orders and reach the same state after the refactor
> **So that** I don't have to re-validate strategies I already trust
>
> — `spec.md §4, US-01, verbatim` · full text: [spec.md](../spec.md)

This task builds the instrument that proves it: a harness that records a strategy run at the emulator-client boundary and replays it for comparison.

## Inlined context

> A pytest harness runs the real exchange emulator as a local subprocess on a temporary SQLite file and the backend bot engine and worker in-process on SQLite. It drives prices through the emulator admin API and records every request at the `EmulatorHTTP` client boundary, which exists both before and after the refactor. It snapshots mid-cycle states (the emulator DB file plus the bot and order rows), and it timestamps fill → reaction on the wall clock.
>
> — `sad.md §4, pillar 7, verbatim` · full text: [sad.md](../sad.md)

> **Chosen:** Option 1, the pytest harness. The `EmulatorHTTP` client exists before and after the refactor (ADR-0002), so the same hook records both. Snapshots are file copies, the real WS path gives a true fill → reaction latency, and the whole suite runs as one pytest command before every merge.
> Equivalence is compared on canonicalised records: order-mutating requests only (side, price and quantity after rounding, client tag, sequence of place and cancel). Read requests are recorded for diagnosis but not compared […]. The other compared records are final bot state (status, cycles, positions, PnL) and event categories (event type + stored error type + raw code). Wording is ignored, per the glossary.
> Recordings are a one-time artifact of the pre-refactor commit. Re-recording means checking out that commit, so the recordings and their source commit are versioned with the harness.
> Clock-dependent strategy logic uses `app.core.clock`, so replays pin the simulated clock while latency uses the wall clock (spec §6).
>
> — `adr/0007, Decision outcome + Consequences, abridged` · full text: [ADR-0007](../adr/0007-record-golden-runs-at-the-emulator-client-in-a-pytest-harness.md)

> | Reaction latency (fill → bot reaction) | p95 ≤ pre-refactor baseline + 0.5 s on the emulator […], measured from the moment the emulator publishes the fill event to the moment the bot sends its next order or saves its new state, whichever comes first | wall-clock timestamps recorded by the golden-run harness (not the simulated clock) |
> | Adapter overhead | ≤ 5 ms added to the p95 duration of a single exchange request, compared with the pre-refactor run on the same emulator | wall-clock request timing in the golden-run harness, pre-refactor vs migrated |
>
> — `spec.md §6, NFR rows «Reaction latency» + «Adapter overhead», abridged` · full text: [spec.md](../spec.md)

> **Hard rule:** Tests use pytest with an SQLite override and the emulator as the exchange double, with no SDK mocks. In this step only mechanical test edits are allowed (spec §6 «Existing test suites»).
>
> — `sad.md §2, Conventions, verbatim` · full text: [sad.md](../sad.md)

> The golden-run harness runs the backend on SQLite, while production runs PostgreSQL 18, so float and JSON round-trips may differ | Low | Matches the existing test convention. One manual mid-cycle smoke run on the `docker compose` stack before the last PR of the step
>
> — `sad.md §11, risk row «harness on SQLite», verbatim` · full text: [sad.md](../sad.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([spec.md](../spec.md) · [sad.md](../sad.md) · [adr/](../adr/)) and follow it. Do not guess.

## Data delta

No DB changes. (The harness snapshots the existing `trading_bots` / `trading_bot_orders` rows and the emulator SQLite file — `data-model.md §Test fixtures`.)

## API contract

Internal — no API surface. (Drives the emulator's existing admin plane `/api/admin/…` for prices; that plane is not exchange access — ADR-0009.)

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

This task delivers the mechanism these ACs are verified by; T02 records the runs.

## Checklist

- [ ] Fixture that starts `EXCHANGE_EMULATOR` as a uvicorn subprocess on a temp SQLite file and a free port; tears it down — `BE/tests/golden/conftest.py`
- [ ] Recording hook on the `EmulatorHTTP` HTTP client (request method, path, params, wall-clock start/end) that works on today's `BE/app/bot_engine/bybit/emulator_client.py` without editing it (monkeypatch the transport) — `BE/tests/golden/harness.py`
- [ ] Scenario driver: push a price sequence through the emulator admin API, pin `app.core.clock` to the simulated time, run the worker tick loop in-process — `harness.py`
- [ ] Snapshot/restore of a mid-cycle state: copy of the emulator DB file + dump of the bot's `trading_bots` / `trading_bot_orders` rows — `harness.py`
- [ ] Canonicaliser: keep order-mutating requests (side, rounded price/qty, client tag, place/cancel sequence), final bot state (status, cycles, positions, PnL), event categories (event type + stored error type + raw code); drop wording and read requests from the compared record — `BE/tests/golden/canonical.py`
- [ ] Wall-clock timings: fill-event publish → next order or state save (latency), per-request duration (overhead), stored alongside each recording — `harness.py`
- [ ] Recording format on disk (JSON) carrying the source commit hash — `harness.py`
- [ ] Smoke test: one short DCA scenario recorded twice gives identical canonical records — `BE/tests/golden/test_harness_smoke.py`
- [ ] Register a `golden` pytest marker so the gate (T03) can run the suite in one command

## Edge cases

| Case | Behaviour |
|---|---|
| Emulator subprocess fails to start or port busy | Fixture fails the test with the emulator's stderr; no silent skip |
| Two replays of the same scenario differ only in event wording | Canonical records are equal (wording ignored) |
| A read request differs between runs (e.g. lookup by tag vs last-50 history) | Not a diff — recorded for diagnosis only |
| Simulated clock vs wall clock | Strategy logic sees the pinned simulated clock; latency/overhead use the wall clock |

## Definition of Done

- [ ] `pytest BE/tests/golden/test_harness_smoke.py` passes: two recordings of the same scenario are canonically equal
- [ ] The harness runs on the current (pre-refactor) code with no production-code change
- [ ] Existing suites still pass (`pytest` in `BE/` and `EXCHANGE_EMULATOR/`)
- [ ] every Hard Rule inlined above still holds (no SDK mocks)
- [ ] lint + vet clean
