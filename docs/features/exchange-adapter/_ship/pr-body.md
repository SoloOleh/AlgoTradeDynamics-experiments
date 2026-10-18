## Summary

Roadmap step 0. Every strategy, runtime, stream, backtest path and the connection check now reaches the exchange only through an exchange-neutral **exchange adapter**, chosen by the bot's venue. Bybit moves behind the adapter with no change in trading behaviour, as proven by golden runs and the reference failure table. An unsupported venue or a missing capability fails closed. This unblocks the Binance chain (steps 1–8). Spec: [`docs/features/exchange-adapter/spec.md`](docs/features/exchange-adapter/spec.md). Changelog: [`docs/features/exchange-adapter/_ship/changelog.md`](docs/features/exchange-adapter/_ship/changelog.md).

## Acceptance criteria

- AC-01 — fresh golden runs: trading equivalence for Grid, DCA, Momentum, Pattern Scalper ✓
- AC-02 — the bot page's P&L, fees and closed cycles are unchanged across the migration ✓
- AC-03 — each emulator bot trades only on its own emulator account ✓
- AC-04 — mid-cycle golden runs are equivalent from the pre-refactor and the previous-step states ✓
- AC-05 — no second ladder and no take-profit re-placement after a deploy ✓
- AC-06 — every row of the reference failure table: same action, severity and stored error type ✓
- AC-07 — a network timeout is retried after back-off and pauses once the retry limit is exhausted ✓
- AC-08 — a failed lookup is never proof that an order is gone (deliberate deviation) ✓
- AC-09 — start is refused for a venue with no adapter, and nothing is sent ✓
- AC-10 — a running bot whose venue loses its adapter is paused, and its orders are left alone ✓
- AC-11 — another user's bot is treated as non-existent, and nothing is sent ✓
- AC-12 — demo with keys → connected ✓ (integration test against the emulator; the real-demo run is pending, see Verification)
- AC-13 / AC-13b — not configured / failing with the error class, with no keys and no raw message ✓
- AC-14 — the connection check refuses anonymous callers ✓
- AC-15 / AC-15b — a missing capability: refused at start / paused while running ✓
- AC-16 — keys and secrets scrubbed at every sink (a key is shown at most as `****abcd`) ✓
- AC-17 — a stand-in adapter runs all four strategies unchanged ✓

## Design

- Spec: `docs/features/exchange-adapter/spec.md`
- Architecture: `docs/features/exchange-adapter/sad.md`
- Decisions: `docs/features/exchange-adapter/adr/` (ADR-0001 … ADR-0010)
- Data model: `docs/features/exchange-adapter/data-model.md` (`schema_change: false`, no migration)
- API: `docs/features/exchange-adapter/contracts/openapi.yaml` (+ `events.md`, `api-sync-report.md`)
- Test plan: `docs/features/exchange-adapter/test-plan.md`
- Reviews: `docs/features/exchange-adapter/_review/`. Ninth re-review `review-2026-10-18-4.md`: **PASS**.

## Tasks (SDD-Task trailers)

73 tasks (T01–T73), one commit each with `SDD-Task` / `SDD-AC` trailers. Tracker: `docs/features/exchange-adapter/tasks/tracker.md`. List: `git log main..HEAD --grep SDD-Task`. Milestones:

- T01–T03 `4b91cb7` `4aed1a7` `e40cd8f`: golden-run harness, the 8 recordings, the failure table, the static search, `scripts/premerge.sh`
- T04–T09 `9dc1725` … `8caf682`: port, errors, capabilities, scrubber, Bybit adapter, venue registry, fail-closed guard at start and on every worker tick
- T10–T14 `816da13` … `92b2bab`: helpers (with the AC-08 lookup rule), DCA, Grid, Momentum and Pattern Scalper migrated
- T15–T20 `862c931` … `fe5e188`: performance, streams, scrubbing at every sink, per-venue connection check, stand-in adapter, backtests on the adapter, legacy session factory deleted, static search at 0
- T21–T73 `71464c5` … `1dcba06`: review fix rounds (9 reviews)

## Verification

- **Pre-merge gate** (`scripts/premerge.sh`, 2026-10-18, exit 0): golden runs 31 passed / 2 skipped · failure table 46 passed / 6 skipped · static search 2 passed · backend suite 404 passed · emulator suite 7 passed.
- Lint: `ruff --select F,E9 BE/app` has 1 finding, an unused `timedelta` import at `BE/app/services/backtest_service.py:8`. It is already on `main`, so this branch did not introduce it.
- **Ran the feature** (local backend on SQLite and the emulator via uvicorn; Docker not available on this machine):
  - AC-14: anonymous `GET /api/venues/bybit/{demo,testnet}/connection` → `401`.
  - AC-13: signed in, no demo keys → `200 status=not_configured`, "Venue bybit/demo has no exchange keys configured".
  - AC-13b / AC-16: fake testnet keys → `status=failing, error_class=exchange_unavailable`. The answer has no raw message, and neither the key nor the secret appears in the backend log (grep count 0).
  - Emulator venue → `connected`. `binance/live` → `not_supported`.
  - AC-01 (smoke): started an emulator Grid bot → `running / waiting_for_entry`, 2 entry orders placed on the emulator with `bot-1-g1-entry-*` tags. One filled, and a position take-profit followed.
  - AC-11: a second user's `start / stop / sync / cancel-orders / close-position / position / orders` on that bot all → `404 Trading bot not found`, the same answer as for a non-existent bot.
  - AC-09: a bot re-pointed in the DB to `binance/emulator` → start `422 exchange.venue_unsupported` naming the venue, with 0 requests to the emulator.
  - AC-10: the **running** bot re-pointed to `binance` → paused on the next worker tick (~16 s) with the event "Bot paused: venue binance/emulator has no exchange adapter". No write requests reached the emulator, and its 3 orders were left as they were.
- **Ship preconditions** (tracker, review 2026-10-15 finding 5). Both are still open; neither could run on the machine that prepared this PR:
  - [ ] (1) A real connection check against **Bybit demo** with demo keys (AC-12, test-plan row «connection check reports a configured demo venue as connected»). No demo keys are configured here.
  - [ ] (2) A manual mid-cycle smoke run on **`docker compose` with PostgreSQL** (test-plan «Test data», «Known gap», T20). Docker is not available here. The SQLite smoke run above is the stand-in.

## Operational notes

- Migration: none (`schema_change: false`).
- **Breaking:** `GET /api/bybit/test` is removed. It is replaced by `GET /api/venues/{exchange}/{environment}/connection`, which needs sign-in. The frontend never called the old endpoint.
- **Deploy as one unit onto the pre-refactor version.** That is the previous state the AC-04 `pre_refactor` replays cover. Splitting this branch into several deploys requires recapturing the previous-step snapshots first.
- Feature flag / config: none new.
- Rollback: redeploy the previous version (no schema to revert). Bots paused by the guard stay paused until they are restarted.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
