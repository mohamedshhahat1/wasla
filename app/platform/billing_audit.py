"""The audit entry every platform billing mutation writes (BILL-12, spec: audit).

One shape for all of them: who (the staff member and their platform role),
what (the action and its target), where (the workspace, when there is one),
why (the operator's reason), what changed (before and after), and which request
did it. Written through `AuditTrail`, so it lands in the workspace's own trail
when there is a workspace and in the platform's otherwise.

Never a secret. `before` and `after` are built by the callers from commercial
fields - prices, limits, statuses, versions - and `_scrub` removes any key that
names a credential, as a second line in case a future caller passes a row
wholesale.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_var
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.user import User
from app.services.audit_service import AuditTrail

# Keys that must never reach an audit row, however a caller built its dict.
_SECRET_MARKERS: Final = (
    "token",
    "secret",
    "hmac",
    "fingerprint",
    "api_key",
    "password",
    "payment_key",
)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in _SECRET_MARKERS)
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def record_platform_billing(
    session: AsyncSession,
    action: AuditAction,
    *,
    actor: User,
    reason: str | None,
    target_type: str,
    target_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None = None,
    target_label: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Stage the audit entry for one platform billing mutation."""
    meta: dict[str, Any] = {
        "actor_role": actor.platform_role.value if actor.platform_role else None,
        "reason": reason,
        "before": _scrub(before) if before is not None else None,
        "after": _scrub(after) if after is not None else None,
        "request_id": request_id_var.get(),
    }
    if extra:
        meta.update(_scrub(extra))
    AuditTrail(session, tenant_id=tenant_id).record(
        action,
        actor=actor,
        actor_kind=AuditActorKind.PLATFORM_STAFF,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        meta=meta,
    )


__all__ = ["record_platform_billing"]
