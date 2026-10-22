"""The time zones a workspace may choose (TZ-02).

Public, because a workspace's zone is chosen at signup - before there is an
account, a token or a workspace to authorise against. It is reference data
derived from the pinned IANA database and carries nothing about any customer.
Counted per client address like the rest of the unauthenticated surface.

The list it serves is exactly the list `POST /workspaces`, registration and
`PATCH /workspace` validate against (`app.core.timezones`): an id from here is
always accepted, and nothing else is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.api.rate_limits import PublicRateLimit
from app.api.route import CommittingRoute
from app.core.timezones import (
    MAX_TIMEZONE_QUERY_LENGTH,
    TZDATA_VERSION,
    listing,
    listing_etag,
    suggested,
)
from app.schemas.timezone import TimezoneListResponse, TimezoneRead

router = APIRouter(route_class=CommittingRoute, tags=["Timezones"])

# An hour: the list changes with a database release or a daylight-saving
# transition, and the ETag changes with either.
CACHE_CONTROL = "public, max-age=3600"


@router.get(
    "/timezones",
    response_model=TimezoneListResponse,
    summary="The time zones a workspace may choose",
    responses={304: {"description": "Not modified since the ETag the client holds."}},
)
async def list_timezones(
    request: Request,
    response: Response,
    limit: PublicRateLimit,
    q: Annotated[
        str | None,
        Query(max_length=MAX_TIMEZONE_QUERY_LENGTH, description="Matches id or label, any case."),
    ] = None,
) -> TimezoneListResponse | Response:
    """Canonical IANA zones, suggested ones first, each with its offset from UTC now.

    Sorted by offset, then name. `utc_offset` is computed at request time, so
    a zone in daylight saving shows its summer offset. The ETag changes with
    the database release and with any zone's offset.
    """
    now = datetime.now(UTC)
    etag = listing_etag(at=now, query=q)
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return TimezoneListResponse(
        tzdata_version=TZDATA_VERSION,
        suggested=[TimezoneRead.from_entry(entry) for entry in suggested(at=now)],
        timezones=[TimezoneRead.from_entry(entry) for entry in listing(at=now, query=q)],
    )
