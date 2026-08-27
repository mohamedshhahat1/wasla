"""Plans and subscriptions: what a workspace is allowed to do, and until when.

Two tables and one deliberate asymmetry between them.

`plans` are the platform's, not a workspace's: a handful of rows, edited rarely,
read constantly. Their limits live in JSONB keyed by a closed vocabulary
(`LimitKey`) rather than in columns, because the set of things worth limiting
grows with the product and a column per limit means a migration every time
somebody has a pricing idea. The vocabulary is what keeps that from becoming a
free-for-all: a key outside it is refused at the service boundary, so a typo
cannot silently grant an unlimited allowance.

`subscriptions` are one per workspace, and that is enforced by a unique index
rather than by a service. A workspace with two subscriptions has two answers to
"what am I allowed to do", and there is no correct way to pick one.

An absent limit means **unlimited**, and that is the single most important rule
here. Enterprise plans are defined by not having the limits everyone else has,
and the alternative encodings are all worse: a magic number somebody eventually
compares against, or a nullable column per limit, which is the column-per-limit
problem with extra steps.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_PLAN_NAME_LENGTH: Final = 100
MAX_PLAN_CODE_LENGTH: Final = 50
# ISO 4217. Stored per plan rather than globally: a platform selling in more
# than one currency is a normal thing to become, and retrofitting it means
# rewriting every stored price.
CURRENCY_LENGTH: Final = 3
DEFAULT_CURRENCY: Final = "USD"


class LimitKey(StrEnum):
    """What a plan can put a ceiling on.

    Two kinds, and the difference decides how each is checked:

    - **Resource limits** count rows that exist *now* - numbers, agents, people.
      Checked with a `COUNT`, and a workspace over the limit stays over it until
      something is deleted.
    - **Usage limits** count what was consumed *in the current billing period*,
      read from `usage_events`. They reset when the period rolls over, which is
      what makes "1,000 messages a month" mean anything.

    `PERIOD_` is in the name of the second kind so a reader never has to guess
    which sort of question a key is asking.
    """

    WHATSAPP_NUMBERS = "whatsapp_numbers"
    AGENTS = "agents"
    TEAM_MEMBERS = "team_members"
    KNOWLEDGE_DOCUMENTS = "knowledge_documents"
    PERIOD_MESSAGES = "period_messages"
    PERIOD_AI_REQUESTS = "period_ai_requests"
    PERIOD_CAMPAIGN_MESSAGES = "period_campaign_messages"


# The limits that count rows rather than consumption. Written out rather than
# inferred from the `PERIOD_` prefix: a name is a naming convention, and this is
# a behavioural distinction that decides which query runs.
RESOURCE_LIMITS: Final[frozenset[LimitKey]] = frozenset(
    {
        LimitKey.WHATSAPP_NUMBERS,
        LimitKey.AGENTS,
        LimitKey.TEAM_MEMBERS,
        LimitKey.KNOWLEDGE_DOCUMENTS,
    }
)

PERIOD_LIMITS: Final[frozenset[LimitKey]] = frozenset(LimitKey) - RESOURCE_LIMITS


class BillingInterval(StrEnum):
    """How long a billing period lasts.

    Two, not an arbitrary number of days. Every price a customer compares is
    quoted per month or per year, and an interval nobody quotes is one nobody
    can price.
    """

    MONTHLY = "monthly"
    YEARLY = "yearly"


class SubscriptionStatus(StrEnum):
    """Where a workspace stands with the platform.

    `PAST_DUE` is deliberately distinct from `CANCELLED`. A payment that failed
    is a conversation to have with a customer, not a decision to cut them off,
    and collapsing the two means the first failed card ends a relationship.

    `EXPIRED` is what a trial becomes when nobody acts. It is separate from
    `CANCELLED` because nobody chose it, and the two want different emails.
    """

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Statuses in which a workspace may still use the product. `PAST_DUE` is in the
# list on purpose: service continues while a payment problem is sorted out, and
# the platform decides separately when that grace has run out.
SERVING_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
    }
)

# Statuses from which nothing further happens on its own.
TERMINAL_SUBSCRIPTION_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    }
)

BILLING_INTERVAL_TYPE = _enum_type(BillingInterval, name="billing_interval")
SUBSCRIPTION_STATUS_TYPE = _enum_type(SubscriptionStatus, name="subscription_status")


class Plan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What the platform sells.

    Not tenant-scoped: a plan belongs to the platform and is read by every
    workspace. A workspace with a bespoke arrangement gets its own plan row
    rather than an override on its subscription, so there is one place that
    answers "what is this workspace entitled to" and it is always a plan.
    """

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_plans_code"),
        Index("ix_plans_is_public", "is_public"),
    )

    # A stable identifier for the plan, safe to write in configuration and in a
    # support conversation. The name is for people and may be changed freely;
    # this may not.
    code: Mapped[str] = mapped_column(String(MAX_PLAN_CODE_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_PLAN_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Numeric, never float. A price is money, and binary floating point cannot
    # represent 19.99 - which is the sort of error that reaches an invoice.
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
    )
    interval: Mapped[BillingInterval] = mapped_column(BILLING_INTERVAL_TYPE, nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Keyed by `LimitKey`, validated on write. An absent key is unlimited.
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # A bespoke plan written for one customer is not shown on a pricing page.
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Retired rather than deleted: subscriptions still point at it, and their
    # history has to keep meaning what it meant.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Display order on a pricing page. Stored because "cheapest first" stops
    # being right the moment a plan is priced by usage rather than by month.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def limit_for(self, key: LimitKey) -> int | None:
        """The ceiling for one key, or None for unlimited.

        A stored value that is not a positive integer is treated as unlimited
        rather than as zero. Zero would mean "this workspace may do nothing",
        which is never what a malformed row was meant to say, and a plan edited
        badly should not lock a paying customer out of their own product.
        """
        raw = self.limits.get(key.value)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw if raw >= 0 else None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Plan(code={self.code!r}, interval={self.interval!r})"


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One workspace's standing arrangement.

    Tenant-owned but not `TenantScopedMixin`: the platform reads across every
    subscription, and a workspace reads exactly one - its own - which the
    service resolves by tenant id. The unique index is what makes "its own"
    unambiguous.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        # One per workspace. A workspace with two has two answers to "what am I
        # allowed to do", and no correct way to choose between them.
        UniqueConstraint("tenant_id", name="uq_subscriptions_tenant_id"),
        Index("ix_subscriptions_tenant_id", "tenant_id"),
        Index("ix_subscriptions_status", "status"),
        Index("ix_subscriptions_plan_id", "plan_id"),
        # The sweep that ends trials and rolls periods over reads this.
        Index("ix_subscriptions_current_period_end", "current_period_end"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT, not CASCADE: deleting a plan out from under a paying
        # workspace would leave it entitled to nothing, mid-period.
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(SUBSCRIPTION_STATUS_TYPE, nullable=False)
    # The window usage limits are counted over. Stored rather than derived from
    # `created_at` and the interval, because a plan change mid-period moves the
    # boundary and the arithmetic afterwards has to agree with what was billed.
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A cancellation a customer asked for but that has not taken effect yet.
    # They keep what they paid for until the period ends, which is both fair and
    # what every subscription product does.
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Where this subscription lives at a payment provider, once there is one.
    # Nullable because there is not one yet, and a local deployment never has
    # one; a subscription is a complete, usable record without it.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def is_serving(self) -> bool:
        """Whether the workspace may use the product right now.

        Read by `EntitlementService` when it resolves which plan applies. That
        it is read at all is recent: this property and `SERVING_STATUSES` both
        existed from the start and neither was consulted, so a cancelled
        subscription kept granting its plan.
        """
        return self.status in SERVING_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SUBSCRIPTION_STATUSES

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Subscription(tenant_id={self.tenant_id!r}, status={self.status!r})"
