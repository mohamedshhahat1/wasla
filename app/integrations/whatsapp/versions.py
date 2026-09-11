"""When the configured Graph API version stops working, and warning before it does.

Meta retires a Graph API version roughly two years after it ships, and a
deployment pointed at a retired one stops sending and stops receiving - not
gradually, and not with a warning from inside the application. The version is
configurable, so this is a planning item rather than a defect, but nothing
said when the planning had to happen (MSG-21).

**The table is maintained by hand, and that is the design.** Fetching Meta's
changelog at start-up would make booting depend on a third party's website
being up and parseable, which is a worse failure than the one being prevented:
a deployment that cannot reach Meta's documentation must still start. So the
dates are copied from the changelog, the source and the date they were read are
recorded below, and `docs/WHATSAPP.md` says whose job it is to refresh them.

A version that is *not* in the table warns too, and says so differently. That
is the honest answer to "this release has not heard of the version you
configured": it may be newer than this table, in which case somebody should
update the table, or it may be a typo, in which case somebody should fix the
setting. Silence would make a typo look like an endorsement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final

# Graph API versions and the dates Meta publishes as their last day, read from
# https://developers.facebook.com/docs/graph-api/changelog/ on 2026-09-11.
#
# Only versions this deployment might plausibly be pointed at are listed. An
# older one that Meta has already retired needs no warning threshold - it is
# already broken, and the "unknown version" branch says so.
META_API_SUNSETS: Final[dict[str, date]] = {
    "v21.0": date(2027, 1, 21),
    "v22.0": date(2027, 5, 20),
    "v23.0": date(2027, 10, 8),
    "v24.0": date(2028, 2, 18),
    "v25.0": date(2028, 7, 29),
    # v26.0 was the current version when this table was written and Meta had
    # not yet published its expiry. Absent rather than guessed: a date invented
    # here would eventually warn on the wrong day, and being wrong about this
    # is worse than being silent.
}

# How long before a sunset the warning starts. Long enough that moving version
# is a piece of planned work - re-verify the send and media contracts, deploy,
# watch - rather than an incident.
SUNSET_WARNING_DAYS: Final = 90


def api_version_warning(version: str, *, today: date | None = None) -> str | None:
    """What to say about this Graph API version, or None if nothing.

    Returns a message rather than logging one, so the caller decides the level
    and the fields and a test can assert on the text without capturing logs.

    Three outcomes. A version with plenty of life left is silent. One inside
    its final ninety days, or already past its last day, warns with the date.
    One this release has never heard of warns that it cannot be checked, which
    covers both a version newer than this table and a typo in the setting.
    """
    moment = today or datetime.now(UTC).date()
    sunset = META_API_SUNSETS.get(version)
    if sunset is None:
        return (
            f"META_API_VERSION is set to {version}, which this release has no "
            "sunset date for. Check Meta's Graph API changelog and update "
            "META_API_SUNSETS in app/integrations/whatsapp/versions.py."
        )

    remaining = (sunset - moment).days
    if remaining < 0:
        return (
            f"META_API_VERSION is set to {version}, which Meta retired on "
            f"{sunset.isoformat()}. WhatsApp sends and webhooks will be failing."
        )
    if remaining <= SUNSET_WARNING_DAYS:
        return (
            f"META_API_VERSION is set to {version}, which Meta retires on "
            f"{sunset.isoformat()} ({remaining} days). Plan the move to a "
            "current version and re-verify the send and media contracts."
        )
    return None
