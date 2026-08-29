"""The sweep that advances a subscription when its period ends, and the
subscription a new workspace is given.

Two things only a real database can show. The first is the query: which rows a
sweep picks up and which it leaves alone, including the ones whose period ended
long ago but which nobody should touch again. The second is registration - a
workspace is created, a membership is created, and a subscription with them, in
one transaction that either all happens or none of it does.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.token_store import RefreshTokenStore
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    Plan,
    SubscriptionStatus,
)
from app.db.models.invoice import InvoiceStatus
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider
from app.repositories.billing_repository import (
    PlatformSubscriptionRepository,
    SubscriptionRepository,
)
from app.repositories.invoice_repository import InvoiceRepository
from app.services.auth_service import AuthService
from app.services.subscription_service import SubscriptionService
from app.workers.billing_worker import BillingWorker

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
ENDED = NOW - timedelta(hours=1)


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class FakeTokenStore:
    """Registration issues a refresh token; nothing here reads it back."""

    async def remember(self, *args: object, **kwargs: object) -> None:
        return None

    async def revoke(self, *args: object, **kwargs: object) -> None:
        return None

    async def is_revoked(self, *args: object, **kwargs: object) -> bool:
        return False


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        # Generated rather than written down. Nothing here depends on its value,
        # and a literal long enough to satisfy the setting is also long enough to
        # look like a leaked credential to a secret scanner.
        "jwt_secret": secrets.token_urlsafe(32),
    }
    values.update(overrides)
    return Settings(**values)


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(session, *, code: str = "pro", trial_days: int = 0) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal("99.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        trial_days=trial_days,
        limits={},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscription(session, tenant, plan, *, status, end=ENDED, cancel=False):
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=status,
        current_period_start=end - timedelta(days=30),
        current_period_end=end,
    )
    subscription.cancel_at_period_end = cancel
    await session.flush()
    return subscription


def _worker(db_session) -> BillingWorker:
    return BillingWorker(database=SessionHandle(db_session), settings=_settings())


# --------------------------------------------------------------- the sweep


async def test_a_finished_trial_expires(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, trial_days=14)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.TRIALING,
    )

    handled = await _worker(db_session).run_once(now=NOW)

    assert handled == 1
    assert subscription.status is SubscriptionStatus.EXPIRED
    assert subscription.is_serving is False


async def test_an_active_subscription_opens_its_next_period(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(db_session, tenant, plan, status=SubscriptionStatus.ACTIVE)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    # The new period starts where the old one ended, so a sweep that runs late
    # does not silently shorten the customer's month.
    assert subscription.current_period_start == ENDED
    assert subscription.current_period_end == ENDED + timedelta(days=31)


async def test_a_pending_cancellation_takes_effect(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        cancel=True,
    )

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.ended_at == NOW


async def test_a_period_still_running_is_left_alone(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        end=NOW + timedelta(days=5),
    )

    assert await _worker(db_session).run_once(now=NOW) == 0


async def test_a_subscription_that_already_ended_is_never_picked_up_again(db_session):
    """Its period ending is the past, not an event. Sweeping it forever would
    rewrite `ended_at` on every pass."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.CANCELLED,
        end=NOW - timedelta(days=90),
    )

    assert await _worker(db_session).run_once(now=NOW) == 0


async def test_an_unpaid_subscription_rolls_over_still_unpaid(db_session):
    """A new period does not settle an old debt."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.PAST_DUE,
    )

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert subscription.current_period_end > NOW


async def test_the_sweep_crosses_workspaces(db_session):
    """The one query in this module that is supposed to: a platform sweep sees
    every workspace, which is why it uses the platform repository."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session)
    await _subscription(db_session, acme, plan, status=SubscriptionStatus.TRIALING)
    await _subscription(db_session, rival, plan, status=SubscriptionStatus.TRIALING)

    assert await _worker(db_session).run_once(now=NOW) == 2


async def test_a_sweep_is_bounded(db_session):
    """Rows are held until the commit, so ten thousand renewals on the first of
    the month take several passes rather than one enormous transaction."""
    plan = await _plan(db_session)
    for index in range(3):
        tenant = await _tenant(db_session, f"tenant-{index}")
        await _subscription(db_session, tenant, plan, status=SubscriptionStatus.ACTIVE)

    worker = BillingWorker(
        database=SessionHandle(db_session),
        settings=_settings(),
        claim_limit=2,
    )
    assert await worker.run_once(now=NOW) == 2


async def test_the_due_query_ignores_what_is_not_due(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        end=NOW + timedelta(days=1),
    )

    due = await PlatformSubscriptionRepository(db_session).due(now=NOW)
    assert list(due) == []


# ------------------------------------------------------------- registration


def _auth(session, settings) -> AuthService:
    return AuthService(
        session=session,
        settings=settings,
        token_store=RefreshTokenStore(FakeTokenStore()),
    )


async def test_registering_puts_the_workspace_on_the_default_plan(db_session):
    await _plan(db_session, code="starter", trial_days=14)
    settings = _settings(default_plan_code="starter")

    session_result = await _auth(db_session, settings).register(
        email="owner@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await db_session.flush()

    tenant_id = session_result.workspace.tenant.id
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant_id).get()
    assert subscription is not None
    assert subscription.status is SubscriptionStatus.TRIALING


async def test_registration_survives_a_catalogue_that_has_no_such_plan(db_session):
    """A signup that 500s over billing configuration is the least forgivable
    failure in the product. The workspace is still entitled to the default plan
    by code, so the worst case is a missing row."""
    settings = _settings(default_plan_code="not-a-plan")

    session_result = await _auth(db_session, settings).register(
        email="owner@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await db_session.flush()

    tenant_id = session_result.workspace.tenant.id
    assert await SubscriptionRepository(db_session, tenant_id=tenant_id).get() is None


async def test_a_second_workspace_gets_its_own_subscription(db_session):
    """One per workspace, not one per person: the same owner registering twice
    is two companies with two arrangements."""
    await _plan(db_session, code="starter")
    settings = _settings(default_plan_code="starter")
    service = _auth(db_session, settings)

    first = await service.register(
        email="one@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="One",
        workspace_slug="one",
    )
    second = await service.register(
        email="two@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Two",
        workspace_slug="two",
    )
    await db_session.flush()

    for result in (first, second):
        tenant_id = result.workspace.tenant.id
        assert await SubscriptionRepository(db_session, tenant_id=tenant_id).get() is not None


async def test_a_workspace_created_before_billing_can_still_subscribe(db_session):
    """The upgrade path for every workspace that predates this phase."""
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")

    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="pro",
        now=NOW,
    )

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.tenant_id == tenant.id


# ------------------------------------------------------------- invoicing


async def test_the_sweep_invoices_the_period_it_closes(db_session):
    """Billed before the roll-over, not after: afterwards the row describes the
    next month and the invoice would cover the wrong window."""
    from app.repositories.invoice_repository import InvoiceRepository

    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
    )
    closing_period_start = subscription.current_period_start

    await _worker(db_session).run_once(now=NOW)
    await db_session.flush()

    invoices = await InvoiceRepository(db_session, tenant_id=tenant.id).list_invoices()
    assert len(invoices) == 1
    assert invoices[0].period_start == closing_period_start
    assert invoices[0].period_end == ENDED
    assert invoices[0].plan_code == "pro"


async def test_a_trial_is_never_invoiced(db_session):
    """Nobody agreed to pay for it, and a bill saying "Pro plan" for a period
    the customer was told was free is a bill for something nobody sold."""
    from app.repositories.invoice_repository import InvoiceRepository

    tenant = await _tenant(db_session)
    plan = await _plan(db_session, trial_days=14)
    await _subscription(db_session, tenant, plan, status=SubscriptionStatus.TRIALING)

    await _worker(db_session).run_once(now=NOW)
    await db_session.flush()

    assert await InvoiceRepository(db_session, tenant_id=tenant.id).list_invoices() == []


async def test_two_sweeps_bill_the_period_once(db_session):
    """The constraint makes it impossible; the service check makes the second
    sweep a no-op rather than an integrity error."""
    from app.repositories.invoice_repository import InvoiceRepository

    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(db_session, tenant, plan, status=SubscriptionStatus.ACTIVE)
    worker = _worker(db_session)

    await worker.run_once(now=NOW)
    # The second sweep finds a period that has already rolled forward, so it
    # picks nothing up at all - and even if it did, the invoice is idempotent.
    await worker.run_once(now=NOW)
    await db_session.flush()

    invoices = await InvoiceRepository(db_session, tenant_id=tenant.id).list_invoices()
    assert len(invoices) == 1


async def test_an_invoice_that_cannot_be_issued_does_not_stop_the_roll_over(
    db_session,
    monkeypatch,
):
    """A billing problem must not become a customer whose plan never renews."""
    from app.workers import billing_worker as worker_module

    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
    )

    class BrokenInvoices:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def issue_for_period(self, **kwargs: object):
            raise RuntimeError("the invoice service is having a day")

    monkeypatch.setattr(worker_module, "InvoiceService", BrokenInvoices)

    handled = await _worker(db_session).run_once(now=NOW)

    assert handled == 1
    assert subscription.current_period_end > NOW


# --------------------------------------------------------------- the chasing


async def _overdue_invoice(session, tenant, subscription, *, issued: datetime, amount="99.00"):
    """A renewal invoice, as the sweep leaves one when it bills a period."""
    return InvoiceRepository(session, tenant_id=tenant.id).create(
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        plan_code="pro",
        amount_due=Decimal(amount),
        currency="USD",
        period_start=issued - timedelta(days=30),
        period_end=issued,
        lines=[],
    )


async def _issued(session, invoice, *, at: datetime):
    """Mark the invoice as actually sent, which is when the grace starts."""
    invoice.issued_at = at
    await session.flush()
    return invoice


async def _renewing(session, tenant, plan, *, status=SubscriptionStatus.ACTIVE):
    """A subscription mid-period, so the roll-over half of the sweep ignores it."""
    return await _subscription(
        session,
        tenant,
        plan,
        status=status,
        end=NOW + timedelta(days=20),
    )


async def _past_due_audits(session):
    rows = await session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.SUBSCRIPTION_PAST_DUE)
    )
    return rows.scalars().all()


async def test_a_renewal_left_unpaid_past_the_grace_marks_the_workspace_behind(db_session):
    """The half of recurring billing that was missing entirely.

    Invoices were already being issued at every period end and nothing ever
    looked at whether they were paid, so a workspace that stopped paying stayed
    `active` for ever and kept its whole plan. That is not a billing bug, it is
    the product given away.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=8))

    handled = await _worker(db_session).run_once(now=NOW)

    assert handled == 1
    assert subscription.status is SubscriptionStatus.PAST_DUE
    # Still served: a payment problem is a conversation to have, not a
    # disconnection, and `PAST_DUE` is in `SERVING_STATUSES` for that reason.
    assert subscription.is_serving is True


async def test_a_renewal_inside_the_grace_is_left_alone(db_session):
    """Cards expire and finance departments pay on Fridays.

    A customer one working day late has not stopped paying, and marking them
    behind on the morning the invoice was issued would make the state mean
    nothing.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=3))

    handled = await _worker(db_session).run_once(now=NOW)

    assert handled == 0
    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_the_grace_runs_from_when_the_customer_was_asked(db_session):
    """Not from the period boundary, which the customer never saw.

    An invoice for a period that ended weeks ago is not weeks overdue if it was
    only issued yesterday - and a sweep that had been down for a fortnight
    would otherwise mark every one of its customers behind the moment it came
    back.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(
        db_session,
        tenant,
        subscription,
        issued=NOW - timedelta(days=60),
    )
    await _issued(db_session, invoice, at=NOW - timedelta(days=1))

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_an_invoice_nobody_was_ever_sent_is_not_chased(db_session):
    """An abandoned checkout leaves an open invoice with no `issued_at`.

    Chasing somebody for a bill they were never sent is worse than not chasing,
    and it would mark a workspace behind for clicking Upgrade and changing its
    mind.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    await _overdue_invoice(db_session, tenant, subscription, issued=NOW - timedelta(days=60))

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_a_trial_is_not_marked_behind(db_session):
    """Nobody agreed to pay for it, so nobody can be late paying for it."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, trial_days=14)
    subscription = await _renewing(db_session, tenant, plan, status=SubscriptionStatus.TRIALING)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.TRIALING


async def test_a_cancelled_workspace_is_not_chased_for_an_old_bill(db_session):
    """It is already not being served, and chasing it would serve it again.

    `PAST_DUE` is a serving status, so moving a cancelled subscription into it
    would hand back the entitlements the cancellation took away - which is the
    exact bug the previous commit fixed, reintroduced from the other end.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan, status=SubscriptionStatus.CANCELLED)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.is_serving is False


async def test_a_paid_renewal_is_never_chased(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    invoice.status = InvoiceStatus.PAID
    invoice.amount_paid = Decimal("99.00")
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_a_workspace_already_behind_is_not_marked_behind_again(db_session):
    """The sweep runs every ten minutes; it must not audit every ten minutes."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))
    worker = _worker(db_session)

    first = await worker.run_once(now=NOW)
    second = await worker.run_once(now=NOW)

    assert (first, second) == (1, 0)
    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert len(await _past_due_audits(db_session)) == 1


async def test_being_marked_behind_is_audited_with_what_is_owed(db_session):
    """ "Why did this workspace stop being active" has to have an answer."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))

    await _worker(db_session).run_once(now=NOW)

    rows = await _past_due_audits(db_session)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.id
    assert rows[0].meta["outstanding"] == "99.00"
    assert rows[0].meta["invoice_id"] == str(invoice.id)


# ------------------------------------------------------- automatic renewal


def _paymob_settings(moto: int | None):
    """A worker configured to take renewals from saved cards, or not."""
    values = {
        "billing_provider": "paymob",
        "paymob_secret_key": "sk_test_notreal000000",
        "paymob_public_key": "pk_test_notreal000000",
        "paymob_hmac_secret": "a-test-hmac-secret",
        "paymob_integration_ids": [4097558],
        "app_public_url": "https://app.example.com",
    }
    if moto is not None:
        values["paymob_moto_integration_id"] = moto
    return _settings(**values)


async def _card(session, tenant, *, token: str = "tok-worker"):  # noqa: S107 - a fixture handle
    method = PaymentMethod(
        tenant_id=tenant.id,
        provider="paymob",
        provider_token=token,
        provider_token_id="15978654",
        masked_pan="xxxx-xxxx-xxxx-2346",
        brand="MasterCard",
        status=PaymentMethodStatus.ACTIVE,
        is_default=True,
    )
    session.add(method)
    await session.flush()
    return method


def _charging_worker(db_session, monkeypatch, *, moto: int | None = 9900001, seen=None):
    """A worker whose provider answers the two-step charge without a socket."""

    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        if "intention" in str(request.url):
            return httpx.Response(
                201,
                json={"id": "pi_w", "client_secret": "c", "payment_keys": [{"key": "k"}]},
            )
        return httpx.Response(200, json={"id": 910000001, "success": True, "pending": False})

    original = PaymobProvider.__init__

    def patched(self, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        original(self, **kwargs)

    monkeypatch.setattr(PaymobProvider, "__init__", patched)
    return BillingWorker(
        database=SessionHandle(db_session),
        settings=_paymob_settings(moto),
    )


async def test_the_sweep_charges_a_saved_card_for_a_due_renewal(db_session, monkeypatch):
    """The whole automatic path, driven by the worker rather than the service."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=1))
    await _card(db_session, tenant)
    seen: list[str] = []

    handled = await _charging_worker(db_session, monkeypatch, seen=seen).run_once(now=NOW)

    assert handled >= 1
    assert any("payments/pay" in url for url in seen)
    assert invoice.collection_attempts == 1
    # A request, not a settlement: the callback decides, as it does for a
    # customer paying a link.
    assert invoice.status is InvoiceStatus.PAID or invoice.status is InvoiceStatus.OPEN


async def test_the_sweep_does_not_charge_without_the_merchant_capability(
    db_session,
    monkeypatch,
):
    """No Moto integration, so the automatic path is silently skipped.

    This is the state of the account this was built against, and renewals fall
    back to being invoiced and chased - which is what the rest of the sweep
    already does.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=1))
    await _card(db_session, tenant, token="tok-nomoto")
    seen: list[str] = []

    await _charging_worker(db_session, monkeypatch, moto=None, seen=seen).run_once(now=NOW)

    assert seen == []
    assert invoice.collection_attempts == 0


async def test_the_sweep_never_charges_a_cancelled_workspace(db_session, monkeypatch):
    """Belt and braces at the worker level too, because this is the one."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan, status=SubscriptionStatus.CANCELLED)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))
    await _card(db_session, tenant, token="tok-cancelled")
    seen: list[str] = []

    await _charging_worker(db_session, monkeypatch, seen=seen).run_once(now=NOW)

    assert seen == []
    assert invoice.collection_attempts == 0
    assert subscription.status is SubscriptionStatus.CANCELLED


async def test_a_provider_outage_does_not_stop_the_rest_of_the_sweep(
    db_session,
    monkeypatch,
):
    """One workspace's trouble must not strand every other renewal behind it."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _renewing(db_session, tenant, plan)
    invoice = await _overdue_invoice(db_session, tenant, subscription, issued=NOW)
    await _issued(db_session, invoice, at=NOW - timedelta(days=30))
    await _card(db_session, tenant, token="tok-outage")

    def exploding(request):
        raise httpx.ConnectError("provider down", request=request)

    original = PaymobProvider.__init__

    def patched(self, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(exploding))
        original(self, **kwargs)

    monkeypatch.setattr(PaymobProvider, "__init__", patched)
    worker = BillingWorker(
        database=SessionHandle(db_session),
        settings=_paymob_settings(9900001),
    )

    # The sweep completes rather than raising, and the dunning half still runs.
    handled = await worker.run_once(now=NOW)

    assert handled >= 1
    assert subscription.status is SubscriptionStatus.PAST_DUE
