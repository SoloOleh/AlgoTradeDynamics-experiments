---
id: T28
title: "Validate the guard and connection-check responses against the contract file"
layer: "tests"
deps: ["T20"]
acs: ["AC-09", "AC-12", "AC-13", "AC-13b", "AC-14"]
files_hint: ["BE/tests/contract_schema.py", "BE/tests/test_bots_guard_api.py", "BE/tests/test_venue_connection.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T28 — Validate the guard and connection-check responses against the contract file

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 12.

## Problem

test-plan.md:25 asks for contract validation against the file; the tests compare with hand-written dicts.

## Acceptance

- AC: AC-09, AC-12, AC-13, AC-13b, AC-14 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** Every real response body from start, the guarded actions and GET /api/venues/{exchange}/{environment}/connection (four statuses + 401) validates against contracts/openapi.yaml with $ref resolved; no hand-rolled schema copies.
