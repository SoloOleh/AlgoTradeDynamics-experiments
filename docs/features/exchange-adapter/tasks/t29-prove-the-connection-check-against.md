---
id: T29
title: "Prove the connection check against the emulator for rejected keys and a closed port"
layer: "tests"
deps: ["T28"]
acs: ["AC-12", "AC-13b"]
files_hint: ["BE/tests/test_venue_connection.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "done"
---

# T29 — Prove the connection check against the emulator for rejected keys and a closed port

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 13.

## Problem

The tests monkeypatch `create_session`, so the real EmulatorHTTP path into `check_connection` never runs (test-plan.md:66,70).

## Acceptance

- AC: AC-12, AC-13b — full text in [spec.md §5](../spec.md).
- **Definition of Done:** On the emulator venue a wrong key answers failing/rejected_credentials and a closed port answers failing/network, through the real client path, with no key in the answer.
