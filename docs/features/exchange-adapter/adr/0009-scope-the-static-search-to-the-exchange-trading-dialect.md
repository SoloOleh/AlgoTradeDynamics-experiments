---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-12"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0009 — Scope the static search to the exchange trading dialect, not the emulator admin plane

- **Status:** Accepted
- **Date:** 2026-10-12
- **Deciders:** Oleh Solomko (Architect), design Socratic walk

## Context

Spec §6 requires «0 places in strategies or runtimes that reach an exchange other than through an exchange adapter», checked by a static search over the bot engine, the backtest service and the connection check, with no exemptions. The backtest service uses `BacktestEmulatorClient` (`BE/app/services/backtest_emulator.py`) for two kinds of call. One is the exchange's trading/market dialect: `instrument_info` → `/v5/market/instruments-info`, used at `backtest_service.py:208`. The other is the emulator's admin plane: datasets, candles, accounts, `set_price`, dashboard, orders and executions under `/api/admin/…`. Real exchanges have no admin plane. The search needs a precise definition of «reaching an exchange» (SAD §10 QG-3).

## Decision drivers

- Spec §6 «Exchange access outside adapters»: 0, no exemptions, including backtests (spec §3).
- Spec §3 non-goal: backtest behaviour, results and replay logic do not change.
- SAD §1 QG-3 and ADR-0001: the port speaks platform terms that a real exchange can serve.

## Considered options

1. **The trading dialect is in scope and the admin plane is not.** The search flags, outside `bot_engine/exchange/`: `pybit` / `EmulatorHTTP` imports, `get_bybit_session` / `get_bybit_credentials`, `/v5/` exchange paths and exchange WS endpoints, and Bybit field names. The emulator admin plane (`/api/admin/…`) is simulator control, not exchange access.
2. **All emulator traffic goes through the adapter.** Admin calls count as exchange access too, so the port gains simulator-control methods.

## Decision outcome

**Chosen:** Option 1. «Exchange» means the trading and market-data surface a real venue offers. Simulator control (datasets, accounts, prices) has no counterpart on Bybit or Binance and would pollute the port with emulator-only concepts (against ADR-0001). The backtest's one real exchange call, `instrument_info`, moves to the Bybit emulator adapter's `instrument_rules`, so the backtest reaches the exchange like any other emulator bot, as spec §3 states.

## Consequences

**Positive**
- The search is a mechanical check (imports, path prefixes, field-name tokens) that runs in seconds pre-merge.
- The port stays implementable by real exchanges only. The emulator's control plane stays a test and backtest concern.

**Negative**
- `BacktestEmulatorClient` keeps one HTTP client that talks to the emulator for admin calls. A future edit could slip a `/v5/` call back in, which the search catches by path prefix.
- `backtest_service.py` must change its instrument-info read. Results must stay identical (spec §3), and the existing backtest tests guard this.

**Neutral**
- If an admin-plane call is ever needed on a real venue (for example account provisioning), it becomes a port method in its own decision.

## Links

- Spec: [[../spec.md]] §3 «Changing backtest behaviour», §6 «Exchange access outside adapters», §7 KPI
- SAD: [[../sad.md]] §10 QG-3, §11
- Related ADR: [[0001-define-a-typed-exchange-neutral-adapter-port]]
