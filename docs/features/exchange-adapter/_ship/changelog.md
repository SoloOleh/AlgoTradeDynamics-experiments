# Changelog — exchange-adapter

## exchange-adapter — Bybit moves behind one exchange-neutral adapter, with no change in trading behaviour

**What:** every strategy (Grid, DCA, Momentum, Pattern Scalper), every runtime, the live exchange streams, performance figures, backtests and the connection check now reach the exchange only through an **exchange adapter** chosen by the bot's **venue** (exchange + environment). The adapter speaks platform terms, reports failures as **error classes** and declares its **capabilities** per market type. Bybit (emulator, demo, testnet, live) is the only adapter today. Operators see the same orders, state, events and P&L as before. Three things are new:

- A bot whose venue has no adapter, or whose strategy needs a capability the adapter does not declare for its market type, is **refused at start** (HTTP 422, venue or capability named). If it is already running, it is **paused** on its next check. Nothing is sent to any exchange, and its existing orders are left alone.
- The connection check is now **per venue** and **requires sign-in**. It answers `connected` / `not_configured` / `failing` (with the error class) / `not_supported`, and never includes a key, a secret or the raw exchange message.
- Keys and secrets are **scrubbed** from bot events, logs, notifications and the connection check answer. A key is shown at most masked to its last 4 characters (`****abcd`).

**Why:** the Binance work (roadmap steps 1–8) needs one seam to plug into. Without it, Binance would have to be translated into Bybit shapes or every strategy would have to be forked, and either path puts trusted Bybit bots at risk ([spec](../spec.md) §1–§2). Key decisions:
- [ADR-0001](../adr/0001-define-a-typed-exchange-neutral-adapter-port.md): a typed, exchange-neutral port.
- [ADR-0002](../adr/0002-implement-the-bybit-adapter-over-the-existing-clients.md): the Bybit adapter wraps the existing clients.
- [ADR-0003](../adr/0003-keep-stored-order-and-error-vocabulary-as-platform-vocabulary.md): stored order and error values stay unchanged, so no data migration is needed.
- [ADR-0004](../adr/0004-resolve-adapters-by-venue-through-a-fail-closed-guard.md): adapters are resolved by venue through a fail-closed guard.
- [ADR-0005](../adr/0005-classify-errors-in-the-adapter-and-react-in-the-engine.md): the adapter classifies errors and the engine reacts.
- [ADR-0006](../adr/0006-declare-capabilities-statically-per-market-type.md): capabilities are declared statically per market type.
- [ADR-0008](../adr/0008-scrub-secrets-at-the-adapter-and-at-every-sink.md): secrets are scrubbed at the adapter and at every sink.

Equivalence is proven by golden runs recorded on the pre-refactor code (fresh and mid-cycle, [ADR-0007](../adr/0007-record-golden-runs-at-the-emulator-client-in-a-pytest-harness.md)) and by the reference failure table.

**How to use:** nothing changes for existing bots. Connection check, per [openapi.yaml](../contracts/openapi.yaml):

```
GET /api/venues/bybit/demo/connection        (Authorization: Bearer <token>)
→ 200 {"venue":{"exchange":"bybit","environment":"demo"},"status":"not_configured","error_class":null,
       "message":"Venue bybit/demo has no exchange keys configured"}
```

Guard refusal on start: `422 {"detail":"Venue binance/emulator has no exchange adapter","code":"exchange.venue_unsupported",…}`.

**Operational notes:**
- Migration: none. No schema or data change ([data-model.md](../data-model.md) «Why no migration»).
- **Breaking API change:** `GET /api/bybit/test` is removed. Use `GET /api/venues/{exchange}/{environment}/connection`, which needs a signed-in user. The frontend never called the old endpoint.
- Deploy: ship the whole branch as **one deploy onto the pre-refactor version**. That is the previous state AC-04's `pre_refactor` mid-cycle replays cover. Splitting it into several deploys first requires recapturing the previous-step snapshots ([test-plan.md](../test-plan.md) «Test data»).
- Config: no new settings. Demo, testnet and live keys stay in `BYBIT_<ENV>_API_KEY` / `BYBIT_<ENV>_API_SECRET`.
- Pre-merge gate: `scripts/premerge.sh` runs the golden runs, the failure table, the static search and both test suites. There is no CI ([ADR-0009](../adr/0009-scope-the-static-search-to-the-exchange-trading-dialect.md), [ADR-0010](../adr/0010-extend-the-fastapi-error-body-and-keep-the-unversioned-api-prefix.md)).
- Rollback: redeploy the previous version. There is no schema change to revert. Bots paused by the guard stay paused until the Operator restarts them.

**Acceptance criteria delivered:** AC-01–AC-17 (incl. AC-13b, AC-15b): trading equivalence for all four strategies, fresh and mid-cycle; no second ladder after a deploy; the same reaction and severity to every exchange failure; a failed order lookup no longer counts as proof that the order is gone (deliberate deviation, AC-08); fail-closed handling of an unsupported venue or a missing capability; owner-only access; the authenticated per-venue connection check; secrets scrubbed; a stand-in adapter runs every strategy unchanged.

**Known follow-ups** (spec §8, owner Oleh Solomko, due before the step 1 spec): rounding-boundary golden runs not recorded; the reaction-latency NFR not yet validated; Bybit field names still read from stored `raw_response`; parts of the port still untyped; zero performance figures shown for guard-refused Momentum/Pattern Scalper bots; the 36-character client tag limit; no AC yet for the `not_supported` status or for guard 422s on non-start actions.
