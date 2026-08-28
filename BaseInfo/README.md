# BaseInfo — drop the operations workbook here

Put **`Solar Report.xlsx`** in this folder. The system reads the
**`Solar Store Info`** sheet and updates the database from it.

## What gets read

| Column | Goes to | Notes |
|---|---|---|
| `Name Code` | `stores.store_code` | The key. 4 letters. Required. |
| `Name` | `stores.store_name` | Required. |
| `Store code` | `stores.retail_store_code` | Optional (B105 etc.) |
| `Elec Zone` + `Tariff Rate` + `On-Peak` | `tariffs` | A new rate card is created if unseen. |
| `Address`, blank column K | `address`, `province` | |
| **`Lat`, `Long`** | `stores.lat/lng` | See below. |
| `Capacity(kW)` | `stores.installed_kwp` | Required, must be > 0. |
| `Phase` | `stores.rollout_phase` | |
| `Before Vat`, `Vat 7%`, `Net` | CapEx columns | |
| `On System` | `stores.commissioned_at` | |

## Coordinates

Fill **both** `Lat` and `Long`. A position is accepted only when:

- both cells are filled,
- the two values are **different** — an earlier version of this workbook had
  `Long` holding a copy of `Lat` in every row, which puts every pin in the Gulf
  of Guinea, so that exact case is now rejected, and
- the point falls inside Thailand.

If the pair fails any of those, the importer falls back to the coordinates in
`Sheet13`, and tells you it did. If neither source has a usable position the
store still imports — it simply does not appear on the map, and the count of
such stores is reported.

## Running it

Dry run first. It writes nothing and prints exactly what would change:

```bash
docker compose exec backend python -m app.baseinfo
```

Then apply:

```bash
docker compose exec backend python -m app.baseinfo --apply
```

The `importer` service in `docker-compose.yml` watches this folder and imports
automatically when the file changes. Its log shows every import:

```bash
docker compose logs -f importer
```

## What it will and will not do

- **Adds** stores that are new in the workbook.
- **Updates** stores whose details changed, listing each field it changed.
- **Never deletes.** A store in the database but missing from the workbook is
  reported and left alone — telemetry history is attached to it, and the usual
  cause is a row nobody filled in rather than a closed branch. If a branch really
  has closed, deactivate it deliberately rather than by deleting a spreadsheet row.
- **Never imports a row that fails validation.** Bad rows are listed with their
  row number and the reason.
- **Never touches `is_active`.** Whether a branch is live is an operational
  decision, not a spreadsheet cell.

## Files here are not committed

`.gitignore` excludes the workbooks: they contain branch addresses and
investment figures. Only this README is tracked.
