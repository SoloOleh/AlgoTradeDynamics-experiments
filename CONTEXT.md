---
status: Living
updated_at: "2026-10-09"
---

# Domain Context — AlgoTradeDynamics

<!--
CONTEXT.md is the domain glossary — not a spec and not a scratch pad. NO implementation
detail here (no datastore/broker/framework names, no API contracts) — only domain words
and the boundaries between them. Implementation choices live in the SAD and ADRs; behaviour
lives in spec.md.

Terms get fixed inline, the moment they surface in an interview / spec / review — never
batched «I'll consolidate later». Empty H2 → prune before commit; keep only the sections
that carry real content. ## Glossary is mandatory; the other two are optional.
-->

## Glossary

- Capability — something an exchange adapter declares it can do on a given market type (for example hold positions, set leverage, place reduce-only closes, report realised profit per trade), which strategies consult instead of checking the exchange's name. NOT a strategy mode compatibility rule (which strategy modes are allowed on which market, decision D2), which is built on top of capabilities.
- Error class — the exchange-neutral category a failed exchange request falls into (for example rate-limited, insufficient balance, invalid order, unknown order, rejected credentials, temporary outage), which alone decides the bot's reaction and its severity. NOT the raw exchange error code, which is kept alongside for diagnosis but never drives behaviour outside the exchange adapter.
- Exchange adapter — the one place through which bots talk to a given exchange: it takes requests in platform terms (place, cancel and look up orders, prices, balances, positions, instrument rules, live events) and returns results and error classes in platform terms. NOT the exchange's own client library, which speaks that exchange's dialect, and NOT the exchange emulator, which is a stand-in exchange that an adapter can point at.
- Operator — a signed-in platform user who configures, starts, stops and supervises their own bots on their own exchange accounts and carries the trading risk. NOT the exchange account (the operator's account at the exchange that the bots trade on), and NOT another platform user, whose bots an operator can never see or control.
- Venue — the combination of exchange and environment a bot trades on (for example Bybit emulator, Bybit demo, Bybit live); the platform picks exactly one exchange adapter per venue, or refuses. NOT market type (spot vs USDT-M futures), which is chosen per bot inside a venue, and NOT the exchange alone.
