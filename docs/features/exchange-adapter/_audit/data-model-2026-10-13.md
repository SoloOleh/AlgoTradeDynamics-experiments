# Audit — data-model — exchange-adapter — 2026-10-13

**Mode:** brownfield, size M, route standard (from `.size` / `.route`).
**Outcome:** **no schema change → 0 staged migrations.** This is a valid outcome, not a failure.
`docs/features/exchange-adapter/data-model.md` documents the existing tables the feature reads and
writes. `docs/features/exchange-adapter/migrations/` is not created, and nothing was written to the
live `BE/alembic/versions/` tree.

## Staged migrations

None. The data model has no `.up` / `.down` pair to promote.

**Promote-time convention hint (for any later schema change in this feature):** Alembic, Python
revision files `BE/alembic/versions/NNNN_snake_description.py` with `upgrade()` / `downgrade()`, so not
raw `.up.sql` / `.down.sql`. The repo is sequential, and the next number is about `0015`. `implement`
assigns the real number at promotion, because another feature may promote first. Migrations
auto-apply on backend start (`README.md`), so any future change here must be expand → backfill →
contract and safe for bots that are mid-cycle.

## Convention source and divergences

- `docs/architecture-map.md` §Conventions: Alembic, integer autoincrement PKs, `ix_<table>_<col>` index
  names. Corroborated against `BE/alembic/versions/0001…0014` and `BE/app/models/`.
- `sad.md` §4 pillar 3 + ADR-0003: frozen stored vocabulary, no data migration. §6 «Data-model hints»
  and §7 «Rollback»: no schema change.
- **Divergence: none.** The architecture and the repo agree.

## Verified claims from the SAD

| SAD claim | Verified against | Result |
|---|---|---|
| `order_link_id` is already indexed | `0005_add_bot_events_and_order_linking.py:36`, model `index=True` | ✅ |
| `incident_type` is free text, so new values need no migration | `incidents.incident_type VARCHAR(80)`, `0014` | ✅ |
| Venue derives from existing `exchange` + `environment` | `trading_bots.exchange VARCHAR(40)`, `environment VARCHAR(20)` | ✅ |
| Stored error types and statuses fit existing columns | `last_error_type VARCHAR(40)`, `status VARCHAR(30)` | ✅ |

**Note for `tasks` / `implement`:** `create_bot_incident` stores `incident_type.upper()[:80]`
(`BE/app/services/operations_service.py:104`). The guard-pause incidents will therefore persist as
`VENUE_UNSUPPORTED` / `CAPABILITY_MISSING`, not the lowercase spelling in `sad.md` §6 and §7. Tests
and incident filters must use the uppercase form.

## Drift detection (offline; the domain layer is SQLAlchemy ORM)

Method: the column names in `BE/app/models/*.py` (`Mapped[...] = mapped_column`) were compared with the
union of `create_table` / `add_column` / `drop_column` across `BE/alembic/versions/*.py`. This is a
static parse with no DB introspection, because the local Python has no SQLAlchemy installed.

| Table | field-without-column | column-without-field |
|---|---|---|
| `trading_bots` | — | — |
| `trading_bot_orders` | — | — |
| `trading_bot_events` | — | — |
| `incidents` | — | — |
| `operation_logs` | — | — |

**No drift. No `_drift/` fixes proposed.** The check covered names only. Type and nullability were
compared by reading the five tables above and no mismatch was seen. A full type and nullability diff
needs `alembic check` against a live DB, which can't run offline.

## Self-check (4 mandatory)

| Check | Result |
|---|---|
| Naming matches the repo's convention | ✅ N/A: no new identifiers |
| Down reversibility | ✅ N/A: no migrations |
| FK indexes | ⚠️ **Pre-existing gap, not introduced here:** `trading_bot_orders.parent_order_id` (FK → `trading_bot_orders(id)` `ON DELETE SET NULL`, migration `0005`) has no index. No code filters on it (it is only written: `grid_runtime.py:221,928`, `momentum.py:742`, `pattern_scalper.py:1157`). Its only cost is the SET NULL scan on parent delete, and bot deletion already cascades by `bot_id`. This feature adds no query on it, so **no migration is staged here**. Fix it later in its own change if it ever matters. All other FKs on the touched tables are indexed |
| Convention adherence | ✅ The model follows the detected conventions. No house style was imposed |

## Breaking-change decompositions

None.

## Open `<!-- TBD -->` items

- `data-model.md` → `trading_bots`: does a guard pause also write `last_error_type` /
  `last_error_action`, or only the event and incident? This is not a schema question (any value
  ≤ 40 chars fits). It goes to `tasks`.

## Next stage

`api exchange-adapter`. The API stage is **not** skippable: the connection check moves from the
unauthenticated `/api/bybit/test` to a venue-parameterised route that requires sign-in (`sad.md` §5),
which is a contract change.
