"""A second claim on a WhatsApp number answers with its own code (FEB-06, FE-3, EC-02).

The connect screen tells "that number is already connected" apart from the other
409s of the same route (no free channel slot, a channel the plan does not include)
and from the generic "this changed, reload" conflict. Only the code is new: the
status, the sentence and the refusal to name the workspace holding the number are
what they were.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, register_exception_handlers
from app.db.models import Tenant, TenantStatus
from app.repositories.whatsapp_repository import (
    NUMBER_ALREADY_CONNECTED,
    WhatsAppAccountDirectory,
)
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.fake_ownership import FakeOwnershipVerifier
from tests.workspace_timezone import TEST_TIMEZONE

NUMBER = "109876543299"
TOKEN = "EAAG-a-fake-credential-for-the-conflict-code"
SENTENCE = "That WhatsApp number is already connected."


async def _tenant(session: AsyncSession, slug: str) -> Tenant:
    tenant = Tenant(
        timezone=TEST_TIMEZONE, name=slug.title(), slug=slug, status=TenantStatus.ACTIVE
    )
    session.add(tenant)
    await session.flush()
    return tenant


def _service(session: AsyncSession) -> WhatsAppAccountService:
    return WhatsAppAccountService(session=session, ownership=FakeOwnershipVerifier().owns(NUMBER))


async def _claimed_by_holder(session: AsyncSession) -> Tenant:
    holder = await _tenant(session, "holder-co")
    await _service(session).connect(tenant_id=holder.id, phone_number_id=NUMBER, access_token=TOKEN)
    await session.flush()
    return holder


def test_the_code_is_named() -> None:
    assert NUMBER_ALREADY_CONNECTED == "whatsapp_number_already_connected"


async def test_a_second_claim_carries_the_code_with_the_same_status_and_sentence(
    db_session: AsyncSession,
) -> None:
    await _claimed_by_holder(db_session)
    second = await _tenant(db_session, "second-co")

    with pytest.raises(ConflictError) as raised:
        await _service(db_session).connect(
            tenant_id=second.id, phone_number_id=NUMBER, access_token=TOKEN
        )

    assert raised.value.error_code == NUMBER_ALREADY_CONNECTED
    assert raised.value.status_code == 409
    assert raised.value.message == SENTENCE
    assert raised.value.details is None


async def test_a_claim_past_the_read_check_carries_the_same_code(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integrity path (the race) answers exactly as the read check does."""
    await _claimed_by_holder(db_session)
    second = await _tenant(db_session, "racer-co")

    async def blind(self: object, phone_number_id: str) -> None:
        return None

    monkeypatch.setattr(WhatsAppAccountDirectory, "get_by_phone_number_id", blind)

    with pytest.raises(ConflictError) as raised:
        await _service(db_session).connect(
            tenant_id=second.id, phone_number_id=NUMBER, access_token=TOKEN
        )

    assert raised.value.error_code == NUMBER_ALREADY_CONNECTED
    assert raised.value.status_code == 409
    assert raised.value.message == SENTENCE


async def test_the_answer_on_the_wire_names_no_workspace(db_session: AsyncSession) -> None:
    """The envelope a browser receives: the code, the sentence, nothing about the holder."""
    holder = await _claimed_by_holder(db_session)
    second = await _tenant(db_session, "asker-co")
    with pytest.raises(ConflictError) as raised:
        await _service(db_session).connect(
            tenant_id=second.id, phone_number_id=NUMBER, access_token=TOKEN
        )
    refusal = raised.value

    app = FastAPI()
    app.state.settings = get_settings()
    register_exception_handlers(app)

    @app.post("/claim")
    async def claim() -> None:
        raise refusal

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/claim")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == NUMBER_ALREADY_CONNECTED
    assert body["error"]["message"] == SENTENCE
    text = response.text.lower()
    for leak in ("holder", str(holder.id), holder.slug, "workspace", "tenant"):
        assert leak.lower() not in text
