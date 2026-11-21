"""WhatsApp Embedded Signup and Coexistence, against a real database (ADR-204).

Meta is the in-memory fake of `tests.fake_onboarding` - every call logged, so a
test can say what Meta was *not* asked - and ownership proof is the assisted
connect's own fake. What is asserted is the locked decisions' order and their
effects on rows: nothing is written before the proof, nothing secret is stored
unsealed, Coexistence's history is the past and never a turn, and Meta taking
the number away disables it like any disable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import generate_key
from app.core.exceptions import ExternalServiceError
from app.db.models import Tenant, TenantStatus, WhatsAppAccount, WhatsAppAccountStatus
from app.db.models.channel import (
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionHealth,
)
from app.db.models.channel_event import ChannelEvent, ChannelEventKind, ChannelEventState
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageOrigin,
)
from app.db.models.usage import UsageEvent
from app.integrations.whatsapp.onboarding import (
    EmbeddedSignupCodeExpiredError,
    EmbeddedSignupCodeInvalidError,
)
from app.integrations.whatsapp.ownership import NumberOwnershipError
from app.integrations.whatsapp.payload import account_update_events
from app.services.channel_capacity import ChannelCapacityExceededError, ChannelCapacityGuard
from app.services.coexistence_service import (
    CoexistenceProjector,
    apply_account_updates,
    request_due_syncs,
)
from app.services.credential_service import CredentialService, build_cipher
from app.services.whatsapp_account_service import WhatsAppAccountService
from app.services.whatsapp_onboarding_service import (
    COEXISTENCE_SENDS_PER_MINUTE,
    EmbeddedSignupCoexistenceUnavailableError,
    EmbeddedSignupDisabledError,
    EmbeddedSignupOnboardingLimitError,
    EmbeddedSignupRequest,
    ReleaseFollowUp,
    WhatsAppOnboardingService,
)
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.fake_onboarding import BUSINESS_TOKEN, EXPIRED_CODE, INVALID_CODE, FakeOnboardingClient
from tests.fake_ownership import FakeOwnershipVerifier
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

NUMBER = "109876543210"
COEXISTING = "109876543299"
WABA = "555000111"
BUSINESS_DISPLAY = "+20 100 000 0000"
CUSTOMER = "201234567890"


@pytest.fixture
def settings() -> Iterator[Settings]:
    yield Settings(
        _env_file=None,
        environment="test",
        meta_app_id="1234567890",
        meta_app_secret="es-app-secret-never-logged",
        meta_verify_token="es-verify",
        meta_embedded_signup_config_id="es-config",
        credential_encryption_keys=[generate_key()],
    )


async def _tenant(session: AsyncSession, slug: str = "acme") -> Tenant:
    tenant = Tenant(
        timezone=TEST_TIMEZONE, name=slug.title(), slug=slug, status=TenantStatus.ACTIVE
    )
    session.add(tenant)
    await session.flush()
    return tenant


def _services(
    session: AsyncSession,
    settings: Settings,
    client: FakeOnboardingClient,
    verifier: FakeOwnershipVerifier | None = None,
) -> WhatsAppOnboardingService:
    owner = verifier or FakeOwnershipVerifier().owns(NUMBER, waba_id=WABA).owns(
        COEXISTING, waba_id=WABA
    )
    owner.display_numbers.update({NUMBER: BUSINESS_DISPLAY, COEXISTING: "+20 100 000 0099"})
    credentials = CredentialService(settings)
    accounts = WhatsAppAccountService(session=session, ownership=owner, credentials=credentials)
    return WhatsAppOnboardingService(
        session=session,
        settings=settings,
        accounts=accounts,
        ownership=owner,
        client=client,
        credentials=credentials,
        cipher=build_cipher(settings),
    )


def _request(number: str = NUMBER, *, code: str = "es-code-ok", hint: bool | None = None) -> Any:
    return EmbeddedSignupRequest(
        code=code, phone_number_id=number, messaging_account_id=WABA, is_on_biz_app_hint=hint
    )


async def _onboard(
    session: AsyncSession,
    settings: Settings,
    client: FakeOnboardingClient,
    tenant: Tenant,
    number: str = NUMBER,
    *,
    key: str | None = None,
) -> WhatsAppAccount:
    service = _services(session, settings, client)
    result = await service.onboard(
        tenant_id=tenant.id, request=_request(number), idempotency_key=key or uuid.uuid4().hex
    )
    assert result.complete is not None
    await result.complete()
    return result.account


def _now_value(obj: Any, name: str) -> Any:
    """Read an attribute afresh: a mutation the service made, not a narrowed type."""
    return getattr(obj, name)


async def _accounts(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(WhatsAppAccount))).scalar_one()
    )


# ----------------------------------------------------------- EX: the order


async def test_ex02_a_disabled_deployment_never_calls_meta(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()
    off = settings.model_copy(update={"meta_embedded_signup_config_id": None})

    with pytest.raises(EmbeddedSignupDisabledError):
        await _services(db_session, off, client).onboard(
            tenant_id=tenant.id, request=_request(), idempotency_key="k1"
        )

    assert client.calls == []


async def test_ex02_capacity_is_asked_before_the_code_is_exchanged(
    db_session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-E18: a workspace that cannot hold the number never makes Wasla call Meta."""
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()

    async def full(self: ChannelCapacityGuard, channel: Any) -> None:
        raise ChannelCapacityExceededError()

    monkeypatch.setattr(ChannelCapacityGuard, "precheck", full)

    with pytest.raises(ChannelCapacityExceededError):
        await _services(db_session, settings, client).onboard(
            tenant_id=tenant.id, request=_request(), idempotency_key="k1"
        )

    assert client.calls == []
    assert await _accounts(db_session) == 0


async def test_oq12_the_weekly_onboarding_limit_refuses_before_meta(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    await _onboard(db_session, settings, FakeOnboardingClient(), tenant)
    client = FakeOnboardingClient()
    limited = settings.model_copy(update={"meta_onboarding_weekly_limit": 1})

    with pytest.raises(EmbeddedSignupOnboardingLimitError):
        await _services(db_session, limited, client).onboard(
            tenant_id=tenant.id, request=_request(COEXISTING), idempotency_key="k2"
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (EXPIRED_CODE, EmbeddedSignupCodeExpiredError),
        (INVALID_CODE, EmbeddedSignupCodeInvalidError),
    ],
)
async def test_ex05_a_refused_code_writes_nothing(
    db_session: AsyncSession, settings: Settings, code: str, error: type[Exception]
) -> None:
    """P-E19."""
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()

    with pytest.raises(error):
        await _services(db_session, settings, client).onboard(
            tenant_id=tenant.id, request=_request(code=code), idempotency_key="k1"
        )

    assert client.steps() == ["exchange"]
    assert await _accounts(db_session) == 0


async def test_ex08_the_sessions_ids_are_assertions_proven_with_the_exchanged_token(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E20: the same refusal and sentence as the assisted connect, nothing written."""
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()
    stranger = FakeOwnershipVerifier().owns("a-number-the-business-really-owns")

    with pytest.raises(NumberOwnershipError) as refused:
        await _services(db_session, settings, client, stranger).onboard(
            tenant_id=tenant.id, request=_request(), idempotency_key="k1"
        )

    assert refused.value.error_code == "whatsapp_ownership_unverified"
    assert stranger.calls[0]["token"] == BUSINESS_TOKEN
    assert stranger.calls[0]["claimed_waba_id"] == WABA
    assert client.steps() == ["exchange"]
    assert await _accounts(db_session) == 0


# -------------------------------------------- EX / SB: a new Cloud API number


async def test_a_new_number_is_connected_subscribed_and_registered_with_a_sealed_pin(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()

    account = await _onboard(db_session, settings, client, tenant)

    assert client.steps() == ["exchange", "coexistence", "subscribe", "register"]
    assert account.onboarding_method == "embedded_signup"
    assert account.onboarding_mode == "cloud_api"
    assert account.messaging_account_id == WABA
    assert account.subscribed_at is not None and account.registered_at is not None
    pin = client.calls[-1]["pin"]
    assert len(pin) == 6 and pin.isdigit()
    # Sealed like the token, never plaintext (SB-08, SB-12).
    assert account.registration_pin_encrypted is not None
    assert pin not in account.registration_pin_encrypted
    assert account.access_token_encrypted is not None
    assert BUSINESS_TOKEN not in account.access_token_encrypted
    assert account.sync_deadline_at is None


async def test_sb02_a_failed_subscription_keeps_the_connection_and_retry_finishes_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient().fails("subscribe")

    account = await _onboard(db_session, settings, client, tenant)
    connection = await db_session.get(ChannelConnection, account.id)

    assert account.status is WhatsAppAccountStatus.ACTIVE
    assert account.subscribed_at is None
    assert connection is not None and connection.health is ConnectionHealth.WEBHOOK_UNHEALTHY

    client.failing.clear()
    service = _services(db_session, settings, client)
    retried = await service.retry_setup(tenant_id=tenant.id, account_id=account.id)

    assert retried.subscribed_at is not None
    assert _now_value(connection, "health") is ConnectionHealth.OK
    # Registration had already succeeded and is not repeated (SB-04).
    assert client.steps().count("register") == 1


async def test_ex12_a_replayed_key_returns_the_first_answer_and_calls_meta_not_at_all(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    first = await _onboard(db_session, settings, FakeOnboardingClient(), tenant, key="same")
    client = FakeOnboardingClient()

    replay = await _services(db_session, settings, client).onboard(
        tenant_id=tenant.id, request=_request(), idempotency_key="same"
    )

    assert replay.replayed and replay.account.id == first.id
    assert client.calls == []


# ------------------------------------------------------------ CX: Coexistence


async def test_cx_coexistence_is_metas_answer_never_registered_20_mps_syncs_owed(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E21: no register call, 1200 sends a minute, the 24-hour sync deadline owed."""
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient().coexists(COEXISTING)
    service = _services(db_session, settings, client)

    result = await service.onboard(
        tenant_id=tenant.id,
        # The browser's hint says otherwise; Meta's read wins (CX-02, CX-05).
        request=_request(COEXISTING, hint=False),
        idempotency_key="k1",
    )
    assert result.complete is not None
    await result.complete()
    account = result.account
    connection = await db_session.get(ChannelConnection, account.id)

    assert account.onboarding_mode == "coexistence"
    assert "register" not in client.steps()
    assert account.registration_pin_encrypted is None
    assert connection is not None and connection.sends_per_minute == COEXISTENCE_SENDS_PER_MINUTE
    assert account.sync_deadline_at is not None and account.onboarded_at is not None
    assert account.sync_deadline_at - account.onboarded_at == timedelta(hours=24)
    assert account.sync_next_attempt_at is not None


async def test_cx06_an_unreadable_platform_writes_nothing(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()
    client.unreadable_platform = True

    with pytest.raises(ExternalServiceError):
        await _services(db_session, settings, client).onboard(
            tenant_id=tenant.id, request=_request(), idempotency_key="k1"
        )

    assert await _accounts(db_session) == 0


async def test_coexistence_switched_off_refuses_a_business_app_number(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    off = settings.model_copy(update={"meta_embedded_signup_coexistence": False})

    with pytest.raises(EmbeddedSignupCoexistenceUnavailableError):
        await _services(db_session, off, FakeOnboardingClient().coexists(COEXISTING)).onboard(
            tenant_id=tenant.id, request=_request(COEXISTING), idempotency_key="k1"
        )

    assert await _accounts(db_session) == 0


# -------------------------------------------------------------- SY: the syncs


async def test_sy_each_sync_is_requested_once_contacts_first_and_never_after_the_deadline(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    client = FakeOnboardingClient().fails("sync:history")
    credentials = CredentialService(settings)
    now = datetime.now(UTC)

    await request_due_syncs(db_session, client=client, credentials=credentials, now=now)
    assert client.steps() == ["sync:smb_app_state_sync", "sync:history"]
    assert account.contacts_sync_request_id is not None
    assert _now_value(account, "history_sync_request_id") is None
    assert account.history_sync_attempts == 1
    assert _now_value(account, "sync_next_attempt_at") > now

    client.failing.clear()
    await request_due_syncs(
        db_session, client=client, credentials=credentials, now=account.sync_next_attempt_at
    )
    # The accepted contacts request is never sent again (SY-03).
    assert client.steps().count("sync:smb_app_state_sync") == 1
    assert _now_value(account, "sync_next_attempt_at") is None
    assert account.sync_next_attempt_at is None

    late = FakeOnboardingClient()
    account.history_sync_request_id = None
    account.sync_next_attempt_at = account.sync_deadline_at
    await request_due_syncs(
        db_session, client=late, credentials=credentials, now=account.sync_deadline_at
    )
    assert late.calls == []
    assert account.sync_next_attempt_at is None


# ---------------------------------------------------------- HI / CT: history


def _history(
    *, phase: int, chunk: int, progress: int, messages: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "history",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000099",
                                "phone_number_id": COEXISTING,
                            },
                            "history": [
                                {
                                    "metadata": {
                                        "phase": phase,
                                        "chunk_order": chunk,
                                        "progress": progress,
                                    },
                                    "threads": [
                                        {
                                            "id": CUSTOMER,
                                            "context": {"wa_id": CUSTOMER},
                                            "messages": messages,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _line(message_id: str, text: str, *, from_business: bool, at: datetime) -> dict[str, Any]:
    line: dict[str, Any] = {
        "from": "201000000099" if from_business else CUSTOMER,
        "id": message_id,
        "timestamp": str(int(at.timestamp())),
        "type": "text",
        "text": {"body": text},
        "history_context": {"status": "read" if from_business else "delivered"},
    }
    if from_business:
        line["to"] = CUSTOMER
    return line


async def test_hi_imported_history_is_the_past_never_a_turn_a_meter_or_a_window(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E12, P-E13, P-E14: chunks twice and out of order change nothing."""
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    before = int(
        (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    )
    then = datetime.now(UTC) - timedelta(days=30)
    late = _history(
        phase=2,
        chunk=1,
        progress=100,
        messages=[_line("wamid.h2", "Thanks!", from_business=False, at=then)],
    )
    early = _history(
        phase=0,
        chunk=1,
        progress=40,
        messages=[
            _line(
                "wamid.h0",
                "Hello, how can I help?",
                from_business=True,
                at=then - timedelta(hours=1),
            )
        ],
    )
    ingestion = WhatsAppIngestionService(session=db_session)
    for delivery in (late, early, late):
        await ingestion.ingest(delivery)
    await db_session.flush()
    await CoexistenceProjector(db_session).run()
    await CoexistenceProjector(db_session).run()
    await db_session.flush()

    lines = (
        (await db_session.execute(select(Message).where(Message.connection_id == account.id)))
        .scalars()
        .all()
    )
    by_id = {line.wa_message_id: line for line in lines}
    assert set(by_id) == {"wamid.h0", "wamid.h2"}
    assert all(line.imported for line in lines)
    assert by_id["wamid.h0"].origin is MessageOrigin.EXTERNAL
    assert by_id["wamid.h0"].direction is MessageDirection.OUTBOUND
    assert by_id["wamid.h2"].origin is MessageOrigin.CUSTOMER
    conversation = (
        await db_session.execute(select(Conversation).where(Conversation.account_id == account.id))
    ).scalar_one()
    # Never a window (HI-09), never a meter (HI-08).
    assert conversation.last_inbound_at is None
    after = int(
        (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    )
    assert after == before
    assert account.history_progress == 100
    assert account.history_phase_max == 2
    assert account.history_completed_at is not None
    events = (
        (
            await db_session.execute(
                select(ChannelEvent).where(ChannelEvent.kind == ChannelEventKind.HISTORY)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2
    assert {event.state for event in events} == {ChannelEventState.PROCESSED}


async def test_hi04_a_declined_share_is_recorded_and_nothing_is_retried(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E17."""
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    declined = _history(phase=0, chunk=1, progress=0, messages=[])
    value = declined["entry"][0]["changes"][0]["value"]
    value["history"] = [
        {
            "errors": [
                {
                    "code": 2593109,
                    "title": (
                        "History sync is turned off by the business from the WhatsApp Business App"
                    ),
                }
            ]
        }
    ]

    await WhatsAppIngestionService(session=db_session).ingest(declined)
    await db_session.flush()
    await CoexistenceProjector(db_session).run()

    assert account.history_declined_at is not None
    assert account.history_completed_at is None


async def test_ct_an_address_book_names_only_contacts_wasla_holds(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    then = datetime.now(UTC) - timedelta(days=3)
    await WhatsAppIngestionService(session=db_session).ingest(
        _history(
            phase=0,
            chunk=1,
            progress=100,
            messages=[_line("wamid.c1", "Hi", from_business=False, at=then)],
        )
    )
    sync = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "smb_app_state_sync",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": COEXISTING},
                            "state_sync": [
                                {
                                    "type": "contact",
                                    "contact": {"full_name": "Mona Adel", "phone_number": CUSTOMER},
                                    "action": "add",
                                },
                                {
                                    "type": "contact",
                                    "contact": {
                                        "full_name": "Never Wrote",
                                        "phone_number": "201555555555",
                                    },
                                    "action": "add",
                                },
                                {
                                    "type": "contact",
                                    "contact": {"phone_number": CUSTOMER},
                                    "action": "remove",
                                },
                            ],
                        },
                    }
                ],
            }
        ],
    }
    await WhatsAppIngestionService(session=db_session).ingest(sync)
    await db_session.flush()
    await CoexistenceProjector(db_session).run()
    await db_session.flush()

    contacts = (
        (await db_session.execute(select(Contact).where(Contact.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert [contact.display_name for contact in contacts] == ["Mona Adel"]


# ------------------------------------------------- EH / ER: echoes and edits


def _messages(field: str, value: dict[str, Any], number: str = COEXISTING) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": field,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000099",
                                "phone_number_id": number,
                            },
                            **value,
                        },
                    }
                ],
            }
        ],
    }


async def _customer_wrote(session: AsyncSession, number: str, message_id: str, text: str) -> None:
    await WhatsAppIngestionService(session=session).ingest(
        _messages(
            "messages",
            {
                "contacts": [{"profile": {"name": "Mona"}, "wa_id": CUSTOMER}],
                "messages": [
                    {
                        "from": CUSTOMER,
                        "id": message_id,
                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                        "type": "text",
                        "text": {"body": text},
                    }
                ],
            },
            number,
        )
    )
    await session.flush()


def _echo(message_id: str, text: str) -> dict[str, Any]:
    return {
        "message_echoes": [
            {
                "from": "201000000099",
                "to": CUSTOMER,
                "id": message_id,
                "timestamp": str(int(datetime.now(UTC).timestamp())),
                "type": "text",
                "text": {"body": text},
            }
        ]
    }


async def test_eh_an_app_reply_is_external_and_hands_the_conversation_to_a_person(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E03, P-E15: ADR-129 unchanged, now reached by Coexistence echoes."""
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    await _customer_wrote(db_session, COEXISTING, "wamid.in1", "Hello")
    conversation = (
        await db_session.execute(select(Conversation).where(Conversation.account_id == account.id))
    ).scalar_one()
    window = conversation.last_inbound_at

    await WhatsAppIngestionService(session=db_session).ingest(
        _messages("smb_message_echoes", _echo("wamid.app1", "Answered from my phone"))
    )
    await db_session.flush()
    await db_session.refresh(conversation)

    echoed = (
        await db_session.execute(select(Message).where(Message.wa_message_id == "wamid.app1"))
    ).scalar_one()
    assert echoed.origin is MessageOrigin.EXTERNAL
    assert conversation.mode is ConversationMode.HUMAN
    # Only the customer opens the window (EH-05).
    assert conversation.last_inbound_at == window


async def test_eh06_an_echo_on_a_cloud_api_number_is_kept_not_projected(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    await _onboard(db_session, settings, FakeOnboardingClient(), tenant)
    await _customer_wrote(db_session, NUMBER, "wamid.in2", "Hello")

    await WhatsAppIngestionService(session=db_session).ingest(
        _messages("smb_message_echoes", _echo("wamid.app2", "From nowhere"), NUMBER)
    )
    await db_session.flush()

    projected = await db_session.execute(
        select(Message).where(Message.wa_message_id == "wamid.app2")
    )
    assert projected.scalar_one_or_none() is None
    kept = await db_session.execute(
        select(ChannelEvent).where(ChannelEvent.event_id == "wamid.app2")
    )
    assert kept.scalar_one().kind is ChannelEventKind.ECHO


async def test_er_an_edit_keeps_the_original_and_a_revoke_hides_the_text(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E05, P-E06: neither is a turn."""
    tenant = await _tenant(db_session)
    await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    await _customer_wrote(db_session, COEXISTING, "wamid.orig", "I want 2 rooms")
    now = str(int(datetime.now(UTC).timestamp()))

    await WhatsAppIngestionService(session=db_session).ingest(
        _messages(
            "messages",
            {
                "messages": [
                    {
                        "from": CUSTOMER,
                        "id": "wamid.edit1",
                        "timestamp": now,
                        "type": "edit",
                        "edit": {
                            "original_message_id": "wamid.orig",
                            "message": {"type": "text", "text": {"body": "I want 3 rooms"}},
                        },
                    }
                ]
            },
        )
    )
    await db_session.flush()
    line = (
        await db_session.execute(select(Message).where(Message.wa_message_id == "wamid.orig"))
    ).scalar_one()
    assert line.body == "I want 2 rooms"
    assert line.edited_body == "I want 3 rooms"
    assert line.visible_body == "I want 3 rooms"

    await WhatsAppIngestionService(session=db_session).ingest(
        _messages(
            "messages",
            {
                "messages": [
                    {
                        "from": CUSTOMER,
                        "id": "wamid.revoke1",
                        "timestamp": now,
                        "type": "revoke",
                        "revoke": {"original_message_id": "wamid.orig"},
                    }
                ]
            },
        )
    )
    await db_session.flush()
    await db_session.refresh(line)
    assert _now_value(line, "revoked_at") is not None
    assert _now_value(line, "visible_body") is None
    # Neither became a line of its own.
    rows = await db_session.execute(
        select(func.count()).select_from(Message).where(Message.wa_message_id.like("wamid.%1"))
    )
    assert rows.scalar_one() == 0


# ---------------------------------------------------- AU / RL: disconnects


def _partner_removed(reason: str = "PRIMARY_INACTIVITY") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                # The partner's own business id, not the customer's (EXT-ES-34).
                "id": "2949482758682047",
                "time": int(datetime.now(UTC).timestamp()),
                "changes": [
                    {
                        "field": "account_update",
                        "value": {
                            "event": "PARTNER_REMOVED",
                            "waba_info": {"waba_id": WABA, "owner_business_id": "2329417887457253"},
                            "disconnection_info": {"reason": reason, "initiated_by": "SYSTEM"},
                        },
                    }
                ],
            }
        ],
    }


async def test_au_partner_removed_disables_with_metas_reason_once_and_frees_the_slot(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E04: resolved by waba_info.waba_id, never by entry.id (AU-01)."""
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    payload = _partner_removed()

    await apply_account_updates(db_session, account_update_events(payload))
    await apply_account_updates(db_session, account_update_events(payload))
    await db_session.flush()
    connection = await db_session.get(ChannelConnection, account.id)

    assert account.status is WhatsAppAccountStatus.DISABLED
    assert account.access_token_encrypted is None
    assert account.provider_disconnect_reason == "PRIMARY_INACTIVITY"
    assert account.provider_disconnect_initiated_by == "SYSTEM"
    assert connection is not None
    assert connection.disabled_reason is ConnectionDisabledReason.PROVIDER_DISCONNECTED
    assert connection.health is ConnectionHealth.PROVIDER_DISCONNECTED

    reconnected = _partner_removed()
    reconnected["entry"][0]["id"] = WABA
    reconnected["entry"][0]["changes"][0]["value"] = {"event": "ACCOUNT_RECONNECTED"}
    await apply_account_updates(db_session, account_update_events(reconnected))
    # Nothing re-enables a connection but a new onboarding (AU-06).
    assert account.status is WhatsAppAccountStatus.DISABLED


async def test_rl05_relinking_a_disconnected_number_takes_the_same_flow(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    await apply_account_updates(db_session, account_update_events(_partner_removed()))
    await db_session.flush()

    again = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )

    assert again.id == account.id
    assert again.status is WhatsAppAccountStatus.ACTIVE
    assert again.provider_disconnect_reason is None
    assert again.access_token_encrypted is not None


async def test_rl_release_unsubscribes_after_and_never_deregisters(
    db_session: AsyncSession, settings: Settings
) -> None:
    """P-E09: the fake has no deregister call to make, and none is attempted."""
    tenant = await _tenant(db_session)
    account = await _onboard(db_session, settings, FakeOnboardingClient(), tenant)
    client = FakeOnboardingClient()
    service = _services(db_session, settings, client)

    released = await service._accounts.release(tenant_id=tenant.id, account_id=account.id)
    owed = service._accounts.release_follow_up
    assert owed is not None
    assert await service.unsubscribe(ReleaseFollowUp(token=owed[0], messaging_account_id=owed[1]))

    assert client.steps() == ["unsubscribe"]
    assert client.calls[0]["waba"] == WABA
    assert released.access_token_encrypted is None
    assert released.registration_pin_encrypted is None


async def test_rl03_a_failed_unsubscribe_never_undoes_the_release(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    account = await _onboard(db_session, settings, FakeOnboardingClient(), tenant)
    service = _services(db_session, settings, FakeOnboardingClient().fails("unsubscribe"))

    released = await service._accounts.release(tenant_id=tenant.id, account_id=account.id)
    owed = service._accounts.release_follow_up
    assert owed is not None

    assert not await service.unsubscribe(
        ReleaseFollowUp(token=owed[0], messaging_account_id=owed[1])
    )
    assert released.released_at is not None


# ----------------------------------- gaps the mutants found (M-R06/07/14/17)


async def test_sb12_the_pin_and_the_token_never_reach_a_log(
    db_session: AsyncSession, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """M-R06: the PIN is sealed, never logged (SB-12, P-E23)."""
    caplog.set_level(logging.DEBUG)
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient()
    await _onboard(db_session, settings, client, tenant)
    pin = client.calls[-1]["pin"]
    assert client.steps()[-1] == "register"
    logged = caplog.text + "".join(repr(record.__dict__) for record in caplog.records)
    assert pin not in logged
    assert BUSINESS_TOKEN not in logged
    assert "es-code-ok" not in logged


@pytest.mark.parametrize(
    ("number", "hint", "registered", "mode"),
    [
        (NUMBER, True, True, "cloud_api"),
        (COEXISTING, False, False, "coexistence"),
    ],
)
async def test_cx02_metas_read_decides_never_the_browsers_hint(
    db_session: AsyncSession,
    settings: Settings,
    number: str,
    hint: bool,
    registered: bool,
    mode: str,
) -> None:
    """M-R07: a hint that disagrees with Meta changes nothing (CX-02, CX-05)."""
    tenant = await _tenant(db_session)
    client = FakeOnboardingClient().coexists(COEXISTING)
    service = _services(db_session, settings, client)
    result = await service.onboard(
        tenant_id=tenant.id, request=_request(number, hint=hint), idempotency_key=uuid.uuid4().hex
    )
    assert result.complete is not None
    await result.complete()
    assert ("register" in client.steps()) is registered
    assert result.account.onboarding_mode == mode


async def test_hi13_a_message_delivered_in_two_chunks_is_one_line(
    db_session: AsyncSession, settings: Settings
) -> None:
    """M-R14: the per-message identity rule, not the delivery digest (HI-06, HI-13)."""
    tenant = await _tenant(db_session)
    account = await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    then = datetime.now(UTC) - timedelta(days=5)
    shared = _line("wamid.twice", "Twice", from_business=False, at=then)
    ingestion = WhatsAppIngestionService(session=db_session)
    await ingestion.ingest(_history(phase=0, chunk=1, progress=10, messages=[shared]))
    await ingestion.ingest(
        _history(
            phase=1,
            chunk=2,
            progress=60,
            messages=[shared, _line("wamid.once", "Once", from_business=False, at=then)],
        )
    )
    await db_session.flush()
    await CoexistenceProjector(db_session).run()
    await db_session.flush()
    ids = (
        (
            await db_session.execute(
                select(Message.wa_message_id).where(Message.connection_id == account.id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(str(value) for value in ids) == ["wamid.once", "wamid.twice"]


async def test_ct05_an_address_book_name_never_replaces_a_name_wasla_holds(
    db_session: AsyncSession, settings: Settings
) -> None:
    """M-R17: a member's name wins over the owner's phone book (CT-05)."""
    tenant = await _tenant(db_session)
    await _onboard(
        db_session, settings, FakeOnboardingClient().coexists(COEXISTING), tenant, COEXISTING
    )
    await _customer_wrote(db_session, COEXISTING, "wamid.named", "Hello")
    contact = (
        await db_session.execute(select(Contact).where(Contact.tenant_id == tenant.id))
    ).scalar_one()
    contact.name = "Mona from sales"
    contact.display_name = "Mona from sales"
    await db_session.flush()
    await WhatsAppIngestionService(session=db_session).ingest(
        _messages(
            "smb_app_state_sync",
            {
                "state_sync": [
                    {
                        "type": "contact",
                        "contact": {"full_name": "Phone Book Name", "phone_number": CUSTOMER},
                        "action": "add",
                    }
                ]
            },
        )
    )
    await db_session.flush()
    await CoexistenceProjector(db_session).run()
    await db_session.flush()
    await db_session.refresh(contact)
    assert contact.name == "Mona from sales"
    assert contact.display_name == "Mona from sales"
