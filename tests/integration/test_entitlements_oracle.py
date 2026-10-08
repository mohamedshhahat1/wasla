"""Channel capacity and the AI turn meter against an independent SQL oracle (ADR-131).

A generated population of workspaces - every serving and non-serving status
and none at all, monthly and yearly prices, a base of zero, a few or
unlimited, a version that never stated its channel types, an explicit "no
channel" set; general and typed slots, purchased and granted, live, expired,
not yet started, under refund review and cancelled, and a retired number
top-up; active, disabled and released connections on every channel; AI turns
charged on several channels, held, held long ago and released; open and
resolved reductions - and a set of moments across each term.

For every workspace and moment three formulations must agree:
`EntitlementService` (the engine), `ChannelCapacityCensus` (the set-wise
statement the gauge and the invariants read) and
`entitlements_oracle` (per-workspace SQL written from the decisions).
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent_turn import (
    AgentTurn,
    AgentTurnState,
    AITurnChargeState,
    AITurnReleaseReason,
)
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    PlanVersion,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.conversation import Contact, Conversation
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus
from app.db.models.tenant import Tenant
from app.db.models.topup import (
    TopupEntitlement,
    TopupProduct,
    TopupPurchase,
    TopupScope,
    TopupSource,
    TopupStatus,
    TopupValidity,
)
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.entitlement_census import CapacityCensusRow, ChannelCapacityCensus
from app.services import billing_calendar
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import ORIGINAL_TERMS_EFFECTIVE_AT, PlanCatalog
from tests.integration.entitlements_oracle import (
    OracleCapacity,
    oracle_ai_turns,
    oracle_capacity,
)
from tests.integration.plan_catalogue import own_plan

pytestmark = pytest.mark.integration

WORKSPACES = 42
MOMENTS = 4
HOLD_TTL_SECONDS = 900
CHANNELS = tuple(channel.value for channel in Channel)
STATUSES: tuple[SubscriptionStatus | None, ...] = (
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.SUSPENDED,
    SubscriptionStatus.CANCELLED,
    SubscriptionStatus.EXPIRED,
    None,
)
#: (limits, allowed channel types); None types is a version published before ADR-131.
TERMS: tuple[tuple[dict[str, int], list[str] | None], ...] = (
    ({"channel_connections": 0, "period_ai_turns": 0}, ["whatsapp", "instagram"]),
    ({"channel_connections": 2, "period_ai_turns": 50}, ["whatsapp", "instagram", "messenger"]),
    ({}, list(CHANNELS)),
    ({"channel_connections": 1, "period_ai_turns": 10}, []),
    ({"channel_connections": 3, "period_ai_turns": 100}, ["instagram", "telegram"]),
    ({"whatsapp_numbers": 1, "period_ai_turns": 20}, None),
)
TOPUP_STATES = ("live", "expired", "future", "review", "cancelled")


async def _versions(
    session: AsyncSession,
) -> list[tuple[Plan, PlanVersion, PlanPrice | None, PlanPrice | None]]:
    await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"channel_connections": 1, "period_ai_turns": 5},
        allowed_channel_types=["whatsapp"],
    )
    catalog = PlanCatalog(session)
    built: list[tuple[Plan, PlanVersion, PlanPrice | None, PlanPrice | None]] = []
    for index, (limits, types) in enumerate(TERMS):
        code = f"oracle-ent-{index}-{uuid.uuid4().hex[:6]}"
        if types is None:
            built.append(await _legacy(session, code=code, limits=limits))
            continue
        plan = await own_plan(
            session,
            code=code,
            price=Decimal("100.00"),
            limits=limits,
            allowed_channel_types=types,
        )
        version = await catalog.current_version(plan)
        assert version is not None
        monthly = await catalog.price_for_term(version, interval=BillingInterval.MONTHLY)
        assert monthly is not None
        yearly = PlanPrice(
            plan_version_id=version.id,
            billing_interval=BillingInterval.YEARLY,
            interval_count=1,
            amount=Decimal("1000.00"),
            currency="EGP",
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(yearly)
        await session.flush()
        built.append((plan, version, monthly, yearly))
    return built


async def _legacy(
    session: AsyncSession, *, code: str, limits: dict[str, int]
) -> tuple[Plan, PlanVersion, PlanPrice | None, PlanPrice | None]:
    """A free version shaped as one published before 0093: no channel types, the number key.

    The version trigger now refuses that shape; it is lifted inside this test's
    rolled-back transaction only, and the immutability trigger is untouched.
    """
    plan = await own_plan(
        session, code=code, price=Decimal("0.00"), allowed_channel_types=["whatsapp"]
    )
    version_id = uuid.uuid4()
    await session.execute(
        text("ALTER TABLE plan_versions DISABLE TRIGGER plan_versions_entitlement_terms")
    )
    await session.execute(
        text(
            "INSERT INTO plan_versions (id, plan_id, version, name, price, currency, interval,"
            " trial_days, limits, effective_at, created_at)"
            " VALUES (:id, :plan, 77, 'Legacy', 0, 'EGP', 'monthly', 0, CAST(:limits AS jsonb),"
            " :at, :at)"
        ),
        {
            "id": version_id,
            "plan": plan.id,
            "limits": __import__("json").dumps(limits),
            "at": ORIGINAL_TERMS_EFFECTIVE_AT,
        },
    )
    await session.execute(
        text("ALTER TABLE plan_versions ENABLE TRIGGER plan_versions_entitlement_terms")
    )
    version = await session.get(PlanVersion, version_id)
    assert version is not None
    return plan, version, None, None


async def _product(session: AsyncSession) -> TopupProduct:
    product = TopupProduct(
        code=f"oracle-slots-{uuid.uuid4().hex[:8]}",
        name="Channel slots",
        entitlement_key=TopupEntitlement.CHANNEL_CONNECTIONS,
        quantity=1,
        price=Decimal("50.00"),
        currency="EGP",
        scope=TopupScope.GLOBAL,
        is_active=True,
        is_public=True,
        validity_policy=TopupValidity.CURRENT_PERIOD_END,
    )
    session.add(product)
    await session.flush()
    return product


class _Builder:
    """One workspace's rows, from a seeded generator."""

    def __init__(self, session: AsyncSession, rng: random.Random, product: TopupProduct) -> None:
        self.session = session
        self.rng = rng
        self.product = product

    async def connections(self, tenant: Tenant) -> WhatsAppAccount:
        rng = self.rng
        number = await self._number(tenant)
        for channel in Channel:
            extra = rng.randint(0, 2) - (1 if channel is Channel.WHATSAPP else 0)
            for _ in range(max(extra, 0)):
                if channel is Channel.WHATSAPP:
                    await self._number(tenant)
                else:
                    self._connection(tenant, channel, ConnectionStatus.ACTIVE)
        # Neither takes a slot (ENT-07).
        self._connection(tenant, rng.choice(list(Channel)[1:]), ConnectionStatus.DISABLED)
        released = self._connection(
            tenant, rng.choice(list(Channel)[1:]), ConnectionStatus.RELEASED
        )
        released.released_at = datetime.now(UTC) - timedelta(days=2)
        await self.session.flush()
        return number

    async def _number(self, tenant: Tenant) -> WhatsAppAccount:
        tag = uuid.uuid4().hex[:10]
        number = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"oracle-{tag}",
            waba_id=f"waba-{tag}",
            display_phone_number="+201000000000",
        )
        self.session.add(number)
        await self.session.flush()
        return number

    def _connection(
        self, tenant: Tenant, channel: Channel, status: ConnectionStatus
    ) -> ChannelConnection:
        connection = ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            channel=channel,
            external_account_id=f"{channel.value}-{uuid.uuid4().hex[:10]}",
            status=status,
            ownership_started_at=datetime.now(UTC) - timedelta(days=self.rng.randint(1, 300)),
        )
        self.session.add(connection)
        return connection

    async def slots(
        self, tenant: Tenant, subscription: Subscription | None, start: datetime, end: datetime
    ) -> None:
        rng = self.rng
        for _ in range(rng.randint(2, 6)):
            state = rng.choice(TOPUP_STATES)
            source = rng.choice([TopupSource.PURCHASE, TopupSource.PLATFORM_GRANT])
            typed = rng.random() < 0.5
            channel = Channel(rng.choice(CHANNELS)) if typed else None
            granted = start + timedelta(days=rng.randint(0, 20))
            expires = granted + timedelta(days=rng.randint(10, 400))
            if state == "future":
                granted = end - timedelta(days=1)
                expires = end + timedelta(days=30)
            if state == "expired":
                expires = granted + timedelta(days=1)
            await self._slot(
                tenant,
                subscription,
                source=source,
                key=TopupEntitlement.CHANNEL_CONNECTIONS,
                channel=channel,
                quantity=rng.randint(1, 3),
                status={
                    "live": TopupStatus.GRANTED,
                    "expired": TopupStatus.EXPIRED,
                    "future": TopupStatus.GRANTED,
                    "review": TopupStatus.REFUND_REVIEW,
                    "cancelled": TopupStatus.CANCELLED,
                }[state],
                granted=granted if state != "cancelled" else None,
                expires=expires,
            )
        if rng.random() < 0.4:
            # A number top-up bought before ADR-131: a typed WhatsApp slot now.
            # The trigger refusing the retired key on new rows is lifted for
            # this insert only; the deferred grant checks queued so far run
            # first, which the table must be free of to be altered.
            await self.session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await self.session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            await self.session.execute(
                text("ALTER TABLE topup_purchases DISABLE TRIGGER topup_purchases_retired_key")
            )
            await self._slot(
                tenant,
                subscription,
                source=TopupSource.PLATFORM_GRANT,
                key=TopupEntitlement.WHATSAPP_NUMBERS,
                channel=None,
                quantity=rng.randint(1, 2),
                status=TopupStatus.GRANTED,
                granted=start,
                expires=end + timedelta(days=60),
            )
            await self.session.execute(
                text("ALTER TABLE topup_purchases ENABLE TRIGGER topup_purchases_retired_key")
            )

    async def _slot(
        self,
        tenant: Tenant,
        subscription: Subscription | None,
        *,
        source: TopupSource,
        key: TopupEntitlement,
        channel: Channel | None,
        quantity: int,
        status: TopupStatus,
        granted: datetime | None,
        expires: datetime,
    ) -> None:
        invoice_id = None
        sale: dict[str, Any] = {
            "unit_price": Decimal("0.00"),
            "total_amount": Decimal("0.00"),
            "reason": "Oracle population.",
        }
        if source is TopupSource.PURCHASE:
            invoice = Invoice(
                tenant_id=tenant.id,
                subscription_id=subscription.id if subscription is not None else None,
                status=InvoiceStatus.PAID,
                purpose=InvoicePurpose.TOPUP,
                plan_code=f"topup:{self.product.code}"[:50],
                # No payment rows: the oracle reads slots, not money, and an
                # invoice holds exactly the money applied to it (a trigger).
                amount_due=Decimal("50.00"),
                amount_paid=Decimal("0.00"),
                currency="EGP",
                period_start=(granted or expires) - timedelta(days=1),
                period_end=expires,
                paid_at=granted or expires,
            )
            self.session.add(invoice)
            await self.session.flush()
            invoice_id = invoice.id
            sale = {
                "unit_price": Decimal("50.00"),
                "total_amount": Decimal("50.00"),
                "reason": None,
                "topup_product_id": self.product.id,
                "product_code": self.product.code,
                "invoice_id": invoice_id,
            }
        self.session.add(
            TopupPurchase(
                tenant_id=tenant.id,
                subscription_id=subscription.id if subscription is not None else None,
                source=source,
                product_name="Channel slots",
                entitlement_key=key,
                channel_type=channel,
                quantity=quantity,
                currency="EGP",
                billing_period_start=(granted or expires) - timedelta(days=1),
                billing_period_end=expires,
                expires_at=expires,
                status=status,
                granted_at=granted,
                **sale,
            )
        )
        await self.session.flush()

    async def turns(
        self, tenant: Tenant, number: WhatsAppAccount, start: datetime, end: datetime
    ) -> None:
        rng = self.rng
        contact = Contact(tenant_id=tenant.id, wa_id=f"2010{rng.randint(10**7, 10**8 - 1)}")
        self.session.add(contact)
        await self.session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, contact_id=contact.id, account_id=number.id
        )
        self.session.add(conversation)
        await self.session.flush()
        span = (end - start).total_seconds()
        now = datetime.now(UTC)
        for _ in range(rng.randint(3, 9)):
            state = rng.choice(["held", "held_recent", "charged", "released"])
            held_at = (
                now - timedelta(seconds=rng.randint(1, HOLD_TTL_SECONDS - 60))
                if state == "held_recent"
                else start + timedelta(seconds=rng.uniform(0, span))
            )
            turn = AgentTurn(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                trigger_message_id=uuid.uuid4(),
                state=AgentTurnState.ENGAGED,
                charge_state=(
                    AITurnChargeState.HELD if state.startswith("held") else AITurnChargeState(state)
                ),
                held_at=held_at,
                charged_at=held_at + timedelta(seconds=4) if state == "charged" else None,
                released_at=held_at + timedelta(seconds=4) if state == "released" else None,
                charge_release_reason=(
                    AITurnReleaseReason.NOT_CHARGEABLE if state == "released" else None
                ),
            )
            self.session.add(turn)
            await self.session.flush()
            if state == "charged":
                self.session.add(
                    UsageEvent(
                        tenant_id=tenant.id,
                        event_type=UsageEventType.AI_TURN,
                        quantity=1,
                        unit=UsageUnit.COUNT,
                        occurred_at=held_at,
                        channel=Channel(rng.choice(CHANNELS)),
                        agent_turn_id=turn.id,
                    )
                )
        # Charges written before the channel dimension, and meters that are not turns.
        for _ in range(rng.randint(0, 4)):
            self.session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    event_type=rng.choice(
                        [
                            UsageEventType.AI_TURN,
                            UsageEventType.AI_REQUEST,
                            UsageEventType.MESSAGE_SENT,
                        ]
                    ),
                    quantity=rng.randint(1, 5),
                    unit=UsageUnit.COUNT,
                    occurred_at=start + timedelta(seconds=rng.uniform(0, span)),
                    channel=Channel.TELEGRAM,
                )
            )
        await self.session.flush()

    def reduction(self, tenant: Tenant, *, open_: bool) -> None:
        moment = datetime.now(UTC) - timedelta(days=1)
        self.session.add(
            ChannelCapacityReduction(
                tenant_id=tenant.id,
                cause=CapacityReductionCause.DOWNGRADE,
                status=(
                    CapacityReductionStatus.PENDING_SELECTION
                    if open_
                    else CapacityReductionStatus.NO_LONGER_NEEDED
                ),
                target_general=1,
                target_typed={},
                target_allowed_types=["whatsapp"],
                effective_at=moment,
                grace_ends_at=moment + timedelta(days=7),
                resolved_at=None if open_ else moment + timedelta(hours=1),
                disabled_connection_ids=None if open_ else [],
            )
        )


async def _population(
    session: AsyncSession, rng: random.Random
) -> list[tuple[Tenant, list[datetime]]]:
    versions = await _versions(session)
    builder = _Builder(session, rng, await _product(session))
    now = datetime.now(UTC)
    cases: list[tuple[Tenant, list[datetime]]] = []
    for index in range(WORKSPACES):
        plan, version, monthly, yearly = versions[index % len(versions)]
        price = yearly if index % 2 and yearly is not None else monthly
        interval = price.billing_interval if price is not None else BillingInterval.MONTHLY
        status = STATUSES[index % len(STATUSES)]
        anchor = now - timedelta(days=rng.randint(0, 80), hours=rng.randint(0, 23))
        end = billing_calendar.add_interval(anchor, interval)
        tenant = Tenant(name=f"Oracle {index}", slug=f"oracle-ent-{uuid.uuid4().hex[:10]}")
        session.add(tenant)
        await session.flush()
        subscription: Subscription | None = None
        if status is not None:
            first_end = billing_calendar.add_interval(anchor, BillingInterval.MONTHLY)
            subscription = Subscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                plan_version_id=version.id,
                plan_price_id=price.id if price is not None else None,
                status=status,
                billing_anchor_at=anchor,
                current_period_start=anchor,
                current_period_end=end,
                # A stale stored cycle on the yearly ones: the sweep has not run.
                usage_period_start=anchor,
                usage_period_end=first_end if interval is BillingInterval.YEARLY else end,
                ended_at=(
                    anchor + timedelta(days=3)
                    if status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED)
                    else None
                ),
            )
            session.add(subscription)
            await session.flush()
        number = await builder.connections(tenant)
        await builder.slots(tenant, subscription, anchor, end)
        await builder.turns(tenant, number, anchor, end)
        if index % 5 == 0:
            builder.reduction(tenant, open_=True)
        if index % 7 == 3:
            builder.reduction(tenant, open_=False)
        await session.flush()
        horizon = min(end, now + timedelta(days=40))
        span = (horizon - anchor).total_seconds()
        moments = [now] + [
            anchor + timedelta(seconds=rng.uniform(0, span)) for _ in range(MOMENTS - 1)
        ]
        cases.append((tenant, moments))
    return cases


def _frozen(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _by_value(counts: Any) -> dict[str, int]:
    return {channel.value: count for channel, count in counts.items() if count}


def _census_agrees(row: CapacityCensusRow | None, want: OracleCapacity | None) -> list[str]:
    if want is None:
        return [] if row is None else ["census row for an unenforced workspace"]
    if row is None:
        return ["census has no row"]
    got = (row.base, row.general, row.active, row.over_limit, row.outside_allowed_types)
    expected = (
        want.base,
        want.general,
        sum(want.active.values()),
        want.over_limit,
        want.outside_allowed_types,
    )
    return [] if got == expected else [f"census {got} != oracle {expected}"]


async def test_capacity_and_the_ai_meter_agree_three_ways_for_every_workspace_and_moment(
    db_session: AsyncSession,
) -> None:
    rng = random.Random(131)  # noqa: S311 - a reproducible population, not a secret
    cases = await _population(db_session, rng)
    census = ChannelCapacityCensus(db_session, default_plan_code="starter")
    comparisons = 0
    over_limit = Counter[bool]()
    mismatches: list[str] = []
    for tenant, moments in cases:
        for moment in moments:
            service = EntitlementService(
                db_session,
                tenant_id=tenant.id,
                default_plan_code="starter",
                clock=_frozen(moment),
                ai_turn_hold_ttl=timedelta(seconds=HOLD_TTL_SECONDS),
            )
            want = await oracle_capacity(db_session, tenant.id, at=moment)
            assert want is not None
            got = await service.channel_capacity(at=moment)
            label = f"{tenant.slug} @ {moment.isoformat()}"
            pairs = {
                "base": (got.base, want.base),
                "general purchased": (got.general_purchased, want.general_purchased),
                "general granted": (got.general_granted, want.general_granted),
                "typed purchased": (_by_value(got.typed_purchased), want.typed_purchased),
                "typed granted": (_by_value(got.typed_granted), want.typed_granted),
                "active": (_by_value(got.active), want.active),
                "allowed": (frozenset(c.value for c in got.allowed), want.allowed),
                "over limit": (got.over_limit, want.over_limit),
            }
            mismatches += [
                f"{label}: {name} {pair[0]!r} != {pair[1]!r}"
                for name, pair in pairs.items()
                if pair[0] != pair[1]
            ]
            over_limit[want.over_limit] += 1

            slots = await service.check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
            remaining = None if want.general is None else max(want.general - want.overflow, 0)
            if (slots.limit, slots.used, slots.over_limit, slots.remaining) != (
                want.total,
                sum(want.active.values()),
                want.over_limit,
                remaining,
            ):
                mismatches.append(
                    f"{label}: check {(slots.limit, slots.used, slots.over_limit, slots.remaining)}"
                )

            turns = await service.check(LimitKey.PERIOD_AI_TURNS, additional=0)
            expected = await oracle_ai_turns(
                db_session, tenant.id, at=moment, hold_ttl_seconds=HOLD_TTL_SECONDS
            )
            got_turns = (
                turns.limit,
                turns.used,
                turns.held,
                (turns.period_start, turns.period_end),
            )
            want_turns = (expected.limit, expected.used, expected.held, expected.window)
            if got_turns != want_turns:
                mismatches.append(f"{label}: ai turns {got_turns} != {want_turns}")

            rows = {row.tenant_id: row for row in await census.rows(now=moment)}
            mismatches += [f"{label}: {m}" for m in _census_agrees(rows.get(tenant.id), want)]
            comparisons += 1

    evidence = f"entitlement oracle comparisons: {comparisons}, over limit: {dict(over_limit)}"
    print(evidence)  # noqa: T201 - recorded as evidence
    assert comparisons == WORKSPACES * MOMENTS
    # The population is not vacuous: it holds workspaces on both sides of the fit.
    assert over_limit[True] and over_limit[False]
    assert mismatches == []
