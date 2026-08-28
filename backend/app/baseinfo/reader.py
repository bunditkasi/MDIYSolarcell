"""Read and validate the operations workbook.

Single source of truth for parsing ``Solar Report.xlsx``. Nothing else in the
codebase opens that file.

WHY VALIDATION IS SEPARATE FROM IMPORT
--------------------------------------
The importer runs automatically when the file changes, which means nobody is
watching when it runs. Every record therefore has to earn its way in: a row that
fails validation is REJECTED and reported, never written. A silent bad import is
far worse than a skipped one — the numbers still look plausible on the map, and
the error surfaces weeks later in a report nobody can reconcile.

The coordinate rules exist because of a real defect in this workbook: the
``Long`` column has held a copy of ``Lat`` in every row, which would place the
whole Thai fleet off the coast of West Africa. That specific failure is checked
for by name.

Standard library only — openpyxl is not a dependency.
"""

from __future__ import annotations

import datetime as dt
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = [
    "READ_SHEET",
    "Severity",
    "StoreRecord",
    "ValidationIssue",
    "WorkbookReadResult",
    "read_workbook",
]

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

#: The sheet the operations team maintains.
READ_SHEET = "Solar Store Info"

#: Fallback coordinate master: every MR.DIY store code with a verified position.
COORDINATE_SHEET = "Sheet13"

#: stores.lat / stores.lng are NUMERIC(9,6). Coordinates are rounded to match
#: BEFORE comparison, otherwise a workbook value of 13.5864629 never equals the
#: stored 13.586463 and the importer reports the same seven stores as "changed"
#: on every run, forever — noise that trains people to ignore the diff.
#:
#: Six decimal places is ~0.11 m at this latitude. Nothing is lost that matters
#: for a store pin.
COORDINATE_PLACES = Decimal("0.000001")

#: Thailand, with a small margin. Anything outside is a data-entry error, not a
#: branch — the nearest real MR.DIY store to these bounds is well inside them.
TH_LAT_MIN, TH_LAT_MAX = Decimal("5.0"), Decimal("21.0")
TH_LNG_MIN, TH_LNG_MAX = Decimal("97.0"), Decimal("106.5")

# Column positions in "Solar Store Info".
C_NAME_CODE = 1
C_PHASE = 2
C_RETAIL_CODE = 3
C_NAME = 5
C_ELEC_ZONE = 6
C_TARIFF_RATE = 7
C_ON_PEAK = 8
C_ADDRESS = 9
C_PROVINCE = 10
C_LAT = 11
C_LNG = 12
C_CAPACITY = 13
C_CAPEX_BEFORE_VAT = 14
C_CAPEX_VAT = 15
C_CAPEX_NET = 16
C_ON_SYSTEM = 17


class Severity(str, Enum):
    #: The row is rejected and never written.
    ERROR = "ERROR"
    #: The row is imported, but something needs a human's attention.
    WARNING = "WARNING"


class CoordinateSource(str, Enum):
    SHEET = "SHEET"
    FALLBACK = "FALLBACK"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    row_number: int
    store_code: str
    field: str
    severity: Severity
    message: str

    def __str__(self) -> str:
        return (
            f"row {self.row_number:>4} [{self.store_code or '?':<6}] "
            f"{self.severity.value:<7} {self.field}: {self.message}"
        )


@dataclass(frozen=True, slots=True)
class StoreRecord:
    row_number: int
    store_code: str
    store_name: str
    retail_store_code: str | None = None
    province: str | None = None
    address: str | None = None
    installed_kwp: Decimal | None = None
    lat: Decimal | None = None
    lng: Decimal | None = None
    coordinate_source: CoordinateSource = CoordinateSource.NONE
    utility: str | None = None
    tariff_code: str | None = None
    on_peak_rate: Decimal | None = None
    rollout_phase: int | None = None
    commissioned_at: dt.date | None = None
    capex_before_vat: Decimal | None = None
    capex_vat: Decimal | None = None
    capex_net: Decimal | None = None

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lng is not None


@dataclass
class WorkbookReadResult:
    records: list[StoreRecord] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    rejected: list[ValidationIssue] = field(default_factory=list)
    rows_seen: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def with_location(self) -> int:
        return sum(1 for r in self.records if r.has_location)

    def summary(self) -> str:
        return (
            f"{self.rows_seen} rows read, {len(self.records)} accepted "
            f"({self.with_location} with coordinates), "
            f"{len(self.rejected)} rejected, {len(self.warnings)} warnings"
        )


# --------------------------------------------------------------------------- #
# Minimal xlsx reading
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


def _sheet_paths(z: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}

    out: dict[str, str] = {}
    sheets_el = workbook.find("m:sheets", NS)
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
# Cell coercion
# --------------------------------------------------------------------------- #


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    text = " ".join(row[index].split())
    if text.startswith("#"):  # #N/A, #REF!, #VALUE!
        return ""
    # Excel wraps some multi-line addresses in literal quotes.
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _dec(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _int(value: str) -> int | None:
    number = _dec(value)
    return None if number is None else int(number)


def _excel_date(value: str) -> dt.date | None:
    """Excel serial -> date. Excel's epoch is 1899-12-30."""
    serial = _int(value)
    if serial is None or serial <= 0:
        return None
    try:
        return dt.date(1899, 12, 30) + dt.timedelta(days=serial)
    except OverflowError:
        return None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_workbook(path: str | Path) -> WorkbookReadResult:
    """Parse and validate the workbook.

    Rows failing validation appear in ``rejected`` and are absent from
    ``records`` — the caller cannot accidentally write them.
    """
    workbook = Path(path)
    result = WorkbookReadResult()

    with zipfile.ZipFile(workbook) as z:
        shared = _shared_strings(z)
        sheets = _sheet_paths(z)

        if READ_SHEET not in sheets:
            result.issues.append(
                ValidationIssue(
                    0,
                    "",
                    "workbook",
                    Severity.ERROR,
                    f"Sheet {READ_SHEET!r} not found. Present: {sorted(sheets)}",
                )
            )
            result.rejected = result.errors
            return result

        rows = _rows(z, sheets[READ_SHEET], shared)
        fallback = (
            _coordinate_fallback(_rows(z, sheets[COORDINATE_SHEET], shared))
            if COORDINATE_SHEET in sheets
            else {}
        )

    seen_codes: dict[str, int] = {}

    for offset, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        result.rows_seen += 1

        store_code = _cell(row, C_NAME_CODE)
        issues: list[ValidationIssue] = []

        def problem(
            field_name: str,
            severity: Severity,
            message: str,
            # Bound at definition time. A closure over the loop variables would
            # be correct only because it happens to be called within the same
            # iteration — a latent bug the moment anyone defers a call.
            _row: int = offset,
            _code: str = store_code,
            _sink: list[ValidationIssue] = issues,
        ) -> None:
            _sink.append(ValidationIssue(_row, _code, field_name, severity, message))

        # -- Identity ----------------------------------------------------- #
        if not store_code:
            problem("store_code", Severity.ERROR, "Name Code is blank")
        elif store_code in seen_codes:
            problem(
                "store_code",
                Severity.ERROR,
                f"duplicate of row {seen_codes[store_code]}",
            )
        else:
            seen_codes[store_code] = offset

        store_name = _cell(row, C_NAME)
        if not store_name:
            problem("store_name", Severity.ERROR, "Name is blank")

        # -- Capacity ----------------------------------------------------- #
        installed_kwp = _dec(_cell(row, C_CAPACITY))
        if installed_kwp is None:
            problem(
                "installed_kwp", Severity.ERROR, "Capacity(kW) is blank or unreadable"
            )
        elif installed_kwp <= 0:
            problem(
                "installed_kwp",
                Severity.ERROR,
                f"Capacity must be positive, got {installed_kwp}",
            )

        # -- Coordinates -------------------------------------------------- #
        lat, lng, source, coord_issues = _resolve_coordinates(
            offset, store_code, _cell(row, C_LAT), _cell(row, C_LNG), fallback
        )
        issues.extend(coord_issues)

        # -- Tariff ------------------------------------------------------- #
        utility = _cell(row, C_ELEC_ZONE) or None
        if utility and utility not in {"PEA", "MEA"}:
            problem("utility", Severity.WARNING, f"unexpected Elec Zone {utility!r}")
            utility = None

        tariff_code = _cell(row, C_TARIFF_RATE)
        if tariff_code.endswith(".0"):
            tariff_code = tariff_code[:-2]

        result.issues.extend(issues)

        if any(i.severity is Severity.ERROR for i in issues):
            result.rejected.extend(i for i in issues if i.severity is Severity.ERROR)
            continue

        result.records.append(
            StoreRecord(
                row_number=offset,
                store_code=store_code,
                store_name=store_name,
                retail_store_code=_cell(row, C_RETAIL_CODE) or None,
                province=_cell(row, C_PROVINCE) or None,
                address=_cell(row, C_ADDRESS) or None,
                installed_kwp=installed_kwp,
                lat=lat,
                lng=lng,
                coordinate_source=source,
                utility=utility,
                tariff_code=tariff_code or None,
                on_peak_rate=_dec(_cell(row, C_ON_PEAK)),
                rollout_phase=_int(_cell(row, C_PHASE)),
                commissioned_at=_excel_date(_cell(row, C_ON_SYSTEM)),
                capex_before_vat=_dec(_cell(row, C_CAPEX_BEFORE_VAT)),
                capex_vat=_dec(_cell(row, C_CAPEX_VAT)),
                capex_net=_dec(_cell(row, C_CAPEX_NET)),
            )
        )

    return result


def _coordinate_fallback(rows: list[list[str]]) -> dict[str, tuple[Decimal, Decimal]]:
    """Build the store-code -> position map from the coordinate master sheet."""
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for row in rows[1:]:
        code = _cell(row, 0)
        lat, lng = _dec(_cell(row, 1)), _dec(_cell(row, 2))
        if code and lat is not None and lng is not None and _in_thailand(lat, lng):
            out[code] = (lat, lng)
    return out


def _in_thailand(lat: Decimal, lng: Decimal) -> bool:
    return TH_LAT_MIN <= lat <= TH_LAT_MAX and TH_LNG_MIN <= lng <= TH_LNG_MAX


def _round_coordinate(value: Decimal) -> Decimal:
    """Round to the precision the database column actually holds."""
    return value.quantize(COORDINATE_PLACES)


def _resolve_coordinates(
    row_number: int,
    store_code: str,
    lat_text: str,
    lng_text: str,
    fallback: dict[str, tuple[Decimal, Decimal]],
) -> tuple[Decimal | None, Decimal | None, CoordinateSource, list[ValidationIssue]]:
    """Pick a trustworthy position, or none at all.

    Order: the sheet's own Lat/Long if they survive validation, then the
    coordinate master sheet, then nothing. A store with no position is loaded
    and simply does not appear on the map — that is honest, whereas guessing a
    position is not.
    """
    issues: list[ValidationIssue] = []

    def note(severity: Severity, message: str) -> None:
        issues.append(
            ValidationIssue(row_number, store_code, "coordinates", severity, message)
        )

    lat, lng = _dec(lat_text), _dec(lng_text)

    if lat is not None and lng is not None:
        # The defect this workbook actually had: Long holding a copy of Lat.
        # Checked by name because it is silent otherwise — both values parse,
        # both look like numbers, and the pin lands in the Gulf of Guinea.
        if lat == lng:
            note(
                Severity.WARNING,
                f"Lat and Long are identical ({lat}); the Long column looks unfilled. "
                f"Using the {COORDINATE_SHEET} fallback instead.",
            )
        elif not _in_thailand(lat, lng):
            note(
                Severity.WARNING,
                f"({lat}, {lng}) is outside Thailand; falling back to {COORDINATE_SHEET}.",
            )
        else:
            return (
                _round_coordinate(lat),
                _round_coordinate(lng),
                CoordinateSource.SHEET,
                issues,
            )

    elif lat is not None or lng is not None:
        note(
            Severity.WARNING,
            "only one of Lat/Long is filled in; a position needs both.",
        )

    if store_code in fallback:
        flat, flng = fallback[store_code]
        return (
            _round_coordinate(flat),
            _round_coordinate(flng),
            CoordinateSource.FALLBACK,
            issues,
        )

    note(
        Severity.WARNING,
        "no usable coordinates in either sheet — this store will not appear on the map.",
    )
    return None, None, CoordinateSource.NONE, issues
