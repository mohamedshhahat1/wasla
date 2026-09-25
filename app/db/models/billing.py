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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base, RevisionedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

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


class LimitKey(StrEnum):
    """What a plan can put a ceiling on.

    Two kinds, and the difference decides how each is checked:

    - **Resource limits** measure what exists *now* - numbers, agents, people,
      bytes held in the object store. Checked with a `COUNT` or a `SUM`, and a
      workspace over the limit stays over it until something is deleted.
    - **Usage limits** count what was consumed *in the current billing period*,
      read from `usage_events`. They reset when the period rolls over, which is
      what makes "1,000 messages a month" mean anything.

    `PERIOD_` is in the name of the second kind so a reader never has to guess
    which sort of question a key is asking.

    `STORAGE_BYTES` is the first resource limit that is not a row count, and
    the distinction matters: `usage_events` records `STORAGE_USED` when bytes
    are *written* and never subtracts when they are purged, so it answers
    "how much has this workspace ever stored" and cannot answer "how much is it
    holding". A capacity limit needs the second question, so it is a `SUM` over
    the rows that still name an object (ADR-091).
    """

    WHATSAPP_NUMBERS = "whatsapp_numbers"
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
        LimitKey.WHATSAPP_NUMBERS,
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
TOPUP_LIMITS: Final[frozenset[LimitKey]] = frozenset(
    {
        LimitKey.PERIOD_MESSAGES,
        LimitKey.PERIOD_AI_TURNS,
        LimitKey.PERIOD_CAMPAIGN_MESSAGES,
        LimitKey.STORAGE_BYTES,
        LimitKey.WHATSAPP_NUMBERS,
        LimitKey.TEAM_MEMBERS,
        LimitKey.KNOWLEDGE_DOCUMENTS,
    }
)

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
SUBSCRIPTION_STATUS_TYPE = _enum_type(SubscriptionStatus, name="subscription_status")
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
        """The ceiling for one key, or None for unlimited.

        A stored value that is not a positive integer is treated as unlimited
        rather than as zero. Zero would mean "this workspace may do nothing",
        which is never what a malformed row was meant to say, and a plan edited
        badly should not lock a paying customer out of their own product.
        """
        return validated_limit(self.limits.get(key.value))

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Plan(code={self.code!r}, interval={self.interval!r})"


class PlanVersion(Base, UUIDPrimaryKeyMixin):
    """One immutable set of commercial terms for a plan (BILL-12).

    Everything a customer is charged and allowed: price, currency, interval and
    limits, plus the name as it was shown. Written once and never changed - a
    trigger refuses any UPDATE, so "what did version 3 of Pro cost" has one
    answer for ever, and an invoice that names a version keeps meaning what it
    meant.

    `effective_at` is when a version becomes the one *new* customers get. It
    changes nobody already subscribed: a subscription keeps the version it
    points at until an operator migrates it, which is the contract that stops a
    catalogue edit silently re-pricing or downgrading an existing customer.
    """

    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_id_version"),
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

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"PlanVersion(plan_id={self.plan_id!r}, version={self.version!r})"


# The immutability of a version is a property of the table, not of the code
# that happens to write it today. Attached as DDL so `create_all` (the model-
# built test schema) gets exactly what migration 0071 installs.
_PLAN_VERSION_IMMUTABLE_FUNCTION = DDL("""
    CREATE OR REPLACE FUNCTION plan_versions_refuse_update() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'plan_versions rows are immutable; publish a new version'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql
    """)  # type: ignore[no-untyped-call]
_PLAN_VERSION_IMMUTABLE_TRIGGER = DDL(  # type: ignore[no-untyped-call]
    "CREATE TRIGGER plan_versions_immutable BEFORE UPDATE ON plan_versions "
    "FOR EACH ROW EXECUTE FUNCTION plan_versions_refuse_update()"
)
event.listen(PlanVersion.__table__, "after_create", _PLAN_VERSION_IMMUTABLE_FUNCTION)
event.listen(PlanVersion.__table__, "after_create", _PLAN_VERSION_IMMUTABLE_TRIGGER)

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
    $$ LANGUAGE plpgsql
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
    $$ LANGUAGE plpgsql
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
    $$ LANGUAGE plpgsql
    """
PLAN_DERIVE_SCOPE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER plans_derive_scope BEFORE INSERT ON plans "
    "FOR EACH ROW EXECUTE FUNCTION plans_derive_scope()"
)
event.listen(Plan.__table__, "after_create", DDL(PLAN_DERIVE_SCOPE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_DERIVE_SCOPE_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_TENANT_IMMUTABLE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Plan.__table__, "after_create", DDL(PLAN_TENANT_IMMUTABLE_TRIGGER_SQL))  # type: ignore[no-untyped-call]


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
    # The terms this subscription is held to: its price at renewal and the
    # limits it is enforced against (BILL-12). Pinned, so a catalogue change
    # reaches it only by an explicit migration. Nullable for a row written
    # before 0071 that no backfill could attribute; the entitlement engine
    # then pins it to the plan's current version on first read.
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
    # Reserved, and written by nothing (BILL-23). Wasla owns the recurring
    # schedule itself - renewal is a Wasla invoice collected from a saved card -
    # and Paymob's Subscription module is deliberately not used, so there is no
    # provider-side subscription for these to name. Kept rather than dropped so
    # a future provider that does hold subscriptions has somewhere to write;
    # nothing may read them as meaningful today.
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
        self.scheduled_change_source = None
        self.scheduled_change_reason = None
        self.scheduled_change_actor_id = None
        self.scheduled_change_at = None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Subscription(tenant_id={self.tenant_id!r}, status={self.status!r})"


event.listen(Subscription.__table__, "after_create", DDL(CUSTOM_PLAN_SCOPE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Subscription.__table__, "after_create", DDL(SUBSCRIPTIONS_CUSTOM_PLAN_TRIGGER_SQL))  # type: ignore[no-untyped-call]


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
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT: this is part of the financial record of why somebody held a
        # plan they did not pay for (BILL-19).
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[BillingAdjustmentKind] = mapped_column(
        BILLING_ADJUSTMENT_KIND_TYPE, nullable=False
    )
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
