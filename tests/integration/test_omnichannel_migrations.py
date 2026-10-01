"""Migrations 0082-0084 against a database holding real 0081 rows.

Built from scratch to `0081` and seeded with the shapes production holds - two
workspaces, a live number and a released one, contacts, conversations on both,
inbound and outbound messages, a file, stored events, a campaign with a
recipient - then upgraded and inspected, downgraded, and upgraded again.

What is proved:

- **Backfill parity.** Every number has its connection, every contact its phone
  identity, every conversation a participant that is its own contact's phone,
  every message its conversation's connection, every file a handle locator,
  every event the WhatsApp channel, every recipient the identity it goes to -
  and the counts of what was there are unchanged.
- **The keys are valid** - no constraint left NOT VALID, no index INVALID - and
  the NOT NULLs hold.
- **The round trip is lossless** when nothing new-shaped exists.
- **A refused downgrade changes nothing.** With a username sender stored, the
  downgrade to 0081 is refused by 0082's guard - and the database is still at
  0084 with every key in place, because the downgrade is one transaction.
- **A downgrade that loses nothing is allowed**: to 0083, with that sender kept.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

NEUTRAL_KEYS = (
    "fk_conversations_tenant_connection",
    "fk_conversations_tenant_participant",
    "fk_messages_tenant_conversation_connection",
    "fk_message_media_tenant_message",
    "fk_message_media_tenant_conversation",
    "fk_whatsapp_events_tenant_connection",
    "fk_campaign_recipients_tenant_participant",
)
NEUTRAL_UNIQUES = (
    "uq_conversations_tenant_id_id_account_id",
    "uq_messages_tenant_id_conversation_id_id",
    "uq_messages_tenant_id_connection_id_wa_message_id",
    "uq_whatsapp_events_tenant_id_account_id_event_id",
    "uq_message_media_message_id_position",
)
SUPERSEDED = (
    "fk_conversations_tenant_account",
    "fk_message_media_message_id_messages",
    "fk_message_media_conversation_id_conversations",
    "fk_whatsapp_events_account_id_whatsapp_accounts",
    "uq_message_media_message_id",
)


def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    getattr(command, action)(config, revision)


async def _admin(admin_url: str, statement: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _execute(url: str, statements: list[tuple[str, dict[str, Any]]]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for statement, params in statements:
                await connection.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _rows(url: str, statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(statement), params or {})).all())
    finally:
        await engine.dispose()


def _one(url: str, statement: str, params: dict[str, Any] | None = None) -> Any:
    return asyncio.run(_rows(url, statement, params))[0][0]


class Seed:
    """Two workspaces at 0081, with every shape the backfill has to map."""

    def __init__(self) -> None:
        self.acme, self.rival = uuid.uuid4(), uuid.uuid4()
        self.live, self.released, self.rival_number = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.contacts = [uuid.uuid4() for _ in range(3)]
        self.conversations = [uuid.uuid4() for _ in range(3)]
        self.messages = [uuid.uuid4() for _ in range(4)]
        self.media = uuid.uuid4()
        self.template, self.campaign, self.recipient = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.tag = uuid.uuid4().hex[:8]

    def statements(self) -> list[tuple[str, dict[str, Any]]]:
        now = datetime.now(UTC)
        out: list[tuple[str, dict[str, Any]]] = []
        for tenant, slug in ((self.acme, "acme"), (self.rival, "rival")):
            out.append(
                (
                    "INSERT INTO tenants (id, name, slug, status) VALUES (:id, :n, :s, 'active')",
                    {"id": tenant, "n": slug.title(), "s": f"{slug}-{self.tag}"},
                )
            )
        number = (
            "INSERT INTO whatsapp_accounts (id, tenant_id, phone_number_id, waba_id,"
            " display_phone_number, status, ownership_started_at, released_at)"
            " VALUES (:id, :tenant, :pn, :waba, '+201000000000', :status, :start, :released)"
        )
        out += [
            (
                number,
                {
                    "id": self.released,
                    "tenant": self.acme,
                    "pn": f"PN-{self.tag}",
                    "waba": "waba-acme",
                    "status": "released",
                    "start": now - timedelta(days=30),
                    "released": now - timedelta(days=10),
                },
            ),
            (
                number,
                {
                    "id": self.live,
                    "tenant": self.acme,
                    "pn": f"PN-{self.tag}",
                    "waba": "waba-acme",
                    "status": "active",
                    "start": now - timedelta(days=10),
                    "released": None,
                },
            ),
            (
                number,
                {
                    "id": self.rival_number,
                    "tenant": self.rival,
                    "pn": f"PN-rival-{self.tag}",
                    "waba": "waba-rival",
                    "status": "active",
                    "start": now - timedelta(days=5),
                    "released": None,
                },
            ),
        ]
        # The same phone number in two workspaces: two people (probe X1).
        phones = ("201000000501", "201000000502", "201000000501")
        owners = (self.acme, self.acme, self.rival)
        for contact, tenant, phone in zip(self.contacts, owners, phones, strict=True):
            out.append(
                (
                    "INSERT INTO contacts (id, tenant_id, wa_id) VALUES (:id, :tenant, :phone)",
                    {"id": contact, "tenant": tenant, "phone": phone},
                )
            )
        numbers = (self.live, self.released, self.rival_number)
        for conversation, contact, tenant, account in zip(
            self.conversations, self.contacts, owners, numbers, strict=True
        ):
            out.append(
                (
                    "INSERT INTO conversations (id, tenant_id, contact_id, account_id, status,"
                    " mode, last_inbound_at) VALUES (:id, :tenant, :contact, :account, 'open',"
                    " 'ai', :at)",
                    {
                        "id": conversation,
                        "tenant": tenant,
                        "contact": contact,
                        "account": account,
                        "at": now - timedelta(hours=1),
                    },
                )
            )
        message = (
            "INSERT INTO messages (id, tenant_id, conversation_id, wa_message_id, direction,"
            " kind, status, origin, body) VALUES (:id, :tenant, :conversation, :wamid,"
            " :direction, :kind, :status, :origin, 'hi')"
        )
        shapes = (
            (self.conversations[0], "inbound", "text", "received", "customer"),
            (self.conversations[0], "outbound", "text", "sent", "human"),
            (self.conversations[1], "inbound", "image", "received", "customer"),
            (self.conversations[2], "inbound", "text", "received", "customer"),
        )
        for index, (conversation, direction, kind, status, origin) in enumerate(shapes):
            tenant = self.rival if conversation == self.conversations[2] else self.acme
            out.append(
                (
                    message,
                    {
                        "id": self.messages[index],
                        "tenant": tenant,
                        "conversation": conversation,
                        "wamid": f"wamid.{self.tag}.{index}",
                        "direction": direction,
                        "kind": kind,
                        "status": status,
                        "origin": origin,
                    },
                )
            )
        out.append(
            (
                "INSERT INTO message_media (id, tenant_id, message_id, conversation_id,"
                " wa_media_id, status, byte_size, is_voice, attempts)"
                " VALUES (:id, :tenant, :message, :conversation, 'media-handle-1', 'pending',"
                " 0, false, 0)",
                {
                    "id": self.media,
                    "tenant": self.acme,
                    "message": self.messages[2],
                    "conversation": self.conversations[1],
                },
            )
        )
        out.append(
            (
                "INSERT INTO whatsapp_events (id, tenant_id, account_id, event_id, kind, state,"
                " received_at, payload) VALUES (:id, :tenant, :account, :event, 'message',"
                " 'processed', :at, '{}')",
                {
                    "id": uuid.uuid4(),
                    "tenant": self.acme,
                    "account": self.live,
                    "event": f"wamid.{self.tag}.0",
                    "at": now,
                },
            )
        )
        out += [
            (
                "INSERT INTO whatsapp_templates (id, tenant_id, account_id, name, language,"
                " category, status, variable_count) VALUES (:id, :tenant, :account, 'offer',"
                " 'en', 'marketing', 'approved', 0)",
                {"id": self.template, "tenant": self.acme, "account": self.live},
            ),
            (
                "INSERT INTO campaigns (id, tenant_id, account_id, template_id, name, status,"
                " audience_size, messages_per_minute) VALUES (:id, :tenant, :account,"
                " :template, 'Offer', 'draft', 1, 60)",
                {
                    "id": self.campaign,
                    "tenant": self.acme,
                    "account": self.live,
                    "template": self.template,
                },
            ),
            (
                "INSERT INTO campaign_recipients (id, tenant_id, campaign_id, contact_id,"
                " status, attempts) VALUES (:id, :tenant, :campaign, :contact, 'pending', 0)",
                {
                    "id": self.recipient,
                    "tenant": self.acme,
                    "campaign": self.campaign,
                    "contact": self.contacts[0],
                },
            ),
        ]
        return out


def _counts(url: str) -> dict[str, int]:
    return {
        table: _one(url, f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table names
        for table in (
            "tenants",
            "whatsapp_accounts",
            "contacts",
            "conversations",
            "messages",
            "message_media",
            "whatsapp_events",
            "campaign_recipients",
        )
    }


def _assert_upgraded(url: str, seed: Seed, before: dict[str, int]) -> None:
    assert _counts(url) == before, "the upgrade changed how many rows there are"

    mirrored = asyncio.run(
        _rows(
            url,
            "SELECT count(*) FROM whatsapp_accounts a JOIN channel_connections c ON c.id = a.id"
            " AND c.tenant_id = a.tenant_id AND c.channel = 'whatsapp'"
            " AND c.external_account_id = a.phone_number_id"
            " AND c.status::text = a.status::text"
            " AND c.released_at IS NOT DISTINCT FROM a.released_at",
        )
    )[0][0]
    assert mirrored == before["whatsapp_accounts"]
    assert _one(url, "SELECT count(*) FROM channel_connections") == before["whatsapp_accounts"]

    phones = _one(
        url,
        "SELECT count(*) FROM contacts k JOIN contact_identities i ON i.contact_id = k.id"
        " AND i.tenant_id = k.tenant_id AND i.channel = 'whatsapp' AND i.kind = 'phone'"
        " AND i.scope = 'workspace' AND i.scope_ref = '' AND i.value = k.wa_id",
    )
    assert phones == before["contacts"]
    assert _one(url, "SELECT count(*) FROM contact_identities") == before["contacts"]

    pinned = _one(
        url,
        "SELECT count(*) FROM conversations c JOIN contact_identities i"
        " ON i.id = c.participant_identity_id AND i.contact_id = c.contact_id"
        " AND i.kind = 'phone' WHERE c.channel = 'whatsapp'",
    )
    assert pinned == before["conversations"]

    placed = _one(
        url,
        "SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id"
        " WHERE m.connection_id = c.account_id",
    )
    assert placed == before["messages"]

    located = asyncio.run(
        _rows(url, "SELECT position, locator_kind::text, locator FROM message_media")
    )
    assert located == [(0, "handle", "media-handle-1")]
    assert _one(url, "SELECT count(*) FROM whatsapp_events WHERE channel = 'whatsapp'") == 1
    assert (
        _one(
            url,
            "SELECT count(*) FROM campaign_recipients r JOIN conversations c"
            " ON c.contact_id = r.contact_id AND c.account_id = :live"
            " WHERE r.participant_identity_id = c.participant_identity_id",
            {"live": seed.live},
        )
        == 1
    )

    for name in (*NEUTRAL_KEYS, *NEUTRAL_UNIQUES):
        assert (
            _one(
                url,
                "SELECT count(*) FROM pg_constraint WHERE conname = :n AND convalidated",
                {"n": name},
            )
            == 1
        ), f"{name} is missing or NOT VALID"
    for name in SUPERSEDED:
        assert _one(url, "SELECT count(*) FROM pg_constraint WHERE conname = :n", {"n": name}) == 0
    assert _one(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == 0
    for table, column in (
        ("conversations", "participant_identity_id"),
        ("messages", "connection_id"),
    ):
        assert (
            _one(
                url,
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = :t AND column_name = :c",
                {"t": table, "c": column},
            )
            == "NO"
        )
    assert _one(url, "SELECT version_num FROM alembic_version") == "0084"


def _assert_at_0081(url: str, before: dict[str, int]) -> None:
    assert _counts(url) == before
    assert _one(url, "SELECT version_num FROM alembic_version") == "0081"
    for table in ("channel_connections", "contact_identities"):
        assert _one(url, "SELECT to_regclass(:t) IS NULL", {"t": table}) is True
    for name in SUPERSEDED:
        assert (
            _one(
                url,
                "SELECT count(*) FROM pg_constraint WHERE conname = :n AND convalidated",
                {"n": name},
            )
            == 1
        ), f"{name} was not restored"
    assert (
        _one(
            url,
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'contacts' AND column_name = 'wa_id'",
        )
        == "NO"
    )


def test_the_channel_foundation_carries_real_rows_forward_and_back(database_url: str) -> None:
    name = f"wasla_omni_mig_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    seed = Seed()
    try:
        _alembic(target, "upgrade", "0081")
        asyncio.run(_execute(target, seed.statements()))
        before = _counts(target)

        _alembic(target, "upgrade", "head")
        _assert_upgraded(target, seed, before)

        _alembic(target, "downgrade", "0081")
        _assert_at_0081(target, before)

        _alembic(target, "upgrade", "head")
        _assert_upgraded(target, seed, before)

        # A username sender: a contact with no phone, and its business-scoped id.
        username = uuid.uuid4()
        asyncio.run(
            _execute(
                target,
                [
                    (
                        "INSERT INTO contacts (id, tenant_id, wa_id) VALUES (:id, :t, NULL)",
                        {"id": username, "t": seed.acme},
                    ),
                    (
                        "INSERT INTO contact_identities (id, tenant_id, contact_id, channel, kind,"
                        " scope, scope_ref, value, source) VALUES (:id, :t, :c, 'whatsapp',"
                        " 'bsuid', 'provider_account', 'waba-acme', 'EG.0migration', 'provider')",
                        {"id": uuid.uuid4(), "t": seed.acme, "c": username},
                    ),
                ],
            )
        )

        with pytest.raises(RuntimeError, match="contacts with no WhatsApp phone number"):
            _alembic(target, "downgrade", "0081")
        # Refused as a whole: still 0084, every neutral key still there and valid.
        assert _one(target, "SELECT version_num FROM alembic_version") == "0084"
        for key in (*NEUTRAL_KEYS, *NEUTRAL_UNIQUES):
            assert (
                _one(
                    target,
                    "SELECT count(*) FROM pg_constraint WHERE conname = :n AND convalidated",
                    {"n": key},
                )
                == 1
            ), f"{key} did not survive the refused downgrade"
        assert _one(target, "SELECT count(*) FROM contacts WHERE id = :id", {"id": username}) == 1

        # Losing nothing is allowed: 0083 still holds every identity.
        _alembic(target, "downgrade", "0083")
        assert _one(target, "SELECT version_num FROM alembic_version") == "0083"
        assert _one(target, "SELECT count(*) FROM contacts WHERE id = :id", {"id": username}) == 1
        _alembic(target, "upgrade", "head")
        assert _one(target, "SELECT version_num FROM alembic_version") == "0084"
        assert _one(target, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == 0
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE {name} WITH (FORCE)"))
