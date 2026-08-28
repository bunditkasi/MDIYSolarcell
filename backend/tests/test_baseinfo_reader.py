"""Workbook validation.

These matter more than most tests here: the importer runs unattended when the
file changes, so this is the only thing standing between a mistyped cell and the
production database.

Each test builds a real .xlsx in a temp directory rather than mocking the
parser — the parser is part of what is being tested.
"""

from __future__ import annotations

import zipfile
from decimal import Decimal
from pathlib import Path

from app.baseinfo.reader import (
    READ_SHEET,
    CoordinateSource,
    Severity,
    read_workbook,
)

# Column layout of "Solar Store Info", by index.
HEADER = [
    "No", "Name Code", "Phase", "Store code", "Name Code", "Name", "Elec Zone",
    "Tariff Rate", "On-Peak", "Address", "", "Lat", "Long", "Capacity(kW)",
    "Before Vat", "Vat 7%", "Net", "On System",
]


def _col_ref(index: int, row: int) -> str:
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"{letters}{row}"


def _sheet_xml(rows: list[list[str]]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r, row in enumerate(rows, start=1):
        parts.append(f'<row r="{r}">')
        for c, value in enumerate(row):
            if value == "":
                continue
            ref = _col_ref(c, r)
            escaped = (
                str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def make_workbook(
    tmp_path: Path,
    store_rows: list[list[str]],
    coordinate_rows: list[list[str]] | None = None,
) -> Path:
    """Write a minimal but genuine .xlsx with the two sheets we read."""
    path = tmp_path / "Solar Report.xlsx"
    sheets = [(READ_SHEET, "sheet1.xml")]
    if coordinate_rows is not None:
        sheets.append(("Sheet13", "sheet2.xml"))

    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>',
        )
        sheet_tags = "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
            for i, (name, _) in enumerate(sheets, start=1)
        )
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships"><sheets>{sheet_tags}</sheets></workbook>',
        )
        rel_tags = "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/{file}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            for i, (_, file) in enumerate(sheets, start=1)
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            f'package/2006/relationships">{rel_tags}</Relationships>',
        )
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml([HEADER, *store_rows]))
        if coordinate_rows is not None:
            z.writestr(
                "xl/worksheets/sheet2.xml",
                _sheet_xml([["Code", "Lat", "Long"], *coordinate_rows]),
            )
    return path


def store_row(
    code: str = "PABC",
    name: str = "MR.DIY Test Branch",
    capacity: str = "48.4",
    lat: str = "13.75",
    lng: str = "100.50",
    zone: str = "PEA",
    tariff: str = "3.2.2",
    on_peak: str = "4.1839",
) -> list[str]:
    row = [""] * 18
    row[1] = code
    row[2] = "3"
    row[3] = "B105"
    row[5] = name
    row[6] = zone
    row[7] = tariff
    row[8] = on_peak
    row[9] = "123 Test Road"
    row[10] = "Bangkok"
    row[11] = lat
    row[12] = lng
    row[13] = capacity
    return row


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_reads_a_valid_row(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row()]))

    assert len(result.records) == 1
    record = result.records[0]
    assert record.store_code == "PABC"
    assert record.installed_kwp == Decimal("48.4")
    assert record.lat == Decimal("13.75")
    assert record.lng == Decimal("100.50")
    assert record.coordinate_source is CoordinateSource.SHEET
    assert record.utility == "PEA"
    assert record.tariff_code == "3.2.2"
    assert not result.rejected


def test_blank_rows_are_ignored(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row(), [""] * 18]))
    assert result.rows_seen == 1


# --------------------------------------------------------------------------- #
# The coordinate defect this workbook actually had
# --------------------------------------------------------------------------- #


def test_long_holding_a_copy_of_lat_is_caught(tmp_path: Path) -> None:
    """The real defect: every row had Long == Lat, which would have put the
    whole Thai fleet in the Gulf of Guinea."""
    path = make_workbook(
        tmp_path,
        [store_row(lat="13.5864629", lng="13.5864629")],
        coordinate_rows=[["PABC", "13.5864629", "100.554984"]],
    )
    result = read_workbook(path)

    record = result.records[0]
    assert record.coordinate_source is CoordinateSource.FALLBACK
    assert record.lng == Decimal("100.554984"), "must not use the duplicated value"
    assert any("identical" in w.message for w in result.warnings)


def test_coordinates_outside_thailand_are_rejected(tmp_path: Path) -> None:
    path = make_workbook(
        tmp_path,
        [store_row(lat="48.85", lng="2.35")],  # Paris
        coordinate_rows=[["PABC", "13.75", "100.50"]],
    )
    result = read_workbook(path)

    assert result.records[0].coordinate_source is CoordinateSource.FALLBACK
    assert any("outside Thailand" in w.message for w in result.warnings)


def test_sheet_coordinates_win_when_they_are_valid(tmp_path: Path) -> None:
    """Once the team fills Lat/Long in properly, that sheet is authoritative —
    the fallback must not override a good value."""
    path = make_workbook(
        tmp_path,
        [store_row(lat="7.88", lng="98.39")],
        coordinate_rows=[["PABC", "13.75", "100.50"]],
    )
    result = read_workbook(path)

    record = result.records[0]
    assert record.coordinate_source is CoordinateSource.SHEET
    assert record.lat == Decimal("7.88")


def test_store_with_no_coordinates_anywhere_still_imports(tmp_path: Path) -> None:
    """35 real stores are in this state. Refusing them would lose a fifth of the
    fleet; inventing positions would be worse."""
    result = read_workbook(
        make_workbook(tmp_path, [store_row(lat="", lng="")], coordinate_rows=[])
    )

    record = result.records[0]
    assert record.lat is None and record.lng is None
    assert record.coordinate_source is CoordinateSource.NONE
    assert not record.has_location
    assert any("not appear on the map" in w.message for w in result.warnings)


def test_half_filled_coordinate_pair_is_not_used(tmp_path: Path) -> None:
    result = read_workbook(
        make_workbook(tmp_path, [store_row(lat="13.75", lng="")], coordinate_rows=[])
    )
    assert result.records[0].lat is None
    assert any("both" in w.message for w in result.warnings)


def test_na_cells_are_treated_as_empty(tmp_path: Path) -> None:
    """The workbook is full of #N/A from broken lookups."""
    result = read_workbook(
        make_workbook(tmp_path, [store_row(lat="#N/A", lng="#N/A")], coordinate_rows=[])
    )
    assert result.records[0].lat is None


# --------------------------------------------------------------------------- #
# Rejection — rows that must never reach the database
# --------------------------------------------------------------------------- #


def test_row_without_a_store_code_is_rejected(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row(code="")]))

    assert result.records == []
    assert any(i.field == "store_code" for i in result.rejected)


def test_row_without_a_name_is_rejected(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row(name="")]))
    assert result.records == []


def test_duplicate_store_code_is_rejected(tmp_path: Path) -> None:
    """The second occurrence is rejected; the first still imports. Silently
    letting the last row win would make the result depend on sheet order."""
    result = read_workbook(
        make_workbook(tmp_path, [store_row(code="PABC"), store_row(code="PABC")])
    )

    assert len(result.records) == 1
    assert any("duplicate" in i.message for i in result.rejected)


def test_missing_capacity_is_rejected(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row(capacity="")]))

    assert result.records == []
    assert any(i.field == "installed_kwp" for i in result.rejected)


def test_zero_or_negative_capacity_is_rejected(tmp_path: Path) -> None:
    for bad in ("0", "-10"):
        result = read_workbook(make_workbook(tmp_path, [store_row(capacity=bad)]))
        assert result.records == [], f"capacity {bad} must be rejected"


def test_one_bad_row_does_not_block_the_good_ones(tmp_path: Path) -> None:
    """A single typo must not cost the entire import."""
    result = read_workbook(
        make_workbook(
            tmp_path,
            [store_row(code="PAAA"), store_row(code="", name="broken"), store_row(code="PCCC")],
        )
    )

    assert [r.store_code for r in result.records] == ["PAAA", "PCCC"]
    assert len(result.rejected) == 1


def test_rejected_rows_carry_their_row_number(tmp_path: Path) -> None:
    """An operator has to find the cell. A message with no row number is close
    to useless in a 153-row sheet."""
    result = read_workbook(
        make_workbook(tmp_path, [store_row(), store_row(code=""), store_row(code="PZZZ")])
    )

    issue = result.rejected[0]
    assert issue.row_number == 3, "header is row 1, so the second data row is row 3"
    assert issue.severity is Severity.ERROR


def test_missing_sheet_is_an_error_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "Solar Report.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships"><sheets>'
            '<sheet name="Wrong" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" '
            'Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet"/></Relationships>',
        )
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml([["x"]]))

    result = read_workbook(path)
    assert result.records == []
    assert any("not found" in i.message for i in result.rejected)


# --------------------------------------------------------------------------- #
# Field coercion
# --------------------------------------------------------------------------- #


def test_tariff_code_stored_as_a_number_is_normalised(tmp_path: Path) -> None:
    """Excel turns 8114 into "8114.0"; the database holds "8114"."""
    result = read_workbook(make_workbook(tmp_path, [store_row(tariff="8114.0")]))
    assert result.records[0].tariff_code == "8114"


def test_unexpected_elec_zone_is_dropped_with_a_warning(tmp_path: Path) -> None:
    result = read_workbook(make_workbook(tmp_path, [store_row(zone="EGAT")]))

    assert result.records[0].utility is None
    assert any(i.field == "utility" for i in result.warnings)


def test_excel_serial_date_becomes_a_real_date(tmp_path: Path) -> None:
    row = store_row()
    row[17] = "45522"
    result = read_workbook(make_workbook(tmp_path, [row]))

    commissioned = result.records[0].commissioned_at
    assert commissioned is not None
    assert commissioned.isoformat() == "2024-08-18"
