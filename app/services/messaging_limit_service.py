"""What Meta allows a business portfolio to send, and how Wasla learns it (LIM-01, LIM-02).

**One table from Meta's words to a number.** Meta states a portfolio's limit
as `TIER_250`, `TIER_2K`, `TIER_10K`, `TIER_100K` or `TIER_UNLIMITED` (also
`TIER_50` on the WABA enum), and the deprecated per-phone webhook field as an
integer with -1 for unlimited ("Messaging limits", "business_capability_update"
and the WhatsApp Business Account reference, read 2026-10-16). Anything else -
`UNTIERED`, `TIER_NOT_SET`, a value Meta adds tomorrow - is **the lowest
level, 250**: never guessed upward, counted
(`wasla_campaign_messaging_limit_unknown_total`) and alerted.

**Learned four ways, all outside any lock.** On connect and on credential
rotation (with the credential just proven), on enable and when a campaign is
scheduled on a number whose value is older than
`CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS` (with the number's own stored
credential), and from a `business_capability_update` webhook. A read that
fails keeps the last stored value; a number whose limit was never read has an
unknown limit, and a campaign on it is neither scheduled nor sent (LIM-01).

**The portfolio is the unit** (LIM-02), keyed on the WABA because Meta names
no field for a WABA's portfolio (`app/db/models/messaging_limit.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialDecryptionError
from app.core.logging import get_logger
from app.core.telemetry import record_messaging_limit_alert, record_messaging_limit_unknown
from app.db.models.messaging_limit import (
    LimitSource,
    PortfolioKeyKind,
    WhatsAppBusinessPortfolio,
)
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.messaging_limit import (
    MessagingLimitReader,
    MessagingLimitUnreadableError,
    MetaMessagingLimitReader,
)
from app.integrations.whatsapp.payload import AccountAlertEvent, CapabilityEvent
from app.services.credential_service import CredentialService

logger = get_logger(__name__)

#: Meta's values and the unique people per moving 24 hours each allows; None
#: is unlimited.
TIER_LIMITS: Final[dict[str, int | None]] = {
    "TIER_50": 50,
    "TIER_250": 250,
    "TIER_2K": 2_000,
    "TIER_10K": 10_000,
    "TIER_100K": 100_000,
    "TIER_UNLIMITED": None,
}
#: The deprecated per-phone webhook field's integers (-1 is unlimited).
INTEGER_LIMITS: Final[dict[int, int | None]] = {
    50: 50,
    250: 250,
    2_000: 2_000,
    10_000: 10_000,
    100_000: 100_000,
    -1: None,
}
#: What an unrecognised value counts as: the lowest level a new portfolio has.
LOWEST_LEVEL: Final = 250
#: The stable error code a schedule is refused with on a never-read limit.
LIMIT_UNKNOWN_CODE: Final = "messaging_limit_unknown"


@dataclass(frozen=True, slots=True)
class Interpretation:
    limit: int | None
    recognised: bool


def interpret(value: str | int) -> Interpretation:
    """Meta's value as a number of unique people; the lowest level when unrecognised."""
    if isinstance(value, bool):
        return Interpretation(LOWEST_LEVEL, False)
    if isinstance(value, int):
        if value in INTEGER_LIMITS:
            return Interpretation(INTEGER_LIMITS[value], True)
        return Interpretation(LOWEST_LEVEL, False)
    text = value.strip()
    if text in TIER_LIMITS:
        return Interpretation(TIER_LIMITS[text], True)
    if text.lstrip("-").isdigit():
        return interpret(int(text))
    return Interpretation(LOWEST_LEVEL, False)


@dataclass(frozen=True, slots=True)
class PortfolioLimit:
    """A number's portfolio limit as the send path sees it."""

    portfolio_id: uuid.UUID | None
    known: bool
    #: Unique people per moving 24 hours; None is unlimited (when known).
    messaging_limit: int | None
    tier: str | None = None
    checked_at: datetime | None = None

    @property
    def unlimited(self) -> bool:
        return self.known and self.messaging_limit is None


UNKNOWN_LIMIT: Final = PortfolioLimit(portfolio_id=None, known=False, messaging_limit=None)


async def limit_for_account(session: AsyncSession, account_id: uuid.UUID) -> PortfolioLimit:
    """The portfolio limit a connection sends under, as committed now (LIM-01).

    Unknown when the connection has no portfolio or its portfolio was never
    read. Read by columns, so a row an earlier statement of this session
    loaded is not trusted over the database.
    """
    row = (
        await session.execute(
            select(
                WhatsAppBusinessPortfolio.id,
                WhatsAppBusinessPortfolio.checked_at,
                WhatsAppBusinessPortfolio.messaging_limit,
                WhatsAppBusinessPortfolio.tier,
            )
            .join(WhatsAppAccount, WhatsAppAccount.portfolio_id == WhatsAppBusinessPortfolio.id)
            .where(WhatsAppAccount.id == account_id)
        )
    ).one_or_none()
    if row is None:
        return UNKNOWN_LIMIT
    portfolio_id, checked_at, messaging_limit, tier = row
    if checked_at is None:
        return PortfolioLimit(portfolio_id=portfolio_id, known=False, messaging_limit=None)
    return PortfolioLimit(
        portfolio_id=portfolio_id,
        known=True,
        messaging_limit=messaging_limit,
        tier=tier,
        checked_at=checked_at,
    )


async def portfolio_for_waba(session: AsyncSession, waba_id: str) -> WhatsAppBusinessPortfolio:
    """The portfolio row of a WABA, created unknown if Meta has not been asked about it, locked."""
    await session.execute(
        insert(WhatsAppBusinessPortfolio)
        .values(
            id=uuid.uuid4(),
            key_kind=PortfolioKeyKind.WABA.value,
            meta_key=waba_id,
        )
        .on_conflict_do_nothing(constraint="uq_whatsapp_business_portfolios_key")
    )
    portfolio = await session.scalar(
        select(WhatsAppBusinessPortfolio)
        .where(
            WhatsAppBusinessPortfolio.key_kind == PortfolioKeyKind.WABA.value,
            WhatsAppBusinessPortfolio.meta_key == waba_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert portfolio is not None  # noqa: S101 - inserted or already there
    return portfolio


async def release_limit_waits(session: AsyncSession, portfolio_id: uuid.UUID) -> int:
    """Make every campaign waiting on this portfolio's limit due now (LIM-01, LIM-04).

    Across workspaces, because the limit is the portfolio's. The next sweep
    asks the ledger again under the new figure: it sends, or waits anew.
    """
    from app.db.models.campaign import Campaign, CampaignStatus, CampaignWaitReason

    accounts = select(WhatsAppAccount.id).where(WhatsAppAccount.portfolio_id == portfolio_id)
    result = await session.execute(
        update(Campaign)
        .where(
            Campaign.account_id.in_(accounts),
            Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
            Campaign.waiting_reason.in_(
                [CampaignWaitReason.DAILY_LIMIT.value, CampaignWaitReason.LIMIT_UNKNOWN.value]
            ),
        )
        .values(next_send_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


class MessagingLimitService:
    """Reading, storing and applying Meta's messaging limit. Platform-level: no tenant."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        reader: MessagingLimitReader | None = None,
        credentials: CredentialService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._reader = reader or MetaMessagingLimitReader(
            api_version=settings.meta_messaging_limit_api_version
        )
        self._credentials = credentials or CredentialService(settings)

    # ------------------------------------------------------------------ reading

    async def read_quietly(self, *, access_token: str, phone_number_id: str) -> str | None:
        """Meta's value for a number's limit, or None if it could not be read.

        A Graph call: callers make it before taking any lock (ADR-093).
        """
        try:
            return await self._reader.read(
                access_token=access_token, phone_number_id=phone_number_id
            )
        except MessagingLimitUnreadableError:
            return None

    async def read_with_stored_credential(self, account: WhatsAppAccount) -> str | None:
        """The same read with the number's own stored credential (ADR-034)."""
        try:
            credential = self._credentials.resolve(account)
        except CredentialDecryptionError:
            return None
        return await self.read_quietly(
            access_token=credential.token, phone_number_id=account.phone_number_id
        )

    def is_stale(self, limit: PortfolioLimit, *, at: datetime) -> bool:
        if not limit.known or limit.checked_at is None:
            return True
        age = timedelta(seconds=self._settings.campaign_messaging_limit_refresh_seconds)
        return at - limit.checked_at >= age

    async def refresh_if_stale(
        self, account: WhatsAppAccount, *, at: datetime | None = None
    ) -> PortfolioLimit:
        """Read again when the stored value is older than the refresh age, or never read.

        For a schedule (LIM-01). The read happens before anything here is
        written, so no lock is held across it.
        """
        moment = at or datetime.now(UTC)
        current = await limit_for_account(self._session, account.id)
        if not self.is_stale(current, at=moment):
            return current
        value = await self.read_with_stored_credential(account)
        await self.attach(account, value, source=LimitSource.API, at=moment)
        return await limit_for_account(self._session, account.id)

    # ------------------------------------------------------------------ storing

    async def _portfolio_for_waba(self, waba_id: str) -> WhatsAppBusinessPortfolio:
        return await portfolio_for_waba(self._session, waba_id)

    async def attach(
        self,
        account: WhatsAppAccount,
        value: str | int | None,
        *,
        source: LimitSource,
        at: datetime | None = None,
    ) -> WhatsAppBusinessPortfolio:
        """Link the number to its portfolio, and record Meta's value when there is one.

        Without a value (the read failed) the portfolio keeps what it had - an
        unknown one stays unknown, so the number stays closed (LIM-01).
        """
        portfolio = await self._portfolio_for_waba(account.waba_id)
        account.portfolio_id = portfolio.id
        if value is not None:
            await self._record(portfolio, value, source=source, at=at or datetime.now(UTC))
        await self._session.flush()
        return portfolio

    async def _record(
        self,
        portfolio: WhatsAppBusinessPortfolio,
        value: str | int,
        *,
        source: LimitSource,
        at: datetime,
    ) -> None:
        if portfolio.checked_at is not None and at < portfolio.checked_at:
            # An older answer arriving late - a webhook delivered out of
            # order - never overwrites a newer one.
            return
        reading = interpret(value)
        stated = str(value)[:40]
        changed = portfolio.checked_at is None or reading.limit != portfolio.messaging_limit
        if changed:
            portfolio.changed_at = at
            logger.info(
                "whatsapp.messaging_limit_changed",
                extra={
                    "event": "whatsapp.messaging_limit_changed",
                    "portfolio_id": str(portfolio.id),
                    "messaging_limit": reading.limit,
                    "previous": portfolio.messaging_limit,
                    "source": source.value,
                },
            )
        portfolio.tier = stated
        portfolio.messaging_limit = reading.limit
        portfolio.recognised = reading.recognised
        portfolio.source = source.value
        portfolio.checked_at = at
        if changed:
            # A new limit takes effect at the next batch (LIM-01): a campaign
            # waiting for the old one's window, or for any limit at all, asks
            # again now rather than at the moment the old figure gave it.
            await release_limit_waits(self._session, portfolio.id)
        if not reading.recognised:
            logger.error(
                "whatsapp.messaging_limit_unrecognised",
                extra={
                    "event": "whatsapp.messaging_limit_unrecognised",
                    "portfolio_id": str(portfolio.id),
                    "treated_as": LOWEST_LEVEL,
                },
            )
            await record_messaging_limit_unknown()

    # ------------------------------------------------------------------ webhooks

    async def apply_capability_events(self, events: list[CapabilityEvent]) -> int:
        """Apply `business_capability_update` deliveries (LIM-01). Idempotent; ordered by time.

        Keyed by the entry's WABA id: the portfolio row is created if Meta
        speaks of a WABA no number has linked yet, so a later connect finds the
        limit already known.
        """
        applied = 0
        for event in events:
            portfolio = await self._portfolio_for_waba(event.waba_id)
            await self._record(
                portfolio,
                event.value,
                source=LimitSource.WEBHOOK,
                at=event.at or datetime.now(UTC),
            )
            await self._session.execute(
                update(WhatsAppAccount)
                .where(
                    WhatsAppAccount.waba_id == event.waba_id,
                    WhatsAppAccount.portfolio_id.is_(None),
                )
                .values(portfolio_id=portfolio.id)
                .execution_options(synchronize_session=False)
            )
            applied += 1
        await self._session.flush()
        return applied

    async def apply_account_alerts(self, alerts: list[AccountAlertEvent]) -> int:
        """Record `account_alerts` (a refused limit increase, among others). Nothing changes."""
        for alert in alerts:
            logger.warning(
                "whatsapp.account_alert",
                extra={
                    "event": "whatsapp.account_alert",
                    "alert_type": alert.alert_type,
                },
            )
            await record_messaging_limit_alert(alert.alert_type)
        return len(alerts)


__all__ = [
    "INTEGER_LIMITS",
    "LIMIT_UNKNOWN_CODE",
    "LOWEST_LEVEL",
    "TIER_LIMITS",
    "UNKNOWN_LIMIT",
    "Interpretation",
    "MessagingLimitService",
    "PortfolioLimit",
    "interpret",
    "limit_for_account",
    "portfolio_for_waba",
    "release_limit_waits",
]
