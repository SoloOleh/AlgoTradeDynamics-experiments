---
status: Draft
owner: "Oleh Solomko"
updated_at: "2026-10-17"
feature_size: M
route: standard
---

# API sync report — exchange-adapter

**Inputs read:** `data-model.md` (present, `schema_change: false`), `sad.md` (frontmatter
`target_surfaces: [backend-service]`, §4, §5, §6 Flows 1–10, §8), `spec.md` §4/§5, `CONTEXT.md`
(root + feature), `adr/0001–0009`, and the live code the contract must not break:
`BE/app/api/{bots,bybit,deps}.py`, `BE/app/schemas/trading_bot.py`, `BE/app/core/rate_limit.py`,
`BE/app/bot_engine/exchange_streams.py`, `FE/src/api/client.js`.

**Contract form:** `backend-service` → OpenAPI 3.1 (`openapi.yaml`) + `events.md` (Flows 1, 3, 7
are event-driven). **Scope:** only the operations whose HTTP behaviour this feature changes, plus
the removal of `GET /api/bybit/test`. Unchanged success bodies are referenced by their FastAPI model
names and list only the fields the ACs touch, so they are not hand-copied.

**Deviations from the sdd defaults (ADR-0010):** error body is `{detail, code?, details?}`, not
`{code, message, details?}`. Paths keep the unversioned `/api/` prefix. No list operation is added,
so the cursor-pagination default doesn't apply. No `Idempotency-Key`: no operation's flow has a
retry or an async actor.

## A. Field origins

| schema_path | origin | confidence |
|---|---|---|
| getVenueConnection.path.exchange | data-model.md → trading_bots.exchange VARCHAR(40) (venue key, ADR-0004) | high |
| getVenueConnection.path.environment | data-model.md → trading_bots.environment VARCHAR(20) | high |
| getVenueConnection.200.venue.{exchange,environment} | echo of the path params (same bounds) | high |
| getVenueConnection.200.status | spec AC-12/AC-13/AC-13b + sad.md §6 Flow 8 «not supported» branch (decided at this stage) | high |
| getVenueConnection.200.error_class | ADR-0005 error-class list (13 values); spec AC-13b | high |
| getVenueConnection.200.message | spec AC-13/AC-13b (venue named, no raw message); fixed text per status | medium |
| getVenueConnection.401 | `BE/app/api/deps.py` get_current_user (unchanged body); spec AC-14 | high |
| getVenueConnection.429.rate_limit | `BE/app/core/rate_limit.py` RateLimitMiddleware (`api_read`, 300/60 s default) | high |
| *.path.bot_id | data-model.md → trading_bots.id INTEGER PK | high |
| *.404.detail | `BE/app/api/bots.py` «Trading bot not found» (unchanged); spec AC-11 | high |
| GuardRefusal.code | sad.md §6 Flow 2/6 refusal branches → `exchange.venue_unsupported` / `exchange.capability_missing` (contract proposal, no registry) | high |
| GuardRefusal.detail | sad.md §6 Flow 6 «venue named» / «capability and market type named»; spec AC-09, AC-15 | medium |
| GuardRefusal.details.exchange / environment | data-model.md → trading_bots.exchange (40) / environment (20) | high |
| GuardRefusal.details.capability | sad.md §5 capability catalogue (ADR-0006), 10 refusable values | high |
| GuardRefusal.details.market_type | data-model.md → trading_bots.category; `CategoryLiteral` {linear, spot, inverse} | high |
| startBot.400 / startBot.422 (existing validation) | `BE/app/api/bots.py:175-177` (unchanged) | high |
| closeBotPosition.requestBody.confirm | `TradingBotClosePositionRequest.confirm` (unchanged) | high |
| TradingBot.{id,exchange,environment,category,runtime_status} | data-model.md → trading_bots columns | high |
| TradingBot.last_error | data-model.md → trading_bots.last_error VARCHAR(500), scrubbed (AC-16) | high |
| TradingBot.last_error_type / last_error_code | data-model.md → VARCHAR(40), values unchanged (ADR-0003, AC-06) | high |
| TradingBotOrder.order_link_id | data-model.md → trading_bot_orders.order_link_id VARCHAR(160) | high |
| TradingBotOrder.status | data-model.md → trading_bot_orders.status VARCHAR(30), frozen vocabulary | high |
| TradingBotOrder.{exchange,environment,category} | data-model.md → VARCHAR(40/20/20) | high |
| TradingBotOrder.raw_response | data-model.md → trading_bot_orders.raw_response JSON | high |
| TradingBotPerformance.{closed_cycles,closed_fees,net_realized_pnl} | `TradingBotPerformanceResponse` (unchanged), spec AC-02 | high |
| ClearHistoryResult.exchange_cancel_error | `TradingBotClearHistoryResponse` (unchanged shape); now scrubbed + guard reason (F-4) | medium |
| events: exchange.stream_event.v1.{bot_id,topic,data,received_at} | `exchange_streams.py:40-45` ExchangeStreamEvent; `data` neutral per ADR-0001 | high (envelope) / low (`data` fields, settled in `port.py` at `tasks`) |
| events: bot.guard_paused.v1.event_type | sad.md §8 «Events» categories; strings proposed here, accepted (F-3) | medium |
| events: bot.guard_paused.v1.payload.* | mirrors GuardRefusal.details (same origins) | high |
| events: incidents.incident_type | data-model.md → `VENUE_UNSUPPORTED` / `CAPABILITY_MISSING` (≤ 80) | high |

## B. Drift checklist

1. **Endpoint ↔ data-model** *(core)* — ✓ with one accepted exception. Every bot operation reads
   `trading_bots` (ownership by `user_id`, venue by `exchange` + `environment`, market type by
   `category`), and the mutating ones write `trading_bots` / `trading_bot_orders` /
   `trading_bot_events` as today. **F-1 (accepted):** `getVenueConnection` reads and writes no
   table. That is by design: Flow 8's postcondition says «nothing is persisted», and keys come from
   server configuration (spec §1 scope assumptions). Its venue fields reuse the `trading_bots`
   bounds.
2. **Error code ↔ repo error definition** *(core)* — ✓ (no registry). The repo has no central
   `module.error_name` list. `BE/app/bot_engine/error_handling.py` `ErrorType` holds **stored error
   types**, a different vocabulary (ADR-0003). No error registry was found, so the two codes
   `exchange.venue_unsupported` and `exchange.capability_missing` are this contract's proposal.
   Reconcile them when `bot_engine/exchange/errors.py` (`VenueNotSupported`, `CapabilityMissing`,
   `sad.md` §5) defines them. `ErrorClass` enum = ADR-0005's list, 13 of 13.
3. **Validation ↔ constraint** *(core)* — ✓. `maxLength` 40 / 20 / 30 / 160 / 500 match the
   data-model column types. `MarketType` = `CategoryLiteral`. `Capability` = sad.md §5 catalogue
   minus `client_tag_max_length`, which is a declared value, not refusable (sad.md §5). The venue
   path params are deliberately **not** enums, so an unknown venue reaches the guard (ADR-0004) and
   is answered `not_supported` instead of failing validation.
4. **OpenAPI ↔ sequence** *(supporting)* — ✓ with two follow-ups.
   - Flow 8: every branch has a response (401 / `not_supported` / `not_configured` / `connected` /
     `failing`). The rate-limit step has no `alt` in the flow. 429 comes from the existing
     middleware.
   - Flow 6: 404 / 422 `venue_unsupported` / 422 `capability_missing` / 200.
   - Flow 2: 404 and 422 guard refusal on stop, sync, cancel-orders, close-position, position, risk.
     **F-2 (follow-up):** `getBotPerformance` does not refuse. It skips the best-effort refresh and
     returns stored figures (200), unlike Flow 2's drawing.
   - **F-4 (follow-up, sequence gap):** `clearBotHistory` reaches the exchange but no §6 flow covers
     it and SAD §4 doesn't list it as a guard entry point.
   - No orphan sequence: Flows 1, 3, 4, 5, 7, 9, 10 have no HTTP endpoint by design (worker,
     harness, sinks). Flows 1, 3, 7 are covered by `events.md`.

### Back-feed (coverage)

| AC | Contract coverage |
|---|---|
| AC-01, AC-03, AC-04, AC-05, AC-06, AC-07, AC-08, AC-17 | No HTTP surface: worker / golden-run harness. Event invariants in `events.md` (`exchange.stream_event.v1`, `bot_error`) |
| AC-02 | `getBotPerformance` 200 (unchanged figures) |
| AC-09 | `startBot` 422 `exchange.venue_unsupported` |
| AC-10 | `events.md` `bot.guard_paused.v1` (`venue_unsupported`) |
| AC-11 | 404 `BotNotFound` on every bot operation, checked before the guard |
| AC-12 / AC-13 / AC-13b | `getVenueConnection` 200 `connected` / `not_configured` / `failing` + `error_class` |
| AC-14 | `getVenueConnection` 401, BearerAuth required |
| AC-15 | `startBot` 422 `exchange.capability_missing` |
| AC-15b | `events.md` `bot.guard_paused.v1` (`capability_missing`) |
| AC-16 | `detail` / `message` / `exchange_cancel_error` / event `message` + `payload` documented as scrubbed |

Every operation maps to a user story: `getVenueConnection` → US-05; `startBot` → US-04, US-06;
`stopBot`, `syncBot`, `cancelBotOrders`, `closeBotPosition`, `getBotPosition`, `getBotRisk` →
US-04 (AC-11 + Flow 2 guard); `getBotPerformance` → US-01 (AC-02); `clearBotHistory` → US-04,
US-07. **F-5 (spec gap):** two contract outcomes have no AC: the connection check's
`not_supported`, and a guard 422 on the Operator's own bot for actions other than start.

## C. Resolutions

Sections A and B record the drift as found on 2026-10-13. The **Status** column records where each
finding has since landed.

| # | Finding | Action | Owner / due | Status |
|---|---|---|---|---|
| F-1 | Connection check reads no entity | **Accept**: by design (Flow 8, nothing persisted) | — | Closed: by design |
| F-2 | `getBotPerformance` degrades (200) where Flow 2 draws a refusal | **Save-as-OQ**: add «guard refuses → skip refresh, stored figures» to Flow 2's page-read branch. The contract keeps 200 | `sequences`, before the contract is finalized | Resolved: `sad.md` Flow 2 page-read degrade branch and the ADR-0004 amendment (T42) |
| F-3 | Guard-pause `event_type` strings not named upstream | **Accept**: `venue_unsupported` / `capability_missing`. `last_error_*` TBD stays with `tasks` (data-model.md) | — | Resolved: `data-model.md` «Guard pause» note under `trading_bots` (T43) |
| F-4 | `clearBotHistory` reaches the exchange with no flow and no guard entry point. `exchange_cancel_error` is raw `str(exc)` today (AC-16 leak path) | **Save-as-OQ**: add it to Flow 2, to SAD §4's guard entry-point list and to Flow 9's sinks. The contract keeps it behind the guard, scrubbed | `sequences` (+ `design` for §4), before the contract is finalized | Resolved: `sad.md` Flow 2 clear-history branches, §4 pillar 4, §8 Secrets row (T49) and Flow 9's postcondition (T53) |
| F-5 | No AC for `not_supported` or for guard refusal on non-start actions | **Save-as-OQ**: add ACs under US-05 and US-04. The contract keeps both outcomes | `specify`, before the contract is finalized | Deferred: `spec.md` §8, owner Oleh Solomko, due before the roadmap step 1 spec (T42) |

Earlier stage-level decisions (asked before generation): error body superset + unversioned prefix
(ADR-0010), route `GET /api/venues/{exchange}/{environment}/connection`, `not_supported` as a 200
status, and guard refusal = 422 on every exchange-reaching action except the best-effort ones.
These close the two api-stage items in SAD §6 «Flagged for design».

## D. Implementation notes for `tasks`

- `closeBotPosition` maps `ValueError → 400` today. Guard refusals must be mapped to 422
  explicitly (don't let them subclass `ValueError` into that branch). The same applies to
  `startBot`, which maps every `ValueError` to 422/400 by message text.
- Removing `GET /api/bybit/test` changes `BE/tests/test_api.py:17-23`, the one test spec §6
  allows to change.
- `getBotRisk` reaches the exchange only for Momentum and Pattern Scalper, but the guard runs for
  every strategy so the rule stays one line.

## E. Lint

Not run: `spectral` is not installed or wired in this repo. Suggested:
`npx @stoplight/spectral-cli lint docs/features/exchange-adapter/contracts/openapi.yaml`. The YAML
parses, and every `$ref` resolves (checked locally).
