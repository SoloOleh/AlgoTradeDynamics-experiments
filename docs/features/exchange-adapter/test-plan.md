---
status: Draft
owner: "Oleh Solomko"
reviewers: ["Oleh Solomko", "Tech Lead"]
updated_at: "2026-10-13"
feature_size: "M"
---

# Test plan — exchange-adapter

Every strategy and runtime reaches an exchange only through the exchange adapter for the bot's venue. Bybit moves behind it with trading equivalence, including bots that are mid-cycle across a deploy. An unsupported venue or a missing capability fails closed, and no key or secret leaks.

Inputs: [spec.md](./spec.md) §5/§6, [sad.md](./sad.md) §6 Flows 1–10 and §10 (`target_surfaces: [backend-service]`, so there are no UI tiers), [data-model.md](./data-model.md) (no schema change), [contracts/openapi.yaml](./contracts/openapi.yaml), [contracts/events.md](./contracts/events.md), ADR-0007 (in [adr/](./adr/)), [tasks.json](./tasks.json).

**Decisions settled at this stage:**
- **Golden-run scenarios** (spec §8 OQ, due here): accepted the spec default. Each strategy gets one `trend-and-range` scenario and one `rounding-boundary` scenario, where prices sit exactly on rounding-step boundaries. For Momentum and Pattern Scalper, mid-cycle means an open position with its take-profit and stop orders placed. For Grid and DCA it means open entry orders plus a take-profit (AC-04). The NFR count covers 8 recordings (4 strategies × {fresh `trend-and-range`, mid-cycle}) and 12 replays. The 4 `rounding-boundary` fresh recordings are counted separately on top of that, so T02 records 12 files and replays 16 runs in total. **Deferred:** the 4 `rounding-boundary` recordings were never made, so today 8 recordings are replayed as 12 runs. Recording them is an open item in [spec §8](./spec.md#8-open-questions).
- **Stand-in adapter for AC-17** (SAD §6 «Flagged for design»): a separate adapter class on a test venue that implements the port in its own code and talks to the same exchange emulator. The golden scenarios and the canonical comparison therefore apply unchanged.

## Levels

| Level | Scope | Strategy (generic — no tool names) |
|---|---|---|
| Unit | Pure logic: the canonicaliser, the Bybit error classifier together with the engine's class table, the AC-08 lookup rule, the guard and capability check, the venue registry binding, the scrubber, and the static search over source files. | In-memory, no network, no database. The failure-table and capability-invariant tests are table-driven. |
| Integration | One module against the real dependencies it owns: the backend against a throwaway database, the adapter against the real exchange emulator. | The exchange emulator runs as a subprocess with its own throwaway database file. The backend uses a throwaway database file (the repo's existing test-database convention). The app runs in-process behind the app's test HTTP client. Nothing is mocked except the client's HTTP transport, which is replaced only to replay recorded Bybit failure payloads. |
| Contract | The shapes in `contracts/openapi.yaml` (`getVenueConnection`, `GuardRefusal`, `404`, `401`) and `contracts/events.md` (`bot.guard_paused.v1`). | Validate the real response or event produced by the app against the schema in the contract file. No hand-rolled copies of the schema. |
| E2E | A full bot cycle: the worker tick loop plus strategy plus adapter, against the real emulator, with prices driven through the emulator admin plane. | The golden-run harness (ADR-0007). It records at the emulator-client boundary, restores mid-cycle snapshots, and diffs canonical records against the pre-refactor recordings. |
| Load | The two numeric guard-rail NFRs (reaction latency, adapter overhead). | Wall-clock timings recorded by the golden-run harness, compared pre-refactor vs migrated. No separate load tool (the one in your repo, or e.g. k6 / Locust) is needed. |
| Component | <!-- N/A: no UI surface in target_surfaces --> | — |
| Visual-regression | <!-- N/A: no UI surface in target_surfaces --> | — |
| E2E-through-UI | <!-- N/A: no UI surface in target_surfaces --> | — |

## AC coverage

| AC (spec.md §5) | Test name (intent-based) | Level | Expected outcome |
|---|---|---|---|
| AC-01 happy path | `fresh trend-and-range run of each strategy is equivalent to its recording` | e2e | For each of Grid, DCA, Momentum and Pattern Scalper, the order-mutating requests, the final bot state and the event categories are equal to the pre-refactor recording |
| AC-01 happy path | `fresh rounding-boundary run of each strategy is equivalent to its recording` | e2e | **Deferred, see [spec §8](./spec.md#8-open-questions): not recorded yet.** Prices and quantities after rounding are equal to the recording at every step boundary |
| AC-01 happy path | `canonical records ignore event wording and read requests` | unit | Two records that differ only in message text or in read requests compare equal |
| AC-01 happy path | `canonical records detect a changed order` | unit | A different side, rounded price or quantity, client tag, or place/cancel order is reported as a difference |
| AC-02 cross-context | `bot performance from adapter executions matches the pre-migration figures` | integration | Profit and loss, fees and closed-cycle count are identical before and after for a bot with closed cycles on the emulator |
| AC-03 domain invariant | `two bots with their own emulator keys trade only on their own accounts` | integration | The emulator shows each account's orders and balance changed only by its own bot |
| AC-03 domain invariant | `venue registry binds each bot's adapter to the account from its configuration` | unit | A bot with a key gets an adapter bound to that key. A bot without one gets the shared default emulator account. Two bots never share a session bound to the wrong key |
| AC-04 happy path | `mid-cycle recording resumes equivalently from the pre-refactor state` | e2e | For each strategy, the cycle completes with trading equivalence to the recording |
| AC-04 happy path | `mid-cycle recording resumes equivalently from the previous step's fresh state` | e2e | The same holds when starting from the state captured on the previous migration step. The snapshots were last captured for the last strategy migration step (`394be42`); see «Test data» for what that covers in this release |
| AC-05 domain invariant | `mid-cycle grid and dca runs place no duplicate and cancel nothing unintended` | e2e | 0 duplicate orders and 0 unintended cancellations across all Grid and DCA mid-cycle replays |
| AC-05 domain invariant | `orders placed by the previous version are recognised on the first tick` | e2e | Given entry and take-profit orders with the previous version's client tags on the emulator and in the bot's order rows, the migrated code's first tick sends no place and no cancel. Proven inside the mid-cycle golden replays, not by a separate one-tick test: `test_mid_cycle_run_completes_equivalently` resumes from those orders and `assert_equivalent` fails on any duplicate place or unintended cancel (`BE/tests/golden/test_golden_runs.py`) |
| AC-06 error | `every reference failure row yields the recorded reaction` | unit | For each row, the recorded payload goes through the Bybit classifier and then the engine class table. The action, severity, stored error type and raw code all equal the row. The failed-lookup row expects the AC-08 reaction |
| AC-06 error | `a running bot hit by each reference failure records the same reaction` | integration | With the recorded payload replayed at the client transport, the bot's stored error fields, its event and its runtime status match the row |
| AC-06 error | `a refusal with an unmapped code stays internal` | unit + integration | An answer body refusing with a code the adapter does not map arrives unclassified: a refused Momentum / Pattern Scalper placement stops the bot as internal with the raw code stored (failure-table row `emulator_unknown_rejection`), a refused Grid/DCA placement is recorded as before the refactor (`test_bots_api.py`, position take-profit case), a lookup raises it, and the connection check reports `exchange_unavailable` (ADR-0005; review 2026-10-18-2, finding 2; review 2026-10-18-3, finding 2) |
| AC-06 error | `retryable failure past the retry limit pauses the bot` | unit + integration | The exhausted-retry row: the bot is paused with the recorded severity |
| AC-07 error | `order placement timeout retries after back-off and keeps the bot running` | integration | The bot stays running, a next retry time is set, and a temporary-network event is recorded. No stopped bot |
| AC-07 error | `order placement timeout past the retry limit pauses the bot` | integration | After the existing retry limit the bot is paused, as today |
| AC-07 error | `temporary network failure maps to the network reaction` | unit | The failure-table rows for temporary network and exhausted retry give the recorded action and severity |
| AC-08 domain invariant | `failed lookup keeps the order active and asks again` | unit | A lookup failure other than unknown-order keeps the order active, with nothing tied to it placed or cancelled. A class the bot already retries leads to «still active, retry later»; any other class takes its AC-06 reaction at once |
| AC-08 domain invariant | `unknown-order answer marks the order gone` | unit | Only this error class, or absence from both open orders and history, leads to «gone» |
| AC-08 domain invariant | `order found in history takes its final state from history` | unit | Filled or cancelled is taken from the history entry |
| AC-08 domain invariant | `lookup failures past the retry limit pause the bot` | unit + integration | The bot is paused after the existing limit, as today |
| AC-08 domain invariant | `no order tied to an unknown-state order is placed or cancelled` | integration | While a lookup keeps failing on the emulator, the order row stays active and 0 related place or cancel requests are sent |
| AC-09 error | `starting a bot whose venue has no adapter is refused with the venue named` | integration | The start is refused with the venue named, the bot does not run, and the recording adapter saw 0 requests |
| AC-09 error | `guard refuses an unknown venue without a default adapter` | integration | No fallback to any Bybit adapter |
| AC-09 error | `venue refusal body matches the guard refusal contract` | contract | The body matches `GuardRefusal` with the venue-unsupported code and the venue details |
| AC-10 cross-context | `running bot whose venue lost its adapter is paused on its next tick` | integration | The bot is paused with the reason stated, a guard-pause event and a venue-unsupported incident are recorded, the Operator is notified, and 0 exchange requests are sent |
| AC-10 cross-context | `running bot whose venue lost its adapter is paused on its next live event` | integration | Same outcome on the live-event path |
| AC-10 cross-context | `guard pause is not retried as an exchange error` | integration | The bot goes straight to paused with no retry scheduled. Its orders on the emulator are unchanged |
| AC-11 authorization | `every exchange-reaching action on another user's bot looks like a missing bot` | integration | For start, stop, sync, cancel orders, close position, and the position and order reads, the response is byte-identical to the one for a non-existent bot id, the guard is not called, and 0 exchange requests are sent |
| AC-11 authorization | `another user's bot on an unsupported venue still looks like a missing bot` | integration | The answer reveals neither the venue nor the state. A guard refusal never leaks through |
| AC-12 happy path | `connection check reports a configured demo venue as connected` | integration | For a signed-in Operator with demo keys and the demo client pointed at the local emulator, the status is connected |
| AC-12 happy path | `connection check answer matches the venue connection contract` | contract | Every status (connected, not configured, failing, not supported) matches the `getVenueConnection` schema |
| AC-13 error | `connection check for a venue without keys reports not configured` | integration | The venue is named, the status is not configured, and no key value appears |
| AC-13 error | `not-configured answer matches the contract` | contract | Matches the schema. There is no error class |
| AC-13b error | `connection check whose exchange request fails reports failing with its error class` | integration | Rejected credentials (a wrong key on the emulator) and a network failure (a closed port) each report failing with the matching error class. The answer contains neither the raw exchange message nor any injected key or secret |
| AC-13b error | `failing answer matches the contract` | contract | The error class is one of the contract's values |
| AC-14 authorization | `connection check without signing in is refused` | integration | Refused for configured, unconfigured and unknown venues alike. The three answers are identical and reveal nothing about configuration |
| AC-14 authorization | `unauthenticated refusal matches the contract` | contract | Matches the contract's unauthenticated response |
| AC-15 domain invariant | `guard refuses a strategy needing an undeclared capability` | unit | The refusal names the missing capability and the market type |
| AC-15 domain invariant | `every bybit configuration accepted today passes the capability check` | unit | For every strategy × market type × settings variant accepted today, the required capabilities are a subset of the Bybit declaration |
| AC-15 domain invariant | `starting a bot with a missing capability is refused without exchange requests` | integration | On the stand-in venue with a reduced capability set, the start is refused, the capability and market type are named, and 0 requests are sent |
| AC-15b cross-context | `running bot whose adapter dropped a needed capability is paused on its next tick` | integration | The bot is paused with the capability and market type named, a capability-missing incident is recorded, the Operator is notified, and 0 requests are sent |
| AC-15b cross-context | `running bot whose adapter dropped a needed capability is paused on its next live event` | integration | Same outcome on the live-event path |
| AC-16 error | `scrubber masks keys to their last four characters and removes secrets` | unit | Known keys appear as `****abcd`. Secrets are removed entirely, including inside a connection address and in the `api_key=`, `X-API-Key:` and `api_secret` patterns |
| AC-16 error | `no sink receives a key or secret from a failing exchange request` | integration | With keys, secrets and an emulator key inside a stream address injected into the failure, none of the bot events, the bot's last error, incidents, operation logs, root log output, notifications or the connection-check answer contains them unmasked |
| AC-17 happy path | `each strategy trades through the stand-in adapter unchanged` | e2e | The `trend-and-range` fresh scenario on the stand-in venue gives canonical records equal to the Bybit recording, apart from the venue fields, for all four strategies |
| AC-17 happy path | `no exchange access outside adapters` | unit | The static search (ADR-0009 scope, no exemptions) finds 0 hits by the end of the step |

## Edge cases / error paths

- Emulator subprocess fails to start, or its port is busy → expected: the suite fails with the emulator's error output and is never silently skipped.
- Two replays differ only in event wording or in read requests (for example lookup by client tag vs the last 50 history items) → expected: no difference reported.
- A mid-cycle Momentum bot with stored order rows that carry only the Bybit reduce-only key → expected: today's accessor (`TradingBotOrder.is_reduce_only`, which reads the Bybit `reduceOnly` key) reads it correctly and the resume is equivalent (inside the AC-04 Momentum replays). The neutral accessor with a legacy fallback is deferred ([spec §8](./spec.md#8-open-questions), ADR-0003 amendment 2026-10-18).
- A key shorter than four characters, or a key that appears twice in one message → expected: still fully masked, and no partial secret survives.
- Records stored before the deploy that contain a key → expected: left as they are (AC-16 applies only to new records). The tests assert only on records written during the test.
- A guard refusal on the Operator's own bot for stop, sync, cancel or close (SAD Flow 2) → expected: refused with the reason, and nothing sent to any exchange.
- Connection check for a venue with no adapter → expected: not supported, with the venue named and nothing sent (covered by the contract row for AC-12).
- A venue path value longer than the stored column bounds → expected: rejected by validation before the guard runs.
- A retryable failure that succeeds on its last allowed retry → expected: the bot keeps running and its retry state behaves as recorded on the pre-refactor code (a failure-table neighbour of the exhausted row).
- Deploy with an order that filled between the previous version's last tick and the migrated version's first tick → expected: the final state is taken from the order history and no re-placement happens (inside the AC-04 replays from the previous step's fresh state).

## Test data

- **Seed strategy:** the golden recordings and mid-cycle snapshots are a one-time artifact of the pre-refactor commit (ADR-0007). They consist of the emulator database file plus the bot's `trading_bots` and `trading_bot_orders` rows, versioned with the harness together with their source commit. The reference failure table is built from real recorded Bybit error payloads, one row per stored error type plus the exhausted-retry row and the failed-lookup row. Users, bots and order rows for the other tests come from the existing register-and-login fixture pattern and the existing bot API (`example.test` / `test.dev` addresses only, no real-looking personal data). No new factory tables are needed (`data-model.md` §Test fixtures).
- **Previous-step snapshots (AC-04):** each `mid_cycle_previous_step/` snapshot was captured on the parent of a strategy migration step (`GOLDEN_CAPTURE_PREVIOUS=1`), all four last at `394be42`. Later commits (SAD migration steps 7–8 and the review fix rounds) were not recaptured. This release ships the branch as one deploy onto the pre-refactor version, so the deploy's real previous state is the one `mid_cycle_pre_refactor/` replays. If the branch is ever split into several deploys, recapture every previous-step snapshot on the tree deployed just before each one.
- **Integration dependency:** the real exchange emulator as a subprocess on a free port with its own throwaway database file, plus the backend on a throwaway database file. No mocked store and no mocked exchange SDK. The client's HTTP transport is replaced only to replay recorded failure payloads, because the emulator has no fault injection until roadmap step 2. A recording stand-in adapter counts requests on the guard paths.
- **Cleanup boundary:** per test, emulator state is isolated with fresh accounts and keys, a fresh user and a fresh bot. Per suite, the emulator subprocess and both database files are removed. Snapshots are restored from a copy, and the stored original is never modified.
- **Known gap:** production runs a different database engine from the throwaway test database (SAD §11, Low). One manual mid-cycle smoke run on the full local stack before the last PR of the step closes it. Likewise, real Bybit demo connectivity for AC-12 is confirmed once by hand before the last PR.

## NFR validation (load)

**Deferred, see [spec §8](./spec.md#8-open-questions).** The scenarios below are the plan. What runs today is 1 pre-refactor baseline per strategy (`BE/tests/golden/recordings/baseline.json`) and one comparison inside every golden replay, below the minimum sample sizes; Momentum and Pattern Scalper record no fill samples.

- **NFR: reaction latency, p95 ≤ pre-refactor baseline + 0.5 s on the emulator** → scenario: replay all 12 golden runs at their recorded pace (one price step per worker tick, one bot at a time). Run the full set 3 times on the pre-refactor commit (baseline) and 3 times on the migrated build, on the same machine, with at least 100 fill events sampled per side. Metric: p95 of the wall-clock time from the emulator publishing a fill event to the bot sending its next order or saving its new state, whichever comes first. Assert migrated p95 ≤ baseline p95 + 0.5 s.
- **NFR: adapter overhead, ≤ 5 ms added to the p95 duration of a single exchange request** → scenario: the same 3 + 3 runs, with at least 1000 exchange requests sampled per side. Metric: p95 of the wall-clock duration of a single request at the emulator-client boundary. Assert migrated p95 ≤ baseline p95 + 5 ms.
- If a side samples fewer events than the minimum, raise the number of runs. Never lower the minimum.

## CI placement

There is no CI pipeline. Every check runs as the single-command local pre-merge routine built in T03, and each PR description records its result (SAD §2, §11).

- **On every PR:** unit, contract and integration tests; the 12 golden replays (the 4 `rounding-boundary` replays are deferred, [spec §8](./spec.md#8-open-questions)); the reference failure table; the static search; the existing backend and emulator suites. The spec requires golden runs and the failure table before every merge, so the e2e tier is not deferred to a schedule. From T19 onward, the AC-17 stand-in runs are added to every PR.
- **On PRs that touch the adapter or a runtime, and before the last PR of the step:** the load measurements (3 × the full replay set, pre-refactor vs migrated; deferred, [spec §8](./spec.md#8-open-questions)), plus the manual mid-cycle smoke on the full local stack and the manual Bybit demo connection check.
