"""Apply the workbook to the database.

Reads through ``reader.read_workbook``, diffs against what is stored, and writes
only what actually changed.

THREE RULES THAT MATTER MORE THAN THE CODE
------------------------------------------
1. A STORE IS NEVER DELETED. Telemetry rows reference ``store_id``, and that
   history is the evidence base for ESG and financial reporting on a >1,000M THB
   asset programme. Deleting a branch would take its history with it. A store
   that vanishes from the workbook is REPORTED and otherwise left untouched —
   the usual cause is somebody not filling in a row, not a closure.

2. NOTHING IS WRITTEN UNTIL EVERYTHING VALIDATES. Rejected rows never reach the
   database. Because the import can run unattended when the file changes, a
   partial write of half-checked data is the failure mode to design against.

3. DRY RUN IS THE DEFAULT. ``apply=True`` has to be asked for. Seeing the diff
   before it happens is the whole point of having an importer rather than a
   pile of UPDATE statements.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.baseinfo.reader import (
    StoreRecord,
    ValidationIssue,
    WorkbookReadResult,
)
from app.infrastructure.db.orm import StoreORM, TariffORM

logger = logging.getLogger(__name__)

__all__ = ["FieldChange", "ImportReport", "StoreDiff", "import_workbook"]

#: Columns the workbook owns. Anything not listed here is managed elsewhere and
#: an import must not touch it — `is_active`, for instance, is an operational
#: decision, not a spreadsheet cell.
MANAGED_FIELDS = (
    "store_name",
    "retail_store_code",
    "province",
    "address",
    "installed_kwp",
    "lat",
    "lng",
    "rollout_phase",
    "commissioned_at",
    "capex_before_vat",
    "capex_vat",
    "capex_net",
    "tariff_id",
)


@dataclass(frozen=True, slots=True)
class FieldChange:
    field_name: str
    old: object
    new: object

    def __str__(self) -> str:
        return f"{self.field_name}: {_show(self.old)} -> {_show(self.new)}"


@dataclass(frozen=True, slots=True)
class StoreDiff:
    store_code: str
    store_name: str
    is_new: bool
    changes: tuple[FieldChange, ...] = ()

    @property
    def has_changes(self) -> bool:
        return self.is_new or bool(self.changes)


@dataclass
class ImportReport:
    source_file: str
    started_at: datetime
    applied: bool = False
    read_result: WorkbookReadResult | None = None
    created: list[StoreDiff] = field(default_factory=list)
    updated: list[StoreDiff] = field(default_factory=list)
    unchanged: int = 0
    #: In the database but absent from the workbook. Reported, never deleted.
    missing_from_workbook: list[str] = field(default_factory=list)
    tariffs_created: int = 0
    aborted_reason: str | None = None

    @property
    def rejected(self) -> list[ValidationIssue]:
        return list(self.read_result.rejected) if self.read_result else []

    @property
    def warnings(self) -> list[ValidationIssue]:
        return list(self.read_result.warnings) if self.read_result else []

    @property
    def succeeded(self) -> bool:
        return self.aborted_reason is None

    def render(self) -> str:
        """Human-readable report. This is what an operator actually reads."""
        lines: list[str] = []
        mode = "APPLIED" if self.applied else "DRY RUN (nothing written)"
        lines.append(f"BaseInfo import — {mode}")
        lines.append(f"source: {self.source_file}")
        lines.append("")

        if self.aborted_reason:
            lines.append(f"ABORTED: {self.aborted_reason}")
            lines.append("")

        if self.read_result:
            lines.append(self.read_result.summary())
            lines.append("")

        if self.rejected:
            lines.append(f"REJECTED ROWS ({len(self.rejected)}) — not written:")
            lines.extend(f"  {issue}" for issue in self.rejected[:25])
            if len(self.rejected) > 25:
                lines.append(f"  ... and {len(self.rejected) - 25} more")
            lines.append("")

        if self.warnings:
            # Grouped, not listed. A real import produces 100+ warnings of two
            # or three kinds; printing them one per line guarantees nobody reads
            # any of them. The shape of the problem is what matters, plus enough
            # store codes to go and look at.
            lines.append(f"WARNINGS ({len(self.warnings)}) — imported, but check these:")
            for kind, codes in _group_warnings(self.warnings).items():
                shown = ", ".join(codes[:8])
                more = f" ... +{len(codes) - 8} more" if len(codes) > 8 else ""
                lines.append(f"  [{len(codes):>3}] {kind}")
                lines.append(f"        {shown}{more}")
            lines.append("")

        if self.created:
            lines.append(f"NEW STORES ({len(self.created)}):")
            lines.extend(f"  + {d.store_code:<6} {d.store_name}" for d in self.created[:40])
            if len(self.created) > 40:
                lines.append(f"  ... and {len(self.created) - 40} more")
            lines.append("")

        if self.updated:
            lines.append(f"CHANGED STORES ({len(self.updated)}):")
            for diff in self.updated[:40]:
                lines.append(f"  ~ {diff.store_code:<6} {diff.store_name}")
                lines.extend(f"      {change}" for change in diff.changes)
            if len(self.updated) > 40:
                lines.append(f"  ... and {len(self.updated) - 40} more")
            lines.append("")

        if self.missing_from_workbook:
            lines.append(
                f"IN DATABASE BUT NOT IN THE WORKBOOK ({len(self.missing_from_workbook)}) "
                f"— left untouched, nothing deleted:"
            )
            lines.append("  " + ", ".join(sorted(self.missing_from_workbook)))
            lines.append("")

        lines.append(
            f"summary: {len(self.created)} new, {len(self.updated)} changed, "
            f"{self.unchanged} unchanged, {self.tariffs_created} tariffs added"
        )
        return "\n".join(lines)


def _group_warnings(warnings: list[ValidationIssue]) -> dict[str, list[str]]:
    """Collapse warnings into kinds, keeping the store codes for each.

    Messages embed specific values ("Lat and Long are identical (13.58)"), so
    they are normalised to a stable key before grouping — otherwise every store
    forms its own group and nothing is collapsed at all.
    """
    groups: dict[str, list[str]] = {}
    for issue in warnings:
        key = re.sub(r"\(-?[\d.,\s]+\)", "(...)", issue.message)
        key = re.sub(r"\s+", " ", key).strip()
        if len(key) > 88:
            key = key[:85] + "..."
        groups.setdefault(key, []).append(issue.store_code or "?")
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def _show(value: object) -> str:
    if value is None:
        return "(none)"
    return str(value)


def _differs(old: object, new: object) -> bool:
    """Compare, treating numerically equal Decimals as equal.

    Decimal("48.4") and Decimal("48.40") are the same capacity. Without this the
    importer reports a change on every run and the diff becomes noise nobody
    reads — which defeats the point of having a diff at all.
    """
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    if isinstance(old, Decimal) and isinstance(new, Decimal):
        return old.compare(new) != 0
    return old != new


async def import_workbook(
    session: AsyncSession,
    read_result: WorkbookReadResult,
    *,
    source_file: str,
    apply: bool = False,
) -> ImportReport:
    """Diff the workbook against the database and optionally write the changes."""
    report = ImportReport(
        source_file=source_file,
        started_at=datetime.now(UTC),
        applied=apply,
        read_result=read_result,
    )

    if not read_result.records:
        report.aborted_reason = (
            "no valid rows in the workbook — refusing to touch the database"
        )
        return report

    tariff_ids = await _ensure_tariffs(session, read_result.records, apply=apply, report=report)

    existing_rows = (await session.scalars(select(StoreORM))).all()
    existing = {row.store_code: row for row in existing_rows}
    workbook_codes = {record.store_code for record in read_result.records}

    report.missing_from_workbook = sorted(set(existing) - workbook_codes)

    for record in read_result.records:
        target = _desired_values(record, tariff_ids)
        row = existing.get(record.store_code)

        if row is None:
            report.created.append(
                StoreDiff(record.store_code, record.store_name, is_new=True)
            )
            if apply:
                session.add(StoreORM(store_code=record.store_code, **target))
            continue

        changes = tuple(
            FieldChange(name, getattr(row, name), value)
            for name, value in target.items()
            if _differs(getattr(row, name), value)
        )

        if not changes:
            report.unchanged += 1
            continue

        report.updated.append(
            StoreDiff(record.store_code, record.store_name, is_new=False, changes=changes)
        )
        if apply:
            for change in changes:
                setattr(row, change.field_name, change.new)

    if apply:
        await session.flush()
        logger.info(
            "BaseInfo import applied: %d new, %d changed, %d unchanged",
            len(report.created),
            len(report.updated),
            report.unchanged,
        )

    return report


def _desired_values(
    record: StoreRecord, tariff_ids: dict[tuple[str, str], Any]
) -> dict[str, Any]:
    """What the row should look like after import.

    Deliberately excludes ``is_active``: whether a branch is live is an
    operational decision, and an import must not silently reactivate a store
    somebody switched off.
    """
    tariff_id = None
    if record.utility and record.tariff_code:
        tariff_id = tariff_ids.get((record.utility, record.tariff_code))

    return {
        "store_name": record.store_name,
        "retail_store_code": record.retail_store_code,
        "province": record.province,
        "address": record.address,
        "installed_kwp": record.installed_kwp,
        "lat": record.lat,
        "lng": record.lng,
        "rollout_phase": record.rollout_phase,
        "commissioned_at": record.commissioned_at,
        "capex_before_vat": record.capex_before_vat,
        "capex_vat": record.capex_vat,
        "capex_net": record.capex_net,
        "tariff_id": tariff_id,
    }


async def _ensure_tariffs(
    session: AsyncSession,
    records: list[StoreRecord],
    *,
    apply: bool,
    report: ImportReport,
) -> dict[tuple[str, str], Any]:
    """Create any tariff the workbook references but the database lacks.

    Only the on-peak rate is created, because that is all the workbook carries.
    ``off_peak_rate`` stays NULL rather than 0: a zero here would silently halve
    every TOU savings figure downstream, and NULL forces the gap to be noticed.
    """
    existing = {
        (row.utility, row.tariff_code): row.tariff_id
        for row in (await session.scalars(select(TariffORM))).all()
    }

    wanted: dict[tuple[str, str], Decimal | None] = {}
    for record in records:
        if record.utility and record.tariff_code:
            key = (record.utility, record.tariff_code)
            if key not in existing and key not in wanted:
                wanted[key] = record.on_peak_rate

    for (utility, code), on_peak in wanted.items():
        if on_peak is None:
            logger.warning(
                "Tariff %s %s appears in the workbook with no On-Peak rate; skipping.",
                utility,
                code,
            )
            continue

        report.tariffs_created += 1
        if apply:
            tariff = TariffORM(
                tariff_name=f"{utility} {code}",
                tariff_code=code,
                utility=utility,
                on_peak_rate=on_peak,
                off_peak_rate=None,
                demand_charge_rate=Decimal("0"),
                effective_from=report.started_at.date(),
            )
            session.add(tariff)
            await session.flush()
            existing[(utility, code)] = tariff.tariff_id

    return existing
