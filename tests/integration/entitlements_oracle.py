"""An independent SQL oracle for channel capacity and the AI turn meter (ADR-131).

Written against the tables and nothing else, so it can disagree with the
engine: `EntitlementService` decides in Python from repositories, the census
(`app.repositories.entitlement_census`) in one set-wise statement, and this in
per-workspace queries of its own. Three formulations of one rule; the oracle
tests require all three to agree for every workspace and moment they build.

What it restates, from the decisions rather than from the code:

- **The terms in force.** A serving subscription (trialing, active, past due)
  holds its pinned version; anything else - no subscription, suspended,
  cancelled, expired - the default plan at its version in effect then
  (ADR-061, ENT-16).
- **Channel capacity** (ENT-05, ENT-11). Base: the version's
  `channel_connections`; on a version that never stated channel types, its
  retired `whatsapp_numbers` (choice A); absent, unlimited; a value that is not
  a non-negative integer, unlimited. Slots added by live top-ups and grants -
  granted or under refund review, inside `[granted_at, expires_at)` - general
  when untyped, typed by their channel otherwise, a retired number top-up typed
  WhatsApp. Active connections: status active, never released. Over the limit
  when the connections typed slots do not absorb exceed the general slots.
- **Channel types** (ENT-09): the version's set; unstated, WhatsApp alone.
- **AI turns** (ENT-01..03): `used` is the workspace's `ai_turn` charges in the
  usage cycle in force, whatever their channel; `held` is its turns holding a
  unit, taken inside that cycle and younger than the TTL. The cycle comes from
  `annual_billing_oracle`, which derives it from the billing anchor with
  PostgreSQL's own month arithmetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import LimitKey
from tests.integration.annual_billing_oracle import oracle_entitlement

SERVING: Final = ("trialing", "active", "past_due")
LEGACY_TYPES: Final = frozenset({"whatsapp"})


@dataclass(frozen=True, slots=True)
class OracleCapacity:
    base: int | None
    general_purchased: int
    general_granted: int
    typed_purchased: dict[str, int]
    typed_granted: dict[str, int]
    active: dict[str, int]
    allowed: frozenset[str]

    @property
    def general(self) -> int | None:
        if self.base is None:
            return None
        return self.base + self.general_purchased + self.general_granted

    @property
    def typed(self) -> dict[str, int]:
        channels = set(self.typed_purchased) | set(self.typed_granted)
        return {
            channel: self.typed_purchased.get(channel, 0) + self.typed_granted.get(channel, 0)
            for channel in channels
        }

    @property
    def total(self) -> int | None:
        general = self.general
        return None if general is None else general + sum(self.typed.values())

    @property
    def overflow(self) -> int:
        typed = self.typed
        return sum(max(0, count - typed.get(channel, 0)) for channel, count in self.active.items())

    @property
    def over_limit(self) -> bool:
        general = self.general
        return general is not None and self.overflow > general

    @property
    def outside_allowed_types(self) -> bool:
        return any(count and channel not in self.allowed for channel, count in self.active.items())


@dataclass(frozen=True, slots=True)
class OracleAITurns:
    limit: int | None
    used: int
    held: int
    window: tuple[datetime, datetime]


def _limit(raw: Any) -> int | None:
    """A stored limit as the decisions read it: a non-negative integer, else unlimited."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


async def _terms(
    session: AsyncSession, tenant_id: uuid.UUID, *, at: datetime, default_plan_code: str
) -> tuple[dict[str, Any], list[str] | None] | None:
    """The limits and channel types in force at `at`, or None when nothing is enforced."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT status::text AS status, plan_id, plan_version_id"
                    " FROM subscriptions WHERE tenant_id = :tenant"
                ),
                {"tenant": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if row is not None and row["status"] in SERVING:
        found = (
            await session.execute(
                text("SELECT limits, allowed_channel_types FROM plan_versions WHERE id = :id"),
                {"id": row["plan_version_id"]},
            )
        ).first()
        assert found is not None, "every subscription the oracle reads is pinned"
        return dict(found[0] or {}), found[1]
    plan = (
        await session.execute(
            text("SELECT id, limits, allowed_channel_types FROM plans WHERE code = :code"),
            {"code": default_plan_code},
        )
    ).first()
    if plan is None:
        return None
    version = (
        await session.execute(
            text(
                "SELECT limits, allowed_channel_types FROM plan_versions"
                " WHERE plan_id = :plan AND effective_at <= :at ORDER BY version DESC LIMIT 1"
            ),
            {"plan": plan[0], "at": at},
        )
    ).first()
    if version is None:
        return dict(plan[1] or {}), plan[2]
    return dict(version[0] or {}), version[1]


async def oracle_capacity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    at: datetime,
    default_plan_code: str = "starter",
) -> OracleCapacity | None:
    """The workspace's channel slots and their occupants at `at`; None if unenforced."""
    await session.flush()
    terms = await _terms(session, tenant_id, at=at, default_plan_code=default_plan_code)
    if terms is None:
        return None
    limits, allowed = terms
    if "channel_connections" in limits:
        base = _limit(limits["channel_connections"])
    elif allowed is None:
        base = _limit(limits.get("whatsapp_numbers"))
    else:
        base = None

    general_purchased = general_granted = 0
    typed_purchased: dict[str, int] = {}
    typed_granted: dict[str, int] = {}
    rows = await session.execute(
        text(
            "SELECT entitlement_key::text, channel_type::text, source::text, quantity"
            " FROM topup_purchases WHERE tenant_id = :tenant"
            " AND entitlement_key::text IN ('channel_connections', 'whatsapp_numbers')"
            " AND status::text IN ('granted', 'refund_review')"
            " AND granted_at IS NOT NULL AND granted_at <= :at AND expires_at > :at"
        ),
        {"tenant": tenant_id, "at": at},
    )
    for key, channel, source, quantity in rows.all():
        slot = "whatsapp" if key == "whatsapp_numbers" else channel
        granted = source == "platform_grant"
        if slot is None:
            if granted:
                general_granted += quantity
            else:
                general_purchased += quantity
            continue
        bucket = typed_granted if granted else typed_purchased
        bucket[slot] = bucket.get(slot, 0) + quantity

    active = {
        channel: int(count)
        for channel, count in (
            await session.execute(
                text(
                    "SELECT channel::text, count(*) FROM channel_connections"
                    " WHERE tenant_id = :tenant AND status::text = 'active'"
                    " AND released_at IS NULL GROUP BY 1"
                ),
                {"tenant": tenant_id},
            )
        ).all()
    }
    return OracleCapacity(
        base=base,
        general_purchased=general_purchased,
        general_granted=general_granted,
        typed_purchased=typed_purchased,
        typed_granted=typed_granted,
        active=active,
        allowed=LEGACY_TYPES if allowed is None else frozenset(allowed),
    )


async def oracle_ai_turns(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    at: datetime,
    hold_ttl_seconds: int,
    default_plan_code: str = "starter",
) -> OracleAITurns:
    """The workspace's AI turn allowance at `at`: one meter, every channel (ENT-01)."""
    turns = await oracle_entitlement(
        session,
        tenant_id,
        LimitKey.PERIOD_AI_TURNS,
        at=at,
        default_plan_code=default_plan_code,
    )
    assert turns.window is not None
    held = int(
        await session.scalar(
            text(
                "SELECT count(*) FROM agent_turns WHERE tenant_id = :tenant"
                " AND charge_state::text = 'held'"
                " AND held_at >= :since AND held_at < :until"
                " AND held_at > CAST(:at AS timestamptz) - make_interval(secs => :ttl)"
            ),
            {
                "tenant": tenant_id,
                "since": turns.window[0],
                "until": turns.window[1],
                "at": at,
                "ttl": hold_ttl_seconds,
            },
        )
        or 0
    )
    return OracleAITurns(limit=turns.limit, used=turns.used, held=held, window=turns.window)


__all__ = ["OracleAITurns", "OracleCapacity", "oracle_ai_turns", "oracle_capacity"]
