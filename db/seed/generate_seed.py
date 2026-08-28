"""SUPERSEDED — use `python -m app.baseinfo` instead.

Kept only for generating bootstrap .sql files offline, without a database. The
live path is now the BaseInfo importer, which validates, diffs against what is
stored, and never deletes:

    docker compose exec backend python -m app.baseinfo            # dry run
    docker compose exec backend python -m app.baseinfo --apply    # write

Do not add features here. Two independent parsers of the same workbook would
drift apart, and the importer is the one with the validation rules.

Generate SQL seed files from the operations workbook (Solar Report.xlsx).

    python db/seed/generate_seed.py "C:/Users/Bundi/Downloads/Solar Report.xlsx"

Writes  db/seed/01_tariffs.sql  and  db/seed/02_stores.sql.

WHY THIS IS A SCRIPT AND NOT HAND-WRITTEN SQL
---------------------------------------------
The roster is maintained continuously in the workbook, and the fleet grows by
roughly 200 stores a year. Re-running this is how the seed stays current; typing
153 INSERT statements by hand would be stale within a week.

COORDINATE HANDLING — read this before changing anything
--------------------------------------------------------
The "Solar Store Info" sheet has Lat and Long columns, but the Long column
contains a copy of Lat in all 153 rows (a broken lookup). Using it would place
every store in the Gulf of Guinea, since a longitude of ~13 is not in Thailand.

"Sheet13" is the real coordinate master: 1,190 store codes with genuine
lat/lng pairs. This script therefore joins Solar Store Info -> Sheet13 on the
4-letter Name Code and IGNORES the Info sheet's Lat/Long columns entirely.

Every coordinate is then validated against Thailand's bounding box. Anything
outside it is dropped rather than loaded, and reported at the end.

Depends only on the standard library — openpyxl is not required.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

# Thailand's extent, with a small margin. Used to reject bad coordinates.
TH_LAT_MIN, TH_LAT_MAX = 5.0, 21.0
TH_LNG_MIN, TH_LNG_MAX = 97.0, 106.5

# "Solar Store Info" column positions.
C_NAME_CODE = 1
C_PHASE = 2
C_RETAIL_CODE = 3
C_NAME = 5
C_ELEC_ZONE = 6
C_TARIFF_RATE = 7
C_ON_PEAK = 8
C_ADDRESS = 9
C_PROVINCE = 10
C_CAPACITY = 13
C_CAPEX_BEFORE_VAT = 14
C_CAPEX_VAT = 15
C_CAPEX_NET = 16
C_ON_SYSTEM = 17


# --------------------------------------------------------------------------- #
# Minimal xlsx reader
# --------------------------------------------------------------------------- #


def _col_index(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref)
    if match is None:
        return 0
    n = 0
    for ch in match.group(1):
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")) for si in root]


def _sheets(z: zipfile.ZipFile) -> dict[str, str]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    out: dict[str, str] = {}
    sheets_el = wb.find("m:sheets", NS)
    for sheet in [] if sheets_el is None else list(sheets_el):
        target = targets.get(sheet.get(f"{{{NS['r']}}}id"), "")
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        out[sheet.get("name") or ""] = target
    return out


def _rows(z: zipfile.ZipFile, path: str, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(z.read(path))
    result: list[list[str]] = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", NS):
            idx = _col_index(c.get("r") or "")
            ctype = c.get("t")
            v = c.find("m:v", NS)
            if ctype == "s" and v is not None and v.text is not None:
                i = int(v.text)
                cells[idx] = shared[i] if i < len(shared) else ""
            elif ctype == "inlineStr":
                is_el = c.find("m:is", NS)
                cells[idx] = (
                    "".join(t.text or "" for t in is_el.iter(f"{{{NS['m']}}}t"))
                    if is_el is not None
                    else ""
                )
            elif v is not None and v.text is not None:
                cells[idx] = v.text
        result.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) else ""


def sql_str(value: str | None) -> str:
    if value is None or value == "" or value.startswith("#N/A"):
        return "NULL"
    text = " ".join(value.split())
    # Several address cells are wrapped in literal double quotes by Excel's
    # multi-line handling; they are not part of the address.
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    if not text:
        return "NULL"
    return "'" + text.replace("'", "''") + "'"


def sql_num(value: str | None, places: int = 2) -> str:
    if not value or value.startswith("#"):
        return "NULL"
    try:
        return f"{round(float(value), places)}"
    except ValueError:
        return "NULL"


def sql_int(value: str | None) -> str:
    if not value or value.startswith("#"):
        return "NULL"
    try:
        return str(int(float(value)))
    except ValueError:
        return "NULL"


def excel_date(value: str | None) -> str:
    """Excel serial -> SQL DATE literal. Excel's epoch is 1899-12-30."""
    if not value or value.startswith("#"):
        return "NULL"
    try:
        serial = int(float(value))
    except ValueError:
        return "NULL"
    if serial <= 0:
        return "NULL"
    return "'" + (dt.date(1899, 12, 30) + dt.timedelta(days=serial)).isoformat() + "'"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    workbook = Path(sys.argv[1])
    if not workbook.is_file():
        print(f"ERROR: workbook not found: {workbook}")
        return 1

    out_dir = Path(__file__).resolve().parent

    with zipfile.ZipFile(workbook) as z:
        shared = _shared_strings(z)
        sheets = _sheets(z)
        info = _rows(z, sheets["Solar Store Info"], shared)
        coord_rows = _rows(z, sheets["Sheet13"], shared)
        solar_rows = _rows(z, sheets["Solar"], shared) if "Solar" in sheets else []

    # -- Coordinate master (Sheet13) -----------------------------------------
    coords: dict[str, tuple[float, float]] = {}
    rejected_coords: list[tuple[str, float, float]] = []
    for row in coord_rows[1:]:
        code, lat_s, lng_s = cell(row, 0), cell(row, 1), cell(row, 2)
        if not code:
            continue
        try:
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            continue
        if TH_LAT_MIN <= lat <= TH_LAT_MAX and TH_LNG_MIN <= lng <= TH_LNG_MAX:
            coords[code] = (lat, lng)
        else:
            rejected_coords.append((code, lat, lng))

    # -- Monitoring platform per site ('Solar' sheet) -------------------------
    monitoring: dict[str, str] = {}
    for row in solar_rows[1:]:
        source, code = cell(row, 0), cell(row, 1)
        if code and source:
            monitoring[code] = source

    # -- Tariffs --------------------------------------------------------------
    tariffs: dict[tuple[str, str], str] = {}
    for row in info[1:]:
        zone, code, on_peak = (
            cell(row, C_ELEC_ZONE),
            cell(row, C_TARIFF_RATE),
            cell(row, C_ON_PEAK),
        )
        if zone in {"PEA", "MEA"} and code and on_peak:
            # Normalise "8114.0" -> "8114"; Excel stored some codes as numbers.
            code = code[:-2] if code.endswith(".0") else code
            tariffs.setdefault((zone, code), on_peak)

    tariff_lines = [
        "-- =============================================================================",
        "-- 01_tariffs.sql  — GENERATED by db/seed/generate_seed.py. Do not edit by hand.",
        "--",
        "-- Source: Solar Report.xlsx, 'Solar Store Info' (Elec Zone / Tariff Rate / On-Peak).",
        "--",
        "-- off_peak_rate and demand_charge_rate are left NULL / 0 on purpose: the source",
        "-- workbook publishes on-peak rates only. Fill them in from the official PEA and",
        "-- MEA tariff announcements before trusting any TOU savings figure. A guessed",
        "-- number here would corrupt every downstream financial report silently.",
        "-- =============================================================================",
        "",
    ]
    for (zone, code), on_peak in sorted(tariffs.items()):
        tariff_lines.append(
            "INSERT INTO tariffs "
            "(tariff_name, tariff_code, utility, on_peak_rate, off_peak_rate, "
            "demand_charge_rate, effective_from)\n"
            f"VALUES ('{zone} {code}', '{code}', '{zone}', {sql_num(on_peak, 4)}, "
            "NULL, 0, DATE '2024-01-01')\n"
            "ON CONFLICT (utility, tariff_code, effective_from) DO NOTHING;"
        )
    tariff_lines.append("")
    (out_dir / "01_tariffs.sql").write_text("\n".join(tariff_lines), encoding="utf-8")

    # -- Stores ---------------------------------------------------------------
    store_lines = [
        "-- =============================================================================",
        "-- 02_stores.sql  — GENERATED by db/seed/generate_seed.py. Do not edit by hand.",
        "--",
        "-- Source: Solar Report.xlsx, 'Solar Store Info' joined to 'Sheet13' on Name Code.",
        "--",
        "-- Coordinates come from Sheet13, NOT from the Info sheet: that sheet's Long",
        "-- column holds a copy of Lat in every row. Stores with no Sheet13 entry are",
        "-- loaded with NULL lat/lng and simply do not appear on the map until real",
        "-- coordinates are supplied.",
        "-- =============================================================================",
        "",
    ]

    loaded = with_coords = without_coords = 0
    missing_codes: list[str] = []
    seen: set[str] = set()

    for row in info[1:]:
        code = cell(row, C_NAME_CODE)
        name = cell(row, C_NAME)
        capacity = cell(row, C_CAPACITY)
        if not code or not name or not capacity:
            continue
        if code in seen:
            continue
        seen.add(code)

        pair = coords.get(code)
        if pair:
            lat_sql, lng_sql = f"{pair[0]:.6f}", f"{pair[1]:.6f}"
            with_coords += 1
        else:
            lat_sql = lng_sql = "NULL"
            without_coords += 1
            missing_codes.append(code)

        zone = cell(row, C_ELEC_ZONE)
        tariff_code = cell(row, C_TARIFF_RATE)
        tariff_code = tariff_code[:-2] if tariff_code.endswith(".0") else tariff_code
        tariff_sql = (
            "(SELECT tariff_id FROM tariffs "
            f"WHERE utility = '{zone}' AND tariff_code = '{tariff_code}' LIMIT 1)"
            if zone in {"PEA", "MEA"} and tariff_code
            else "NULL"
        )

        store_lines.append(
            "INSERT INTO stores (store_code, retail_store_code, store_name, province,\n"
            "                    address, installed_kwp, lat, lng, tariff_id,\n"
            "                    rollout_phase, monitoring_source, commissioned_at,\n"
            "                    capex_before_vat, capex_vat, capex_net)\n"
            f"VALUES ({sql_str(code)}, {sql_str(cell(row, C_RETAIL_CODE))}, "
            f"{sql_str(name)}, {sql_str(cell(row, C_PROVINCE))},\n"
            f"        {sql_str(cell(row, C_ADDRESS))}, {sql_num(capacity)}, "
            f"{lat_sql}, {lng_sql}, {tariff_sql},\n"
            f"        {sql_int(cell(row, C_PHASE))}, {sql_str(monitoring.get(code))}, "
            f"{excel_date(cell(row, C_ON_SYSTEM))},\n"
            f"        {sql_num(cell(row, C_CAPEX_BEFORE_VAT))}, "
            f"{sql_num(cell(row, C_CAPEX_VAT))}, {sql_num(cell(row, C_CAPEX_NET))})\n"
            "ON CONFLICT (store_code) DO NOTHING;"
        )
        loaded += 1

    store_lines.append("")
    (out_dir / "02_stores.sql").write_text("\n".join(store_lines), encoding="utf-8")

    # -- Report ---------------------------------------------------------------
    print(f"tariffs written      : {len(tariffs)}")
    print(f"stores written       : {loaded}")
    print(f"  with coordinates   : {with_coords}")
    print(f"  WITHOUT coordinates: {without_coords}  (will not appear on the map)")
    if missing_codes:
        print(f"  missing codes      : {', '.join(sorted(missing_codes))}")
    if rejected_coords:
        print(f"rejected coordinates (outside Thailand): {len(rejected_coords)}")
    print(f"\nwrote {out_dir / '01_tariffs.sql'}")
    print(f"wrote {out_dir / '02_stores.sql'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
