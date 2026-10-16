"""Read every live WhatsApp number's messaging limit from Meta, before the campaign workers start.

    python -m scripts.read_messaging_limits            # every live number
    python -m scripts.read_messaging_limits --stale    # only those older than the refresh age

**The deploy step of 0110** (docs/RUNBOOK.md, "Campaign page features
(0108-0119)"). A number's business-portfolio limit is unknown until it is read,
and a campaign on a number whose limit is unknown is neither scheduled nor
sent (LIM-01, fail closed). Every number connected before 0110 starts unknown,
so a running campaign on one waits (`waiting_reason = limit_unknown`) until
this - or a schedule, an enable, or Meta's `business_capability_update`
webhook - reads it. Run it after `alembic upgrade head` and before starting the
campaign workers, and the campaigns carry on without waiting.

**Idempotent.** Each number is read with its own stored credential (ADR-034)
and its portfolio row updated; running it twice reads twice and changes
nothing the second time unless Meta's answer changed. A number whose read fails
keeps what it had - unknown stays unknown, and is listed. Nothing is ever
guessed. One short transaction per number, and the Graph call is made outside
it (ADR-093).

The URL is `DATABASE_URL`. Prints one line per number (its internal id and the
outcome - never a phone number, a token or a WABA id) and exits 1 if any read
failed.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models.messaging_limit import LimitSource
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.whatsapp.messaging_limit import MessagingLimitReader
from app.services.messaging_limit_service import MessagingLimitService, limit_for_account


@dataclass(frozen=True, slots=True)
class Outcome:
    account_id: str
    read: bool
    known: bool
    messaging_limit: int | None


async def read_all(
    database: Database,
    settings: Settings,
    *,
    stale_only: bool = False,
    reader: MessagingLimitReader | None = None,
) -> list[Outcome]:
    """Read and store every live number's limit. Returns what happened to each."""
    async with database.session_factory() as session:
        ids = list(
            (
                await session.execute(
                    select(WhatsAppAccount.id)
                    .where(WhatsAppAccount.released_at.is_(None))
                    .order_by(WhatsAppAccount.id)
                )
            ).scalars()
        )
    outcomes: list[Outcome] = []
    for account_id in ids:
        async with database.session_factory() as session:
            account = await session.get(WhatsAppAccount, account_id)
            if account is None:
                continue
            limits = MessagingLimitService(session, settings=settings, reader=reader)
            moment = datetime.now(UTC)
            if stale_only and not limits.is_stale(
                await limit_for_account(session, account.id), at=moment
            ):
                continue
            # Nothing is held across the read: the session has only read.
            await session.commit()
            value = await limits.read_with_stored_credential(account)
            await limits.attach(account, value, source=LimitSource.API, at=moment)
            await session.commit()
            current = await limit_for_account(session, account.id)
            outcomes.append(
                Outcome(
                    account_id=str(account_id),
                    read=value is not None,
                    known=current.known,
                    messaging_limit=current.messaging_limit,
                )
            )
    return outcomes


async def _run(stale_only: bool) -> int:
    settings = get_settings()
    database = Database(settings)
    try:
        outcomes = await read_all(database, settings, stale_only=stale_only)
    finally:
        await database.dispose()
    failed = 0
    for outcome in outcomes:
        if outcome.read:
            limit = "unlimited" if outcome.messaging_limit is None else outcome.messaging_limit
            sys.stdout.write(f"{outcome.account_id}  read  messaging_limit={limit}\n")
        else:
            failed += 1
            state = "known (kept)" if outcome.known else "UNKNOWN"
            sys.stdout.write(f"{outcome.account_id}  FAILED  {state}\n")
    sys.stdout.write(f"{len(outcomes)} numbers, {failed} not read\n")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv not in ([], ["--stale"]):
        sys.stdout.write(__doc__ or "")
        return 2
    return asyncio.run(_run(stale_only=argv == ["--stale"]))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))
