---
status: Draft
owner: "Oleh Solomko"
updated_at: "2026-10-07"
depth: "medium"
---

# Idea brief — binance-integration

## 1. Raw idea

Хочу додати інтеграцію з Binance до наявного проєкту AlgoTradeDynamics. Потрібна підтримка обох ринків: Spot і USDT-M Futures.Користувач має обирати біржу Bybit або Binance, тип ринку та торгову пару, підключати обліковий запис і запускати ботів через наявний інтерфейс платформи.На Spot потрібні купівля та продаж активів, перевірка доступного балансу й облік виконаних угод. На USDT-M Futures — LONG/SHORT, налаштування плеча та маржі, відкриття, відстеження й закриття позицій.Мета — підтримати наявні стратегії Grid, DCA, Momentum і Pattern Scalper у сумісних із ними режимах. Потрібно визначити, які режими кожної стратегії доступні на кожному ринку, та явно блокувати несумісні налаштування.Інтеграція має охоплювати ринкові дані, створення й скасування ордерів, відстеження виконань, балансів і позицій, історію операцій та обробку помилок. Після втрати з'єднання платформа має відновлювати актуальний стан із біржі.Роботу потрібно розділити на невеликі етапи. Спочатку перевірити підключення та ринкові дані, потім повний торговий цикл однієї стратегії на Spot і Futures, після цього додати решту стратегій.Початкові перевірки проводимо в тестових середовищах. Реальну торгівлю плануємо окремим етапом після перевірки інтеграції.Основні ризики: дублювання ордерів після таймауту, часткові виконання, неправильне округлення ціни або кількості, перевищення API-лімітів і розбіжності між станом платформи та біржі. API-ключі не повинні потрапляти в Git, логи або документацію.

## 2. Problem

AlgoTradeDynamics can run its four strategies (Grid, DCA, Momentum, Pattern Scalper) only on Bybit, so the operator cannot trade on Binance Spot or USDT-M Futures from the platform. Adding a second exchange with real money multiplies the failure modes the operator already named: duplicate orders after a timeout, partial fills, wrong price/quantity rounding, rate-limit breaches, and drift between the platform's view and the exchange's actual state. The Binance testnets cannot reproduce most of these on demand, so "works on testnet" does not prove they are handled.

## 3. Users

- **Operator (sole user)** — the project owner, trading their own money on their own Binance and Bybit accounts. There is one set of API keys per exchange and one person who carries the risk. No other users, no per-user isolation, no shared hosting.
- The operator configures bots through the existing platform UI (exchange → market → pair → strategy) and expects to step in by hand when a bot pauses on a state mismatch.

## 4. Why now

The interview did not surface an external trigger: no incident, deadline or contract. The work is self-directed. The cost of waiting is low because the plan is testnet-first and real money only enters at a separate, capped canary stage. Bybit keeps working as-is in the meantime.

## 5. Out of scope

- **Multi-user / third-party use.** Per-user key encryption, roles and audit are out because the operator is the only user.
- **Entering API keys through the UI.** Keys live in a local environment file outside Git, one pair for testnet and one for live. The UI shows only "connected / not connected" and a masked key. This removes the browser → backend → storage leak path.
- **Hedge position mode and Cross margin on Futures.** Only One-way + Isolated is supported. The platform checks the account mode before starting a bot and refuses with a clear message if it differs. A delta-neutral Grid with simultaneous LONG and SHORT positions is therefore an incompatible setting and is blocked.
- **More than one active bot per pair on Futures.** In One-way mode, two bots trading opposite directions would net out each other's positions.
- **Bringing Bybit up to the new guarantees** (order tagging, pause-on-divergence, fault checklist). This becomes a separate later roadmap step once the guarantees are proven on Binance. Until then the two exchanges behave differently on purpose, and that difference is documented.
- **Shadow mode on live market data.** It was rejected because it exercises none of the named order/fill/state risks.
- **Going straight to full working capital.** Live trading starts as a capped canary (see §7).
- **COIN-M Futures and other Binance products.** They were not requested.

## 6. Risks

- **Emulator fidelity (weakest spot).** The fault checklist relies on extending the existing `EXCHANGE_EMULATOR` to behave like Binance Spot and USDT-M Futures, including step/tick filters, error codes, partial fills, accepted-but-unanswered orders and rate-limit rejections. If the emulator disagrees with real Binance, the checklist goes green while the risk stays open. How easy the emulator is to extend has not been checked.
- **Assumes everything on the account was placed by the platform; false if** the operator trades the same account by hand or a duplicate slipped through after a timeout. Mitigation agreed in the interview: the exchange is the source of truth, every bot order carries its own client tag, and after a timeout the bot looks the order up by that tag before re-sending. Any unexplained mismatch pauses the bot and shows the difference; nothing is auto-corrected.
- **Pause-on-divergence trades uptime for safety.** A bot paused overnight stays paused until the operator acts, which is accepted. Frequent false pauses would erode trust in the guarantee.
- **Assumes testnet behaviour ≈ live behaviour; false for** liquidity, slippage, fees, funding payments and occasional testnet balance resets. The canary stage exists to catch this gap on small money.
- **Strategy × market compatibility is still undefined.** Momentum and Pattern Scalper lose their SHORT side on Spot. Without an explicit matrix, an incompatible setting could reach the exchange instead of being blocked in the UI.
- **Canary sizes may hit exchange minimum order sizes**, which forces either a larger canary or excludes some pairs.
- **Key leakage.** Keys must never reach Git, logs or docs. Keeping them in an environment file narrows the paths, but error logging that echoes exchange request payloads or headers stays a risk.

## 7. Recommendation

Build Binance as a parallel exchange for a single operator, with three non-negotiable guarantees: the exchange is the truth, orders are tagged so timeouts can't create duplicates, and the bot pauses on any unexplained mismatch. Futures are restricted to One-way + Isolated. Stage the work as follows:

1. Connectivity and market data.
2. A Binance-faithful emulator with switchable faults.
3. The full DCA cycle on Spot and Futures.
4. Grid as the deliberate stress test (many open orders, partial fills, rate limits).
5. Momentum and Pattern Scalper.
6. A capped live canary with a kill switch: one DCA bot on Spot and one low-leverage DCA bot on Futures.
7. Bybit parity.

A stage is done only when its scenario checklist passes, including forced faults on the emulator (connection drop mid-order, backend restart with an open position, bad quantity rounding), and the bot runs about 24h on testnet with zero unexplained divergence.

## 8. Open questions

- Use a dedicated Binance sub-account for bots, so that any foreign order means a bug and not a manual trade? — owner: Oleh Solomko
- Can the existing `EXCHANGE_EMULATOR` be extended to Binance semantics, or does it need a separate emulator? — owner: `/sdd:survey` (then Oleh Solomko)
- The strategy × market × mode compatibility matrix: which modes of Grid / DCA / Momentum / Pattern Scalper are allowed on Spot vs Futures, and which get blocked? — owner: Oleh Solomko
- Canary parameters: capital amount, maximum leverage, hard loss cap, and how long it must run clean before capital is raised. — owner: Oleh Solomko
- What happens to a running soak test when the testnet resets balances: does it restart the 24h clock or count as an explained divergence? — owner: Oleh Solomko
