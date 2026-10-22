"""The timezones API's shapes (TZ-02)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel

from app.core.timezones import TimezoneEntry


class TimezoneRead(BaseModel):
    """One IANA zone, with its offset from UTC when it was asked."""

    id: str
    label: str
    utc_offset: str
    offset_minutes: int
    region: str

    @classmethod
    def from_entry(cls, entry: TimezoneEntry) -> Self:
        return cls(
            id=entry.id,
            label=entry.label,
            utc_offset=entry.utc_offset,
            offset_minutes=entry.offset_minutes,
            region=entry.region,
        )


class TimezoneListResponse(BaseModel):
    tzdata_version: str
    suggested: list[TimezoneRead]
    timezones: list[TimezoneRead]
