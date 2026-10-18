---
id: T36
title: "Record the port's Decimal numbers in ADR-0001 and the SAD debt list"
layer: "docs"
deps: ["T34"]
acs: []
files_hint: ["docs/features/exchange-adapter/adr/0001-define-a-typed-exchange-neutral-adapter-port.md", "docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-15"
status: "done"
---

# T36 — Record the port's Decimal numbers in ADR-0001 and the SAD debt list

Follow-up task from the independent review — [`_review/review-2026-10-15.md`](../_review/review-2026-10-15.md), finding(s) 2.

## Problem

ADR-0001 (Neutral consequence) and sad.md §11 still say "floats, Decimal later", while T31/T33 moved the port to `Decimal` and sad.md §8 already documents it.

## Acceptance

- AC: n/a (quality follow-up).
- **Definition of Done:** ADR-0001 carries a dated amendment that ticker/position prices and execution figures are Decimals built from the exchange text (T31, T33); sad.md §11 accepted-debt line matches §8. No code change.
