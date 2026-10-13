---
id: T18
title: "Replace the Bybit-demo test route with an authenticated per-venue connection check"
layer: "ports"
deps: ["T04", "T07"]
blocks: ["T20"]
acs: ["AC-12", "AC-13", "AC-13b", "AC-14", "AC-16"]
files_hint: ["BE/app/api/venues.py", "BE/app/api/bybit.py", "BE/app/main.py", "BE/app/bot_engine/exchange/bybit/adapter.py", "BE/tests/test_api.py", "BE/tests/test_venue_connection.py"]
owner: "Oleh Solomko"
estimate: "M"
context_budget: "M"
status: "todo"
---

<!-- To the executing agent: work from what is inlined here. If a slice is insufficient, ambiguous,
or contradicts the code in front of you, open the named file for the full text and follow that.
Do not invent the missing part. Every inline is a snapshot; the source always wins. -->

# T18 — Replace the Bybit-demo test route with an authenticated per-venue connection check

## Place in the sequence

- **Blocked by:** T04 — scrubber, T07 — guard (venue-only resolve) · **Blocks:** T20 — Close the step · **Wave:** 6 (independent of the strategy chain).
- **Lane:** shares `exchange/bybit/adapter.py` with T06 (already merged by then) — serialized.

## Why (user story)

> **As an** Operator
> **I want** the exchange connection check, which now goes through the exchange adapter and takes a venue, to tell me for any venue of this step whether it is connected, not configured or failing, without exposing keys (the existing screen keeps checking only the Bybit demo venue; per-venue status on screen arrives with step 1)
> **So that** I know my bots can reach the exchange before I start them
>
> — `spec.md §4, US-05, verbatim` · full text: [spec.md](../spec.md)

This task delivers the whole user story.

## Inlined context

> alt caller is not signed in → refused, nothing revealed about which venues are configured (AC-14)
> else signed in → apply the existing per-user rate limit → resolve adapter for the venue, no default venue
> alt venue has no adapter → venue named as not supported
> else adapter found → check connection
> alt no keys configured for the venue → venue named as not configured, no key value shown (AC-13)
> else keys configured → authenticated account request → exchange answers → connected (AC-12) · exchange request fails → failing, with the error class and no raw message (AC-13b)
> Postcondition: never with a key, a secret or a raw exchange message. Nothing is persisted
>
> — `sad.md §6, Flow 8, abridged` · full text: [sad.md](../sad.md)

> It **replaces** today's unauthenticated, demo-only `/api/bybit/test` with a venue-parameterised route under an exchange-neutral name. […] Nothing in `FE/src` calls the old route, so it is removed without a UI change. The existing connection-check test changes, which spec §6 allows for this test only. There is no default venue (ADR-0004).
>
> — `sad.md §5, Connection check, abridged` · full text: [sad.md](../sad.md)

> Removing `GET /api/bybit/test` changes `BE/tests/test_api.py:17-23`, the one test spec §6 allows to change.
>
> — `contracts/api-sync-report.md §D, verbatim` · full text: [api-sync-report.md](../contracts/api-sync-report.md)

> **Hard rule:** the connection check returns `str(exc)` to the caller today (`BE/app/api/bybit.py:30`) — the new answer never carries the raw exchange message.
>
> — `adr/0008, Context, abridged` · full text: [ADR-0008](../adr/0008-scrub-secrets-at-the-adapter-and-at-every-sink.md)

**Fallback:** insufficient or contradicted by the code → read the named file in full
([openapi.yaml](../contracts/openapi.yaml) · [sad.md](../sad.md)) and follow it. Do not guess.

## Data delta

No DB changes. (Nothing is persisted — Flow 8 postcondition.)

## API contract

- `GET /api/venues/{exchange}/{environment}/connection` (operationId `getVenueConnection`, BearerAuth) → `200 VenueConnection` · errors: `401` (unchanged body, reveals nothing), `422` path validation, `429` existing `api_read` limit.
- Path params: `exchange` string 1–40, `environment` string 1–20 — **not** enums, so an unknown venue reaches the guard.
- `VenueConnection = {venue: {exchange, environment}, status ∈ {connected, not_configured, failing, not_supported}, error_class: ErrorClass | null (set only when failing), message: fixed text per status}`, `additionalProperties: false`.
- Messages: `"Venue bybit/demo is connected"`, `"Venue bybit/live has no exchange keys configured"`, `"Venue bybit/testnet is failing: rejected_credentials"`, `"Venue binance/live has no exchange adapter"`.
- **Removed:** `GET /api/bybit/test`.

— `contracts/openapi.yaml, operationId getVenueConnection + schema VenueConnection, abridged` · full text: [openapi.yaml](../contracts/openapi.yaml)

## Acceptance criteria

### AC-12 — happy path

> **Given** a signed-in Operator and Bybit demo keys configured on the server
> **When** the Operator runs the connection check for the Bybit demo venue
> **Then** the system reports the venue as connected, as it does today
>
> — `spec.md §5, AC-12, verbatim` · full text: [spec.md](../spec.md)

### AC-13 — error

> **Given** a signed-in Operator and no keys configured for a venue
> **When** the Operator runs the connection check for that venue
> **Then** the system reports that venue (named) as not configured, without showing any key value
>
> — `spec.md §5, AC-13, verbatim` · full text: [spec.md](../spec.md)

### AC-13b — error

> **Given** a signed-in Operator and keys configured for a venue
> **When** the Operator runs the connection check for that venue and the exchange request fails
> **Then** the system reports that venue (named) as failing, with the error class of the failure, and the answer contains neither the raw exchange message nor any key or secret
>
> — `spec.md §5, AC-13b, verbatim` · full text: [spec.md](../spec.md)

### AC-14 — authorization

> **Given** someone who is not signed in
> **When** they try to run the connection check for any venue
> **Then** the system refuses, and the refusal reveals nothing about which venues are configured
>
> — `spec.md §5, AC-14, verbatim` · full text: [spec.md](../spec.md)

### AC-16 — error

> **Given** a request to an exchange fails while the bot's keys are in use
> **When** the failure is recorded in bot events and logs, sent as a notification, or reported by the connection check
> **Then** none of them contains a key or a secret of any venue, including the emulator account key (also when it appears inside a connection address); at most a masked key showing only its last 4 characters (`****abcd`) is shown, and a secret is never shown even in part. This applies to everything recorded after the deploy; records stored before it are not rewritten
>
> — `spec.md §5, AC-16, verbatim` · full text: [spec.md](../spec.md)

## Checklist

- [ ] `check_connection()` on `BybitAdapter`: no keys → `not_configured` with no request; else an authenticated account request → `connected` or `failing(error_class)` — `exchange/bybit/adapter.py`
- [ ] Route with `get_current_user`, venue-only guard resolve (`VenueNotSupported` → `not_supported`), fixed messages, never `str(exc)` — `api/venues.py`; mount under `/api` — `main.py`
- [ ] Delete `/api/bybit/test` — `api/bybit.py`; update `BE/tests/test_api.py:17-23` (the one allowed behaviour change)
- [ ] Tests for all five statuses/branches + 401 body identical for configured and unconfigured venues + a failing answer whose raw text contains a key → absent — `BE/tests/test_venue_connection.py`

## Edge cases

| Case | Behaviour |
|---|---|
| Unknown venue `binance/live` | `200 not_supported`, no request sent |
| Anonymous call for a configured vs unconfigured venue | Identical `401` bodies (AC-14) |
| `environment` 21 chars | `422` standard FastAPI body |
| Exchange error text echoes the API key | Not in `message`; only `error_class` returned |

## Definition of Done

- [ ] `pytest BE/tests/test_venue_connection.py BE/tests/test_api.py` passes
- [ ] Handler returns the spec'd outcome for AC-12, AC-13, AC-13b, AC-14
- [ ] Static search: no hits in the connection-check module
- [ ] every Hard Rule inlined above still holds
- [ ] lint + vet clean
