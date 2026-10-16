"""A campaign's recipient list as CSV (PG-14).

UTF-8 with a byte-order mark, so Excel opens Arabic names as Arabic; one
header row; times in the workspace's own zone with their offset. A cell a
spreadsheet would read as a formula - starting with `=`, `+`, `-`, `@`, a tab
or a carriage return - is written with a leading `'`, so opening the file
never runs what a customer typed as their name (CSV injection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from app.core.timezones import local
from app.repositories.campaign_repository import ExportRow

COLUMNS: Final = (
    "contact_name",
    "phone",
    "status",
    "reason_code",
    "provider_code",
    "sent_at",
    "delivered_at",
    "read_at",
    "replied_at",
    "opted_out_after",
)
BOM: Final = "\ufeff"
_FORMULA_STARTS: Final = ("=", "+", "-", "@", "\t", "\r")


def neutralised(value: str) -> str:
    """`value` safe to open in a spreadsheet: a formula-looking cell is quoted with `'`."""
    return f"'{value}" if value.startswith(_FORMULA_STARTS) else value


def _time(at: datetime | None, timezone: str) -> str:
    return local(at, timezone).isoformat() if at is not None else ""


def _write(rows: Sequence[Sequence[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows([[neutralised(cell) for cell in row] for row in rows])
    return buffer.getvalue().encode("utf-8")


def csv_header() -> bytes:
    return BOM.encode("utf-8") + _write([COLUMNS])


def csv_rows(rows: Sequence[ExportRow], *, timezone: str) -> bytes:
    return _write(
        [
            [
                row.contact_name or "",
                row.phone or "",
                row.status.value,
                row.reason_code or "",
                str(row.provider_code) if row.provider_code is not None else "",
                _time(row.sent_at, timezone),
                _time(row.delivered_at, timezone),
                _time(row.read_at, timezone),
                _time(row.replied_at, timezone),
                _time(row.opted_out_after_at, timezone),
            ]
            for row in rows
        ]
    )


__all__ = ["BOM", "COLUMNS", "csv_header", "csv_rows", "neutralised"]
