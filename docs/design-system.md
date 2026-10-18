---
status: Living
tool: code
figma_file: ""
pen_file: ""
updated_at: "2026-10-11"
---

# Design system — AlgoTradeDynamics

> The project's **design canon**, produced once per repo by `design-system` and read by
> `ux-flows` / `screens` / `implement` / `review`. Committed — the tool choice and the inventory
> are team-wide, not per-developer. `architecture-map.md` §Frontend / UI foundation stays the
> inventory of the **code**; this file is the **design-side** canon (tool, posture, tokens,
> component inventory, cross-screen conventions). Refresh via `/sdd:design-system` when the
> foundation changes.

## Platform posture

- **Posture:** desktop-first — the product is an operator trading console (bots, backtests, audit, compliance) built around wide data tables (`min-width` 900–1180px, e.g. `FE/src/pages/BacktestsPage/BacktestsPage.module.css:1` `.table`) and multi-column form grids; mobile is a graceful collapse, not a design target.
- **Breakpoints / device classes:** desktop ≥ 1000px (content capped by `--container: 1240px`, `FE/src/styles/variables.css:83`); tablet collapse at `max-width: 1000–1100px` (grids → 2 columns); phone collapse at `max-width: 680px` (grids → 1 column, hero stacks). Wide tables scroll horizontally inside `.tableWrap` instead of reflowing. Breakpoints are page-local `@media` rules today, not tokens.

## Design tool

- **Tool:** code — no shared design file exists and the team is small; screens are inline markdown wireframes in each feature's `screens.md`, composed from the in-repo components below.
- **Library location:** the in-repo components are the library — `FE/src/components/ui/`, `FE/src/components/trading/`, styled by the tokens in `FE/src/styles/variables.css`.

## Token source

All tokens are CSS custom properties on `:root` in `FE/src/styles/variables.css`; globals and base typography in `FE/src/styles/globals.css`. **Dark theme only** — there is no light palette. A screen never re-declares a token inline (existing page CSS that hard-codes hex values such as `#f05b63` / `#77b5ff` is legacy debt, not precedent).

- **Colors:** `FE/src/styles/variables.css:7-42` — backgrounds `--bg-0..2`, surfaces `--surface` / `--surface-strong` / `--surface-elevated`, lines `--line` / `--line-strong`, text `--text-primary/secondary/tertiary/muted`, semantic `--accent` (emerald, success / primary action), `--danger` (coral, errors / destructive / loss), `--warning` (amber), each with `-glow` / `-soft` variants; gradients `--gradient-*`.
- **Spacing / sizing:** `FE/src/styles/variables.css:50-84` — `--space-1..10` (4 → 128px), radii `--r-sm..2xl`, `--r-full`, shadows `--shadow-sm/md/lg/glow`, motion `--ease-*` / `--t-fast/base/slow`, layout `--container`, `--nav-height`.
- **Typography:** `FE/src/styles/variables.css:45-47` — `--font-display` / `--font-body` (Manrope), `--font-mono` (JetBrains Mono — used for all numeric/market values: prices, qty, PnL, ids).

## Component inventory

| Component | Source (`file:line` / node / URL) | States it supports | Notes |
|---|---|---|---|
| Button | `FE/src/components/ui/Button/Button.jsx:4` | variants primary / danger / ghost / link; sizes sm / md / lg; default / hover / disabled / loading («Working…» label) | optional `icon` / `iconRight` (lucide-react) |
| Card | `FE/src/components/ui/Card/Card.jsx:3` | default | polymorphic `as`; the container for forms, empty states, panels |
| CardHeader | `FE/src/components/ui/Card/Card.jsx:11` | with / without `action` | `eyebrow` + `title` + right-aligned action slot |
| ConfirmModal | `FE/src/components/ui/ConfirmModal/ConfirmModal.jsx:6` | closed / open; neutral / danger (`isDanger`) | opened imperatively via `useConfirmModal()` (`FE/src/context/ConfirmModalContext.jsx:5`); Esc / backdrop cancel |
| ConnectionStatus | `FE/src/components/ui/ConnectionStatus/ConnectionStatus.jsx:3` | live / reconnecting / offline | WebSocket health pill |
| SideBadge | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:6` | buy / sell | |
| StatusBadge | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:24` | order / bot status values, unknown → raw text or «—» | |
| OrderTypeBadge | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:56` | order types; `reduceOnly` marker | |
| RoleBadge | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:68` | role inferred from `linkId` | |
| EventBadge | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:91` | runtime event types | |
| PnlValue | `FE/src/components/trading/TradingBadges/TradingBadges.jsx:111` | positive / negative / zero | mono, colored by sign |
| Navbar | `FE/src/components/layout/Navbar/Navbar.jsx:10` | guest / authenticated | app chrome — screens do not redraw it |
| Footer | `FE/src/components/layout/Footer/Footer.jsx:30` | default | app chrome |

**Gaps (no primitive yet — page-local CSS today):** form field (label + input/select), inline alert/state banner, data table, stat tile, tabs. A `screens.md` that needs one either reuses the closest page precedent (form fields: `FE/src/pages/BacktestsPage/BacktestsPage.module.css:1` `.formGrid`; state banner: `FE/src/pages/BotsPage/BotsPage.module.css:33` `.state` / `:42` `.stateError`) or declares `NEW: <name>` with a justification, which `implement` then registers here.

## Interaction & writing conventions

- **Errors:** inline page-level banner above the content (`.stateError`, `FE/src/pages/BotsPage/BotsPage.module.css:42`, danger-soft background); no toast system. Destructive or irreversible actions confirm through `ConfirmModal` with `isDanger`.
- **Empty states:** a `Card` with a lucide icon + short heading + one secondary sentence + one primary `Button` CTA (`FE/src/pages/BotsPage/BotsPage.jsx:574-579`); in tables, a dashed-border centered text block (`.empty`).
- **Loading:** plain text state line («Loading bots…», `FE/src/pages/BotsPage/BotsPage.jsx:569`) or a spinning lucide icon; action buttons use `Button loading`. No skeletons.
- **Validation:** on submit; the submit `Button` shows `loading`, server errors surface in the page-level error banner. Field-level error attachment is not standardized yet — a feature that needs it declares it in `screens.md`.
- **Microcopy tone:** terse, operator-facing, factual; every string is bilingual via `tr('<uk>', '<en>')` from `useLanguage()` (`FE/src/context/LanguageContext.jsx:38`) — Ukrainian first, English second; never hard-code a single-language string.
