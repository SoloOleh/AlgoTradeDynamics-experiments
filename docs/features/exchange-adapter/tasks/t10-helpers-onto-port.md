---
id: T10
title: "Move the order, market-data and position helpers onto the port and add the AC-08 order-lookup rule"
layer: "app"
deps: ["T06", "T07"]
blocks: ["T11", "T15"]
acs: ["AC-08"]
files_hint: ["BE/app/bot_engine/orders.py", "BE/app/bot_engine/market_data.py", "BE/app/bot_engine/positions.py", "BE/tests/test_order_lookup_rule.py", "BE/tests/golden/failure_table/rows.json"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "done"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T10 — Move the order, market-data and position helpers onto the port and add the AC-08 order-lookup rule

## Place in the sequence

- **Blocked by:** T06 — Bybit adapter, T07 — guard · **Blocks:** T11 — Migrate DCA, T15 — Migrate performance · **Wave:** 6 (SAD migration step 2).
- **Lane:** own lane (`orders.py`, `market_data.py`, `positions.py`); touches the failure-table rows file from T03.

## Why (user story)

> **As an** Operator
> **I want** every kind of exchange failure to trigger the same reaction (retry after back-off, resync, pause or stop) with the same severity as today
> **So that** my alerts and recovery habits stay valid
>
> — `spec.md §4, US-03, verbatim` · full text: [spec.md](../spec.md)

This task gives runtimes neutral helpers and the one deliberate deviation: a failed lookup never marks an order gone.

## Inlined context

> | Order lookup (AC-08) | The adapter's `lookup_order(tag)` answers `open`, `final(filled / cancelled / …)` or `not_found`, searching the exchange's open orders and order history **by client tag** (not today's «last 50 history items», `orders.py:39-50`). If a lookup fails with any class other than `unknown_order`, the order stays active, nothing tied to it is placed or cancelled, and the bot asks again up to the existing `error_max_retries` (default 5), then pauses as today. An order is gone only on `unknown_order` or `not_found`, and a `final` answer gives its final state. This is the deliberate AC-08 deviation, introduced in one migration step and recorded as such in the reference failure table |
> | Numbers | […] Rounding stays `round_to_step` (Decimal internally) over neutral `InstrumentRules` (`qty_step`, `tick_size`, `min_order_qty`, `min_notional_value`). No move to Decimal in this step |
>
> — `sad.md §8, rows «Order lookup», «Numbers», abridged` · full text: [sad.md](../sad.md)

> alt order is in open orders → open → order stays active
> else order is in order history → final, with its state (filled or cancelled) → order takes its final state from history
> else unknown-order error class, or the order is in neither → not found → order marked gone
> else lookup fails with any other error class → keep the order active, place and cancel nothing tied to it → lookup failed, ask again later — up to the existing retry limit (default 5) → limit exhausted with no answer → bot paused as today
>
> — `sad.md §6, Flow 5, abridged` · full text: [sad.md](../sad.md)

> The order lookup moves from «last 50 history items» (`orders.py:39-50`) to «by client tag», which changes the read requests and could mask an unintended change in what the bot then does | Medium | Introduced in a single migration step together with the AC-08 deviation. Golden runs compare only order-mutating requests, state and event categories (ADR-0007), so the read change itself is not a diff. The failed-lookup row of the reference failure table carries the AC-08 reaction
>
> — `sad.md §11, risk row «order lookup moves», verbatim` · full text: [sad.md](../sad.md)

> The AC-08 deviation lives in the engine's order-lookup rule, not in the class table. Bybit's resync-and-retry policy for inconsistent order state is unchanged (spec §1).
>
> — `adr/0005, Consequences/Neutral, verbatim` · full text: [ADR-0005](../adr/0005-classify-errors-in-the-adapter-and-react-in-the-engine.md)

> **Hard rule:** Bybit keeps its current recovery policy (resync and retry on inconsistent order state). The new guarantees (lookup before resend, pause on divergence) come to Bybit only in roadmap step 9.
>
> — `spec.md §1, Traceability, verbatim` · full text: [spec.md](../spec.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([sad.md](../sad.md) · [spec.md](../spec.md)) and follow it. Do not guess.

## Data delta

| Column | Type | Constraints | Change |
|---|---|---|---|
| `trading_bot_orders.status` | VARCHAR(30) | NOT NULL, default `New` | write — frozen vocabulary; changes **only** on `unknown_order`, `not_found` or `final` |
| `trading_bot_orders.order_link_id` | VARCHAR(160) | NULL, indexed | read — lookup key (bot + client tag) |
| `trading_bots.error_retry_count`, `next_retry_at` | INTEGER, TIMESTAMPTZ | NOT NULL / NULL | write — lookup retry shares `error_max_retries` |

— `data-model.md §Entities, trading_bot_orders + trading_bots, abridged` · full text: [data-model.md](../data-model.md)

## API contract

Internal — no API surface.

## Acceptance criteria

### AC-08 — domain invariant

> **Given** a running bot has an order it believes is active
> **When** the bot asks the exchange about that order and the lookup fails for any reason other than the unknown-order error class
> **Then** the bot keeps the order as active and asks again later, up to the existing retry limit, after which it pauses as it does today; while the order's state is unknown, the bot places and cancels no orders tied to it. The bot treats the order as gone only when the exchange answers with the unknown-order error class, or when the order is in neither the exchange's open orders nor its order history; if the order history has it, the bot takes its final state (filled or cancelled) from there. A failed lookup is never proof that an order is gone
>
> — `spec.md §5, AC-08, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] Rewrite helpers to take an `ExchangeAdapter` instead of a session; keep today's function names and return shapes where runtimes rely on them; `get_instrument_rules` returns neutral `InstrumentRules`; `round_to_step` / `normalize_order_request` unchanged — `orders.py`, `market_data.py`, `positions.py`
- [ ] `resolve_order_state(db, bot, adapter, order_row)` implementing the four Flow 5 branches; an «unknown» result blocks place/cancel tied to that order and consumes the shared retry budget; exhaustion → today's pause — `orders.py`
- [ ] Keep the session-taking signatures as thin wrappers until T11–T14 migrate their callers (deleted in T20)
- [ ] Update the failed-order-lookup failure-table row's expected reaction to the AC-08 deviation — `BE/tests/golden/failure_table/rows.json`
- [ ] Tests for all four branches + retry exhaustion + «no place/cancel tied to an unknown order» — `BE/tests/test_order_lookup_rule.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Lookup times out 5 times in a row | Order stays active each time; bot pauses after the limit, as today |
| `unknown_order` error | Order marked gone (stored status per today's «gone» value) |
| Order in history as `Filled` | Takes `Filled` from history |
| Order in neither list | `not_found` → gone |
| Inconsistent order state on place (not lookup) | Today's `resync_and_retry` — unchanged |

## Definition of Done

- [ ] `pytest BE/tests/test_order_lookup_rule.py` passes
- [ ] Failure table passes with the lookup row carrying the AC-08 reaction
- [ ] `pytest -m golden` 12/12 and existing suites pass
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
