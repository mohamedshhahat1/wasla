"""Saved campaign segments: named audience rules (PG-06, ADR-149).

**Rules, never a list.** A segment holds how recently a person wrote and
which lead statuses - nothing that names a contact - and an optional number
it is meant for. **Copied, not referenced, by a campaign**: writing an
audience from a segment copies its rules onto the campaign (`audience`),
so editing or deleting the segment later never changes a campaign that used
it. **Narrowing only**: filters given with a segment can only make the
audience smaller.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.db import advisory_locks
from app.db.models.campaign_segment import MAX_SEGMENTS_PER_WORKSPACE, CampaignSegment
from app.db.models.lead import LeadStatus
from app.db.models.user import User
from app.repositories.campaign_repository import AudienceFilter
from app.repositories.campaign_segment_repository import CampaignSegmentRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository

logger = get_logger(__name__)

SEGMENT_NAME_TAKEN_CODE: Final = "segment_name_taken"
SEGMENT_LIMIT_CODE: Final = "segment_limit_reached"
SEGMENT_VERSION_CONFLICT_CODE: Final = "segment_version_conflict"
SEGMENT_VERSION_REQUIRED_CODE: Final = "segment_version_required"
SEGMENT_ACCOUNT_MISMATCH_CODE: Final = "segment_account_mismatch"


def rules_of(filters: AudienceFilter) -> dict[str, Any]:
    """A segment's stored rules: the filters, never contact ids."""
    return {
        "last_inbound_within_days": filters.last_inbound_within_days,
        "lead_statuses": [status.value for status in filters.lead_statuses],
    }


def filter_of(rules: dict[str, Any]) -> AudienceFilter:
    return AudienceFilter(
        last_inbound_within_days=rules.get("last_inbound_within_days"),
        lead_statuses=tuple(LeadStatus(value) for value in rules.get("lead_statuses") or ()),
    )


def narrowed(segment: AudienceFilter, extra: AudienceFilter) -> AudienceFilter:
    """The segment's rules with `extra` applied on top - never wider than either.

    Recency: the shorter window. Lead statuses: the intersection when both name
    some (an empty intersection reaches nobody - that is what was asked for),
    otherwise whichever names some. Hand-picked contacts narrow further.
    """
    windows = [
        days
        for days in (segment.last_inbound_within_days, extra.last_inbound_within_days)
        if days is not None
    ]
    if segment.lead_statuses and extra.lead_statuses:
        statuses = tuple(s for s in segment.lead_statuses if s in set(extra.lead_statuses))
        if not statuses:
            # Both named statuses and none is shared: nobody qualifies. A
            # status set nobody can hold keeps that true through the query.
            return AudienceFilter(
                last_inbound_within_days=min(windows) if windows else None,
                lead_statuses=(),
                contact_ids=(),
                nobody=True,
            )
    else:
        statuses = segment.lead_statuses or extra.lead_statuses
    return AudienceFilter(
        last_inbound_within_days=min(windows) if windows else None,
        lead_statuses=statuses,
        contact_ids=extra.contact_ids,
    )


class CampaignSegmentService:
    """Create, read, edit and delete one workspace's segments."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._segments = CampaignSegmentRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)

    async def list(self) -> list[CampaignSegment]:
        return await self._segments.list_all()

    async def get(self, segment_id: uuid.UUID) -> CampaignSegment:
        return await self._segments.require_by_id(segment_id)

    async def create(
        self,
        *,
        name: str,
        description: str | None,
        rules: AudienceFilter,
        account_id: uuid.UUID | None,
        actor: User | None = None,
    ) -> CampaignSegment:
        """A new segment; at most a hundred per workspace, names unique (ignoring case)."""
        # Counted and inserted as one step: two admins creating the 100th
        # segment together get one segment and one refusal.
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    advisory_locks.CAMPAIGN_SEGMENTS, self._tenant_id.int & 0x7FFFFFFF
                )
            )
        )
        if await self._segments.count() >= MAX_SEGMENTS_PER_WORKSPACE:
            raise ConflictError(
                f"A workspace keeps at most {MAX_SEGMENTS_PER_WORKSPACE} segments. "
                "Delete one first.",
                error_code=SEGMENT_LIMIT_CODE,
            )
        clean = _name(name)
        if await self._segments.name_taken(clean):
            raise ConflictError(
                "A segment with this name exists.", error_code=SEGMENT_NAME_TAKEN_CODE
            )
        if account_id is not None:
            await self._accounts.require_by_id(account_id)
        segment = self._segments.add(
            CampaignSegment(
                tenant_id=self._tenant_id,
                name=clean,
                description=description,
                rules=rules_of(rules),
                account_id=account_id,
                created_by_id=actor.id if actor is not None else None,
                version=1,
            )
        )
        await self._flush()
        await self._session.refresh(segment, ["created_at", "updated_at"])
        logger.info("campaign_segment.created", extra={"segment_id": str(segment.id)})
        return segment

    async def update(
        self, segment_id: uuid.UUID, *, version: int | None, changes: dict[str, Any]
    ) -> CampaignSegment:
        """Edit from the version read (409 `segment_version_conflict` otherwise)."""
        segment = await self._segments.lock_by_id(segment_id)
        if version is None:
            raise ValidationError(
                "Say which version you edited: If-Match or `version`.",
                error_code=SEGMENT_VERSION_REQUIRED_CODE,
            )
        if version != segment.version:
            raise ConflictError(
                "Somebody changed this segment since you opened it. Reload it and edit again.",
                error_code=SEGMENT_VERSION_CONFLICT_CODE,
                details={"version": segment.version},
            )
        if "name" in changes:
            clean = _name(changes["name"])
            if await self._segments.name_taken(clean, other_than=segment.id):
                raise ConflictError(
                    "A segment with this name exists.", error_code=SEGMENT_NAME_TAKEN_CODE
                )
            segment.name = clean
        if "description" in changes:
            segment.description = changes["description"]
        if "rules" in changes:
            segment.rules = rules_of(changes["rules"])
        if "account_id" in changes:
            if changes["account_id"] is not None:
                await self._accounts.require_by_id(changes["account_id"])
            segment.account_id = changes["account_id"]
        segment.version += 1
        await self._flush()
        await self._session.refresh(segment, ["updated_at"])
        return segment

    async def delete(self, segment_id: uuid.UUID) -> None:
        """Delete a segment. Campaigns that used it keep the rules they copied."""
        segment = await self._segments.lock_by_id(segment_id)
        await self._segments.forget_on_campaigns(segment.id)
        await self._session.delete(segment)
        await self._session.flush()

    async def for_audience(self, segment_id: uuid.UUID, *, account_id: uuid.UUID) -> AudienceFilter:
        """A segment's rules for an audience on `account_id` (422 if meant for another number)."""
        segment = await self._segments.require_by_id(segment_id)
        if segment.account_id is not None and segment.account_id != account_id:
            raise ValidationError(
                "This segment is for another WhatsApp number.",
                error_code=SEGMENT_ACCOUNT_MISMATCH_CODE,
            )
        return filter_of(segment.rules)

    async def _flush(self) -> None:
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as error:
            # The unique name, raced past the read check.
            raise ConflictError(
                "A segment with this name exists.", error_code=SEGMENT_NAME_TAKEN_CODE
            ) from error


def _name(name: str) -> str:
    clean = name.strip()
    if not clean:
        raise ValidationError("A segment needs a name.")
    return clean


__all__ = [
    "SEGMENT_ACCOUNT_MISMATCH_CODE",
    "SEGMENT_LIMIT_CODE",
    "SEGMENT_NAME_TAKEN_CODE",
    "SEGMENT_VERSION_CONFLICT_CODE",
    "SEGMENT_VERSION_REQUIRED_CODE",
    "CampaignSegmentService",
    "filter_of",
    "narrowed",
    "rules_of",
]
