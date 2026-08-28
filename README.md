# MR.DIY Thailand — Enterprise Solar PV Monitoring System

Phase 1 (MVP). Fleet monitoring for MR.DIY Thailand rooftop solar: multi-brand
telemetry ingestion, string-level fault detection, a GIS fleet map, and
financial/ESG reporting.

---

## Quick start

```bash
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` at minimum, then:

```bash
docker compose up -d --build
```

| Service | URL |
|---|---|
| Map | http://localhost:3000/map |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |

Load the real store roster (153 sites from the operations workbook):

```bash
docker compose exec -T db psql -U solarcell -d solarcell -f /seed/01_tariffs.sql
```

```bash
docker compose exec -T db psql -U solarcell -d solarcell -f /seed/02_stores.sql
```

Optional demo telemetry, so the map shows more than "everything offline":

```bash
docker compose exec -T db psql -U solarcell -d solarcell -f /seed/03_demo_telemetry.sql
```

The map also runs without a backend at http://localhost:3000/map?mock=1, which
generates 200 synthetic sites.

---

## The Phase 2 handover, in one page

Phase 1 runs on local PostgreSQL + TimescaleDB with authentication bypassed.
Phase 2 moves to the corporate SQL server with Active Directory, done by
corporate IT. The architecture exists to make that a **configuration change plus
three new classes**, never an edit to business logic, API routes, or the
frontend.

### The three seams

| Concern | Interface | Phase 1 | Phase 2 |
|---|---|---|---|
| Database | `app/domain/repositories.py` → `StoreRepositoryInterface` | `LocalPostgresRepository` | new class against the corporate DB |
| Auth | `app/core/auth/base.py` → `AuthProviderInterface` | `MockAuthProvider` | `EnterpriseSSOProvider` (stub in place) |
| Secrets | `app/core/secrets/base.py` → `SecretsProviderInterface` | `EnvSecretsProvider` | `VaultSecretsProvider` (implemented) |

All three are selected in **one file**: [`backend/app/core/deps.py`](backend/app/core/deps.py).

### The rule that makes it work

`app/domain/` must never import SQLAlchemy, asyncpg, FastAPI, or anything else
tied to storage or transport. It holds plain Python: entities, filters, the
repository contracts, and the PR classification rule.

This is enforced, not merely documented — `tests/test_domain_purity.py` parses
every domain module and fails on a forbidden import. If someone adds a
convenient `from sqlalchemy import ...` to a domain model, CI catches it
immediately rather than Phase 2 discovering it months later.

```bash
docker compose exec backend python -c "import app.domain.repositories"
```

That must succeed with no database present.

### Switching auth on

```bash
AUTH_MODE=enterprise_sso
```

Every endpoint already declares `Depends(get_current_user)`, so nothing needs to
be retrofitted route by route. Until `EnterpriseSSOProvider` is implemented the
API returns a clear `501 Not Implemented` rather than crashing — which is how
the seam can be verified before the work starts.

---

## Layout

```
db/init/        01_extensions.sql · 02_schema.sql   ← the schema, annotated
db/seed/        generate_seed.py  · *.sql           ← regenerated from the workbook
backend/app/
  domain/       entities, filters, repository interfaces, PR rule  (pure)
  infrastructure/  ORM, session, LocalPostgresRepository           (PostgreSQL only)
  core/         config, deps, auth/, secrets/
  analytics/    string_variance.py                  (Intra-String Peer Comparison)
  engines/      carbon.py                           (TGO emission factor)
  services/     dashboard_service.py
  api/v1/       stores · dashboard · health
frontend/src/
  components/map/  StoreMap.tsx · MapLegend.tsx · StoreDetailPanel.tsx
  lib/          pr-status.ts · api.ts · mock-data.ts
```

---

## Data model notes

Eight tables; `telemetry_raw`, `telemetry_string` and `weather_data` are
TimescaleDB hypertables (7-day chunks; 30-day for weather).

Two columns carry more weight than their size suggests:

- **`telemetry_string.mppt_index`** — Intra-String Peer Comparison is only valid
  between strings on the *same* MPPT. Separate MPPTs track independently and
  legitimately differ, so comparing across them produces constant false alarms.
- **`data_adapters.secrets_ref`** — a Vault *lookup key*, never a credential.
  Credentials are fetched in memory at use time. A database backup must not be a
  credential leak.

`stores.lat` / `stores.lng` are nullable, and the database enforces that they
are set as a pair. 35 of the 153 real sites currently have no coordinates; the
map reports how many it omitted rather than hiding the gap.

Compression, retention and continuous-aggregate policies are written out and
commented in `02_schema.sql`, switched off for the pilot. Enable compression
*before* the dataset grows — retrofitting it is far slower.

---

## Known data issues in the source workbook

`Solar Report.xlsx` drives `db/seed/`. Two problems were found and worked around:

1. **The `Long` column duplicates `Lat` in all 153 rows.** Using it would place
   every store off the coast of West Africa. `generate_seed.py` therefore takes
   coordinates from the `Sheet13` tab (1,190 verified store positions), joined
   on the 4-letter Name Code, and validates every pair against Thailand's
   bounding box.
2. **35 sites have no coordinates at all** and load with `NULL` lat/lng. They
   appear in listings and the dashboard but not on the map.

`Store code` is populated for only 54 of 153 rows, so the 4-letter **Name Code**
is used as `store_code` (the natural key) and the retail code is stored
separately as nullable `retail_store_code`.

## Keeping the roster current — the BaseInfo folder

Drop `Solar Report.xlsx` into **`BaseInfo/`**. The `importer` service watches
that folder and updates the database whenever the file changes.
`BaseInfo/README.md` is the operator-facing guide.

```bash
docker compose exec backend python -m app.baseinfo          # dry run, writes nothing
```

```bash
docker compose exec backend python -m app.baseinfo --apply  # write the changes
```

```bash
docker compose logs -f importer                             # what the watcher did
```

What it guarantees:

- **Never deletes a store.** Telemetry references `store_id`; a branch that
  disappears from the workbook is reported and left alone, because the usual
  cause is an unfilled row rather than a closure.
- **Never writes a row that fails validation.** Rejected rows are listed with
  their spreadsheet row number and the reason. This is what makes unattended
  auto-import safe.
- **Never touches `is_active`.** Whether a branch is live is an operational
  decision, not a spreadsheet cell.
- **Idempotent.** Coordinates are rounded to the column's `NUMERIC(9,6)`
  precision before comparison, so re-running reports no phantom changes.

`db/seed/generate_seed.py` is superseded by this and kept only for offline
bootstrap SQL.

---

## Vendor clouds — two topologies, two fault rules

The fleet reports through two different kinds of hardware, and the per-panel
fault detection is not the same on each. `devices.measurement_basis` records
which, and the analytics layer branches on it.

| | Huawei FusionSolar | Atmoce Cloud |
|---|---|---|
| Hardware | String inverter | Microinverter (one per panel) |
| `device_type` | `INVERTER` | `MICROINVERTER` |
| `measurement_basis` | `STRING` | `PANEL` |
| Per-string I/V | Yes | **No — none exists** |
| Comparison | Current, within one MPPT | Power, across panels at one site |
| Function | `analyse_device_strings()` | `analyse_site_panels()` |

The Atmoce limitation is not an oversight: the Atmoce-Cloud API Reference
v1.2.2 was checked in full and contains **zero** occurrences of `MPPT`, and its
only voltage fields are `gridVoltage` / `gridVoltageA-C` on the gateway. Per
panel it publishes `pvData[].pvPower` and cumulative generation, and nothing
else. The specification's I/V comparison is therefore impossible on that
hardware, and power is the correct substitute — panels at one site share
irradiance, which is what makes them comparable.

One caveat before acting on a panel result: panels on differently oriented roof
planes do not share irradiance and will deviate for entirely healthy reasons.
Where a site has multiple orientations, feed one plane at a time.

### API quota — the binding constraint on the whole ingestion design

Atmoce allows **10,000 calls per month per token** and 5 concurrent calls.
Polling each site individually every 15 minutes would need ~440,000 — 44x over.
Two things make it fit:

- `getSitesLastPower` accepts **up to 100 site IDs per call**, so a full fleet
  sweep is 2 calls rather than 127.
- Polling runs only during daylight (`INGESTION_DAYLIGHT_START/END`). PV output
  at night is zero, so off-window polling buys nothing.

| Cadence | Calls/month | Quota used |
|---|---|---|
| **15 min (default)** | 2,760 | 28% |
| 30 min | 1,380 | 14% |
| 60 min | 660 | 7% |

15 minutes is the default because it matches Atmoce's own collection interval —
its API publishes 96 points a day, so an hourly poll discards three of every
four for a quota saving that is not needed.

`DEVICE_OFFLINE_AFTER_MINUTES` is **derived** from the poll interval (2.5x)
rather than set independently. The two are not free parameters: a site can never
look fresher than the rate at which it is polled, so pinning 15 minutes while
polling hourly marks the entire fleet RED forever — an alarm that reports the
poll schedule instead of the plant. Leave the variable blank unless overriding
deliberately.

## Things that need real values before they can be trusted

- **`tariffs.off_peak_rate` is NULL and `demand_charge_rate` is 0.** The
  workbook publishes on-peak rates only. These must be entered from the official
  PEA/MEA tariff announcements before any TOU savings figure means anything. A
  guessed number here corrupts every downstream financial report silently, which
  is why they were left empty rather than estimated.
- **`SOLCAST_API_KEY` is unset.** Without an irradiance baseline, PR cannot be
  computed and pins show as `UNKNOWN` (grey) rather than a colour. The system
  runs fine without it; it just cannot report performance.

---

## Verification

```bash
docker compose exec backend python -m pytest tests/ -q
```

Checks that matter beyond the unit tests:

| What | How |
|---|---|
| Schema applied | `docker compose exec db psql -U solarcell -d solarcell -c "\dt"` |
| Hypertables | `SELECT hypertable_name FROM timescaledb_information.hypertables;` |
| `init.sql` is idempotent | re-run it against a populated database; it must exit 0 |
| Domain layer is pure | `docker compose exec backend python -c "import app.domain.repositories"` |
| Auth seam | set `AUTH_MODE=enterprise_sso`; expect `501`, not a crash |
| No hardcoded secrets | `grep -rniE '(password\|api_key)\s*=\s*["'"'"'][A-Za-z0-9_-]{8,}' backend/app` |

---

## Not in Phase 1

Corporate AD/SSO implementation · corporate database connection · live OEM API
credentials (adapters and the Playwright scraper are scaffolded, but real keys
are needed to test) · compression/retention policies · CI/CD · advanced demand
charge optimisation.
