# Tracker — exchange-adapter

> Status of every task in the epic. `implement` updates `done` as it commits each task.
> States: `todo` · `in_progress` · `blocked` · `review` · `done`.
> Estimate: S ≈ ½ day · M ≈ ¾ day · L ≈ 1 day (every task ≤ 1 day).

| # | Task | Layer | Owner | Estimate | Blocked by | Status |
|---|---|---|---|---|---|---|
| T01 | Build the golden-run harness around the emulator client | tests | Oleh Solomko | M | — | done |
| T02 | Record the 8 golden runs and the latency and overhead baselines on the pre-refactor code | tests | Oleh Solomko | M | T01 | done |
| T03 | Record the reference failure table and add the static search and the one-command pre-merge gate | tests | Oleh Solomko | M | T01 | done |
| T04 | Add the exchange seam core: port, error vocabulary, capabilities and the shared scrubber | domain | Oleh Solomko | M | — | done |
| T05 | Move Bybit error classification into the adapter package and keep reactions in an engine class table | infra | Oleh Solomko | M | T02, T03, T04 | done |
| T06 | Build the Bybit adapter over the existing pybit and EmulatorHTTP clients | infra | Oleh Solomko | L | T04, T05 | done |
| T07 | Add the venue registry, the fail-closed guard and each strategy's required capabilities | domain | Oleh Solomko | M | T06 | done |
| T08 | Run the guard at start and every Operator bot action, after the ownership check | ports | Oleh Solomko | M | T07 | done |
| T09 | Run the guard before every worker tick and pause the bot on a refusal, without retrying | app | Oleh Solomko | M | T05, T07 | done |
| T10 | Move the order, market-data and position helpers onto the port and add the AC-08 order-lookup rule | app | Oleh Solomko | M | T06, T07 | done |
| T11 | Migrate the DCA strategy and runtime onto the exchange adapter | app | Oleh Solomko | M | T08, T09, T10 | done |
| T12 | Migrate the Grid strategy and runtime onto the exchange adapter | app | Oleh Solomko | L | T11 | done |
| T13 | Migrate the Momentum strategy onto the exchange adapter | app | Oleh Solomko | L | T12 | done |
| T14 | Migrate the Pattern Scalper strategy onto the exchange adapter | app | Oleh Solomko | L | T13 | done |
| T15 | Build bot performance from neutral executions through the adapter | app | Oleh Solomko | M | T10 | done |
| T16 | Move live exchange streams behind the adapter and make the stream supervisor and UI live stream exchange-neutral | infra | Oleh Solomko | L | T07, T09 | done |
| T17 | Scrub keys and secrets at every sink and in the root logging filter | wiring | Oleh Solomko | M | T04, T08, T09 | done |
| T18 | Replace the Bybit-demo test route with an authenticated per-venue connection check | ports | Oleh Solomko | M | T04, T07 | done |
| T19 | Prove every strategy runs unchanged through a stand-in adapter on a test venue | tests | Oleh Solomko | M | T14 | done |
| T20 | Route backtest instrument reads through the emulator adapter, delete the Bybit session factory and drive the static search to 0 | wiring | Oleh Solomko | M | T14, T15, T16, T17, T18 | done |
| T21 | Keep the worker alive when the guard call fails with a non-guard error | app | Oleh Solomko | S | T20 | done |
| T22 | Register only real emulator account keys with the shared scrubber | infra | Oleh Solomko | S | T20 | done |
| T23 | Keep the bot page readable for guard-refused Momentum and Pattern Scalper bots | app | Oleh Solomko | S | T20 | done |
| T24 | Restore the Pattern Scalper linear-only start check | domain | Oleh Solomko | S | T20 | done |
| T25 | Scrub the remaining exception sinks | wiring | Oleh Solomko | S | T22 | done |
| T26 | React only to adapter error classes in the engine and replay the failure table through the adapter on a tick | app | Oleh Solomko | S | T25 | done |
| T27 | Declare every capability each strategy actually uses | domain | Oleh Solomko | S | T20 | done |
| T28 | Validate the guard and connection-check responses against the contract file | tests | Oleh Solomko | S | T20 | done |
| T29 | Prove the connection check against the emulator for rejected keys and a closed port | tests | Oleh Solomko | S | T28 | done |
| T30 | Make the AC-10 scheduled-pause tests order-independent | tests | Oleh Solomko | S | T21 | done |
| T31 | Keep position response bodies and stored market-order prices unchanged | app | Oleh Solomko | S | T20 | done |
| T32 | Skip a malformed stream message instead of dropping the connection | infra | Oleh Solomko | S | T20 | done |
| T33 | Rebuild performance figures from exact decimal execution values | app | Oleh Solomko | S | T20 | todo |
| T34 | Remove leftover dead code and give the pre-merge gate its own test database | wiring | Oleh Solomko | S | T26 | todo |
**Total:** 34 tasks — T01–T20 (~16 person-days) plus T21–T34, review follow-ups from `_review/review-2026-10-14.md` (14 × S ≈ ½ d).
