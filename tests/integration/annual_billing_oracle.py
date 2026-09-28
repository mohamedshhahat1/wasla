"""Independent checks of the monthly + annual billing ledger (ADR-116).

Two instruments, both written in SQL against the tables and nothing else, so
they can disagree with the application:

- **The invariant ledger** (`ledger_violations`): each rule is a query that
  returns the rows breaking it. A clean ledger returns none.
- **The entitlement oracle** (`oracle_entitlement`): recomputes a workspace's
  effective limit, usage and usage window for one key from the rows - pinned
  version, live top-ups and grants, usage events - deriving the usage cycle
  from the billing anchor with PostgreSQL's own month arithmetic, never from
  the stored `usage_period_*` columns the service reads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import RESOURCE_LIMITS, LimitKey
from app.services.entitlement_service import PERIOD_METERS

LEDGER: Final[dict[str, str]] = {
    "subscription price is not a price of its version": """
        SELECT s.id FROM subscriptions s JOIN plan_prices p ON p.id = s.plan_price_id
         WHERE p.plan_version_id IS DISTINCT FROM s.plan_version_id""",
    "live subscription on a priced version names no price": """
        SELECT s.id FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id
         WHERE s.ended_at IS NULL AND v.price > 0 AND s.plan_price_id IS NULL""",
    "subscription on a free version names a price": """
        SELECT s.id FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id
         WHERE v.price <= 0 AND s.plan_price_id IS NOT NULL""",
    "invoice price is not a price of its pinned version": """
        SELECT i.id FROM invoices i JOIN plan_prices p ON p.id = i.plan_price_id
         WHERE p.plan_version_id IS DISTINCT FROM i.plan_version_id""",
    "invoice amount, currency or term is not its price's": """
        SELECT i.id FROM invoices i JOIN plan_prices p ON p.id = i.plan_price_id
         WHERE i.amount_due <> p.amount OR i.currency <> p.currency
            OR i.billing_interval <> p.billing_interval
            OR i.interval_count <> p.interval_count""",
    "priced purchase or renewal names no price": """
        SELECT i.id FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id
         WHERE v.price > 0 AND i.plan_price_id IS NULL
           AND i.purpose::text IN ('checkout', 'renewal', 'manual')""",
    "paid annual invoice does not cover twelve calendar months": """
        SELECT i.id FROM invoices i
         WHERE i.billing_interval = 'yearly' AND i.status = 'paid'
           AND NOT (date_part('year', i.period_end) = date_part('year', i.period_start) + 1
                AND date_part('month', i.period_end) = date_part('month', i.period_start)
                AND i.period_end - i.period_start BETWEEN interval '365 days'
                                                     AND interval '366 days')""",
    "paid monthly invoice does not cover one calendar month": """
        SELECT i.id FROM invoices i
         WHERE i.billing_interval = 'monthly' AND i.status = 'paid'
           AND NOT (i.period_end - i.period_start BETWEEN interval '28 days'
                                                     AND interval '31 days'
                AND (date_part('year', i.period_end) * 12 + date_part('month', i.period_end))
                  = (date_part('year', i.period_start) * 12
                     + date_part('month', i.period_start) + 1))""",
    "usage cycle is not inside the billing term": """
        SELECT s.id FROM subscriptions s
         WHERE s.ended_at IS NULL
           AND NOT (s.usage_period_start >= s.current_period_start
                AND s.usage_period_end <= s.current_period_end
                AND s.usage_period_end > s.usage_period_start)""",
    "usage cycle is longer than a calendar month": """
        SELECT s.id FROM subscriptions s
         WHERE s.ended_at IS NULL AND s.plan_price_id IS NOT NULL
           AND s.usage_period_end - s.usage_period_start > interval '31 days'""",
    "live billing term is not its price's length": """
        SELECT s.id FROM subscriptions s JOIN plan_prices p ON p.id = s.plan_price_id
         WHERE s.ended_at IS NULL AND s.status::text IN ('active', 'past_due')
           AND NOT CASE p.billing_interval
               WHEN 'yearly' THEN s.current_period_end - s.current_period_start
                                  BETWEEN interval '365 days' AND interval '366 days'
               ELSE s.current_period_end - s.current_period_start
                                  BETWEEN interval '28 days' AND interval '31 days'
           END""",
    "scheduled price is not a price of the scheduled version": """
        SELECT s.id FROM subscriptions s JOIN plan_prices p ON p.id = s.scheduled_plan_price_id
         WHERE p.plan_version_id IS DISTINCT FROM s.scheduled_plan_version_id""",
    "scheduled change to a priced version names no price": """
        SELECT s.id FROM subscriptions s JOIN plan_versions v
            ON v.id = s.scheduled_plan_version_id
         WHERE v.price > 0 AND s.scheduled_plan_price_id IS NULL""",
    "offer price, version, plan and workspace disagree": """
        SELECT o.id FROM custom_plan_offers o
          JOIN plan_prices p ON p.id = o.plan_price_id
          JOIN plan_versions v ON v.id = o.plan_version_id
          JOIN plans pl ON pl.id = o.plan_id
         WHERE p.plan_version_id <> v.id OR v.plan_id <> pl.id
            OR pl.tenant_id IS DISTINCT FROM o.tenant_id""",
    "a workspace holds another workspace's custom price": """
        SELECT s.id FROM subscriptions s
          JOIN plan_prices p ON p.id IN (s.plan_price_id, s.scheduled_plan_price_id)
          JOIN plan_versions v ON v.id = p.plan_version_id
          JOIN plans pl ON pl.id = v.plan_id
         WHERE pl.tenant_id IS NOT NULL AND pl.tenant_id <> s.tenant_id
        UNION ALL
        SELECT i.id FROM invoices i
          JOIN plan_prices p ON p.id = i.plan_price_id
          JOIN plan_versions v ON v.id = p.plan_version_id
          JOIN plans pl ON pl.id = v.plan_id
         WHERE pl.tenant_id IS NOT NULL AND pl.tenant_id <> i.tenant_id""",
    "a new customer checkout opened on a retired price": """
        SELECT i.id FROM invoices i JOIN plan_prices p ON p.id = i.plan_price_id
         WHERE i.purpose = 'checkout' AND i.custom_plan_offer_id IS NULL
           AND p.retired_at IS NOT NULL AND i.created_at > p.retired_at""",
    "two active prices in one commercial slot": """
        SELECT min(p.id::text)::uuid FROM plan_prices p WHERE p.retired_at IS NULL
         GROUP BY p.plan_version_id, p.billing_interval, p.interval_count, p.currency
        HAVING count(*) > 1""",
    "a free version carries a price": """
        SELECT p.id FROM plan_prices p JOIN plan_versions v ON v.id = p.plan_version_id
         WHERE v.price <= 0""",
    "a usage top-up outlives a monthly usage cycle": """
        SELECT t.id FROM topup_purchases t
         WHERE t.entitlement_key::text LIKE 'period_%'
           AND t.expires_at - t.billing_period_start > interval '31 days'""",
}


async def ledger_violations(
    session: AsyncSession, *, tenant_ids: list[uuid.UUID] | None = None
) -> dict[str, list[uuid.UUID]]:
    """Every rule with the rows breaking it. `tenant_ids` narrows to workspaces."""
    await session.flush()
    found: dict[str, list[uuid.UUID]] = {}
    for rule, query in LEDGER.items():
        rows = [row[0] for row in (await session.execute(text(query))).all()]
        if tenant_ids is not None and rows:
            owned = await session.execute(
                text(
                    "SELECT id FROM subscriptions WHERE id = ANY(:ids) AND tenant_id = ANY(:t) "
                    "UNION SELECT id FROM invoices WHERE id = ANY(:ids) AND tenant_id = ANY(:t) "
                    "UNION SELECT id FROM custom_plan_offers WHERE id = ANY(:ids) "
                    "AND tenant_id = ANY(:t) "
                    "UNION SELECT id FROM topup_purchases WHERE id = ANY(:ids) "
                    "AND tenant_id = ANY(:t)"
                ),
                {"ids": rows, "t": tenant_ids},
            )
            rows = [row[0] for row in owned.all()]
        if rows:
            found[rule] = rows
    return found


@dataclass(frozen=True, slots=True)
class OracleEntitlement:
    limit: int | None
    used: int
    window: tuple[datetime, datetime] | None


_TERMS_SQL: Final = """
    SELECT s.id, s.status::text AS status, s.ended_at, s.plan_id, s.plan_version_id,
           s.current_period_start, s.current_period_end, s.billing_anchor_at,
           s.usage_period_start, s.usage_period_end
      FROM subscriptions s WHERE s.tenant_id = :tenant
"""

# The anchored month containing :at, by PostgreSQL's month arithmetic from the
# anchor itself (`anchor + k months`, clamped to the month), clipped to the
# billing term. Independent of the stored usage columns.
_CYCLE_SQL: Final = """
    WITH k AS (
        SELECT max(n) AS n FROM generate_series(-2400, 2400) AS n
         WHERE CAST(:anchor AS timestamptz) + make_interval(months => n)
               <= CAST(:at AS timestamptz)
    )
    SELECT greatest(CAST(:anchor AS timestamptz) + make_interval(months => k.n),
                    CAST(:start AS timestamptz)),
           least(CAST(:anchor AS timestamptz) + make_interval(months => k.n + 1),
                 CAST(:end AS timestamptz))
      FROM k
"""


async def oracle_entitlement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: LimitKey,
    *,
    at: datetime,
    default_plan_code: str = "starter",
) -> OracleEntitlement:
    """What `EntitlementService.check(key)` should answer at `at`, from the rows."""
    await session.flush()
    row = (await session.execute(text(_TERMS_SQL), {"tenant": tenant_id})).mappings().first()
    serving = row is not None and row["status"] in ("trialing", "active", "past_due")
    if serving:
        assert row is not None
        version_id = row["plan_version_id"]
    else:
        version_id = await session.scalar(
            text(
                "SELECT v.id FROM plan_versions v JOIN plans p ON p.id = v.plan_id "
                "WHERE p.code = :code AND v.effective_at <= :at "
                "ORDER BY v.version DESC LIMIT 1"
            ),
            {"code": default_plan_code, "at": at},
        )
    raw = await session.scalar(
        text("SELECT v.limits -> :key FROM plan_versions v WHERE v.id = :id"),
        {"key": key.value, "id": version_id},
    )
    base = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    added = int(
        await session.scalar(
            text(
                "SELECT coalesce(sum(quantity), 0) FROM topup_purchases "
                "WHERE tenant_id = :tenant AND entitlement_key::text = :key "
                "AND status::text IN ('granted', 'refund_review') "
                "AND granted_at <= :at AND expires_at > :at"
            ),
            {"tenant": tenant_id, "key": key.value, "at": at},
        )
        or 0
    )
    limit = None if base is None else base + added

    if key in RESOURCE_LIMITS:
        return OracleEntitlement(limit=limit, used=-1, window=None)
    if serving:
        assert row is not None
        start, end = row["current_period_start"], row["current_period_end"]
        stored = (row["usage_period_start"], row["usage_period_end"])
        if stored[0] <= at < stored[1] or at >= end or at < stored[1]:
            window = stored
        else:
            anchor = row["billing_anchor_at"] or start
            cycle = (
                await session.execute(
                    text(_CYCLE_SQL), {"anchor": anchor, "at": at, "start": start, "end": end}
                )
            ).one()
            window = (cycle[0], cycle[1])
    else:
        window_row = (
            await session.execute(
                text(
                    "SELECT date_trunc('month', CAST(:at AS timestamptz) AT TIME ZONE 'UTC') "
                    "AT TIME ZONE 'UTC', (date_trunc('month', CAST(:at AS timestamptz) "
                    "AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC'"
                ),
                {"at": at},
            )
        ).one()
        window = (window_row[0], window_row[1])
    meters = [meter.value for meter in PERIOD_METERS[key]]
    used = int(
        await session.scalar(
            text(
                "SELECT coalesce(sum(quantity), 0) FROM usage_events "
                "WHERE tenant_id = :tenant AND event_type::text = ANY(:meters) "
                "AND occurred_at >= :since AND occurred_at < :until"
            ),
            {"tenant": tenant_id, "meters": meters, "since": window[0], "until": window[1]},
        )
        or 0
    )
    return OracleEntitlement(limit=limit, used=used, window=window)


__all__ = ["LEDGER", "OracleEntitlement", "ledger_violations", "oracle_entitlement"]
