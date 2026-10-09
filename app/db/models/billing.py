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

**What a subscriber pays and is allowed is a plan *version*, not a plan**
(BILL-12). A `plans` row is the product's identity - its code, its name, whether
it is on offer. Its commercial terms live in `plan_versions`, which are
immutable once written (a trigger refuses an UPDATE), and every subscription
points at the version that governs it. Publishing a new price or new limits
creates a new version for new customers and changes nobody who is already
subscribed, until an operator migrates them deliberately. Before this, one
`UPDATE plans SET price = ...` silently re-priced every subscriber's next
renewal and one edit of `limits` downgraded paying customers mid-period.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base, RevisionedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type, ordered_type_ddl

MAX_PLAN_NAME_LENGTH: Final = 100
MAX_PLAN_CODE_LENGTH: Final = 50
MAX_BILLING_REASON_LENGTH: Final = 500
# ISO 4217. Stored per plan rather than globally: a platform selling in more
# than one currency is a normal thing to become, and retrofitting it means
# rewriting every stored price.
CURRENCY_LENGTH: Final = 3
DEFAULT_CURRENCY: Final = "EGP"
# Every currency this product can actually bill in. One, and enforced as a
# database constraint as well as at the API: a plan priced in `ZZZ` was
# accepted by the database before (BILL-12), and nothing downstream - Paymob,
# the invoice renderer, revenue reporting - knows what to do with one. Genuine
# multi-currency support is a design of its own, and widening this set is part
# of it rather than a substitute for it.
SUPPORTED_CURRENCIES: Final[frozenset[str]] = frozenset({DEFAULT_CURRENCY})
# The SQL form of the same rule, for the CHECK constraints.
CURRENCY_CHECK_SQL: Final = "currency = 'EGP'"
# The largest price a plan version may carry. Numeric(12, 2) holds ten digits
# before the point; this is well inside it and far beyond any real plan, so an
# operator typing an extra zero is refused rather than stored.
MAX_PLAN_PRICE: Final = Decimal("1000000.00")
# The largest number any one limit may name. A limit is compared with counts
# and sums that are 64-bit in PostgreSQL; anything bigger than this is a typo
# rather than a pricing decision, and "unlimited" is spelled `null`.
MAX_LIMIT_VALUE: Final = 10**15
# A `channel_kind` label as an allowed-channel-types element (ENT-09). The
# labels are short words; the bound is a ceiling, and a trigger checks each
# element is a label the vocabulary knows.
MAX_CHANNEL_LABEL_LENGTH: Final = 32


class LimitKey(StrEnum):
    """What a plan can put a ceiling on.

    Two kinds, and the difference decides how each is checked:

    - **Resource limits** measure what exists *now* - numbers, agents, people,
      bytes held in the object store. Checked with a `COUNT` or a `SUM`, and a
      workspace over the limit stays over it until something is deleted.
    - **Usage limits** count what was consumed *in the current usage cycle* -
      one calendar month, whatever the billing term (ADR-116) - read from
      `usage_events`. They reset when the cycle rolls over, which is what
      makes "1,000 messages a month" mean anything, on a yearly price too.

    `PERIOD_` is in the name of the second kind so a reader never has to guess
    which sort of question a key is asking.

    `STORAGE_BYTES` is the first resource limit that is not a row count, and
    the distinction matters: `usage_events` records `STORAGE_USED` when bytes
    are *written* and never subtracts when they are purged, so it answers
    "how much has this workspace ever stored" and cannot answer "how much is it
    holding". A capacity limit needs the second question, so it is a `SUM` over
    the rows that still name an object (ADR-091).

    `CHANNEL_CONNECTIONS` replaced `whatsapp_numbers` (ENT-05, ADR-131): every
    active connection on any channel takes one slot, a WhatsApp number among
    them. The retired key is not a member - a new plan version, custom plan or
    top-up naming it is refused as an unknown key - and the versions published
    before the change are read through `app.services.entitlement_terms`, the
    one place that still knows the old name.
    """

    CHANNEL_CONNECTIONS = "channel_connections"
    AGENTS = "agents"
    # How many live workspaces one *account* may own. The only key here that is
    # not a property of a workspace, which is why it needs its own category
    # below rather than joining `RESOURCE_LIMITS`.
    #
    # It lives in this vocabulary anyway, and deliberately: it is a thing a plan
    # sells, it belongs in `plans.limits` with everything else a plan sells, and
    # a second storage location for "one more limit that did not fit" is how a
    # pricing model ends up in two places. `EntitlementService` refuses it -
    # that service is scoped to one tenant and genuinely cannot answer a
    # question about a person - and `WorkspaceEntitlementService` answers it.
    OWNED_WORKSPACES = "owned_workspaces"
    TEAM_MEMBERS = "team_members"
    KNOWLEDGE_DOCUMENTS = "knowledge_documents"
    STORAGE_BYTES = "storage_bytes"
    PERIOD_MESSAGES = "period_messages"
    # Customer turns an agent answered in the billing period - what a plan's AI
    # allowance sells (AI-02). Deliberately not provider requests: one turn is a
    # classification plus one to three inference rounds, and a limit written in
    # requests let an implementation detail spend a customer's allowance, so
    # the last unit of every period went on a classification nobody saw.
    PERIOD_AI_TURNS = "period_ai_turns"
    PERIOD_CAMPAIGN_MESSAGES = "period_campaign_messages"


# The limits that count rows rather than consumption. Written out rather than
# inferred from the `PERIOD_` prefix: a name is a naming convention, and this is
# a behavioural distinction that decides which query runs.
RESOURCE_LIMITS: Final[frozenset[LimitKey]] = frozenset(
    {
        LimitKey.CHANNEL_CONNECTIONS,
        LimitKey.AGENTS,
        LimitKey.TEAM_MEMBERS,
        LimitKey.KNOWLEDGE_DOCUMENTS,
        LimitKey.STORAGE_BYTES,
    }
)

# Limits that belong to an *account* rather than to a workspace. Counted across
# every tenant a person owns, so no tenant-scoped service can evaluate one - and
# subtracted from `PERIOD_LIMITS` below, which would otherwise absorb them by
# construction and have the period meters look for a meter that does not exist.
ACCOUNT_LIMITS: Final[frozenset[LimitKey]] = frozenset({LimitKey.OWNED_WORKSPACES})

PERIOD_LIMITS: Final[frozenset[LimitKey]] = frozenset(LimitKey) - RESOURCE_LIMITS - ACCOUNT_LIMITS

# The seven keys a custom plan is written in and a top-up can raise (ADR-113).
# Written out rather than derived: which limits are sold as add-ons is a product
# decision, and `AGENTS` and `OWNED_WORKSPACES` are deliberately not among them.
# The period keys reset with the billing period; the rest are capacities.
# `CHANNEL_CONNECTIONS` took `whatsapp_numbers`' place (ENT-05, ADR-131).
TOPUP_LIMITS: Final[frozenset[LimitKey]] = frozenset(
    {
        LimitKey.PERIOD_MESSAGES,
        LimitKey.PERIOD_AI_TURNS,
        LimitKey.PERIOD_CAMPAIGN_MESSAGES,
        LimitKey.STORAGE_BYTES,
        LimitKey.CHANNEL_CONNECTIONS,
        LimitKey.TEAM_MEMBERS,
        LimitKey.KNOWLEDGE_DOCUMENTS,
    }
)

# The key `CHANNEL_CONNECTIONS` replaced (ENT-05). Spelled here once so a guard
# can refuse it on new rows; reading it is `app.services.entitlement_terms`'
# job alone.
RETIRED_LIMIT_KEY: Final = "whatsapp_numbers"

# The one key a plan names and nothing refuses: an inbound customer message is
# never turned away for a business's billing (ADR-030). A top-up raises the
# allowance it is *measured* against, and says so, rather than inventing an
# enforcement that does not exist.
METER_ONLY_LIMITS: Final[frozenset[LimitKey]] = frozenset({LimitKey.PERIOD_MESSAGES})


class PlanScope(StrEnum):
    """Who a plan may be sold or assigned to (ADR-113).

    ``PUBLIC``
        On the catalogue every workspace sees.
    ``PRIVATE``
        Off the catalogue - Enterprise, a negotiated tier - and assignable to
        any workspace by platform staff.
    ``TENANT``
        Written for exactly one workspace, named by `plans.tenant_id`, and
        never assignable to another. Enforced by the service layer and again
        by a trigger on every table that points a workspace at a plan, so no
        code path - a request, an operator, the billing sweep - can hand one
        company another company's commercial terms.
    """

    PUBLIC = "public"
    PRIVATE = "private"
    TENANT = "tenant"


class BillingInterval(StrEnum):
    """The unit a price's billing term is counted in (ADR-116).

    Two, not an arbitrary number of days. Every price a customer compares is
    quoted per month or per year, and an interval nobody quotes is one nobody
    can price. A term is `interval_count` of these units; only 1 is sold today.

    This is the cadence of *billing*, never of usage: a yearly price bills once
    a year and its usage allowances still reset every calendar month.
    """

    MONTHLY = "monthly"
    YEARLY = "yearly"


# Calendar months in one unit of each interval. The only place the length of a
# year is written down, so nothing anywhere computes 365 or 30 days.
MONTHS_PER_INTERVAL: Final[dict[BillingInterval, int]] = {
    BillingInterval.MONTHLY: 1,
    BillingInterval.YEARLY: 12,
}
# The term lengths the product sells (ADR-116). `interval_count` exists so a
# quarterly or two-year price needs no new model; selling one is a product
# decision, and until it is made the API refuses anything but 1.
SUPPORTED_INTERVAL_COUNTS: Final[frozenset[int]] = frozenset({1})


class SubscriptionStatus(StrEnum):
    """Where a workspace stands with the platform.

    `PAST_DUE` is deliberately distinct from `CANCELLED`. A payment that failed
    is a conversation to have with a customer, not a decision to cut them off,
    and collapsing the two means the first failed card ends a relationship.

    `EXPIRED` is what a trial becomes when nobody acts. It is separate from
    `CANCELLED` because nobody chose it, and the two want different emails.

    `SUSPENDED` is where `PAST_DUE` ends up when nobody ever pays (ADR-061).
    It is deliberately its own value rather than a reuse of `CANCELLED`,
    because this enum's whole job is to record *who decided and why*: a
    cancellation is the customer's decision, an expiry is nobody's, and a
    suspension is the platform's. Collapsing the third into the first would
    misattribute it in the audit trail and count it as churn on a dashboard
    that separates cancellations from failed payments - and it would make
    recovery impossible to express, because paying an invoice deliberately
    does not revive a subscription somebody chose to end.
    """

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Statuses in which a workspace may still use the product. `PAST_DUE` is in the
# list on purpose: service continues while a payment problem is sorted out, and
# `SUSPENDED` is where the platform decides that grace has run out.
SERVING_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
    }
)

# Statuses from which nothing further happens **on its own**. That is what this
# set means, and it is why `SUSPENDED` belongs in it: the sweep opens no new
# period, raises no further invoice and attempts no further collection against
# one. It is not a claim that the row can never move again - a settled payment
# lifts a suspension, which is the one recovery `CheckoutService._settle`
# permits and the one the other two members deliberately refuse.
TERMINAL_SUBSCRIPTION_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {
        SubscriptionStatus.SUSPENDED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    }
)


class ScheduledChangeSource(StrEnum):
    """Why a subscription has a plan change waiting for its period to end.

    ``DOWNGRADE``
        The customer chose a cheaper plan. Effective at the end of the period
        they have already paid for, so nothing bought is forfeited (BILL-03's
        product contract).
    ``OPERATOR``
        Platform staff scheduled a change for one subscription.
    ``MIGRATION``
        A version migration for a whole cohort reached this subscription.
    """

    DOWNGRADE = "downgrade"
    OPERATOR = "operator"
    MIGRATION = "migration"


class BillingAdjustmentKind(StrEnum):
    """An operator's deliberate, non-monetary decision about a subscription.

    Never a payment. The whole reason this exists is that the only honest way
    to give somebody service without money is to say so: a comp recorded as a
    "paid" invoice is a fabricated receipt, and a grant with no record at all is
    a paying plan held without cover, which the invariants refuse.
    """

    # A priced plan version granted for a window, without an invoice.
    COMPLIMENTARY_GRANT = "complimentary_grant"
    # An overdue renewal invoice waived by voiding it, with service restored.
    INVOICE_WAIVER = "invoice_waiver"


BILLING_INTERVAL_TYPE = _enum_type(BillingInterval, name="billing_interval")
PLAN_SCOPE_TYPE = _enum_type(PlanScope, name="plan_scope")
SUBSCRIPTION_STATUS_TYPE = _enum_type(
    SubscriptionStatus,
    name="subscription_status",
    # `suspended` was added by a later migration (0037), after `expired`.
    database_order=("trialing", "active", "past_due", "cancelled", "expired", "suspended"),
)
SCHEDULED_CHANGE_SOURCE_TYPE = _enum_type(ScheduledChangeSource, name="scheduled_change_source")
BILLING_ADJUSTMENT_KIND_TYPE = _enum_type(BillingAdjustmentKind, name="billing_adjustment_kind")


def validated_limit(raw: object) -> int | None:
    """One stored limit value as the entitlement engine reads it.

    Shared by `Plan` and `PlanVersion` so the two cannot disagree about what a
    malformed value means. A value that is not a non-negative integer is read
    as unlimited, never as zero - see `Plan.limit_for` for why. The plan API
    refuses such values on the way in; this is what keeps a row edited by hand
    from locking a paying customer out.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


class Plan(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """What the platform sells: the product's identity.

    Not tenant-scoped: a plan belongs to the platform and is read by every
    workspace. A workspace with a bespoke arrangement gets its own plan row
    rather than an override on its subscription, so there is one place that
    answers "what is this workspace entitled to" and it is always a plan.

    **The commercial columns here are not authoritative.** `price`, `currency`,
    `interval`, `trial_days` and `limits` mirror the most recently published
    `PlanVersion`, written in the same transaction by the plan catalogue
    service so a person reading this table with SQL sees the current offer.
    Nothing that charges a customer or enforces a limit reads them: money and
    entitlements come from the version a subscription or an invoice names. The
    one exception is materialising the first version of a plan that has none
    (a row created by a migration or a test fixture), which snapshots these
    columns exactly as the 0070 backfill did.
    """

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_plans_code"),
        Index("ix_plans_is_public", "is_public"),
        # Defence in depth beneath the plan API (BILL-12). The database used to
        # accept a negative price, a negative trial and currency `ZZZ`.
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("trial_days >= 0", name="trial_days_non_negative"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        # A tenant plan names its one workspace, and nothing else names one
        # (ADR-113). A TENANT plan without an owner would be assignable to
        # anybody, which is the opposite of what it is for.
        CheckConstraint("(scope = 'tenant') = (tenant_id IS NOT NULL)", name="scope_tenant"),
        # `is_public` is what the catalogue has always filtered on; it now
        # means exactly "scope is public", so the two cannot disagree.
        CheckConstraint("(scope = 'public') = is_public", name="scope_visibility"),
        Index("ix_plans_tenant_id", "tenant_id"),
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
    # The channel types of the most recently published version, mirrored like
    # `limits` (ENT-09). Null on a row nothing has published since the field
    # existed; a version materialised from such a row takes the legacy set.
    allowed_channel_types: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(MAX_CHANNEL_LABEL_LENGTH)), nullable=True
    )
    # Who this plan may be sold to - see `PlanScope`. Derived for the
    # catalogue's older reader: `is_public` is true exactly when this is PUBLIC.
    scope: Mapped[PlanScope] = mapped_column(
        PLAN_SCOPE_TYPE, nullable=False, default=PlanScope.PUBLIC
    )
    # The one workspace a TENANT plan belongs to; NULL for every other scope.
    # RESTRICT, like every commercial record (BILL-19), and immutable once
    # written (a trigger refuses the change): a custom plan re-pointed at a
    # second company would carry the first company's subscribers with it.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=True,
    )

    @validates("is_public")
    def _follow_visibility(self, _key: str, value: bool) -> bool:
        """Keep a non-tenant plan's scope in step with `is_public`.

        `is_public` predates scopes and is still what every older caller sets,
        so setting it moves a PUBLIC/PRIVATE plan between the two. A TENANT
        plan is left alone, and making one public is refused by the
        `scope_visibility` constraint rather than silently widening it.
        """
        if self.scope is not PlanScope.TENANT:
            self.scope = PlanScope.PUBLIC if value else PlanScope.PRIVATE
        return value

    @property
    def is_custom(self) -> bool:
        """Whether this plan was written for one workspace."""
        return self.scope is PlanScope.TENANT

    def available_to(self, tenant_id: uuid.UUID) -> bool:
        """Whether `tenant_id` may hold this plan at all, whoever assigns it.

        The binding rule and nothing else - activity and visibility are the
        caller's separate questions. PUBLIC and PRIVATE plans may be held by
        anybody; a TENANT plan only by its owner.
        """
        return self.scope is not PlanScope.TENANT or self.tenant_id == tenant_id

    def limit_for(self, key: LimitKey) -> int | None:
        """The ceiling for one key, or None for unlimited. See `validated_limit`.

        - a non-negative integer is the ceiling - **including 0**, which means
          "none of this" (ADR-113: a plan may deliberately exclude a feature);
        - a missing key or JSON `null` is unlimited;
        - anything malformed - a negative number, a string such as `"5"`, a
          float such as `5.5`, a boolean - is read as unlimited, never as zero:
          a plan edited badly by hand should not lock a paying customer out of
          their own product. The plan API refuses such values on the way in.

        (This used to say non-positive values were unlimited, which the code
        has not done since ADR-113 made 0 mean zero - DB-026.)
        """
        return validated_limit(self.limits.get(key.value))

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Plan(code={self.code!r}, interval={self.interval!r})"


class PlanVersion(Base, UUIDPrimaryKeyMixin):
    """One immutable set of entitlements for a plan (BILL-12, ADR-116).

    What a customer is allowed - the limits - plus the name as it was shown.
    Written once and never changed - a trigger refuses any UPDATE, so "what did
    version 3 of Pro allow" has one answer for ever, and an invoice that names a
    version keeps meaning what it meant.

    **What a customer pays is a `PlanPrice` of the version, not the version**
    (ADR-116). One version may be sold monthly and yearly; both prices grant
    exactly these limits. `price`, `currency` and `interval` here are the terms
    the version was *published* with, and they keep two meanings only:

    - `price = 0` marks a free version: it has no prices, is never checked out
      and never invoiced for money (Starter, a complimentary custom plan);
    - on a priced version they are its first price, which the database
      publishes as a `plan_prices` row when the version is inserted.

    Nothing charges a customer from these columns: checkouts, renewals, offers
    and invoices name a `PlanPrice`.

    `effective_at` is when a version becomes the one *new* customers get. It
    changes nobody already subscribed: a subscription keeps the version it
    points at until an operator migrates it, which is the contract that stops a
    catalogue edit silently re-pricing or downgrading an existing customer.
    """

    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_id_version"),
        # What a subscription names, so its pinned version is always one of its
        # own plan's (DB-004).
        UniqueConstraint("plan_id", "id", name="uq_plan_versions_plan_id_id"),
        Index("ix_plan_versions_plan_id_effective_at", "plan_id", "effective_at"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("trial_days >= 0", name="trial_days_non_negative"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE, and safe: a version anybody used is referenced by a
        # subscription, an invoice or a migration, all RESTRICT, so deleting a
        # plan whose terms were ever sold is refused by those. Only a plan
        # nobody ever bought takes its unused versions with it.
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_PLAN_NAME_LENGTH), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH), nullable=False)
    interval: Mapped[BillingInterval] = mapped_column(BILLING_INTERVAL_TYPE, nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # The channel types a workspace on this version may connect and automate
    # (ENT-09) - a set of `Channel` labels, separate from capacity and never a
    # top-up target. Required on every version published since the field
    # existed (a trigger refuses NULL); NULL only on versions published before
    # it, which are read as the legacy set, WhatsApp alone - never as "all".
    allowed_channel_types: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(MAX_CHANNEL_LABEL_LENGTH)), nullable=True
    )
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Who published it. SET NULL, because the record of the terms outlives the
    # account of the person who wrote them; the audit trail keeps the name.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=True)

    def limit_for(self, key: LimitKey) -> int | None:
        """The ceiling this version sets for one key, or None for unlimited."""
        return validated_limit(self.limits.get(key.value))

    @property
    def is_free(self) -> bool:
        """Whether holding this version costs nothing - it has no prices."""
        return self.price <= 0

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"PlanVersion(plan_id={self.plan_id!r}, version={self.version!r})"


class PlanPrice(Base, UUIDPrimaryKeyMixin):
    """One way to pay for a plan version: an amount per billing term (ADR-116).

    `Business v4` is one set of entitlements; `299 EGP a month` and `2,990 EGP a
    year` are two prices of it. Choosing between them changes how often the
    customer is billed and nothing about what they are allowed - usage
    allowances reset every calendar month on either.

    **Immutable terms.** Version, interval, count, amount and currency never
    change (a trigger refuses it). A new price is a new row; the old one is
    *retired*, which only stops new customers choosing it. Every subscription,
    invoice, scheduled change and offer that names it keeps it, so a published
    price change reaches an existing subscriber only through an explicit
    migration.

    **One active price per commercial slot** - version, interval, count and
    currency - by a partial unique index. Retired rows are history and may
    repeat a slot.

    Tenant ownership is inherited, never stored: a price belongs to a version,
    the version to a plan, and a custom plan to one workspace. Composite foreign
    keys pin every reference to a price *of the version it names*, and the
    custom-plan triggers already refuse another workspace's version - so a
    workspace can never be bound to another's price by any writer.
    """

    __tablename__ = "plan_prices"
    __table_args__ = (
        # What a subscription, an invoice and an offer name together with
        # their version, so a price is always one of that version's own.
        UniqueConstraint("plan_version_id", "id", name="uq_plan_prices_plan_version_id_id"),
        Index(
            "uq_plan_prices_active_slot",
            "plan_version_id",
            "billing_interval",
            "interval_count",
            "currency",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        # A free version has no prices; a price is always money.
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("interval_count > 0", name="interval_count_positive"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at", name="retired_after_created"
        ),
    )

    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE, for the reason `PlanVersion.plan_id` cascades: a price
        # anybody used is held by a subscription, an invoice or an offer, all
        # RESTRICT, so only a never-sold plan takes its prices with it.
        ForeignKey("plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing_interval: Mapped[BillingInterval] = mapped_column(BILLING_INTERVAL_TYPE, nullable=False)
    interval_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=True)
    # Retired: no longer selectable by a new customer. Never deleted, never
    # un-retired - existing subscribers keep it.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    retirement_reason: Mapped[str | None] = mapped_column(
        String(MAX_BILLING_REASON_LENGTH), nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.retired_at is None

    @property
    def months(self) -> int:
        """The billing term in calendar months: 1 for monthly, 12 for yearly."""
        return MONTHS_PER_INTERVAL[self.billing_interval] * self.interval_count

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return (
            f"PlanPrice(version={self.plan_version_id!r}, {self.amount} {self.currency} "
            f"per {self.interval_count} {self.billing_interval.value})"
        )


# The immutability of a version is a property of the table, not of the code
# that happens to write it today. Attached as DDL so `create_all` (the model-
# built test schema) gets exactly what migration 0071 installs.
_PLAN_VERSION_IMMUTABLE_FUNCTION = DDL("""
    CREATE OR REPLACE FUNCTION plan_versions_refuse_update() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'plan_versions rows are immutable; publish a new version'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """)  # type: ignore[no-untyped-call]
_PLAN_VERSION_IMMUTABLE_TRIGGER = DDL(  # type: ignore[no-untyped-call]
    "CREATE TRIGGER plan_versions_immutable BEFORE UPDATE ON plan_versions "
    "FOR EACH ROW EXECUTE FUNCTION plan_versions_refuse_update()"
)
event.listen(PlanVersion.__table__, "after_create", _PLAN_VERSION_IMMUTABLE_FUNCTION)
event.listen(PlanVersion.__table__, "after_create", _PLAN_VERSION_IMMUTABLE_TRIGGER)

# **A version published since ADR-131 states its channel types and never names
# the retired key** (ENT-05, ENT-09). INSERT only: rows published before the
# field existed keep NULL, read as WhatsApp alone, and the immutability trigger
# above already refuses every UPDATE - so neither is ever rewritten and neither
# trigger is ever disabled. Each element must be a `channel_kind` label, listed
# once; an empty set is an explicit "no channel". Restated verbatim by 0093.
PLAN_VERSION_ENTITLEMENT_TERMS_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION plan_versions_entitlement_terms() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    DECLARE
        label text;
    BEGIN
        IF NEW.allowed_channel_types IS NULL THEN
            RAISE EXCEPTION 'a plan version states the channel types it allows'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.limits ? 'whatsapp_numbers' THEN
            RAISE EXCEPTION 'whatsapp_numbers is retired; publish channel_connections instead'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF array_position(NEW.allowed_channel_types, NULL) IS NOT NULL
           OR cardinality(NEW.allowed_channel_types)
              <> (SELECT count(DISTINCT element) FROM unnest(NEW.allowed_channel_types) element)
        THEN
            RAISE EXCEPTION 'each allowed channel type is named once'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        FOREACH label IN ARRAY NEW.allowed_channel_types LOOP
            IF NOT label = ANY (enum_range(NULL::channel_kind)::text[]) THEN
                RAISE EXCEPTION 'unknown channel type in a plan version: %', label
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END LOOP;
        RETURN NEW;
    END;
    $$
    """
PLAN_VERSION_ENTITLEMENT_TERMS_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plan_versions_entitlement_terms BEFORE INSERT ON plan_versions "
    "FOR EACH ROW EXECUTE FUNCTION plan_versions_entitlement_terms()"
)
event.listen(
    PlanVersion.__table__,
    "after_create",
    # `DDL` formats its text with `%`, so the RAISE placeholder is doubled.
    DDL(PLAN_VERSION_ENTITLEMENT_TERMS_FUNCTION_SQL.replace("%", "%%")),  # type: ignore[no-untyped-call]
)
event.listen(
    PlanVersion.__table__, "after_create", DDL(PLAN_VERSION_ENTITLEMENT_TERMS_TRIGGER_SQL)  # type: ignore[no-untyped-call]
)

# **A priced version is published with its price** (ADR-116). The terms a
# version is inserted with become its first `plan_prices` row in the same
# statement, so no writer - the catalogue API, a migration, a fixture, SQL -
# can create a priced version nobody can pay for. A version sold on more than
# one term gets its further prices from the catalogue service afterwards.
# Restated verbatim by migration 0081; `create_all` gets it from here.
PLAN_VERSION_PUBLISH_PRICE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION plan_versions_publish_price() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        INSERT INTO plan_prices (id, plan_version_id, billing_interval, interval_count,
                                 amount, currency, created_at, created_by, reason)
        VALUES (gen_random_uuid(), NEW.id, NEW.interval, 1, NEW.price, NEW.currency,
                NEW.created_at, NEW.created_by, 'Published with the plan version.');
        RETURN NULL;
    END;
    $$
    """
PLAN_VERSION_PUBLISH_PRICE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plan_versions_publish_price AFTER INSERT ON plan_versions "
    "FOR EACH ROW WHEN (NEW.price > 0) EXECUTE FUNCTION plan_versions_publish_price()"
)
# **A price's terms never change, and a retired price stays retired.** Only
# the retirement columns may move, once, from unset to set; `retired_by` may
# also lose its user to that foreign key's SET NULL. And a free version is not
# sold, so it carries no price at all.
PLAN_PRICE_GUARD_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION plan_prices_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF EXISTS (SELECT 1 FROM plan_versions v
                        WHERE v.id = NEW.plan_version_id AND v.price <= 0) THEN
                RAISE EXCEPTION 'a free plan version is not sold and has no prices'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
           OR NEW.billing_interval IS DISTINCT FROM OLD.billing_interval
           OR NEW.interval_count IS DISTINCT FROM OLD.interval_count
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.reason IS DISTINCT FROM OLD.reason THEN
            RAISE EXCEPTION 'plan_prices terms are immutable; retire the price and create another'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.retired_at IS NOT NULL AND (
               NEW.retired_at IS DISTINCT FROM OLD.retired_at
            OR NEW.retirement_reason IS DISTINCT FROM OLD.retirement_reason
            OR (NEW.retired_by IS DISTINCT FROM OLD.retired_by AND NEW.retired_by IS NOT NULL)
        ) THEN
            RAISE EXCEPTION 'a retired price stays retired'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
PLAN_PRICE_GUARD_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plan_prices_guard BEFORE INSERT OR UPDATE ON plan_prices "
    "FOR EACH ROW EXECUTE FUNCTION plan_prices_guard()"
)
event.listen(PlanPrice.__table__, "after_create", DDL(PLAN_PRICE_GUARD_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(PlanPrice.__table__, "after_create", DDL(PLAN_PRICE_GUARD_TRIGGER_SQL))  # type: ignore[no-untyped-call]
# On `plan_prices`, not `plan_versions`: the function inserts into the prices
# table, which `create_all` builds after the versions table.
event.listen(PlanPrice.__table__, "after_create", DDL(PLAN_VERSION_PUBLISH_PRICE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(PlanPrice.__table__, "after_create", DDL(PLAN_VERSION_PUBLISH_PRICE_TRIGGER_SQL))  # type: ignore[no-untyped-call]

# The TENANT binding (ADR-113), as a property of the tables rather than of the
# services that write them today. Every row that points a workspace at a plan -
# a subscription's plan, its pinned version and its scheduled version, an
# invoice's version - is refused if that plan is another workspace's custom
# plan. The services refuse first, with a proper error; this is what makes the
# rule hold for the billing sweep, a migration and a hand-written statement too.
# Restated verbatim by migration 0072; `create_all` gets it from here.
CUSTOM_PLAN_SCOPE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION billing_refuse_foreign_custom_plan() RETURNS trigger AS $$
    DECLARE
        foreign_plan uuid;
    BEGIN
        IF TG_TABLE_NAME = 'subscriptions' THEN
            SELECT p.id INTO foreign_plan FROM plans p
             WHERE p.tenant_id IS NOT NULL
               AND p.tenant_id <> NEW.tenant_id
               AND (p.id = NEW.plan_id
                    OR p.id IN (SELECT v.plan_id FROM plan_versions v
                                 WHERE v.id = NEW.plan_version_id
                                    OR v.id = NEW.scheduled_plan_version_id))
             LIMIT 1;
        ELSE
            SELECT p.id INTO foreign_plan FROM plans p
              JOIN plan_versions v ON v.plan_id = p.id
             WHERE v.id = NEW.plan_version_id
               AND p.tenant_id IS NOT NULL
               AND p.tenant_id <> NEW.tenant_id
             LIMIT 1;
        END IF;
        IF foreign_plan IS NOT NULL THEN
            RAISE EXCEPTION 'custom_plan_not_available_for_workspace'
                USING ERRCODE = 'integrity_constraint_violation',
                      DETAIL = 'The plan belongs to another workspace.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
SUBSCRIPTIONS_CUSTOM_PLAN_TRIGGER_SQL: Final = (
    "CREATE TRIGGER subscriptions_custom_plan_scope BEFORE INSERT OR UPDATE OF "
    "tenant_id, plan_id, plan_version_id, scheduled_plan_version_id ON subscriptions "
    "FOR EACH ROW EXECUTE FUNCTION billing_refuse_foreign_custom_plan()"
)
# A custom plan's owner never changes. Re-pointing one would move its existing
# subscribers' commercial terms to a company that never agreed to them.
PLAN_TENANT_IMMUTABLE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION plans_refuse_tenant_change() RETURNS trigger AS $$
    BEGIN
        IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id THEN
            RAISE EXCEPTION 'a custom plan belongs to one workspace for ever'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
PLAN_TENANT_IMMUTABLE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plans_tenant_immutable BEFORE UPDATE OF tenant_id, scope ON plans "
    "FOR EACH ROW EXECUTE FUNCTION plans_refuse_tenant_change()"
)
# A row written without a scope - by SQL, a seed, an older script - takes the one
# `is_public` implies, so `is_public` stays the authority on public versus
# private for every writer, not only the ORM (ADR-113). A BEFORE trigger runs
# ahead of the NOT NULL check, which is what lets it fill the column.
PLAN_DERIVE_SCOPE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION plans_derive_scope() RETURNS trigger AS $$
    BEGIN
        IF NEW.scope IS NULL THEN
            NEW.scope := CASE WHEN NEW.is_public THEN 'public' ELSE 'private' END;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
PLAN_DERIVE_SCOPE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plans_derive_scope BEFORE INSERT ON plans "
    "FOR EACH ROW EXECUTE FUNCTION plans_derive_scope()"
)
event.listen(Plan.__table__, "after_create", DDL(PLAN_DERIVE_SCOPE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_DERIVE_SCOPE_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_TENANT_IMMUTABLE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_TENANT_IMMUTABLE_TRIGGER_SQL))  # type: ignore[no-untyped-call]


def _term_start(context: Any) -> Any:
    """The billing term's start, as a default for a usage cycle nobody set."""
    return context.get_current_parameters()["current_period_start"]


def _term_end(context: Any) -> Any:
    """The billing term's end, as a default for a usage cycle nobody set."""
    return context.get_current_parameters()["current_period_end"]


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
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
        # What an invoice or an adjustment names, so each can name only its
        # own workspace's subscription (DB-004).
        UniqueConstraint("tenant_id", "id", name="uq_subscriptions_tenant_id_id"),
        # A live period ends after it starts (DB-012). The calendar never
        # produces anything else; this keeps repair SQL from producing it
        # either. An *ended* subscription is exempt: ending one - an immediate
        # cancellation, a void under the cancel policy - sets the period's end
        # to the moment service stopped, and with renewals billed in advance
        # that moment can precede a period that has already begun, or equal it.
        CheckConstraint(
            "ended_at IS NOT NULL OR current_period_end > current_period_start",
            name="period_ordered",
        ),
        # The pinned version is a version *of the subscription's plan*: the
        # audit pinned one plan's subscription to another plan's version with
        # plain SQL, and nothing refused it (DB-004).
        ForeignKeyConstraint(
            ["plan_id", "plan_version_id"],
            ["plan_versions.plan_id", "plan_versions.id"],
            name="fk_subscriptions_plan_version_of_plan",
            ondelete="RESTRICT",
        ),
        # The pinned price, and the scheduled one, are prices *of the version
        # beside them* (ADR-116). A price without its version would escape the
        # composite key (MATCH SIMPLE), so the CHECKs require the pair.
        ForeignKeyConstraint(
            ["plan_version_id", "plan_price_id"],
            ["plan_prices.plan_version_id", "plan_prices.id"],
            name="fk_subscriptions_plan_price_of_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scheduled_plan_version_id", "scheduled_plan_price_id"],
            ["plan_prices.plan_version_id", "plan_prices.id"],
            name="fk_subscriptions_scheduled_price_of_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "plan_price_id IS NULL OR plan_version_id IS NOT NULL", name="price_pinned"
        ),
        CheckConstraint(
            "scheduled_plan_price_id IS NULL OR scheduled_plan_version_id IS NOT NULL",
            name="scheduled_price_pinned",
        ),
        # The usage cycle lies inside the paid billing term (ADR-116): an
        # annual term holds twelve monthly cycles, a monthly term exactly one.
        # An ended subscription is exempt, for the reason `period_ordered` is.
        CheckConstraint(
            "ended_at IS NOT NULL OR (usage_period_end > usage_period_start"
            " AND usage_period_start >= current_period_start"
            " AND usage_period_end <= current_period_end)",
            name="usage_period_within_term",
        ),
        Index("ix_subscriptions_tenant_id", "tenant_id"),
        Index("ix_subscriptions_status", "status"),
        Index("ix_subscriptions_plan_id", "plan_id"),
        # The sweep that ends trials and rolls periods over reads this.
        Index("ix_subscriptions_current_period_end", "current_period_end"),
        # The sweep that opens the next monthly usage cycle reads this.
        Index("ix_subscriptions_usage_period_end", "usage_period_end"),
        # What a RESTRICT on a price scans, like every other referencing key.
        Index("ix_subscriptions_plan_price_id", "plan_price_id"),
        Index(
            "ix_subscriptions_scheduled_plan_price_id",
            "scheduled_plan_price_id",
            postgresql_where=text("scheduled_plan_price_id IS NOT NULL"),
        ),
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
    # The terms this subscription is held to: its price at renewal and the
    # limits it is enforced against (BILL-12). Pinned, so a catalogue change
    # reaches it only by an explicit migration. Nullable for a row written
    # before 0071 that no backfill could attribute; the entitlement engine
    # then pins it to the plan's current version on first read.
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    # The price this subscription renews at, and so its billing term - monthly
    # or yearly (ADR-116). NULL exactly when the version is free; a deferred
    # trigger refuses a live subscription on a priced version without one, so
    # nothing can hold paid terms with no price to renew at.
    plan_price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[SubscriptionStatus] = mapped_column(SUBSCRIPTION_STATUS_TYPE, nullable=False)
    # The moment periods are counted from (BILL-18). Every period end is the
    # anchor plus a whole number of intervals, clamped to the month, so a
    # subscription that started on the 31st renews on the 28th in February and
    # on the 31st again in March. Chaining each end from the previous one
    # instead lost the 31st for ever after the first short month.
    billing_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # A plan change waiting for the current period to end: a customer's
    # downgrade, an operator's scheduled change, or a cohort migration
    # (BILL-12). Applied once, at the boundary, by the billing sweep.
    scheduled_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # The exact price the scheduled change renews at (ADR-116). Pinned when
    # the change is scheduled, never re-resolved at the boundary: a price
    # published or retired in between does not change what was agreed.
    scheduled_plan_price_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    scheduled_change_source: Mapped[ScheduledChangeSource | None] = mapped_column(
        SCHEDULED_CHANGE_SOURCE_TYPE,
        nullable=True,
    )
    scheduled_change_reason: Mapped[str | None] = mapped_column(
        String(MAX_BILLING_REASON_LENGTH),
        nullable=True,
    )
    scheduled_change_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # The **billing term**: what the last paid invoice covers, and when the next
    # one is due - one month on a monthly price, twelve on a yearly one
    # (ADR-116). Stored rather than derived, because a plan change mid-term
    # moves the boundary and the arithmetic afterwards has to agree with what
    # was billed. Capacity top-ups last until its end.
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The **usage cycle**: the calendar month the `period_*` allowances are
    # counted over and usage top-ups expire at. Equal to the billing term on a
    # monthly price; one of its twelve months on a yearly one. Anchored on
    # `billing_anchor_at`, so the twelfth cycle ends exactly where the term
    # does. Advanced by the sweep without any invoice or charge.
    # A row written without a cycle - a monthly term, a free plan - gets the
    # term itself, which is exactly a monthly cycle. Applied by SQLAlchemy at
    # INSERT, never by the database: every service path sets both explicitly
    # (`open_term`), and a yearly term always does.
    usage_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_term_start
    )
    usage_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_term_end
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A cancellation a customer asked for but that has not taken effect yet.
    # They keep what they paid for until the period ends, which is both fair and
    # what every subscription product does.
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reserved, and written by nothing (BILL-23). Wasla owns the recurring
    # schedule itself - renewal is a Wasla invoice collected from a saved card -
    # and Paymob's Subscription module is deliberately not used, so there is no
    # provider-side subscription for these to name. Kept rather than dropped so
    # a future provider that does hold subscriptions has somewhere to write;
    # nothing may read them as meaningful today.
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @validates("current_period_start", "current_period_end")
    def _move_a_monthly_cycle_with_its_term(self, key: str, value: datetime) -> datetime:
        """A usage cycle that *is* the term moves with it (ADR-116).

        On a monthly price the usage cycle and the billing term are one window,
        so a writer that moves the term directly - a repair, an immediate
        cancellation, a test - moves the cycle with it. A cycle that differs
        from its term (a month inside a year) is never touched here: only
        `open_term` and the usage sweep move one of those. Reads the instance
        dictionary only, so an unloaded attribute is never fetched.
        """
        state = self.__dict__
        term = (state.get("current_period_start"), state.get("current_period_end"))
        cycle = (state.get("usage_period_start"), state.get("usage_period_end"))
        if None not in term and cycle == term:
            if key == "current_period_start":
                self.usage_period_start = value
            else:
                self.usage_period_end = value
        return value

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
        """Whether the sweep will advance this row no further.

        Not the same question as "can this ever change again". A suspension is
        terminal in this sense - no period opens, no invoice is raised - and is
        still the one non-serving state a payment can lift (ADR-061).
        """
        return self.status in TERMINAL_SUBSCRIPTION_STATUSES

    @property
    def is_suspended_for_non_payment(self) -> bool:
        """Whether service stopped because a bill went unpaid.

        The single place that distinction is expressed, so the settlement path
        can restore this and only this - a cancellation and an expiry are
        decisions, and paying an old invoice is not a request to undo one.
        """
        return self.status is SubscriptionStatus.SUSPENDED

    @property
    def has_scheduled_change(self) -> bool:
        return self.scheduled_plan_version_id is not None

    def clear_scheduled_change(self) -> None:
        """Forget a pending change, in one place so no field is left behind."""
        self.scheduled_plan_version_id = None
        self.scheduled_plan_price_id = None
        self.scheduled_change_source = None
        self.scheduled_change_reason = None
        self.scheduled_change_actor_id = None
        self.scheduled_change_at = None

    def end_service_at(self, moment: datetime) -> None:
        """Stop the billing term, and the usage cycle inside it, at `moment`.

        What an immediate cancellation does: nothing counts against an
        allowance the workspace no longer has, on either clock.
        """
        self.ended_at = moment
        self.current_period_end = moment
        self.usage_period_end = min(self.usage_period_end, moment)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Subscription(tenant_id={self.tenant_id!r}, status={self.status!r})"


# **A live subscription to a priced version renews at a named price, and a
# scheduled change to one names its price too** (ADR-116). Deferred to commit:
# a purchase writes the version and the price in one flush, but a row may be
# written in stages within a transaction, and only the end state has to agree.
# Re-reads the row, because a deferred trigger's NEW is the row as the queued
# statement left it. Restated verbatim by migration 0081.
SUBSCRIPTION_PRICE_PIN_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION subscriptions_refuse_unpriced_terms() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id
             WHERE s.id = NEW.id AND s.ended_at IS NULL
               AND s.plan_price_id IS NULL AND v.price > 0
        ) THEN
            RAISE EXCEPTION 'a subscription to a priced plan version renews at one of its prices'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM subscriptions s JOIN plan_versions v ON v.id = s.scheduled_plan_version_id
             WHERE s.id = NEW.id AND s.scheduled_plan_price_id IS NULL AND v.price > 0
        ) THEN
            RAISE EXCEPTION 'a scheduled change to a priced plan version names its price'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
SUBSCRIPTION_PRICE_PIN_TRIGGER_SQL: Final = (
    "CREATE CONSTRAINT TRIGGER subscriptions_price_pinned "
    "AFTER INSERT OR UPDATE OF plan_version_id, plan_price_id, scheduled_plan_version_id, "
    "scheduled_plan_price_id, ended_at ON subscriptions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION subscriptions_refuse_unpriced_terms()"
)

_CREATE_SUBSCRIPTION_STATUS, _DROP_SUBSCRIPTION_STATUS = ordered_type_ddl("subscription_status")
event.listen(Subscription.__table__, "before_create", _CREATE_SUBSCRIPTION_STATUS)
event.listen(Subscription.__table__, "after_drop", _DROP_SUBSCRIPTION_STATUS)
event.listen(Subscription.__table__, "after_create", DDL(CUSTOM_PLAN_SCOPE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Subscription.__table__, "after_create", DDL(SUBSCRIPTIONS_CUSTOM_PLAN_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Subscription.__table__, "after_create", DDL(SUBSCRIPTION_PRICE_PIN_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Subscription.__table__, "after_create", DDL(SUBSCRIPTION_PRICE_PIN_TRIGGER_SQL))  # type: ignore[no-untyped-call]


class PlanVersionMigration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Move every subscriber on one version to another, at their next renewal.

    Recorded once and applied lazily by the billing sweep as each affected
    subscription reaches its own period end - which is what keeps an operator's
    request from being a synchronous UPDATE across thousands of rows, and what
    makes "applied once" a property of the roll-over rather than of a job that
    has to remember where it got to.

    A subscription that reaches its renewal adopts the target version on the
    renewal invoice; its entitlements move only when that invoice is settled,
    so a migration can never raise somebody's limits (or price) on credit.
    """

    __tablename__ = "plan_version_migrations"
    __table_args__ = (
        Index("ix_plan_version_migrations_from_version_id", "from_version_id"),
        # One live migration out of any version: two would disagree about where
        # its subscribers go.
        Index(
            "uq_plan_version_migrations_live_from",
            "from_version_id",
            unique=True,
            postgresql_where=text("cancelled_at IS NULL"),
        ),
        CheckConstraint("from_version_id <> to_version_id", name="distinct_versions"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingAdjustment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Service given without money, said out loud (spec: operator grant).

    A complimentary grant or an invoice waiver is a legitimate thing for a
    platform to do and a dangerous thing to do invisibly. Recorded with who,
    why and for what window, so the invariant "no priced entitlement without
    financial coverage" has exactly one sanctioned exception and it is
    queryable.
    """

    __tablename__ = "billing_adjustments"
    __table_args__ = (
        Index("ix_billing_adjustments_tenant_id", "tenant_id"),
        Index("ix_billing_adjustments_subscription_id", "subscription_id"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="window_ordered"),
        # An adjustment explains its own workspace's invoice and subscription
        # (DB-004).
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_billing_adjustments_tenant_invoice",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["subscriptions.tenant_id", "subscriptions.id"],
            name="fk_billing_adjustments_tenant_subscription",
            ondelete="SET NULL (subscription_id)",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT: this is part of the financial record of why somebody held a
        # plan they did not pay for (BILL-19).
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[BillingAdjustmentKind] = mapped_column(
        BILLING_ADJUSTMENT_KIND_TYPE, nullable=False
    )
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
