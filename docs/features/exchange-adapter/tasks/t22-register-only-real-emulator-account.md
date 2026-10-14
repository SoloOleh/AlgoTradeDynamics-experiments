---
id: T22
title: "Register only real emulator account keys with the shared scrubber"
layer: "infra"
deps: ["T20"]
acs: ["AC-16"]
files_hint: ["BE/app/bot_engine/exchange/bybit/adapter.py", "BE/app/bot_engine/exchange/redaction.py", "BE/tests/test_bybit_adapter.py", "BE/tests/test_redaction.py"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-14"
status: "todo"
---

# T22 — Register only real emulator account keys with the shared scrubber

Follow-up task from the independent review — [`_review/review-2026-10-14.md`](../_review/review-2026-10-14.md), finding(s) 2.

## Problem

`settings.emulator_api_key` is free-form API input; the adapter adds it to the process-wide scrubber, so a key like `"Buy"` or `"1"` masks those strings in every user's events, logs and Telegram texts until restart.

## Acceptance

- AC: AC-16 — full text in [spec.md §5](../spec.md).
- **Definition of Done:** A free-form emulator_api_key that is not an emulator-issued key (emu_ + token) or the configured default never reaches the process-wide scrubber, so it cannot mask other users' texts; real keys are still masked.
