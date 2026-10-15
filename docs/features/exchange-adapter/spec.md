---
status: Draft
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-11"
feature_size: "M"
---

# Spec — exchange-adapter

> **Glossary:** [project CONTEXT](../../../CONTEXT.md) · [feature CONTEXT](./CONTEXT.md)
> **Reference module / docs / channels used:** [`docs/roadmap.md`](../../roadmap.md) step 0, [`docs/idea-brief.md`](../../idea-brief.md), [`docs/architecture-map.md`](../../architecture-map.md) §Conventions and §Constraints & known tech-debt, the bot engine code under `BE/app/bot_engine/` and the connection check in `BE/app/api/bybit.py`.

## 1. Context

The Operator can run Grid, DCA, Momentum and Pattern Scalper bots only on Bybit. Every strategy and every runtime reaches the exchange directly: about 22 call sites obtain a Bybit session themselves, the live event streams, error handling and the connection check carry Bybit-specific logic, and together they read Bybit-shaped responses, react to Bybit error codes and message texts, and round prices and quantities with Bybit-shaped instrument rules (`architecture-map.md` §Constraints & known tech-debt). The Binance work on the roadmap (steps 1–8) would otherwise have to either translate Binance into Bybit shapes or fork every strategy, and each of those paths puts the Operator's existing, trusted Bybit bots at risk.

There is no external trigger. This is wave 1 of the roadmap and it blocks step 1 (Connect Binance) and with it the whole Binance chain. Waiting is cheap today but grows more expensive with every strategy change that adds another direct exchange call.

**Committed approach.** Migrate all four strategies and their runtimes, step by step, onto one exchange-neutral **exchange adapter** chosen by the bot's **venue**. The adapter speaks platform terms, reports failures as **error classes**, and declares its **capabilities** per market type, so strategies never check an exchange's name. Every migration step is proven by **golden runs** (from a fresh bot and from a bot mid-cycle under the previous version) and by a reference table of failure → reaction. A bot whose venue has no exchange adapter is refused at start or paused while running, and is never routed to Bybit by default. Rationale: comparable multi-exchange products (unified exchange libraries, open-source bots, hosted bot platforms) all expose a neutral surface and document per-exchange capability differences, but none publishes a method for proving that an already-live exchange behaves identically after moving behind that surface, or refuses an unsupported exchange-and-environment pair. That proof is the point of this step. The sharpest failure mode found is bots that are running across a deploy: their open orders were recorded by the old code and may not be recognised by the new code, which leads to a second ladder and doubled exposure. Golden runs from a fresh bot cannot see this, so mid-cycle golden runs are required. The success criterion is **trading equivalence** for Bybit bots.

Traceability and deliberate deviations:
- Source: roadmap step 0 «Exchange adapter: strategies and runtimes reach the exchange through one exchange-neutral seam; Bybit moves behind it with no behaviour change» (baseline, verbatim).
- Deliberate deviation 1 (AC-08): a failed order lookup is no longer treated as proof that the order is gone. This is safer than today and becomes critical on Binance, where «unknown order» arrives as an error rather than an empty answer.
- Deliberate deviation 2 (AC-14): the connection check becomes available only to signed-in operators. Today it can be called without signing in. This is not a trading behaviour, so it does not break trading equivalence.
- Bybit keeps its current recovery policy (resync and retry on inconsistent order state). The new guarantees (lookup before resend, pause on divergence) come to Bybit only in roadmap step 9.

Scope assumptions:
- **Venues covered in this step:** Bybit emulator, Bybit demo, Bybit testnet and Bybit live, which are all the venues bots run on today. Each of them gets the Bybit exchange adapter, so no existing bot loses its adapter when this step is deployed.
- **Exchange accounts:** the adapter is chosen by the bot's venue, and the exchange account it trades on comes from the bot's configuration, as it does today. On the emulator that is the bot's own emulator account key, or the shared default emulator account when the bot has none. On demo, testnet and live it is the keys configured on the server, shared by all bots of that venue. Per-operator exchange keys are not part of this step (§3).

## 2. Goals

- Every strategy and runtime reaches an exchange only through the exchange adapter for the bot's venue, so adding an exchange means adding an adapter, not changing strategies.
- The Operator's Bybit bots keep trading equivalence at every migration step, including bots that are running when a new version is deployed.
- An unsupported venue or a missing capability fails closed: the bot is refused or paused with a stated reason and is never routed to another exchange.

## 3. Non-goals

- **A Binance exchange adapter and a Binance dialect in the emulator.** These are roadmap steps 1 and 3; this step only makes them possible.
- **Changing backtest behaviour.** Backtests run the same bot engine on emulator bots, so they reach the emulator through the Bybit emulator exchange adapter like any other emulator bot. Their results, their Bybit label and their own replay logic do not change, and no backtest path is exempt from the §6 static search.
- **Per-operator exchange keys.** Demo, testnet and live keys stay configured on the server and shared, as today (§1 Scope assumptions).
- **New guarantees for Bybit** (lookup before resend, pause on unexplained divergence instead of automatic resync). This is roadmap step 9, to be done after the guarantees are proven on Binance.
- **Faults and partial fills in the emulator.** This is roadmap step 2. Error-reaction equivalence here is proven with the reference failure table instead.
- **New UI.** The exchange choice in the bot form and per-venue connection status on screen arrive with step 1. The Operator sees no new screen in this step.
- **The strategy × market × mode compatibility matrix (decision D2).** It will be built on top of capabilities later and is not decided here.

## 4. User stories

### US-01: Keep Bybit bots trading unchanged

**As an** Operator
**I want** my existing Bybit bots of all four strategies to place the same orders and reach the same state after the refactor
**So that** I don't have to re-validate strategies I already trust

### US-02: Running bots survive each deploy

**As an** Operator
**I want** bots that are mid-cycle when a new version is deployed to continue their cycle without duplicating or needlessly cancelling live orders
**So that** I don't have to stop my bots for every release of this step

### US-03: Same reaction to exchange failures

**As an** Operator
**I want** every kind of exchange failure to trigger the same reaction (retry after back-off, resync, pause or stop) with the same severity as today
**So that** my alerts and recovery habits stay valid

### US-04: Unsupported venue fails closed

**As an** Operator
**I want** a bot whose venue has no exchange adapter to be refused at start, and a running one to be paused, with the reason stated
**So that** an order can never land on an exchange I didn't choose

### US-05: Check the connection of a venue

**As an** Operator
**I want** the exchange connection check, which now goes through the exchange adapter and takes a venue, to tell me for any venue of this step whether it is connected, not configured or failing, without exposing keys (the existing screen keeps checking only the Bybit demo venue; per-venue status on screen arrives with step 1)
**So that** I know my bots can reach the exchange before I start them

### US-06: Incompatible setting is blocked by capability

**As an** Operator
**I want** a bot whose strategy needs something its venue cannot do on the bot's market type to be refused at start, with the missing capability named
**So that** an incompatible setting never reaches the exchange

### US-07: Keys never leak through the adapter

**As an** Operator
**I want** my exchange keys and secrets to stay out of bot events, logs, notifications and the connection check answer
**So that** moving error handling into the exchange adapter does not open a new leak path

### US-08: New exchange plugs in without strategy changes

**As an** Operator
**I want** a new exchange to plug in through its own exchange adapter without changing my strategies
**So that** Binance can arrive later without re-validating strategy logic

## 5. Acceptance criteria

### AC-01 (US-01) — happy path

**Given** a golden run from a fresh bot was recorded with the pre-refactor version for each of Grid, DCA, Momentum and Pattern Scalper on the Bybit emulator venue
**When** the same price scenario is replayed after any migration step
**Then** each strategy's run shows trading equivalence with its recording: the same orders in the same order, the same bot state, and the same event categories (event type plus the recorded error type, whose stored values stay the same as today)

### AC-02 (US-01) — cross-context

**Given** an Operator's Bybit bot has closed cycles, fees and realised profit recorded before a migration step
**When** the Operator opens that bot's page after the migration step
**Then** the profit and loss, fees and number of closed cycles shown are the same as before the migration step, because they are still built from the same exchange execution history

### AC-03 (US-01) — domain invariant

**Given** an Operator runs two bots on the Bybit emulator venue, each configured with its own emulator account key
**When** both bots trade
**Then** each bot's orders and balance stay on its own emulator account only, because a bot only ever trades on the exchange account it is configured with (bots that have no key of their own share the default emulator account by design, as today)

### AC-04 (US-02) — happy path

**Given** a golden run from a mid-cycle state was recorded for each strategy, where the pre-refactor version left open entry orders, a take-profit order and (where the strategy holds one) an open position
**When** the bot resumes on the migrated version from each of two starting states: the one recorded by the pre-refactor version, and the same state freshly captured from the previous migration step
**Then** it completes the cycle with trading equivalence to the recording from both starting states

### AC-05 (US-02) — domain invariant

**Given** a Grid or DCA bot is mid-cycle with entry orders and a take-profit order placed by the previous version
**When** the migrated version is deployed and the bot's next check runs
**Then** the bot recognises those orders as its own, places no second ladder, does not cancel and re-place the take-profit, and continues the cycle, because a bot never holds two live copies of the same intended order

### AC-06 (US-03) — error

**Given** the reference failure table, recorded before the refactor, has one row for each error type the bot distinguishes today (at least one real Bybit failure per type, including rate limit, insufficient balance, rejected order, duplicate client tag, unknown order, rejected credentials and temporary network failure), plus one row for a retryable failure that exhausts the existing retry limit, each with the action and severity the bot took
**When** each listed failure occurs for a running bot after a migration step
**Then** the bot takes the same action with the same severity, and the recorded event keeps the same category (the same stored error type as today) and the raw exchange code — except for the failed-order-lookup row, whose expected reaction is recorded as the deliberate deviation of AC-08

### AC-07 (US-03) — error

**Given** a running bot is placing an order
**When** the request to the exchange times out because of a temporary network failure
**Then** the bot retries after a back-off and keeps running, and the Operator sees a temporary-network event rather than a stopped bot; if the failure persists past the existing retry limit, the bot pauses as it does today

### AC-08 (US-03) — domain invariant

**Given** a running bot has an order it believes is active
**When** the bot asks the exchange about that order and the lookup fails for any reason other than the unknown-order error class
**Then** the bot keeps the order as active and asks again later, up to the existing retry limit, after which it pauses as it does today; while the order's state is unknown, the bot places and cancels no orders tied to it. The bot treats the order as gone only when the exchange answers with the unknown-order error class, or when the order is in neither the exchange's open orders nor its order history; if the order history has it, the bot takes its final state (filled or cancelled) from there. A failed lookup is never proof that an order is gone

<!-- added-by-fix: review-2026-10-15-2 finding 1 (T41) -->
**Clarification:** "asks again later, up to the existing retry limit, after which it pauses" applies to the error classes the bot already retries in the AC-06 reference table (rate limited, network, exchange unavailable, time sync, order-state conflict). A lookup that fails with any other class takes that class's AC-06 reaction at once — rejected credentials or denied permission stop the bot, an invalid symbol pauses it, an unclassified failure stops it as internal. In every case the order stays active and nothing tied to it is placed or cancelled.

### AC-09 (US-04) — error

**Given** an Operator owns a bot whose venue has no exchange adapter
**When** the Operator starts the bot
**Then** the system refuses to start it, tells the Operator that the bot's venue (named) has no exchange adapter, and sends nothing to any exchange

### AC-10 (US-04) — cross-context

**Given** a bot is running and a deploy or configuration change has left its venue without an exchange adapter
**When** the bot's next scheduled check or the next live exchange event for that bot arrives
**Then** the bot is paused with the reason stated, the Operator is notified, and no request is sent to any exchange: the bot's existing orders are left on the exchange as they are, neither placed nor cancelled, and the Operator decides what to do with them

### AC-11 (US-04) — authorization

**Given** another platform user owns a bot
**When** the Operator tries any action on that bot that reaches the exchange through an exchange adapter (start, stop, sync, cancel orders, close the position, read its live position or orders)
**Then** the system treats the bot as non-existent, sends nothing to any exchange, and no refusal message reveals that bot's venue or state

### AC-12 (US-05) — happy path

**Given** a signed-in Operator and Bybit demo keys configured on the server
**When** the Operator runs the connection check for the Bybit demo venue
**Then** the system reports the venue as connected, as it does today

### AC-13 (US-05) — error

**Given** a signed-in Operator and no keys configured for a venue
**When** the Operator runs the connection check for that venue
**Then** the system reports that venue (named) as not configured, without showing any key value

### AC-13b (US-05) — error

**Given** a signed-in Operator and keys configured for a venue
**When** the Operator runs the connection check for that venue and the exchange request fails
**Then** the system reports that venue (named) as failing, with the error class of the failure, and the answer contains neither the raw exchange message nor any key or secret

### AC-14 (US-05) — authorization

**Given** someone who is not signed in
**When** they try to run the connection check for any venue
**Then** the system refuses, and the refusal reveals nothing about which venues are configured

### AC-15 (US-06) — domain invariant

**Given** an Operator owns a bot whose strategy needs a capability that the exchange adapter for the bot's venue does not declare for the bot's market type
**When** the Operator starts the bot
**Then** the system refuses to start it, names the missing capability and the market type, and sends nothing to any exchange, because a strategy never relies on a capability its exchange adapter did not declare. A Bybit bot configuration that starts today is never refused by this check in this step

### AC-15b (US-06) — cross-context

**Given** a bot is running and, after a deploy, the exchange adapter for its venue no longer declares a capability the bot's strategy needs on the bot's market type
**When** the bot's next scheduled check or the next live exchange event for that bot arrives
**Then** the bot is paused with the missing capability and market type named, the Operator is notified, and no request is sent to any exchange, as in AC-10

### AC-16 (US-07) — error

**Given** a request to an exchange fails while the bot's keys are in use
**When** the failure is recorded in bot events and logs, sent as a notification, or reported by the connection check
**Then** none of them contains a key or a secret of any venue, including the emulator account key (also when it appears inside a connection address); at most a masked key showing only its last 4 characters (`****abcd`) is shown, and a secret is never shown even in part. This applies to everything recorded after the deploy; records stored before it are not rewritten

### AC-17 (US-08) — happy path

**Given** a stand-in exchange adapter for a test venue that declares the same capabilities as the Bybit adapter
**When** a bot of each of the four strategies runs a golden-run scenario on that test venue
**Then** every strategy trades through the stand-in adapter with no change to the strategy itself

## 6. Non-functional requirements

| Aspect | Target | Measurement |
|---|---|---|
| Trading equivalence (golden runs) | 100% of golden-run replays equivalent after every migration PR: 8 recordings (4 strategies × {fresh, mid-cycle}), replayed as 12 (each mid-cycle recording from both starting states of AC-04) | golden-run suite, run before every merge |
| Error-reaction equivalence | 100% of reference failure table rows (one per error type distinguished today, plus the retry-limit-exhausted row, as defined in AC-06) produce the recorded action and severity (the failed-order-lookup row carries the AC-08 deviation as its expected reaction) | failure-table check, run before every merge |
| Exchange access outside adapters | 0 places in strategies or runtimes that reach an exchange other than through an exchange adapter, at the end of the step | static search over the bot engine, the backtest service and the connection check (no exemptions), run before every merge |
| Existing test suites | 100% of existing backend and emulator tests pass on every migration PR. Only mechanical test edits are allowed (signing in before a call, pointing a stub at the exchange adapter instead of the removed session factory); expected results do not change, except for the connection-check test, whose behaviour AC-14 changes on purpose | test run before every merge |
| Deploy safety | 0 duplicate orders and 0 unintended cancellations across the mid-cycle golden runs | golden-run diff |
| Reaction latency (fill → bot reaction) | p95 ≤ pre-refactor baseline + 0.5 s on the emulator (baseline measured before the first PR), measured from the moment the emulator publishes the fill event to the moment the bot sends its next order or saves its new state, whichever comes first | wall-clock timestamps recorded by the golden-run harness (not the simulated clock) |
| Adapter overhead | ≤ 5 ms added to the p95 duration of a single exchange request, compared with the pre-refactor run on the same emulator | wall-clock request timing in the golden-run harness, pre-refactor vs migrated |

## 6.1 Security / privacy

- **Data classification:** confidential. The adapter handles exchange credentials and the Operator's balances, positions and orders.
- **Personal data touched:** none new. The step adds no new fields about people.
- **AuthZ/AuthN impact:** no new permissions. Bot ownership checks stay where they are and run before any venue routing, so routing never reaches a bot the caller does not own (AC-11). The connection check now requires a signed-in operator (AC-14).
- **Abuse cases:**
  - Key leak through exchange error text: failure messages and raw exchange payloads recorded by the adapter are scrubbed of keys and secrets before they reach events, logs, notifications or the connection check answer (AC-16).
  - Misrouted orders: a bot whose venue has no adapter is refused or paused, never routed to a default exchange, so a configuration mistake cannot spend money on the wrong account (AC-09, AC-10).
  - Cross-account trading on the emulator: each bot keeps its own exchange account identity, so one bot cannot trade another emulator account's balance (AC-03).
  - Cross-user access: another platform user's bot is treated as non-existent, and refusal texts never reveal its venue or state (AC-11).
  - Anonymous connection checks that spend the exchange rate limit with the Operator's keys: refused for anyone who is not signed in (AC-14), and checks by signed-in operators stay under the platform's existing per-user rate limit.
- **Security review:** Required. The handling of exchange credentials and error payloads moves into a new component, and the feature is size M.

## 7. Metrics / KPIs

- **Direct exchange access points outside adapters** — baseline: about 22 session call sites plus the stream, error-handling and connection-check code (exact count taken before the first PR), target: 0 by the end of the step.
- **Golden runs passing** — baseline: 0 recorded, target: 8 of 8 recorded before the first migration PR, and 12 of 12 replays (AC-04 runs each mid-cycle recording from two starting states) passing on every PR until the step closes.
- **Refactor-caused incidents on Bybit bots** (duplicate orders, unexpected stops, error reactions changed other than the AC-08 deviation, unexplained jumps in profit and loss) — baseline: 0, target: 0 within 14 days after the last PR of this step.
- **Strategy files changed when the Binance adapter is added** — baseline: n/a (measured when roadmap step 1 merges), target: 0. This is a leading indicator of the adapter's quality.

## 8. Open questions

- [x] Should the Momentum and Pattern Scalper risk guards keep silently relaxing when the balance or the daily result cannot be read (the daily loss stop then sees zero, and sizing falls back to the maximum), or should they fail closed? Default now: preserve the current behaviour (trading equivalence) and record it as a known gap. — owner: Oleh Solomko, due: before `/sdd:design exchange-adapter`. **Resolved (design): default accepted** — kept unchanged as accepted High debt, to be resolved before Binance bots run live; see [sad.md §11](./sad.md#11-risks-and-technical-debt)
- [ ] Momentum and Pattern Scalper client order tags can be longer than 36 characters, which is Binance's limit. Reshape them now (a visible change on Bybit) or in step 1? Default now: leave them unchanged; the adapter declares its maximum tag length as a capability, and step 1 decides. — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [x] Which price scenarios do the golden runs use, and what is the mid-cycle starting state for Momentum and Pattern Scalper (AC-04 describes it for Grid and DCA)? Default now: one trend-and-range scenario per strategy, plus one scenario whose prices sit exactly on rounding-step boundaries; for Momentum and Pattern Scalper, mid-cycle means an open position with its take-profit and stop orders placed. — owner: Oleh Solomko, due: before `/sdd:plan-tests exchange-adapter`. **Resolved 2026-10-13 (plan-tests): default accepted** — see [test-plan.md](./test-plan.md)
- [x] Should the §6 checks run in a CI pipeline (none exists today) or as a local pre-merge routine? Default now: a local pre-merge routine. — owner: Oleh Solomko, due: before `/sdd:tasks exchange-adapter`. **Resolved (plan-tests): default accepted** — `scripts/premerge.sh` (T03); see [test-plan.md «CI placement»](./test-plan.md#ci-placement)
- [x] Which capabilities does each strategy need, and which does the Bybit exchange adapter declare for spot and for USDT-M futures (AC-15, AC-15b)? Default now: derive both lists from what each strategy uses today, so that no configuration that starts today is refused. — owner: Oleh Solomko, due: before `/sdd:design exchange-adapter`. **Resolved (design): default accepted** — see the capability catalogue in [sad.md §5](./sad.md#5-building-block-view) and [ADR-0006](./adr/0006-declare-capabilities-statically-per-market-type.md)
- [ ] Bybit field names (`cumExecQty`, `avgPrice`, `cumExecValue`, `cumExecFee`, `execFee`, `execPrice`, `reduceOnly`, `orderLinkId`, `orderType`) are still read from and written to `trading_bot_orders.raw_response` outside the adapter (`performance.py`, `grid_runtime.py`, `momentum.py`, `pattern_scalper.py`); the `is_reduce_only` accessor reads only the Bybit key. Move the readers to neutral keys with a legacy-key fallback (data-model.md, ADR-0001)? Deferred from review 2026-10-14 (finding 7). — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [ ] The port is still partly untyped: `cancel_order` returns the raw response dict, `streams()` returns `Any` (callers use `BybitStreams` methods and the emulator admin plane through it), and `Position.margin_mode` carries Bybit's `tradeMode` unchanged. Add a typed `ExchangeStreams` protocol, a typed cancel result and a neutral margin mode (ADR-0001)? Deferred from review 2026-10-14 (finding 8). — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [ ] The rounding-boundary golden runs that test-plan.md and T02 call for were never recorded (only the trend-and-range scenarios exist), so AC-01 is proven on one price scenario per strategy. Record one rounding-boundary scenario per strategy on the pre-refactor commit (ADR-0007) and replay it. Deferred from review 2026-10-14 (finding 10). — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [ ] The §6 reaction-latency NFR is **not yet validated**: Momentum and Pattern Scalper record 0 fill→reaction samples (baseline p95 = 0, so the check cannot fail), and Grid/DCA run once per side instead of the 3 + 3 runs with ≥100 fills and ≥1000 requests that test-plan.md requires. Deferred from review 2026-10-14 (finding 11). — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [ ] A Momentum or Pattern Scalper bot that the guard refuses shows all-zero performance (`closed_cycles` 0, `net_realized_pnl` "0"): the page stays readable (AC-02, AC-10), but these strategies keep no stored figures, so the zeros look like "never traded" (`performance.py:111-118`). Add a "figures unavailable" marker to `getBotPerformance`, or compute the figures from stored order rows? Deferred from review 2026-10-15 (finding 8). — owner: Oleh Solomko, due: before the roadmap step 1 spec
- [ ] Two contract outcomes have no acceptance criterion: the connection check's `not_supported` status (US-05), and a guard 422 on the Operator's own bot for actions other than start — stop, sync, cancel orders, close the position, read live state (US-04). Both are implemented and contract-tested (`test_venue_connection.py`, `test_bots_guard_api.py`). Add AC-13c and AC-11b, or accept the contract as their only source? Carried forward from api-sync F-5 by review 2026-10-15-2 (finding 2). — owner: Oleh Solomko, due: before the roadmap step 1 spec
