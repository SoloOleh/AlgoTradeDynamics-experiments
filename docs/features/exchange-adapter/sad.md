---
status: Draft
owner: "Oleh Solomko"
reviewers: ["Tech Lead", "Security Lead"]
updated_at: "2026-10-12"
feature_size: "M"
target_surfaces: []  # filled in §4 — subset of: backend-service | web-frontend | mobile-app | desktop-app | cli | worker | library-sdk. Read (never re-derived) by api/sequences/tasks/plan-tests/review → _shared/surfaces.md
---

# Software Architecture Document — exchange-adapter

<!-- 12 Arc42 sections. Empty section → <!-- N/A: <one-line reason> -->. -->
<!-- C4 Context (L1) lives inline in §3. C4 Container (L2) lives inline in §5. -->
<!-- Numbers in §10 come VERBATIM from spec.md §6 NFR — no inventing, no rounding. -->

## 1. Introduction and goals

<!-- 🎯 Why: durable memory of «what + the three dominant qualities + who cares». A year from
     now nobody recalls which three qualities were critical for this system.
     📋 Write: 1 ¶ intent + 3 lines of top-3 quality goals + a stakeholders table.
     ¶4 is the override slot — critic `Override` resolutions emit «Decision override: <headline>
     — rationale: <reason>» bullets here so downstream skills see the deliberate choice. -->

**Intent.** Every Grid, DCA, Momentum and Pattern Scalper bot reaches an exchange only through one exchange-neutral **exchange adapter**, chosen by the bot's **venue**. The same goes for its runtime, the live exchange event streams, the error handling and the connection check. The adapter takes requests and returns results in platform terms, reports failures as **error classes**, and declares its **capabilities** per market type. Bybit (emulator, demo, testnet, live) moves behind it with **trading equivalence**, including bots that are mid-cycle when a migration step is deployed. An unsupported venue or a missing capability fails closed. The Operator keeps trusting their Bybit bots, and Binance (roadmap step 1) becomes "add an adapter" instead of "fork the strategies".

**Top-3 quality goals (1-liners; full scenarios in §10):**

1. **Trading equivalence across every migration step.** The same orders, bot state and error reactions as before the refactor, including mid-cycle bots across a deploy (no second ladder, no re-placed take-profit).
2. **Fail-closed safety.** No order ever reaches an exchange the bot's venue did not choose, no strategy relies on a capability its adapter did not declare, and no key or secret leaks through the adapter's error paths.
3. **Exchange extensibility.** 0 places outside exchange adapters reach an exchange, so adding an exchange changes 0 strategy files.

When goals conflict, the higher one wins (for example, keeping today's stored values beats a "purer" neutral vocabulary). Reaction latency and adapter overhead are guard-rails, not top-3 goals; their scenario is QG-4 in §10.

**Stakeholders.**

| Role | Interest | Sign-off owner? |
|---|---|---|
| Operator | Runs Bybit bots of all four strategies. Needs trading equivalence across deploys, fail-closed refusals and pauses with a stated reason, and no key leaks | No |
| Tech Lead | SAD and ADR approval, and ownership of the golden-run and failure-table gate before every merge | Yes |
| Security Lead | Exchange credentials and error payloads move into a new component (spec §6.1 «Security review: Required») | Yes |

<!-- Decision overrides (¶4) — populated by the critic resolution loop, empty otherwise. -->

## 2. Constraints

<!-- 🎯 Why: §4 strategy only works when §2 has fixed WHAT IS ALREADY FIXED — stack, versions,
     deadline, regulatory. This is an input, not an output.
     📋 Write: four blocks — Technical / Organisational / Conventions / Regulatory.
     📌 Pin versions («<datastore> 18», not «<datastore>»); «Q3 deadline — hard», not «ideally».
     Never N/A — every feature inherits at least Conventions + Technical. -->

**Technical.**
- Python 3.14 for the backend and the exchange emulator (`BE/Dockerfile`, `EXCHANGE_EMULATOR/Dockerfile`: `python:3.14-slim`).
- FastAPI 0.141.1, Uvicorn 0.52.4, SQLAlchemy 2.0.52, Alembic 1.19.1, pydantic 2.13.4, PyJWT 2.13.0, pybit 5.17.0 (Bybit SDK), websockets 17.0.1, httpx2 2.12.0 (`BE/requirements.txt`).
- PostgreSQL 18 for all backend state (`docker-compose.yml`). SQLite for `emulator.db` and for the backend test database (`BE/tests/conftest.py:9-15`).
- The bot worker is an in-process asyncio loop started in the FastAPI lifespan (`BE/app/main.py:40-42`, `BE/app/bot_engine/runtime.py`). Strategies are synchronous calls that receive a SQLAlchemy session and the bot. WS events wake the worker, REST stays the source of truth, and there is a 15 s fallback reconciliation (`runtime.py:15`).
- The exchange emulator speaks only the Bybit v5 dialect. It has no fault injection and no partial fills (`EXCHANGE_EMULATOR/app/engine.py:615`), so error-reaction equivalence is proven with the reference failure table (spec AC-06), not with emulator faults.

**Organisational.**
- Team: one developer (Oleh Solomko) acting as Tech Lead. A Security Lead review is required (spec §6.1).
- Budget: size M, about 1–2 sprints. No hard deadline. This is wave 1 of `docs/roadmap.md`, and it blocks step 1 (Connect Binance).
- There is no CI pipeline in the repo. The spec §6 checks (golden runs, failure table, static search, existing suites) run as a local pre-merge routine (spec §8 default; owner Oleh Solomko, due before `/sdd:tasks`).
- Delivery is a sequence of migration PRs. Each one must pass the golden runs, the failure table and 100% of the existing backend and emulator suites before it merges.

**Conventions.**
- [`docs/architecture-map.md`](../../architecture-map.md) §Conventions is the convention source. Exchange clients live in `BE/app/bot_engine/<exchange>/`. Routers are mounted under `/api`. Strategies are registry singletons. Errors are classified into type, severity and action (`retry_backoff`, `resync_and_retry`, `pause`, `stop`). PKs are integer autoincrement. Alembic files are named `NNNN_snake_description.py`.
- Client order tags are `bot-{id}-g{generation}-…` plus a deterministic suffix (`grid_runtime.py:41-47`, `dca_runtime.py:213`).
- Tests use pytest with an SQLite override and the emulator as the exchange double, with no SDK mocks. In this step only mechanical test edits are allowed (spec §6 «Existing test suites»).

**Regulatory / external.**
- Data classification is confidential: exchange credentials plus the Operator's balances, positions and orders (spec §6.1). No new personal data.
- Keys appear at most masked to their last 4 characters (`****abcd`). Secrets never appear, even in part. Records stored before the deploy are not rewritten (AC-16).
- The platform is multi-user. Bot ownership checks run before any venue routing (AC-11), and the connection check requires a signed-in operator (AC-14).

## 3. Context and scope

<!-- 🎯 Why: draws the SYSTEM BOUNDARY — who talks to it from outside, where the trust zone ends.
     Without §3, §5 and §8 (authorization) blur — unclear what's «inside» vs «outside».
     📋 Write: 2–3 sentences of business context + an external-systems table + a C4Context block.
     📌 «External: none (deliberate, no third-party in v1)» is itself a decision worth stating.
     Trust boundary — the line past which you don't trust data without checking it.
     Never N/A — greenfield still draws the planned actors + external systems. -->

AlgoTradeDynamics lets an Operator configure, run and supervise Grid, DCA, Momentum and Pattern Scalper bots, and run backtests of them. Today it trades only on Bybit (emulator, demo, testnet, live). This feature changes no external relationship: the same Operator talks to the same platform, and the platform talks to the same Bybit environments and the same exchange emulator. What changes is the boundary *inside* the platform. Every exchange request leaves through an exchange adapter chosen by the bot's venue, and the trust boundary around exchange responses (error texts that may echo keys, Bybit-shaped payloads) moves into that adapter.

<!-- brownfield: architecture-map.md reflects fb25db3; no BE/EXCHANGE_EMULATOR/FE change since (HEAD d75458e). direct exchange access in 9 backend files (grid_runtime, dca_runtime, momentum, pattern_scalper, orders, performance, market_data, positions, api/bybit), plus exchange_streams.py and error_handling.py. -->

**External systems (in / out):**

| Actor or system | Type | Interaction |
|---|---|---|
| Operator | Person | Signs in, creates bots, starts, stops, syncs and closes them, reads the bot page and the event feed, runs backtests and runs the connection check (API only in this step) |
| Bybit (demo / testnet / live) | System (external) | v5 REST for orders, order lookups, executions, prices, instrument rules, balances and positions, plus the v5 private and public WS streams. Keys are server-wide per environment |
| Exchange emulator | System (internal, separate deployable) | Bybit v5-compatible REST and WS. The venue «Bybit emulator» trades here with a per-bot or default emulator account key. Backtests also run here |
| Telegram | System (external) | Receives bot notifications (pauses, errors) when the Operator has connected Telegram and allows that event type |
| Binance | System (external) | **Out of scope for this step.** No adapter and no connection. Roadmap step 1 adds it behind the seam this step builds |

**C4 Context (L1):**

```mermaid
C4Context
    title exchange-adapter — System Context

    Person(operator, "Operator", "Configures, runs and supervises own bots on own exchange accounts")
    System(platform, "AlgoTradeDynamics", "Web app + backend + in-process bot worker. All exchange access goes through the exchange adapter for the bot's venue")
    System(emulator, "Exchange emulator", "Bybit v5-compatible stand-in exchange, used by the Bybit emulator venue and backtests")
    System_Ext(bybit, "Bybit", "Demo, testnet and live v5 REST + WS")
    System_Ext(telegram, "Telegram", "Bot notifications to the Operator")

    Rel(operator, platform, "Starts, stops and supervises bots, runs the connection check", "HTTPS")
    Rel(platform, bybit, "Orders, lookups, executions, balances, positions, live events via the Bybit adapter", "REST + WS")
    Rel(platform, emulator, "Same requests via the Bybit adapter, emulator venue", "REST + WS")
    Rel(platform, telegram, "Pause and error notifications, keys masked", "HTTPS")
    Rel(telegram, operator, "Delivers notifications")
```

## 4. Solution strategy

<!-- 🎯 Why: the 3–4 STRATEGIC PILLARS every ADR grows from. Without §4 each ADR looks random —
     there's no umbrella. ⭐ The densest section — the blast-radius gate fires almost always here
     (decisions are irreversible + multi-module).
     📋 Write: 3–4 choices; each a heading + 2–3 sentences of rationale.
     📌 «Store content as a table of typed blocks» is a pillar — ADR-0001 grows from it. -->

**Top strategic choices (the seeds for ADRs):**

1. **<e.g. Module isolation through events>** — <2–3 sentences citing quality goals + constraints>.
2. **<e.g. Single-store persistence>** — <2–3 sentences>.
3. **<e.g. Server-rendered read side>** — <2–3 sentences>.

Each tactical decision in later sections should trace to one of these seeds. Tactical decisions that *contradict* a strategic choice are red flags — surface them in §11.

## 5. Building block view

<!-- 🎯 Why: INTERNAL DECOMPOSITION — modules, containers, datastores. The static topology: who
     may talk to whom. Without §5, §6 (the flows) has no vocabulary of participants.
     📋 Write: 1 ¶ on the style (layered / hexagonal / clean / event-driven) + a folder tree + a
     C4Container block.
     📌 Draw ONE Container per declared `target_surface` (frontmatter): a fullstack
     [backend-service, web-frontend] = a backend-API container + a web/SPA container; a
     [backend-service, mobile-app] = the API + the mobile app. The Container(web, …) line below is
     just one surface's container — swap/add per what was declared in §4. → _shared/surfaces.md
     📌 e.g. «web app, content API, media worker, datastore, object store, CDN». -->

<One paragraph: layered / hexagonal / clean / event-driven, and why.>

**Internal decomposition:**

```
<e.g. modules/<feature>/>
├── domain/       <entities + sentinel errors>
├── app/          <use cases / services>
├── infra/        <repository + integration impl>
├── ports/        <handlers, DTOs, error mapping>
└── wiring        <self-wiring entry point>
```

**C4 Container (L2):** <!-- syntax → references/c4-mermaid-syntax.md. Real names, no <placeholder> stubs. ONE Container per declared target_surface (frontmatter); the web container below is one example surface. -->

```mermaid
C4Container
    title <feature> — Containers

    Person(actor, "<Actor>")

    Container_Boundary(app, "<Our system>") {
        Container(web, "<Web/UI>", "<technology>", "<purpose>")
        Container(api, "<API/handler>", "<technology>", "<purpose>")
        ContainerDb(db, "<Datastore>", "<technology>", "<purpose>")
    }

    System_Ext(ext, "<External>", "<purpose>")

    Rel(actor, web, "<interaction>", "<protocol>")
    Rel(web, api, "<calls>")
    Rel(api, db, "<reads/writes>", "<driver>")
    Rel(api, ext, "<emits>", "<protocol>")
```

## 6. Runtime view

<!-- 🎯 Why: the RUNTIME FLOW of 1–2 critical scenarios — who talks to whom, when, in what order.
     Without §6, §5 is just boxes with no life.
     📋 Write: a Mermaid sequenceDiagram. Participants are names from §5 (don't invent new ones).
     Messages are semantic («saves a draft»), NO HTTP verbs / paths / status codes — endpoint-level
     sequences arrive at the `api` stage.
     📌 e.g. «author → web: composes draft → web → content API: save». Seed the primary flow(s) here;
     the `sequences` stage then covers every §5 AC (no cap). Never N/A for M+; XS/S keeps ≥1 happy-path flow. -->

**Critical flow 1: <flow name>**

```mermaid
sequenceDiagram
    actor Actor
    participant Web
    participant Service
    participant Store
    Actor->>Web: <action>
    Web->>Service: <call>
    Service->>Store: <write>
    Store-->>Service: ok
    Service-->>Web: result
    Web-->>Actor: confirmation
```

**Critical flow 2: <e.g. async event propagation>** — <if applicable, otherwise N/A>.

## 7. Deployment view

<!-- 🎯 Why: the TOPOLOGY DevOps must know without reading the deploy charts — how many replicas,
     where the background worker lives, AT WHAT NUMBERS we scale.
     📋 Write: 2–3 sentences on topology + monitoring + concrete threshold numbers.
     📌 e.g. «500 authors → partition by quarter» (not «we'll think about scale later»).
     🎯 N/A allowed for XS/S that reuses an existing deployment unit with no change.
     Deployment-diagram scaffold → templates/deployment.md. -->

<Topology in 2–3 sentences. Where it runs, replicas, scaling thresholds.>

**Monitoring:**
- <Metrics — e.g. `<metric_name>`>
- <Alerts — e.g. «worker lag > 10 min → page on-call»>
- <Tracing — e.g. spans on the request boundary>

**Scaling thresholds:**
- <e.g. comfortable in one table up to N rows/year>
- <e.g. partition by quarter above N rows/year>

<!-- For XS/S with no deployment change: <!-- N/A: reuses existing deployment unit, no infra change --> -->

## 8. Crosscutting concepts

<!-- 🎯 Why: CROSS-CUTTING PATTERNS spanning several modules: logging, errors, authorization, ID
     strategy, events, caching. ⭐ The second-densest section. A pattern inside one module is NOT
     here; a project-wide convention belongs in the convention file.
     📋 Write: a table — concept / convention / where defined. One row per concept.
     📌 e.g. «sortable time-based IDs generated in the app layer» as a default from the convention file. -->

| Concept | Convention | Where defined |
|---|---|---|
| Logging | <e.g. structured, fields `module=<name>`> | <convention file §X or here> |
| Authentication | <e.g. token-based via middleware> | <convention file §X> |
| Error handling | <e.g. domain sentinel → ports error mapping → JSON> | <convention file §X> |
| ID strategy | <e.g. sortable time-based ID in the app layer> | <convention file §X> |
| Internationalisation | <e.g. N/A, single language> | — |
| Observability | <e.g. tracing on the request boundary> | — |
| Events | <module-specific patterns, if any> | <here> |

## 9. Architecture decisions

<!-- 🎯 Why: the REVERSE INDEX onto the adr/ folder. `ls adr/` gives the files; §9 gives the
     semantics — why they exist, which SAD section they attach to, what status.
     📋 Write: a 4-column table, one row per ADR. Mixed status is fine.
     📌 e.g. «0001 | Store content as a table of typed blocks | Accepted | §4». -->

| # | Title | Status | Section |
|---|---|---|---|
| <NNNN> | <imperative — e.g. "Use a sliding-window counter for rate limiting"> | Accepted | §<N> |
| <NNNN> | <imperative — e.g. "Co-locate the worker in the API process"> | Accepted | §<N> |

ADR files live under `docs/features/<slug>/adr/NNNN-<title>.md`.

## 10. Quality requirements

<!-- 🎯 Why: the QUALITY TREE — take a goal from §1 and break it into concrete leaves: tests,
     metrics, configs, drills. ⭐ Without §10, §1 is a manifesto. With §10 each declaration maps
     to something PROVABLE.
     📋 Write: per §1 goal — When / Then / How-verify. Numbers from spec §6 NFR VERBATIM (don't
     round ≤250ms to ≤300ms — that's a critic F6 hit).
     📌 e.g. «p95 ≤ 500 ms on a block update, verified by a 100 req/s load test». -->

Each top-3 goal from §1 expanded into a full scenario:

**QG-1. <quality attribute>**
- **When:** <trigger condition>
- **Then:** <expected behaviour with numbers from spec §6 NFR>
- **How verify:** <test / chaos drill / load test / metric>

**QG-2. <quality attribute>**
- **When:** <trigger>
- **Then:** <expected>
- **How verify:** <how>

**QG-3. <quality attribute>**
- **When:** <trigger>
- **Then:** <expected>
- **How verify:** <how>

## 11. Risks and technical debt

<!-- 🎯 Why: ⭐ collects EVERYTHING that can break — not only the technical. Without §11 risks get
     discussed at standups and lost; debt lives only in the head of whoever accepted it.
     📋 Write: a risk/debt table — severity — mitigation — owner. Accepted debt in its own block.
     📌 The first risk is often a product risk, not a technical one. That's normal. -->

<!-- Severity literals: Low / Medium / High for regular risks; "Open question" for rows created by
     a Save-as-OQ resolution during the Socratic walk (see references/socratic.md). -->

| Risk / debt | Severity | Mitigation | Owner |
|---|---|---|---|
| <e.g. Worker lag may reach hours during a downstream outage> | Medium | <alert >10 min, on-call playbook, retry backoff> | <DevOps> |
| <e.g. No event-schema versioning in v1> | Medium | <ADR-NNNN planned for v2, tolerate unknown fields> | <Backend> |
| Open architectural decision: <decision-headline> | Open question | Resolve before <stage trigger or YYYY-MM-DD>; <inline rationale from the Save-as-OQ> | <owner> |

**Accepted debt (acceptable in v1, plan to fix later):**
- <e.g. the entity is immutable / unversioned — OK for v1, may need audit versioning in v2>

## 12. Glossary

<!-- 🎯 Why: ⭐ the DOMAIN GLOSSARY that ends arguments a year later («checkpoint — weekly or
     biweekly? quarter — calendar or fiscal?»).
     📋 Write: a term / meaning table. Business + technical terms mixed.
     📌 e.g. «Lesson | a unit inside a course made of blocks (text, video)». -->

| Term | Meaning |
|---|---|
| <e.g. domain object A> | <its meaning in this domain> |
| <e.g. domain object B> | <its meaning> |
| <e.g. domain invariant name> | <the rule, in plain language> |
