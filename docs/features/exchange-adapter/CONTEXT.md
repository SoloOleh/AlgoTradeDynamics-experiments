---
status: Living
updated_at: "2026-10-11"
---

# Domain Context — exchange-adapter

<!--
Feature-scoped glossary. Project-wide terms (Operator, Venue, Exchange adapter, Error class,
Capability) live in the repo-root CONTEXT.md; readers read both and the per-feature entry wins.
-->

## Glossary

- Golden run — a reference run of one strategy on the exchange emulator, driven by a fixed price scenario from a fixed starting state (either a fresh bot, or a bot paused mid-cycle by the pre-refactor code; a mid-cycle run is also replayed from the same state freshly captured from the previous migration step), whose orders, bot state and events are recorded before the refactor and must be reproduced identically after each migration step. NOT a testnet soak, which runs on a real test exchange with a moving market and is not reproducible.
- Trading equivalence — two runs are equivalent when they send the same orders (side, price and quantity after rounding, client tag, order of placement and cancellation), reach the same bot state (status, cycles, positions, profit and loss) and react to every error class with the same action and severity. NOT message equivalence: event and notification wording may differ as long as the event category (the event type plus the stored error type, whose values stay the same as today) and the raw exchange code are kept.
