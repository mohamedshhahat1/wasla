"""Every workspace's channel capacity, computed in one SQL statement (ENT-05, ENT-16).

`EntitlementService.check` is the only place a limit is *enforced*; this module
never decides anything. It recomputes, set-wise and from the tables alone, what
that service computes one workspace at a time - for the two readers that need
every workspace at once and must not loop over them:

- the `wasla_channel_capacity_over_limit_workspaces` gauge, at scrape time;
- the entitlement invariants and the oracle tests, which compare it with the
  service for every workspace and so prove the two agree.

The rules it restates, each from the code that owns it:

- **The plan in force** (`EntitlementService._resolve`): a serving subscription
  (trialing, active, past due) holds its pinned version; anything else falls
  back to the deployment's default plan at its version in effect now. With no
  plan at all, capacity is unenforced and nothing is over it.
- **The base** (`entitlement_terms.term_limit`): the version's
  `channel_connections`; on a version that states no channel types (published
  before ADR-131) and no such key, its `whatsapp_numbers`; a value that is not
  a non-negative integer is unlimited (`validated_limit`).
- **Live top-ups** (`TopupPurchaseRepository.active_totals`): granted or under
  refund review, inside `[granted_at, expires_at)`. A typed slot serves its
  channel; a legacy `whatsapp_numbers` top-up is a typed WhatsApp slot.
- **The fit rule** (`channel_fit`): overflow = sum over channels of
  `max(0, active - typed)`; over the limit when overflow exceeds the general
  slots.
- **Allowed types** (`term_channel_types`): unstated means WhatsApp alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import SERVING_STATUSES

_SERVING = ", ".join(f"'{status.value}'" for status in sorted(SERVING_STATUSES))


def _int_or_null(expression: str) -> str:
    """SQL for `validated_limit`: a non-negative JSON integer, else NULL (unlimited)."""
    return (
        f"CASE WHEN jsonb_typeof({expression}) = 'number'"
        f" AND ({expression} #>> '{{}}') ~ '^[0-9]+$'"
        f" THEN ({expression} #>> '{{}}')::bigint END"
    )


#: One row per workspace with a plan in force: `base` (NULL = unlimited), the
#: general slots top-ups and grants add, the overflow typed slots do not
#: absorb, the active connections, whether it is over capacity, whether any
#: active connection is of a type the plan does not allow, and whether its
#: subscription is serving. Parameters: `:now`, `:default_plan_code`.
CAPACITY_CENSUS_SQL = f"""
WITH serving AS (
    SELECT s.tenant_id, s.plan_id, s.plan_version_id
    FROM subscriptions s
    WHERE s.status::text IN ({_SERVING})
),
in_force AS (
    SELECT t.id AS tenant_id,
           (sv.tenant_id IS NOT NULL) AS serving,
           coalesce(sv.plan_id, d.id) AS plan_id,
           CASE
               WHEN sv.tenant_id IS NOT NULL AND sv.plan_version_id IS NOT NULL
                   THEN sv.plan_version_id
               ELSE (
                   SELECT v.id FROM plan_versions v
                   WHERE v.plan_id = coalesce(sv.plan_id, d.id) AND v.effective_at <= :now
                   ORDER BY v.version DESC LIMIT 1
               )
           END AS version_id
    FROM tenants t
    LEFT JOIN serving sv ON sv.tenant_id = t.id
    LEFT JOIN plans d ON d.code = :default_plan_code AND sv.tenant_id IS NULL
),
terms AS (
    SELECT f.tenant_id, f.serving,
           CASE WHEN v.id IS NOT NULL THEN v.limits ELSE p.limits END AS limits,
           CASE WHEN v.id IS NOT NULL THEN v.allowed_channel_types
                ELSE p.allowed_channel_types END AS allowed
    FROM in_force f
    JOIN plans p ON p.id = f.plan_id
    LEFT JOIN plan_versions v ON v.id = f.version_id
),
bases AS (
    SELECT tenant_id, serving,
           coalesce(allowed, ARRAY['whatsapp']::varchar[]) AS allowed,
           CASE
               WHEN coalesce(limits, '{{}}'::jsonb) ? 'channel_connections'
                   THEN {_int_or_null("limits -> 'channel_connections'")}
               WHEN allowed IS NULL
                   THEN {_int_or_null("limits -> 'whatsapp_numbers'")}
           END AS base
    FROM terms
),
topups AS (
    SELECT tp.tenant_id,
           CASE WHEN tp.entitlement_key::text = 'whatsapp_numbers' THEN 'whatsapp'
                ELSE tp.channel_type::text END AS slot_channel,
           sum(tp.quantity) AS quantity
    FROM topup_purchases tp
    WHERE tp.entitlement_key::text IN ('channel_connections', 'whatsapp_numbers')
      AND tp.status::text IN ('granted', 'refund_review')
      AND tp.granted_at IS NOT NULL AND tp.granted_at <= :now AND tp.expires_at > :now
    GROUP BY 1, 2
),
general_extra AS (
    SELECT tenant_id, sum(quantity) AS extra FROM topups
    WHERE slot_channel IS NULL GROUP BY tenant_id
),
active AS (
    SELECT tenant_id, channel::text AS channel, count(*) AS n
    FROM channel_connections
    WHERE status::text = 'active' AND released_at IS NULL
    GROUP BY 1, 2
),
occupancy AS (
    SELECT a.tenant_id,
           sum(a.n) AS active,
           sum(greatest(a.n - coalesce(ty.quantity, 0), 0)) AS overflow
    FROM active a
    LEFT JOIN topups ty ON ty.tenant_id = a.tenant_id AND ty.slot_channel = a.channel
    GROUP BY a.tenant_id
),
outside AS (
    SELECT DISTINCT a.tenant_id
    FROM active a JOIN bases b ON b.tenant_id = a.tenant_id
    WHERE NOT (a.channel = ANY (b.allowed::text[]))
)
SELECT b.tenant_id,
       b.serving,
       b.base,
       coalesce(g.extra, 0) AS general_extra,
       coalesce(o.active, 0) AS active,
       coalesce(o.overflow, 0) AS overflow,
       (b.base IS NOT NULL AND coalesce(o.overflow, 0) > b.base + coalesce(g.extra, 0))
           AS over_limit,
       (x.tenant_id IS NOT NULL) AS outside_allowed_types
FROM bases b
LEFT JOIN general_extra g ON g.tenant_id = b.tenant_id
LEFT JOIN occupancy o ON o.tenant_id = b.tenant_id
LEFT JOIN outside x ON x.tenant_id = b.tenant_id
"""  # noqa: S608 - interpolates module constants only; values are bound parameters


@dataclass(frozen=True, slots=True)
class CapacityCensusRow:
    """One workspace's channel capacity as the census computes it."""

    tenant_id: uuid.UUID
    serving: bool
    base: int | None
    general_extra: int
    active: int
    overflow: int
    over_limit: bool
    outside_allowed_types: bool

    @property
    def general(self) -> int | None:
        return None if self.base is None else self.base + self.general_extra


class ChannelCapacityCensus:
    """Every workspace's channel capacity, set-wise, for gauges and invariants."""

    def __init__(self, session: AsyncSession, *, default_plan_code: str | None) -> None:
        self._session = session
        self._default_plan_code = default_plan_code

    async def rows(self, *, now: datetime) -> list[CapacityCensusRow]:
        result = await self._session.execute(
            text(CAPACITY_CENSUS_SQL),
            {"now": now, "default_plan_code": self._default_plan_code},
        )
        return [
            CapacityCensusRow(
                tenant_id=row.tenant_id,
                serving=bool(row.serving),
                base=None if row.base is None else int(row.base),
                general_extra=int(row.general_extra),
                active=int(row.active),
                overflow=int(row.overflow),
                over_limit=bool(row.over_limit),
                outside_allowed_types=bool(row.outside_allowed_types),
            )
            for row in result.all()
        ]

    async def over_limit_workspaces(self, *, now: datetime) -> int:
        """Workspaces holding more connections than the capacity in force allows."""
        statement = (
            f"SELECT count(*) FROM ({CAPACITY_CENSUS_SQL}) census WHERE over_limit"  # noqa: S608
        )
        result = await self._session.execute(
            text(statement), {"now": now, "default_plan_code": self._default_plan_code}
        )
        return int(result.scalar_one())


__all__ = ["CAPACITY_CENSUS_SQL", "CapacityCensusRow", "ChannelCapacityCensus"]
