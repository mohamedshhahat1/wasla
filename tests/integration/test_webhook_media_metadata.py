"""A hostile or oversized attachment name cannot cost a delivery (MEDIA-05).

Through the real application - lifespan, signature check, `CommittingRoute`,
real ingestion - against a real PostgreSQL, so the database's own refusal is the
thing under test rather than a reimplementation of it.

What the audit reproduced (P4-01): a document name of 301 characters, ASCII or
Arabic, made PostgreSQL refuse the delivery's single transaction. Under asyncpg
the refusal is a generic `DBAPIError`, never the `DataError` the route caught,
so every one of Meta's retries answered 500 for seven days and the text message
sent alongside the file was never stored either. That reopened MSG-03.

Three things are held here, each on its own:

- the name is normalised before it reaches the database, so the file and its
  siblings are stored;
- a value that is still refused - here a sender id too long for its column -
  costs that one message, not the delivery;
- the route's own net fires on class 22 and on nothing else, so a database that
  is down still makes Meta retry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.filenames import MAX_FILENAME_LENGTH
from app.db.models.conversation import Message
from app.db.models.media import MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppEvent
from app.main import create_app
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "media-metadata-app-secret"
REDIS_URL = "redis://localhost:6379/12"
CUSTOMER = "201234567890"


class _RefusalError(Exception):
    """A driver error carrying a SQLSTATE, as asyncpg's adapted errors do."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"refused ({sqlstate})")
        self.sqlstate = sqlstate


def _database_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("INSERT ...", {}, _RefusalError(sqlstate))


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _delivery(phone_number_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nour"}}],
                            "messages": messages,
                        },
                    }
                ],
            }
        ],
    }


def _text(tag: str, *, sender: str = CUSTOMER) -> dict[str, Any]:
    return {
        "id": f"wamid.text.{tag}",
        "from": sender,
        "timestamp": "1760000000",
        "type": "text",
        "text": {"body": "Hello, I am sending the contract now"},
    }


def _document(tag: str, filename: str) -> dict[str, Any]:
    return {
        "id": f"wamid.doc.{tag}",
        "from": CUSTOMER,
        "timestamp": "1760000001",
        "type": "document",
        "document": {
            "id": f"media-{tag}",
            "mime_type": "application/pdf",
            "filename": filename,
            "caption": "Here is the contract",
        },
    }


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=4, max_overflow=2)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def workspace(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, str]]:
    phone_number_id = f"pn-{uuid.uuid4().hex[:10]}"
    async with committing() as session:
        tenant = Tenant(name="Names", slug=f"names-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        session.add(
            WhatsAppAccount(
                tenant_id=tenant.id,
                phone_number_id=phone_number_id,
                waba_id="555000111",
                display_phone_number="+201000000000",
            )
        )
        await session.commit()
        created = (tenant.id, phone_number_id)
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created[0]))
            await session.commit()


@pytest_asyncio.fixture
async def client(prepared_database: str, redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=prepared_database,
        redis_url=REDIS_URL,
        meta_app_secret=APP_SECRET,
        meta_verify_token="verify",
        rate_limit_enabled=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://wasla") as http:
            yield http


async def _post(http: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await http.post(PATH, content=body, headers=_signed(body))


async def _stored(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> tuple[int, int, list[str | None]]:
    async with committing() as session:
        messages = (
            await session.execute(
                select(func.count()).select_from(Message).where(Message.tenant_id == tenant_id)
            )
        ).scalar_one()
        events = (
            await session.execute(
                select(func.count())
                .select_from(WhatsAppEvent)
                .where(WhatsAppEvent.tenant_id == tenant_id)
            )
        ).scalar_one()
        names = list(
            (
                await session.execute(
                    select(MessageMedia.filename).where(MessageMedia.tenant_id == tenant_id)
                )
            ).scalars()
        )
    return int(messages), int(events), names


# ---------------------------------------------------------------- the name


@pytest.mark.parametrize(
    ("label", "filename", "expected"),
    [
        ("ascii_300", "a" * 296 + ".pdf", "a" * 296 + ".pdf"),
        ("ascii_301", "a" * 297 + ".pdf", "a" * 296 + ".pdf"),
        ("arabic_301", "عقد" * 99 + ".pdf", "عقد" * 98 + "عق.pdf"),
        ("ascii_2000", "a" * 1996 + ".pdf", "a" * 296 + ".pdf"),
        ("controls", "contract\u0007\u001b.pdf", "contract.pdf"),
        ("nul", "contract\u0000.pdf", "contract.pdf"),
    ],
)
async def test_a_long_or_hostile_name_is_stored_bounded_with_its_sibling(
    client: httpx.AsyncClient,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, str],
    label: str,
    filename: str,
    expected: str,
) -> None:
    """P4-01, reversed: 200, both messages stored, the name bounded."""
    tenant_id, phone_number_id = workspace
    tag = uuid.uuid4().hex[:8]

    response = await _post(
        client, _delivery(phone_number_id, [_text(tag), _document(tag, filename)])
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    messages, events, names = await _stored(committing, tenant_id)
    assert (messages, events) == (2, 2)
    assert names == [expected]
    assert len(names[0] or "") <= MAX_FILENAME_LENGTH


async def test_a_redelivery_of_the_same_delivery_is_a_duplicate_not_a_failure(
    client: httpx.AsyncClient,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, str],
) -> None:
    """Meta's retry of the same delivery: accepted, and nothing stored twice."""
    tenant_id, phone_number_id = workspace
    tag = uuid.uuid4().hex[:8]
    payload = _delivery(phone_number_id, [_text(tag), _document(tag, "x" * 2000 + ".pdf")])

    first = await _post(client, payload)
    second = await _post(client, payload)

    assert (first.status_code, second.status_code) == (200, 200)
    messages, events, names = await _stored(committing, tenant_id)
    assert (messages, events, len(names)) == (2, 2, 1)


# ----------------------------------------------- one refused message, alone


async def test_a_message_the_database_refuses_costs_only_that_message(
    client: httpx.AsyncClient,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, str],
) -> None:
    """The per-message boundary, proven with a value that is still refused.

    A sender id of forty digits does not fit `contacts.wa_id`. PostgreSQL
    refuses it with a class-22 error inside that message's savepoint; the
    messages either side of it - one of them a document with a 2,000-character
    name - are stored, the delivery is accepted, and Meta has no reason to
    retry it."""
    tenant_id, phone_number_id = workspace
    tag = uuid.uuid4().hex[:8]
    oversized_sender = "2" * 40

    response = await _post(
        client,
        _delivery(
            phone_number_id,
            [
                _text(tag),
                _text(f"{tag}-refused", sender=oversized_sender),
                _document(tag, "b" * 2000 + ".pdf"),
            ],
        ),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    messages, events, names = await _stored(committing, tenant_id)
    assert (messages, events) == (2, 2)
    assert names == ["b" * 296 + ".pdf"]


# ------------------------------------------------ the route's own net (M28)


async def test_a_content_refusal_outside_the_savepoints_is_accepted_not_retried(
    client: httpx.AsyncClient,
    workspace: tuple[uuid.UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's last net is reachable now, and fires on class 22."""
    _, phone_number_id = workspace

    async def refuse(*_: object, **__: object) -> None:
        raise _database_error("22001")

    monkeypatch.setattr(WhatsAppIngestionService, "_enqueue_media", refuse)
    response = await _post(
        client, _delivery(phone_number_id, [_document(uuid.uuid4().hex[:8], "c.pdf")])
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


@pytest.mark.parametrize(
    ("sqlstate", "where"),
    [
        ("40P01", "route"),  # deadlock
        ("40001", "route"),  # serialisation failure
        ("08006", "route"),  # connection failure
        ("40P01", "savepoint"),
        ("57P01", "savepoint"),  # the server shutting down
    ],
)
async def test_a_database_that_cannot_work_is_still_retried_by_meta(
    client: httpx.AsyncClient,
    workspace: tuple[uuid.UUID, str],
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
    where: str,
) -> None:
    """Swallowing every `DBAPIError` would hide an outage as `accepted`, and
    Meta would stop redelivering messages that were never stored."""
    _, phone_number_id = workspace

    async def fail(*_: object, **__: object) -> None:
        raise _database_error(sqlstate)

    target = "_enqueue_media" if where == "route" else "_ingest_one"
    monkeypatch.setattr(WhatsAppIngestionService, target, fail)
    response = await _post(
        client, _delivery(phone_number_id, [_document(uuid.uuid4().hex[:8], "d.pdf")])
    )

    assert response.status_code >= 500
