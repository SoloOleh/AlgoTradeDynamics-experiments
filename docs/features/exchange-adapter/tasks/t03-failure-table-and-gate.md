---
id: T03
title: "Record the reference failure table and add the static search and the one-command pre-merge gate"
layer: "tests"
deps: ["T01"]
blocks: ["T05"]
acs: ["AC-06", "AC-07"]
files_hint: ["BE/tests/golden/failure_table/", "BE/tests/golden/test_failure_table.py", "BE/tests/golden/static_search.py", "BE/tests/golden/test_static_search.py", "scripts/premerge.sh"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T03 — Record the reference failure table and add the static search and the one-command pre-merge gate

## Place in the sequence

- **Blocked by:** T01 — Build the golden-run harness · **Blocks:** T05 — Bybit error classification + engine class table · **Wave:** 2. The table must be recorded against today's `classify_bot_error` before T05 moves it.
- **Lane:** own lane (`BE/tests/golden/failure_table/`, `static_search.py`, `scripts/premerge.sh`).

## Why (user story)

> **As an** Operator
> **I want** every kind of exchange failure to trigger the same reaction (retry after back-off, resync, pause or stop) with the same severity as today
> **So that** my alerts and recovery habits stay valid
>
> — `spec.md §4, US-03, verbatim` · full text: [spec.md](../spec.md)

This task freezes today's failure → reaction map and puts every spec §6 check behind one command.

## Inlined context

> | Error-reaction equivalence | 100% of reference failure table rows (one per error type distinguished today, plus the retry-limit-exhausted row, as defined in AC-06) produce the recorded action and severity (the failed-order-lookup row carries the AC-08 deviation as its expected reaction) | failure-table check, run before every merge |
> | Exchange access outside adapters | 0 places in strategies or runtimes that reach an exchange other than through an exchange adapter, at the end of the step | static search over the bot engine, the backtest service and the connection check (no exemptions), run before every merge |
> | Existing test suites | 100% of existing backend and emulator tests pass on every migration PR. […] | test run before every merge |
>
> — `spec.md §6, NFR rows, abridged` · full text: [spec.md](../spec.md)

> The static search (scope fixed by ADR-0009) runs over `BE/app/bot_engine/`, every backtest service (`BE/app/services/backtest_*.py`, `fast_*_backtest.py`), the connection check and the UI live stream (`BE/app/api/websockets.py`), with no exemptions. Outside `BE/app/bot_engine/exchange/` it flags any import of `pybit` or `EmulatorHTTP`, `get_bybit_session` / `get_bybit_credentials`, and any request to a `/v5/` exchange path or exchange WS endpoint. In the bot engine, the connection check and the UI stream it also flags Bybit field names (`lotSizeFilter`, `priceFilter`, `retCode`, `closedPnl`, `tradeMode`). Backtest code that parses the emulator **admin-plane** payloads (Bybit-shaped by the emulator's design) is exempt from the field-name rule only.
>
> — `sad.md §10, QG-3 How verify, verbatim` · full text: [sad.md](../sad.md)

> There is no CI, so the pre-merge gate (golden runs, failure table, static search, existing suites) can be skipped and a non-equivalent PR can reach the bots | Medium | One command runs the whole gate. Each PR description records its result. Spec §8 OQ «CI vs local routine» stays with its owner, due before `/sdd:tasks`
>
> — `sad.md §11, risk row «no CI», verbatim` · full text: [sad.md](../sad.md)

> Failure-table rows are built from real recorded Bybit error payloads, driven through the Bybit adapter's classifier and the engine's class table. Faults and partial fills arrive in roadmap step 2
>
> — `sad.md §11, risk row «emulator has no fault injection», mitigation, verbatim` · full text: [sad.md](../sad.md)

> Today's stored types (`BotErrorType`): `network`, `rate_limit`, `exchange_unavailable`, `time_sync`, `authentication`, `permission`, `insufficient_funds`, `invalid_order`, `invalid_symbol`, `order_state`, `configuration`, `risk`, `internal`. Actions: `retry_backoff`, `resync_and_retry`, `pause`, `stop`. Retry budget `error_max_retries` default 5; on exhaustion the action becomes `pause` with severity `error`.
>
> — `BE/app/bot_engine/error_handling.py:25-51, 207-221, abridged` (code, at commit `054a7a1`)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([spec.md](../spec.md) · [sad.md](../sad.md) · [ADR-0009](../adr/0009-scope-the-static-search-to-the-exchange-trading-dialect.md)) and follow it. Do not guess.

## Data delta

No DB changes.

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-06 — error

> **Given** the reference failure table, recorded before the refactor, has one row for each error type the bot distinguishes today (at least one real Bybit failure per type, including rate limit, insufficient balance, rejected order, duplicate client tag, unknown order, rejected credentials and temporary network failure), plus one row for a retryable failure that exhausts the existing retry limit, each with the action and severity the bot took
> **When** each listed failure occurs for a running bot after a migration step
> **Then** the bot takes the same action with the same severity, and the recorded event keeps the same category (the same stored error type as today) and the raw exchange code — except for the failed-order-lookup row, whose expected reaction is recorded as the deliberate deviation of AC-08
>
> — `spec.md §5, AC-06, verbatim` · full text: [spec.md](../spec.md)

### AC-07 — error

> **Given** a running bot is placing an order
> **When** the request to the exchange times out because of a temporary network failure
> **Then** the bot retries after a back-off and keeps running, and the Operator sees a temporary-network event rather than a stopped bot; if the failure persists past the existing retry limit, the bot pauses as it does today
>
> — `spec.md §5, AC-07, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Collect one real Bybit failure payload (code, HTTP status, message, exception type) per stored error type distinguished today, incl. rate limit, insufficient balance, rejected order, duplicate client tag, unknown order, rejected credentials, timeout — `BE/tests/golden/failure_table/rows.json`
- [ ] For each row record today's `classify_bot_error` + `handle_bot_runtime_error` result: stored type, severity, action, raw code; add the retry-limit-exhausted row (→ `pause`, `error`) and the failed-order-lookup row with the AC-08 expected reaction marked `deviation: AC-08`
- [ ] Failure-table check: drives each row through the current classification entry point and asserts action, severity, stored type, raw code — `BE/tests/golden/test_failure_table.py` (T05 repoints it at adapter classifier → class table)
- [ ] Static search over the ADR-0009 scope; prints each hit with file:line; asserts count ≤ a recorded baseline now, target 0 (T20 flips the assertion to `== 0`) — `BE/tests/golden/static_search.py`, `test_static_search.py`
- [ ] One command: golden replays + failure table + static search + `pytest` in `BE/` and `EXCHANGE_EMULATOR/`; non-zero exit on any failure — `scripts/premerge.sh` (local routine — spec §8 default; CI is out of scope)

## Edge cases

| Case | Behaviour |
|---|---|
| A stored type has no real Bybit payload available | Row recorded from the emulator/pybit exception shape with `source: synthetic` and flagged; never silently dropped |
| Message-text matching order matters (two patterns match one text) | The row pins today's winning classification; T05 must reproduce it |
| Static search hits admin-plane parsing in backtests | Exempt from the field-name rule only (ADR-0009); `/v5/` paths still flagged |
| Gate run with the emulator suite failing | Script exits non-zero and names the failing stage |

## Definition of Done

- [ ] `pytest BE/tests/golden/test_failure_table.py` passes on unchanged code, one row per stored type + exhausted-retry row + lookup row
- [ ] `scripts/premerge.sh` runs all four checks and exits 0 on the current code; static-search baseline count recorded
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
