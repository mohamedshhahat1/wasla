"""FEB-06 for FE-8's screens: refusals a billing screen tells apart carry their own code.

Each kept its status and its sentence (EC8-03); the code is the only change,
and it is what lets a screen say the right thing - "pay first" rather than
"forbidden", "check the payment you already opened" rather than a generic
conflict. The real dependency graph runs, with the provider's transport
answered in-process, as in `test_billing_authorization.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.dependencies import get_session
from app.db.models import TenantRole
from app.main import create_app
from tests.conftest import AllowingEntitlements
from tests.integration import test_billing_authorization as _shared
from tests.integration.test_billing_authorization import (
    BILLING,
    _act_as,
    _collected,
    _Infra,
    _plan,
    _settings,
    _workspace_rows,
)

# The shared fixtures, bound by name so pytest finds them here.
app = _shared.app
http = _shared.http
provider_transport = _shared.provider_transport

pytestmark = pytest.mark.integration


async def test_a_repeated_checkout_key_is_checkout_already_started(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    tenant, user = await _workspace_rows(db_session, "ec8-key")
    plan = await _plan(db_session)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)
    body = {"plan_code": plan.code, "idempotency_key": "ec8-attempt-1"}

    first = await http.post(f"{BILLING}/checkout", json=body)
    assert first.status_code == 201, first.text
    again = await http.post(f"{BILLING}/checkout", json=body)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "checkout_already_started"
    assert error["message"].startswith("A checkout has already been started for this request.")


async def test_a_review_while_a_refund_is_outstanding_is_refund_already_requested(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """Asking for a review twice is the same request (202 both times); asking
    while a provider refund is already on its way is refused with its code."""
    tenant, user = await _workspace_rows(db_session, "ec8-refund")
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    first = await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})
    assert first.status_code == 202, first.text
    assert (await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})).status_code == 202

    payment.refund_requested_at = datetime.now(UTC)
    await db_session.flush()
    outstanding = await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})
    assert outstanding.status_code == 409
    assert outstanding.json()["error"]["code"] == "refund_already_requested"
    assert outstanding.json()["error"]["message"] == (
        "A refund has already been requested for this payment."
    )


@pytest.fixture
def unconfigured(db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(_settings(billing_provider="manual"))
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def bare(unconfigured: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=unconfigured), base_url="http://wasla.test"
    ) as client:
        yield client


async def test_no_provider_is_payment_provider_unavailable(
    bare: AsyncClient, unconfigured: FastAPI, db_session: AsyncSession
) -> None:
    tenant, user = await _workspace_rows(db_session, "ec8-none")
    plan = await _plan(db_session)
    _act_as(unconfigured, tenant, user, TenantRole.TENANT_OWNER)

    response = await bare.post(f"{BILLING}/checkout", json={"plan_code": plan.code})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "payment_provider_unavailable"
    assert error["message"] == "No payment provider is configured."
