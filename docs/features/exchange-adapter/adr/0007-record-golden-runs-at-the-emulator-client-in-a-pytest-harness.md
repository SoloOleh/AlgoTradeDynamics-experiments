---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0007 — Record golden runs at the emulator client in a pytest harness

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Trading equivalence is proven by golden runs: 8 recordings (4 strategies × {fresh, mid-cycle}) taken once on the pre-refactor code, then 12 replays on every migration PR (spec §6, AC-01, AC-04). The same harness measures the reaction-latency and adapter-overhead NFRs on the wall clock. The recording point must exist both before the refactor (no adapter yet) and after it. The repo has no CI and its tests run on SQLite with the emulator as the exchange double (SAD §2) (SAD §4 pillar 7).

## Decision drivers

- Spec §6 «Trading equivalence», «Deploy safety»: 100% of the 12 replays equivalent; 0 duplicate orders and 0 unintended cancellations.
- Spec §6 «Reaction latency», «Adapter overhead»: wall-clock timestamps, a real fill event, a pre-refactor baseline.
- SAD §2: a local pre-merge routine. It must be fast and deterministic enough to run before every merge.

## Considered options

1. **A pytest harness with the emulator in a subprocess.** `BE/tests/golden/` starts the real emulator as a local uvicorn process on a temporary SQLite file. It runs the backend bot engine and worker loop in the test process on SQLite, drives prices through the emulator admin API, and records requests with a hook on the `EmulatorHTTP` HTTP client. Mid-cycle states are snapshots (a copy of the emulator DB file plus a dump of the bot and order rows).
2. **The full docker compose stack.** A script brings up PostgreSQL 18, the backend and the emulator, drives scenarios through the API, and diffs database state.

## Decision outcome

**Chosen:** Option 1, the pytest harness. The `EmulatorHTTP` client exists before and after the refactor (ADR-0002), so the same hook records both. Snapshots are file copies, the real WS path gives a true fill → reaction latency, and the whole suite runs as one pytest command before every merge. Option 2 is minutes per replay × 12, its timings are flaky, and PostgreSQL snapshots are heavy without CI.

## Consequences

**Positive**
- Equivalence is compared on canonicalised records: order requests (side, price and quantity after rounding, client tag, sequence of place and cancel), final bot state (status, cycles, positions, PnL) and event categories (event type + stored error type + raw code). Wording is ignored, per the glossary.
- The harness doubles as the AC-17 stand-in-adapter runner.

**Negative**
- The backend runs on SQLite, not PostgreSQL 18, in the harness (an existing test convention). Float and JSON round-trips may differ subtly from production.
- Recordings are a one-time artifact of the pre-refactor commit. Re-recording means checking out that commit, so the recordings and their source commit are versioned with the harness.

**Neutral**
- Clock-dependent strategy logic uses `app.core.clock`, so replays pin the simulated clock while latency uses the wall clock (spec §6).

## Links

- Spec: [[../spec.md]] AC-01, AC-04, AC-05, AC-17, §6, §8 OQ on scenarios
- SAD: [[../sad.md]] §4 pillar 7, §10
- Related ADR: [[0002-implement-the-bybit-adapter-over-the-existing-clients]]
