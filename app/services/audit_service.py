"""Recording deliberate acts.

Shaped like the usage and analytics recorders and for the same reason: staged in
the caller's transaction, no I/O of its own, synchronous. An audit entry that
survives the rollback of the thing it describes is a log saying somebody did
something they did not do — which is worse than no log, because it is believed.

The reverse case is the one that decides the design. An action whose audit entry
*fails* must not silently succeed, so nothing here swallows exceptions: staging
performs no I/O, and a failure at flush takes the whole unit of work with it. If
we cannot say who disconnected the number, we do not disconnect it.

Labels are copied at write time rather than joined for later. A person who is
deleted or renamed must not make last quarter's entry unreadable, and "user
8f3c… did something to lead 91ab…" is useless precisely when somebody is asking.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.user import User

# What a system actor is called in the log. A name rather than a blank, because
# "nobody did this, time did" is a real answer to "who cancelled my plan".
SYSTEM_LABEL = "system"


class AuditTrail:
    """Stages audit entries for one workspace, or for the platform.

    `tenant_id` is None for a platform action. That is not an oversight in the
    caller: a platform administrator acts across workspaces rather than inside
    one, and those are the acts most worth recording.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def record(
        self,
        action: AuditAction,
        *,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        target_label: str | None = None,
        tenant_id: uuid.UUID | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Stage one entry.

        `actor_kind` is inferred from the actor when it is not given: a user
        holding a platform role acting through a platform route is platform
        staff, anybody else is a user, and no actor at all is the system.
        Passing it explicitly wins, because a platform administrator acting
        *inside* a workspace they belong to is doing so as a member.
        """
        kind = actor_kind if actor_kind is not None else _kind_for(actor)
        entry = AuditLog(
            tenant_id=tenant_id if tenant_id is not None else self._tenant_id,
            action=action,
            actor_kind=kind,
            actor_id=actor.id if actor is not None else None,
            actor_label=_label_for(actor, kind),
            target_type=target_type,
            target_id=target_id,
            target_label=target_label,
            meta=meta,
        )
        self._session.add(entry)
        return entry


def _kind_for(actor: User | None) -> AuditActorKind:
    if actor is None:
        return AuditActorKind.SYSTEM
    if actor.platform_role is not None:
        return AuditActorKind.PLATFORM_STAFF
    return AuditActorKind.USER


def _label_for(actor: User | None, kind: AuditActorKind) -> str:
    """How the actor is described in the entry.

    An email, because that is what somebody reading the log will recognise and
    can act on. It is a copy: the account may be deleted next week, and the
    entry has to keep meaning something.
    """
    if actor is None:
        return SYSTEM_LABEL
    return actor.email
