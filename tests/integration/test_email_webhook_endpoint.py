"""The Resend delivery-event webhook, over real HTTP against real rows.

The endpoint is unauthenticated, so the signature is the whole boundary and
these tests are mostly attempts to get past it. The other half is the trust
boundary behind it: a verified delivery proves who sent the request, not that
its contents are true, so an address in a payload must never be an address
this system acts on.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.dependencies import get_session
from app.db.models.email import EmailStatus, EmailSuppression, OutboundEmail
from app.integrations.email.signature import compute_signature
from app.main import create_app
from app.repositories.email_repository import EmailOutboxRepository
from app.services.email_templates import EmailTemplate

pytestmark = pytest.mark.integration

WEBHOOK = "/api/v1/webhooks/email"
# base64 of 32 bytes, the shape Svix issues.
SECRET = "whsec_" + "c2VjcmV0LXZhbHVlLWZvci10ZXN0aW5nLW9ubHkh"


class _Infra:
    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def webhook_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        resend_webhook_secret=SECRET,
    )


@pytest.fixture
def app(webhook_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(webhook_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


def _signed(
    payload: dict[str, Any], *, secret: str = SECRET, timestamp: int | None = None
) -> tuple[Any, ...]:
    """The body and the three headers a genuine delivery arrives with."""
    body = json.dumps(payload).encode()
    message_id = f"msg_{uuid.uuid4().hex}"
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    signature = compute_signature(
        payload=body,
        message_id=message_id,
        timestamp=stamp,
        secret=secret,
    )
    return body, {
        "svix-id": message_id,
        "svix-timestamp": stamp,
        "svix-signature": f"v1,{signature}",
        "content-type": "application/json",
    }


def _event(kind: str, email_id: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": {"email_id": email_id, **data}}


async def _sent_email(
    session: AsyncSession,
    *,
    recipient: str = "person@example.com",
    provider_message_id: str = "prov-1",
) -> OutboundEmail:
    """A row already accepted by the provider - what an event refers to."""
    email = await EmailOutboxRepository(session).enqueue(
        recipient=recipient,
        template=EmailTemplate.PASSWORD_CHANGED.value,
        subject="A subject",
        context={},
        idempotency_key=f"key-{uuid.uuid4()}",
        available_at=datetime.now(UTC),
    )
    assert email is not None
    email.status = EmailStatus.SENT
    email.provider = "resend"
    email.provider_message_id = provider_message_id
    await session.flush()
    return email


async def _suppressions(session: AsyncSession) -> list[str]:
    rows = await session.execute(select(EmailSuppression.recipient))
    return sorted(rows.scalars())


async def test_a_valid_delivery_event_marks_the_row_delivered(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _sent_email(db_session)
    body, headers = _signed(_event("email.delivered", "prov-1"))

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.DELIVERED


async def test_an_unsigned_request_is_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    await _sent_email(db_session)

    response = await http.post(WEBHOOK, json=_event("email.delivered", "prov-1"))

    assert response.status_code == 403


async def test_a_forged_signature_is_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    email = await _sent_email(db_session)
    body, headers = _signed(_event("email.delivered", "prov-1"))
    headers["svix-signature"] = "v1,Zm9yZ2VkLXNpZ25hdHVyZS12YWx1ZQ=="

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 403
    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT


async def test_a_signature_from_another_secret_is_refused(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session)
    body, headers = _signed(
        _event("email.delivered", "prov-1"),
        secret="whsec_" + "b3RoZXItc2VjcmV0LXZhbHVlLWZvci10ZXN0cy0h",
    )

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 403


async def test_a_tampered_body_is_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    """The signature covers the exact bytes, so a swapped id must not verify."""
    await _sent_email(db_session)
    _, headers = _signed(_event("email.delivered", "prov-1"))
    tampered = json.dumps(_event("email.bounced", "prov-1")).encode()

    response = await http.post(WEBHOOK, content=tampered, headers=headers)

    assert response.status_code == 403


async def test_a_stale_timestamp_is_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    """A signature never expires, so the window is what bounds a replay."""
    await _sent_email(db_session)
    body, headers = _signed(
        _event("email.delivered", "prov-1"),
        timestamp=int(time.time()) - 4000,
    )

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 403


async def test_a_timestamp_far_in_the_future_is_refused(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session)
    body, headers = _signed(
        _event("email.delivered", "prov-1"),
        timestamp=int(time.time()) + 4000,
    )

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 403


async def test_a_missing_signature_header_is_refused(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session)
    body, headers = _signed(_event("email.delivered", "prov-1"))
    del headers["svix-signature"]

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 403


async def test_one_valid_signature_among_several_is_accepted(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Secret rotation: any one entry matching is a genuine delivery."""
    email = await _sent_email(db_session)
    body, headers = _signed(_event("email.delivered", "prov-1"))
    good = headers["svix-signature"]
    headers["svix-signature"] = f"v1,b3RoZXI= {good}"

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.DELIVERED


async def test_a_hard_bounce_suppresses_the_address_we_recorded(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _sent_email(db_session, recipient="gone@example.com")
    body, headers = _signed(_event("email.bounced", "prov-1", bounce={"type": "Permanent"}))

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.FAILED
    assert await _suppressions(db_session) == ["gone@example.com"]


async def test_a_transient_bounce_suppresses_nothing(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """A full mailbox is not a dead one, and must not be made one."""
    email = await _sent_email(db_session, recipient="busy@example.com")
    body, headers = _signed(_event("email.bounced", "prov-1", bounce={"type": "Transient"}))

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT
    assert await _suppressions(db_session) == []


async def test_a_bounce_with_no_permanence_suppresses_nothing(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Unknown permanence costs a retry; guessing the other way costs a reset."""
    await _sent_email(db_session, recipient="unclear@example.com")
    body, headers = _signed(_event("email.bounced", "prov-1"))

    await http.post(WEBHOOK, content=body, headers=headers)

    assert await _suppressions(db_session) == []


async def test_a_complaint_suppresses_the_address_but_keeps_the_status(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """It arrived - somebody read it and pressed the button."""
    email = await _sent_email(db_session, recipient="annoyed@example.com")
    body, headers = _signed(_event("email.complained", "prov-1"))

    await http.post(WEBHOOK, content=body, headers=headers)

    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT
    assert await _suppressions(db_session) == ["annoyed@example.com"]


async def test_a_forged_recipient_in_the_payload_is_ignored(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """The attack this endpoint exists to refuse.

    A verified delivery still cannot nominate whose mailbox to close: the
    address suppressed is the one *our own row* recorded, so an event naming a
    stranger suppresses the row's recipient or nothing at all.
    """
    await _sent_email(db_session, recipient="ours@example.com")
    body, headers = _signed(
        _event(
            "email.bounced",
            "prov-1",
            bounce={"type": "Permanent"},
            to="victim@example.com",
            email="victim@example.com",
        )
    )

    await http.post(WEBHOOK, content=body, headers=headers)

    assert await _suppressions(db_session) == ["ours@example.com"]
    assert "victim@example.com" not in await _suppressions(db_session)


async def test_an_event_for_an_unknown_message_id_changes_nothing(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _sent_email(db_session, provider_message_id="prov-known")
    body, headers = _signed(_event("email.bounced", "prov-unknown", bounce={"type": "Permanent"}))

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT
    assert await _suppressions(db_session) == []


async def test_the_answer_is_the_same_whether_the_id_is_known(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Otherwise the reply is an oracle for which ids this system has issued."""
    await _sent_email(db_session, provider_message_id="prov-known")
    known_body, known_headers = _signed(_event("email.delivered", "prov-known"))
    unknown_body, unknown_headers = _signed(_event("email.delivered", "prov-missing"))

    known = await http.post(WEBHOOK, content=known_body, headers=known_headers)
    unknown = await http.post(WEBHOOK, content=unknown_body, headers=unknown_headers)

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


async def test_a_replayed_delivery_event_is_harmless(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    email = await _sent_email(db_session)
    body, headers = _signed(_event("email.delivered", "prov-1"))

    first = await http.post(WEBHOOK, content=body, headers=headers)
    second = await http.post(WEBHOOK, content=body, headers=headers)

    assert first.status_code == second.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.DELIVERED


async def test_a_replayed_bounce_writes_one_suppression(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session, recipient="gone@example.com")
    body, headers = _signed(_event("email.bounced", "prov-1", bounce={"type": "Permanent"}))

    await http.post(WEBHOOK, content=body, headers=headers)
    await http.post(WEBHOOK, content=body, headers=headers)

    assert await _suppressions(db_session) == ["gone@example.com"]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "email.opened", "data": {"email_id": "prov-1"}},
        {"type": "email.clicked", "data": {"email_id": "prov-1"}},
        {"type": "email.sent", "data": {"email_id": "prov-1"}},
        {"type": "email.delivery_delayed", "data": {"email_id": "prov-1"}},
        {"type": "something.invented", "data": {"email_id": "prov-1"}},
    ],
)
async def test_an_event_type_we_do_not_act_on_is_acknowledged(
    http: AsyncClient, db_session: AsyncSession, payload: dict[str, Any]
) -> None:
    """A non-2xx eventually disables the endpoint, so nothing here refuses."""
    email = await _sent_email(db_session)
    body, headers = _signed(payload)

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "email.delivered"},
        {"type": "email.delivered", "data": "not-an-object"},
        {"type": "email.delivered", "data": {}},
        {"type": "email.delivered", "data": {"email_id": ""}},
        {"type": "email.delivered", "data": {"email_id": 123}},
        {"type": 42, "data": {"email_id": "prov-1"}},
    ],
)
async def test_a_malformed_payload_is_acknowledged_without_acting(
    http: AsyncClient, db_session: AsyncSession, payload: dict[str, Any]
) -> None:
    email = await _sent_email(db_session)
    body, headers = _signed(payload)

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200
    await db_session.refresh(email)
    assert email.status is EmailStatus.SENT


async def test_a_body_that_is_not_json_is_acknowledged(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session)
    body = b"<html>not json</html>"
    message_id = "msg_x"
    stamp = str(int(time.time()))
    signature = compute_signature(
        payload=body, message_id=message_id, timestamp=stamp, secret=SECRET
    )

    response = await http.post(
        WEBHOOK,
        content=body,
        headers={
            "svix-id": message_id,
            "svix-timestamp": stamp,
            "svix-signature": f"v1,{signature}",
        },
    )

    assert response.status_code == 200


async def test_a_json_array_body_is_acknowledged(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _sent_email(db_session)
    body, headers = _signed([])  # type: ignore[arg-type]

    response = await http.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200


async def test_an_oversized_body_is_refused_before_it_is_read(http: AsyncClient) -> None:
    """The webhook cap, which is tighter than the general one."""
    response = await http.post(
        WEBHOOK,
        content=b"x" * (2 * 1024 * 1024),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


async def test_the_endpoint_refuses_everything_when_no_secret_is_configured(
    db_session: AsyncSession,
) -> None:
    """Absent means refuse every delivery rather than trust any."""
    settings = Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        resend_webhook_secret=None,
    )
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        body, headers = _signed(_event("email.delivered", "prov-1"))
        response = await client.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 503
