---
status: Accepted
owner: "Oleh Solomko"
reviewers: ["Tech Lead"]
updated_at: "2026-10-13"
feature_size: "M"
ticket: "roadmap step 0 — exchange-adapter"
---

# 0010 — Extend FastAPI's `{detail}` error body with `code` and `details`, and keep the unversioned `/api` prefix

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** Oleh Solomko (Backend Lead), `api` stage

## Context

The `api` stage defaults to a `{code, message, details?}` error envelope and URL versioning
(`/api/v1/...`). The backend returns FastAPI's `{"detail": "..."}` on every error, the frontend
reads `body.detail || body.message` (`FE/src/api/client.js:40`), and existing tests assert on
`["detail"]`. Every route is mounted under `/api/` with no version segment (`BE/app/main.py:75-85`).
This feature adds new refusals (venue has no adapter, capability missing) that clients need to tell
apart, and a new venue-parameterised connection-check route. Neither should break the unchanged
frontend (spec §3 «New UI» non-goal) or change expected results in existing tests (spec §6, only
mechanical test edits).

## Decision drivers

- Spec §3: no new UI in this step, so the frontend must keep showing refusal reasons unchanged.
- Spec §6 «Existing test suites»: expected results do not change, except the connection-check test.
- SAD §1 QG-1 (trading equivalence) favours keeping today's observable behaviour over a purer shape.
- Clients (and step 1's UI) need a machine-readable way to tell `venue_unsupported` from
  `capability_missing` without matching English text.

## Considered options

1. **Superset `{detail, code?, details?}`.** `detail` is always present and carries today's text.
   Errors introduced by this feature add a neutral `code` (`module.error_name`) and structured
   `details`. Unchanged errors keep `{detail}`.
2. **The sdd default `{code, message, details?}` for new errors only.** The frontend still shows
   them through its `body.message` fallback, but the API ends up with two error shapes.
3. **`{detail}` only.** Matches the repo exactly, but clients can tell refusals apart only by text.

For the prefix: **keep `/api/...`** (the repo's only convention) vs **introduce `/api/v1/...`** for
the new route only.

## Decision outcome

**Chosen:** Option 1, and keep the unversioned `/api/` prefix. The superset is additive. Every
existing consumer and test keeps working, and the new refusals are machine-distinguishable. One
versioned route among the 53 unversioned ones in `BE/app/api/` would be an inconsistency with no consumer asking for it.

## Consequences

**Positive**
- No frontend or test change is forced by the contract. The 404 «Trading bot not found» and 401
  bodies stay byte-identical (AC-11 keeps revealing nothing).
- Guard refusals carry `code` + `details` that mirror the guard-pause event payload (`events.md`).

**Negative**
- The error body is FastAPI-shaped (`detail`, not `message`). A future move to the sdd default
  envelope would be a breaking change across all routes.
- Not every error carries a `code` yet. Clients must treat `code` as optional.

**Neutral**
- Versioning can be introduced later for all routes at once (e.g. when the Binance UI arrives) with
  its own decision.
- `RequestValidationError` (FastAPI's list-shaped `detail`) is left as is.

## Links

- Spec: [[../spec.md]] §3 «New UI», §6 «Existing test suites», US-04, US-05, US-06
- SAD: [[../sad.md]] §5 «Connection check», §8 «Error handling»
- Contract: [[../contracts/openapi.yaml]], [[../contracts/api-sync-report.md]]
- Related ADR: [[0004-resolve-adapters-by-venue-through-a-fail-closed-guard]]
