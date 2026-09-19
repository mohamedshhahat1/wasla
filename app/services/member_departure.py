"""What a colleague's work becomes when they leave a workspace.

Revocation used to touch the membership row and nothing else (CRM-11). The
removed person stayed the owner of their conversations and leads - customers
assigned to somebody who could no longer open them, and nothing surfacing them
for anybody else - and their scheduled reminders went on being sent in their
name.

The rules, all product decisions (docs/CRM.md):

- **Their conversations and leads become unassigned** (PD-CRM-2). Not handed to
  whoever removed them: removing a colleague is not volunteering to take on
  their customers, and an unowned conversation is findable where a silently
  transferred one is not.
- **Their pending reminders are cancelled**, with `member_revoked` as the reason
  (PD-CRM-8). A reminder is a person's intention; once the person has gone,
  nobody intends it.
- **History stays theirs.** Notes, activity, audit entries and follow-ups that
  already went out still name them. Only what is still *open* moves.

This runs inside the transaction that revokes the membership and after the
membership row has been written, so the row is locked. An assignment to the
same person share-locks that row first (`hold_active_for_user`), so the two
serialise: an assignment that got there first is found and undone here, and one
that arrives second waits, sees a revoked membership and answers not-found.

Locks are taken follow-ups, then leads, then conversations - the order the
follow-up sweep and the lead tool already take them in, so this cannot deadlock
against either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.user import User
from app.services.follow_up_service import FollowUpService
from app.services.inbox_service import InboxService
from app.services.lead_service import LeadService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Departure:
    """How much open work a removal released."""

    follow_ups_cancelled: int
    leads_unassigned: int
    conversations_unassigned: int


async def release_departing_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User | None,
) -> Departure:
    """Release everything open that `user_id` owns in this workspace.

    `actor` is who removed them - themselves, when they left - and is recorded
    against every unassignment. None only for a removal the system made.
    """
    cancelled = await FollowUpService(
        session=session, tenant_id=tenant_id
    ).cancel_member_follow_ups(user_id=user_id)
    leads = await LeadService(session=session, tenant_id=tenant_id).release_member(
        user_id=user_id,
        actor_id=actor.id if actor is not None else None,
    )
    conversations = await InboxService(session=session, tenant_id=tenant_id).release_member(
        user_id=user_id,
        actor=actor,
    )
    departure = Departure(
        follow_ups_cancelled=cancelled,
        leads_unassigned=leads,
        conversations_unassigned=conversations,
    )
    logger.info(
        "membership.work_released",
        extra={
            "event": "membership.work_released",
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "follow_ups_cancelled": cancelled,
            "leads_unassigned": leads,
            "conversations_unassigned": conversations,
        },
    )
    return departure


__all__ = ["Departure", "release_departing_member"]
