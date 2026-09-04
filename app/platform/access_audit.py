"""Recording that platform staff read across workspaces.

Every platform *write* was already audited — a payment recorded, an invoice
voided, an account disabled — and no platform *read* was. That asymmetry was
defensible while the reads were aggregates and it stops being defensible the
moment a customer asks who looked at their workspace (ADR-095).

**Reads are still not audited generally, and that has not changed.**
`AuditAction` says so in as many words: a row per page view would bury the acts
that matter under a million that do not. What makes these different is not that
they are reads, it is *whose data they are*. A workspace administrator reading
their own inbox is looking at their own business. A platform administrator
reading the estate is looking at somebody else's, and there are perhaps a
handful of such people, making a handful of requests a day.

**The entry names the class of data, never the data.** No search string — an
operator searching a workspace list types an address as often as a company name.
No workspace name, no customer content, no filter values. Who, what kind of
thing, which workspace if one was named, and when. That is enough to answer the
question the trail exists for and not enough to be worth stealing.

**It fails closed, and it does so by inheriting rather than by choosing.** The
entry is staged in the request's own transaction, and `CommittingRoute` commits
that transaction before the response is emitted (ADR-062). So a failure to
record the access is a failure to serve it, and the reader gets an error rather
than data nobody knows they saw. That is the same rule `AuditTrail` states for
writes — "if we cannot say who disconnected the number, we do not disconnect
it" — and it wanted no new machinery to hold here.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.user import User
from app.services.audit_service import AuditTrail

# What was reached, as a bounded vocabulary rather than a table name. A reader
# of the trail wants "they looked at billing" rather than "they selected from
# invoices", and a column name in an audit entry is a schema detail that ages.
RESOURCE_WORKSPACES = "workspaces"
RESOURCE_PLATFORM_USAGE = "platform_usage"
RESOURCE_AUDIT_LOG = "audit_log"


class PlatformAccessAudit:
    """Stages one entry per privileged cross-workspace read."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # No tenant on the trail itself: a platform act belongs to the platform.
        # An entry that concerns one workspace names it in `target_id` instead,
        # so it is findable from either direction.
        self._audit = AuditTrail(session)

    def _record(
        self,
        action: AuditAction,
        *,
        actor: User,
        resource: str,
        tenant_id: uuid.UUID | None = None,
        detail: dict[str, object] | None = None,
    ) -> AuditLog:
        meta: dict[str, object] = {"resource": resource}
        if detail:
            meta.update(detail)
        return self._audit.record(
            action,
            actor=actor,
            # Explicit rather than inferred. `_kind_for` would reach the same
            # answer from the actor's role, but this is a platform route and
            # saying so at the call site means a platform administrator who
            # also belongs to a workspace cannot be recorded as a member.
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="tenant" if tenant_id is not None else "platform",
            target_id=tenant_id,
            meta=meta,
        )

    def overview_read(self, *, actor: User, windowed: bool) -> AuditLog:
        """The estate at a glance: counts, with no workspace identifiable.

        Audited anyway, and the reasoning is worth stating because the audit
        that found this said aggregates were defensible. They are — but the
        cost of recording six requests a day is nothing, and the question a
        privacy trail answers is "who was looking at us at all", which an
        aggregate read is part of. What is *not* recorded is any window
        boundary: a date range is a filter, and filters are how an operator
        narrows to one customer.
        """
        return self._record(
            AuditAction.PLATFORM_OVERVIEW_READ,
            actor=actor,
            resource=RESOURCE_PLATFORM_USAGE,
            detail={"windowed": windowed},
        )

    def workspaces_read(
        self,
        *,
        actor: User,
        returned: int,
        searched: bool,
        filtered: bool,
    ) -> AuditLog:
        """A page of workspaces, each with what it consumed.

        The sensitive one of the two listings: it names customers and their
        traffic. `searched` is a boolean and never the term, because an operator
        looking for a particular business types an address as readily as a
        name. `returned` is a count, which says how much was seen without
        saying of whom.
        """
        return self._record(
            AuditAction.PLATFORM_WORKSPACES_READ,
            actor=actor,
            resource=RESOURCE_WORKSPACES,
            detail={"returned": returned, "searched": searched, "filtered": filtered},
        )

    def audit_log_read(
        self,
        *,
        actor: User,
        tenant_id: uuid.UUID | None,
        returned: int,
    ) -> AuditLog:
        """Another workspace's trail — the deepest read on this surface.

        `tenant_id` is recorded when the reader narrowed to one workspace,
        because that is precisely the access a customer would want to know
        about, and a workspace id is not customer content: it is the subject of
        the entry rather than its payload. Omitted when the read was
        platform-wide, which is a different act and reads as one.
        """
        return self._record(
            AuditAction.PLATFORM_AUDIT_LOG_READ,
            actor=actor,
            resource=RESOURCE_AUDIT_LOG,
            tenant_id=tenant_id,
            detail={"returned": returned},
        )


__all__ = [
    "RESOURCE_AUDIT_LOG",
    "RESOURCE_PLATFORM_USAGE",
    "RESOURCE_WORKSPACES",
    "PlatformAccessAudit",
]
