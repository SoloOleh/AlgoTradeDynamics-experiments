---
id: T54
title: "State the reduce-only mitigation as deferred, not done"
layer: "docs"
deps: ["T51"]
acs: []
files_hint: ["docs/features/exchange-adapter/data-model.md", "docs/features/exchange-adapter/sad.md"]
owner: "Oleh Solomko"
estimate: "S"
origin: "review-2026-10-17"
status: "done"
---

# T54 — State the reduce-only mitigation as deferred, not done

Follow-up task from the independent review — [`_review/review-2026-10-17.md`](../_review/review-2026-10-17.md), finding(s) 4.

## Problem

`data-model.md:148` and `sad.md:945` describe «a neutral accessor with a legacy-key fallback» as if built; the accessor reads only `reduceOnly` (`trading_bot_order.py:71-78`) and the neutral-key work is deferred in `spec.md:256`.

## Acceptance

- AC: n/a (ADR-0001, ADR-0003 documentation fidelity).
- **Definition of Done:** data-model.md (raw_response row) and sad.md §11 describe the neutral reduce-only accessor as deferred to spec §8 (raw_response neutral keys), due before the roadmap step 1 spec, with a link to that §8 item; today's accessor reads the Bybit `reduceOnly` key. No code change.
