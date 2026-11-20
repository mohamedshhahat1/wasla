"""The e2e billing-state helper writes rows the API reads back as the journeys expect (EC8-04).

Run against real rows: each helper is applied, and the owner's own routes then
read the saved card (masked, the default) and the refunded payment.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TenantRole
from tests.e2e_stack import billing_states
from tests.integration import test_billing_authorization as _shared
from tests.integration.test_billing_authorization import (
    BILLING,
    _act_as,
    _workspace_rows,
)
from tests.payment_tokens import SETTINGS as TOKEN_SETTINGS

# The shared fixtures, bound by name so pytest finds them here.
app = _shared.app
http = _shared.http
provider_transport = _shared.provider_transport

pytestmark = pytest.mark.integration


async def test_a_saved_card_is_listed_masked_and_default(
    http: AsyncClient, app: object, db_session: AsyncSession
) -> None:
    tenant, user = await _workspace_rows(db_session, "bs-card")
    written = await billing_states.saved_card(db_session, tenant.slug, TOKEN_SETTINGS)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)  # type: ignore[arg-type]

    listed = await http.get(f"{BILLING}/payment-methods")
    assert listed.status_code == 200, listed.text
    (card,) = listed.json()
    assert card["id"] == written["payment_method_id"]
    assert card["masked_pan"] == "2346" and card["is_default"] is True
    assert "e2e-card-token" not in listed.text


async def test_a_refunded_payment_reads_refunded(
    http: AsyncClient, app: object, db_session: AsyncSession
) -> None:
    tenant, user = await _workspace_rows(db_session, "bs-refund")
    written = await billing_states.refunded(db_session, tenant.slug)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)  # type: ignore[arg-type]

    payment = await http.get(f"{BILLING}/payments/{written['payment_id']}")
    assert payment.status_code == 200, payment.text
    assert payment.json()["status"] == "refunded"
    refused = await http.post(f"{BILLING}/payments/{written['payment_id']}/refund", json={})
    assert refused.status_code == 409


def test_the_usage_is_refused_before_any_connection() -> None:
    with pytest.raises(SystemExit):
        billing_states.parse(["refund", "slug"])
    assert billing_states.parse(["saved-card", "acme"]) == ("saved-card", "acme")
