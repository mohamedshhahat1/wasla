"""The time zones a workspace may live in, from the one pinned IANA database (TZ-01, TZ-02).

Every time a campaign respects - quiet hours, the days a delivery plan spreads
over, the local time a person reads - is computed in the workspace's own zone,
so the zone has to be one the platform can compute in identically everywhere.

**One database, the `tzdata` package.** `zoneinfo` prefers the operating
system's zone files and Windows has none, so a zone built by name could carry
Linux's rules in CI and the package's on a developer machine. Zones here are
read from the package's own files (`ZoneInfo.from_file`) and never by name
through the system path, so the list a workspace chooses from, the names it is
validated against and the offsets it is shown are the same on every platform.

**Canonical names only.** The accepted names are the zones of `zone.tab` and
`zone1970.tab` - one or more per country - and nothing else: no `Etc/GMT+3`,
whose sign reads backwards, and no legacy alias such as `US/Eastern` or
`Egypt`. `zone.tab` is the wider of the two and is what keeps a country's own
name for its zone (`Asia/Kuwait`) even where the database merged its rules into
a neighbour's.
"""

from __future__ import annotations

import hashlib
import importlib.resources
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cache
from typing import Final
from zoneinfo import ZoneInfo

import tzdata

from app.core.exceptions import ValidationError

#: The IANA release the zones come from, as the timezones API reports it.
TZDATA_VERSION: Final[str] = tzdata.IANA_VERSION

#: The zone every workspace that existed before 0108 was given: every one of
#: them was Egyptian (ADR-145).
BACKFILL_TIMEZONE: Final = "Africa/Cairo"

#: The stable error code a name outside the list is refused with.
TIMEZONE_UNKNOWN_CODE: Final = "timezone_unknown"

#: The longest name accepted, and the longest `?q=` filter.
MAX_TIMEZONE_LENGTH: Final = 64
MAX_TIMEZONE_QUERY_LENGTH: Final = 64

#: Offered first at onboarding: where Wasla's customers are (TZ-02).
SUGGESTED_TIMEZONES: Final[tuple[str, ...]] = (
    "Africa/Cairo",
    "Asia/Riyadh",
    "Asia/Dubai",
    "Asia/Kuwait",
    "Asia/Qatar",
    "Asia/Bahrain",
    "Asia/Muscat",
    "Asia/Amman",
    "Asia/Baghdad",
    "Africa/Casablanca",
    "Africa/Tunis",
    "Africa/Algiers",
    "Africa/Tripoli",
    "Asia/Beirut",
    "Europe/London",
    "Europe/Istanbul",
)

_ZONEINFO = importlib.resources.files("tzdata") / "zoneinfo"


def _zone_tab_names(filename: str) -> set[str]:
    names: set[str] = set()
    for line in (_ZONEINFO / filename).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) >= 3:
            names.add(columns[2])
    return names


#: Every name a workspace may choose.
CANONICAL_TIMEZONES: Final[frozenset[str]] = frozenset(
    _zone_tab_names("zone.tab") | _zone_tab_names("zone1970.tab")
)


class UnknownTimezoneError(ValidationError):
    """A name that is not one of `CANONICAL_TIMEZONES` (422 `timezone_unknown`)."""

    error_code = TIMEZONE_UNKNOWN_CODE


@cache
def zone(name: str) -> tzinfo:
    """The zone called `name`, read from the pinned database; raises if it is not one."""
    if name not in CANONICAL_TIMEZONES:
        raise UnknownTimezoneError(
            "That time zone is not one Wasla knows. Choose one from GET /api/v1/timezones.",
            error_code=TIMEZONE_UNKNOWN_CODE,
        )
    path = _ZONEINFO.joinpath(*name.split("/"))
    with path.open("rb") as handle:
        return ZoneInfo.from_file(handle, key=name)


def validated(name: str) -> str:
    """`name` when it is a zone a workspace may choose; otherwise 422 `timezone_unknown`."""
    candidate = name.strip()
    zone(candidate)
    return candidate


def offset_of(name: str, at: datetime) -> timedelta:
    """The zone's offset from UTC at `at` - daylight saving included."""
    offset = at.astimezone(zone(name)).utcoffset()
    return offset if offset is not None else timedelta(0)


def format_offset(offset: timedelta) -> str:
    """`+03:00`, `-04:30`, `+00:00`."""
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def local(at: datetime, name: str) -> datetime:
    """`at` in the zone called `name`."""
    return at.astimezone(zone(name))


def local_midnight(at: datetime, name: str) -> datetime:
    """The start of `at`'s local day in zone `name`, as an aware instant (DST-correct)."""
    day = local(at, name).date()
    return datetime(day.year, day.month, day.day, tzinfo=zone(name))


def _label(name: str) -> str:
    parts = name.split("/")
    city = parts[-1].replace("_", " ")
    if len(parts) > 2:
        return f"{city} ({parts[1].replace('_', ' ')})"
    return city


@dataclass(frozen=True, slots=True)
class TimezoneEntry:
    """One zone as the API lists it, with its offset at the moment it was asked."""

    id: str
    label: str
    utc_offset: str
    offset_minutes: int
    region: str


def describe(name: str, at: datetime) -> TimezoneEntry:
    offset = offset_of(name, at)
    return TimezoneEntry(
        id=name,
        label=_label(name),
        utc_offset=format_offset(offset),
        offset_minutes=int(offset.total_seconds() // 60),
        region=name.split("/", 1)[0],
    )


def listing(*, at: datetime | None = None, query: str | None = None) -> list[TimezoneEntry]:
    """Every canonical zone, by offset then name; `query` matches id or label, any case."""
    moment = at or datetime.now(UTC)
    needle = query.strip().casefold() if query else ""
    entries = [describe(name, moment) for name in CANONICAL_TIMEZONES]
    if needle:
        entries = [
            entry
            for entry in entries
            if needle in entry.id.casefold() or needle in entry.label.casefold()
        ]
    return sorted(entries, key=lambda entry: (entry.offset_minutes, entry.id))


def suggested(*, at: datetime | None = None) -> list[TimezoneEntry]:
    moment = at or datetime.now(UTC)
    return [describe(name, moment) for name in SUGGESTED_TIMEZONES]


def listing_etag(*, at: datetime | None = None, query: str | None = None) -> str:
    """Changes when the database release changes or any zone's offset does (TZ-02).

    A filtered listing is a different document, so the filter is part of it.
    """
    moment = at or datetime.now(UTC)
    digest = hashlib.sha256(TZDATA_VERSION.encode())
    digest.update(f"q={(query or '').strip().casefold()}".encode())
    for name in sorted(CANONICAL_TIMEZONES):
        digest.update(f"{name}={offset_of(name, moment)}".encode())
    return f'"{digest.hexdigest()[:32]}"'


__all__ = [
    "BACKFILL_TIMEZONE",
    "CANONICAL_TIMEZONES",
    "MAX_TIMEZONE_LENGTH",
    "MAX_TIMEZONE_QUERY_LENGTH",
    "SUGGESTED_TIMEZONES",
    "TIMEZONE_UNKNOWN_CODE",
    "TZDATA_VERSION",
    "TimezoneEntry",
    "UnknownTimezoneError",
    "describe",
    "format_offset",
    "listing",
    "listing_etag",
    "local",
    "offset_of",
    "suggested",
    "validated",
    "zone",
]


#: The stable error code a wall time the local-time route cannot read is refused with.
LOCAL_TIME_INVALID_CODE: Final = "local_time_invalid"


class InvalidLocalTimeError(ValidationError):
    """Not a wall time `YYYY-MM-DDTHH:MM` (422 `local_time_invalid`)."""

    error_code = LOCAL_TIME_INVALID_CODE


@dataclass(frozen=True, slots=True)
class ResolvedWallTime:
    """A workspace wall time as the instants it names (TZ-08).

    `at` is the one to use: the first of two in the autumn fold (the earlier
    instant, `fold=0`), the first instant that exists after a spring gap.
    `alternatives` lists every instant the wall time could mean - two in the
    fold, one otherwise.
    """

    at: datetime
    ambiguous: bool
    nonexistent: bool
    alternatives: tuple[datetime, ...]


def parse_wall_time(value: str) -> datetime:
    """`2026-10-30T09:00` as a naive datetime; anything else 422 `local_time_invalid`.

    A wall time has no offset - that is the point of resolving it - and no
    seconds: the pages pick minutes.
    """
    candidate = value.strip()
    try:
        wall = datetime.fromisoformat(candidate)
    except ValueError:
        wall = None
    if (
        wall is None
        or wall.tzinfo is not None
        or len(candidate) != len("YYYY-MM-DDTHH:MM")
        or wall.second
        or wall.microsecond
    ):
        raise InvalidLocalTimeError(
            "Send a wall time as YYYY-MM-DDTHH:MM, with no offset and no seconds.",
            error_code=LOCAL_TIME_INVALID_CODE,
        )
    return wall


def resolve_wall_time(wall: datetime, name: str) -> ResolvedWallTime:
    """The instant(s) the naive wall time `wall` names in the zone called `name`."""
    tz = zone(name)
    early = wall.replace(tzinfo=tz, fold=0)
    late = wall.replace(tzinfo=tz, fold=1)
    exists = early.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == wall
    if not exists:
        # Inside a spring gap: the two folds straddle the transition, which is
        # the first instant whose local reading is past the gap.
        low = min(early.astimezone(UTC), late.astimezone(UTC))
        high = max(early.astimezone(UTC), late.astimezone(UTC))
        before = low.astimezone(tz).utcoffset()
        moment = low
        while moment < high and moment.astimezone(tz).utcoffset() == before:
            moment += timedelta(minutes=1)
        first = moment.astimezone(tz)
        return ResolvedWallTime(at=first, ambiguous=False, nonexistent=True, alternatives=(first,))
    if early.utcoffset() != late.utcoffset():
        both = (early.astimezone(UTC).astimezone(tz), late.astimezone(UTC).astimezone(tz))
        return ResolvedWallTime(at=both[0], ambiguous=True, nonexistent=False, alternatives=both)
    single = early.astimezone(UTC).astimezone(tz)
    return ResolvedWallTime(at=single, ambiguous=False, nonexistent=False, alternatives=(single,))
