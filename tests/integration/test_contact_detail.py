"""The contact a member reads, names and follows the consent of (FEB-19; FE-5).

Against PostgreSQL, through the real routes:

- a contact reads with its identities, the channel's name, the name the team
  gave and its consent channel by channel; another workspace's is not found;
- the one edit is the name: trimmed, kept byte for byte, idempotent, audited,
  and told to open inboxes - and never an identity;
- the name the team gave is the one a conversation shows and both searches
  find, with Turkish letters folded;
- every opt-out and every clear is remembered with who did it, in the
  transaction that changes the state; one older than the history still shows;
- one customer's conversations are a view of the inbox, bound into its cursor.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.db.models import User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.consent import ConsentAction, ContactChannelConsent, ContactConsentEvent
from app.db.models.conversation import Contact, Conversation, ConversationStatus
from app.db.models.lead import Lead, LeadStatus
from app.repositories import base as repository_base
from app.services import opt_out
from app.services.contact_service import ContactService
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.integration.test_inbox_contract import (
    BSUID,
    NOW,
    PHONE_A,
    Inbox,
    customer,
    inbox,
    statements,
)
from tests.integration.test_opt_out_ingestion import _account, _contact, _inbound

pytestmark = pytest.mark.integration

__all__ = ["inbox"]

CONTACTS = "/api/v1/contacts"
CONVERSATIONS = "/api/v1/conversations"
LEADS = "/api/v1/leads"
# Obviously fake people. The Turkish names carry every letter the search folds.
IPEK = "\u0130pek I\u015f\u0131k Test"
ILGIN = "\u0131lg\u0131n Test"
ISTANBUL = "Istanbul Test Lead"
ARABIC = "نور (VIP) \U0001f642"


async def _nour(desk: Inbox, **extra: Any) -> Conversation:
    shop = desk.mine
    return await customer(
        desk.database, shop.tenant_id, shop.account_id, name="Nour Test", phone=PHONE_A, **extra
    )


async def _row(desk: Inbox, model: Any, *where: Any) -> Any:
    async with desk.database.session_factory() as session:
        return (await session.execute(select(model).where(*where))).scalars().all()


async def _rename(desk: Inbox, contact_id: uuid.UUID, name: str | None, who: Any = None) -> Any:
    return await desk.client.patch(
        f"{CONTACTS}/{contact_id}",
        json={"name": name},
        headers=(who or desk.mine.member).headers,
    )


# ------------------------------------------------------------------- the read


async def test_a_contact_reads_with_its_identities_names_and_consent(inbox: Inbox) -> None:
    """CD-T1, CD-T3."""
    conversation = await _nour(inbox)
    answer = await inbox.client.get(
        f"{CONTACTS}/{conversation.contact_id}", headers=inbox.mine.member.headers
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == {
        "id",
        "identities",
        "profile_name",
        "name",
        "display_name",
        "channels",
        "opted_out",
        "opted_out_at",
        "created_at",
        "last_inbound_at",
    }
    assert body["profile_name"] == "Nour Test" and body["name"] is None
    assert body["display_name"] == "Nour Test"
    assert [(i["channel"], i["kind"], i["value"]) for i in body["identities"]] == [
        ("whatsapp", "phone", PHONE_A)
    ]
    assert body["opted_out"] is False and body["opted_out_at"] is None
    assert body["last_inbound_at"] is not None
    assert [channel["channel"] for channel in body["channels"]] == ["whatsapp"]


async def test_a_business_scoped_id_is_an_identity_and_never_a_phone(inbox: Inbox) -> None:
    """CD-T1: the kind says what the value is; nothing presents it as a number."""
    shop = inbox.mine
    conversation = await customer(
        inbox.database, shop.tenant_id, shop.account_id, name=None, bsuid=BSUID
    )
    body = (
        await inbox.client.get(f"{CONTACTS}/{conversation.contact_id}", headers=shop.member.headers)
    ).json()
    assert [(i["kind"], i["value"]) for i in body["identities"]] == [("bsuid", BSUID)]
    assert body["display_name"] is None and body["profile_name"] is None


async def test_another_workspaces_contact_and_an_unknown_one_are_the_same_not_found(
    inbox: Inbox,
) -> None:
    """CD-T2, CD-T15: no oracle, on each of the three routes."""
    theirs = await customer(
        inbox.database,
        inbox.theirs.tenant_id,
        inbox.theirs.account_id,
        name="Other",
        phone="201055507001",
    )
    headers = inbox.mine.member.headers
    for suffix, method, payload in (
        ("", "GET", None),
        ("", "PATCH", {"name": "x"}),
        ("/consent-events", "GET", None),
    ):
        bodies = []
        for contact_id in (theirs.contact_id, uuid.uuid4()):
            answer = await inbox.client.request(
                method, f"{CONTACTS}/{contact_id}{suffix}", json=payload, headers=headers
            )
            assert answer.status_code == 404
            bodies.append({**answer.json()["error"], "request_id": None})
        assert bodies[0] == bodies[1]
        assert bodies[0]["code"] == "not_found"


async def test_the_detail_costs_the_same_with_one_conversation_or_five(inbox: Inbox) -> None:
    """CD-T6: the newest inbound time is one aggregate, never a read per conversation."""
    shop = inbox.mine
    first = await _nour(inbox)
    counts = []
    for extra in (0, 4):
        async with inbox.database.session_factory() as session:
            for index in range(extra):
                from tests.integration.test_inbox_contract import _second_account

                account = await _second_account(inbox.database, shop.tenant_id)
                session.add(
                    Conversation(
                        tenant_id=shop.tenant_id,
                        contact_id=first.contact_id,
                        account_id=account,
                        last_inbound_at=NOW - timedelta(minutes=index),
                    )
                )
            await session.commit()
        with statements(inbox.database) as seen:
            answer = await inbox.client.get(
                f"{CONTACTS}/{first.contact_id}", headers=shop.member.headers
            )
        assert answer.status_code == 200
        counts.append(len([s for s in seen if "FROM conversations" in s]))
    assert counts == [1, 1]


# ------------------------------------------------------------------- the name


async def test_a_member_names_a_contact_changes_the_name_and_takes_it_away(inbox: Inbox) -> None:
    """CD-T4, CD-T8."""
    conversation = await _nour(inbox)
    named = await _rename(inbox, conversation.contact_id, f"  {ARABIC}  ")
    assert named.status_code == 200, named.text
    assert named.json()["name"] == ARABIC, "trimmed, and otherwise byte for byte"
    assert named.json()["display_name"] == ARABIC
    assert named.json()["profile_name"] == "Nour Test"

    changed = await _rename(inbox, conversation.contact_id, "Nour (VIP) Test")
    assert changed.json()["display_name"] == "Nour (VIP) Test"

    cleared = await _rename(inbox, conversation.contact_id, None)
    assert cleared.json()["name"] is None
    assert cleared.json()["display_name"] == "Nour Test"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"name": "   "}, "contact_name_invalid"),
        ({"name": ""}, "contact_name_invalid"),
        ({"name": "a\x00b"}, "validation_error"),
        ({"name": "x" * 201}, "validation_error"),
        ({}, "validation_error"),
        ({"name": "Nour", "wa_id": "201000000009"}, "validation_error"),
        ({"name": "Nour", "identities": []}, "validation_error"),
        ({"name": "Nour", "display_name": "Other"}, "validation_error"),
    ],
)
async def test_a_name_that_cannot_be_kept_and_any_other_field_are_refused(
    inbox: Inbox, payload: dict[str, Any], code: str
) -> None:
    """CD-T4, CD-T11 (and EC5: the empty name has its own code)."""
    conversation = await _nour(inbox)
    answer = await inbox.client.patch(
        f"{CONTACTS}/{conversation.contact_id}", json=payload, headers=inbox.mine.member.headers
    )
    assert answer.status_code == 422, answer.text
    assert answer.json()["error"]["code"] == code
    [contact] = await _row(inbox, Contact, Contact.id == conversation.contact_id)
    assert contact.name is None and contact.wa_id == PHONE_A


async def test_the_same_name_again_writes_nothing(inbox: Inbox) -> None:
    """CD-T5: no UPDATE, no audit entry, no frame."""
    conversation = await _nour(inbox)
    assert (await _rename(inbox, conversation.contact_id, "Nour VIP")).status_code == 200
    with statements(inbox.database) as seen:
        again = await _rename(inbox, conversation.contact_id, "Nour VIP")
    assert again.status_code == 200 and again.json()["name"] == "Nour VIP"
    assert not [s for s in seen if s.lstrip().upper().startswith(("UPDATE", "INSERT"))]
    entries = await _row(inbox, AuditLog, AuditLog.target_id == conversation.contact_id)
    assert [entry.action for entry in entries] == [AuditAction.CONTACT_RENAMED]
    assert entries[0].meta == {"from": None, "to": "Nour VIP"}
    assert entries[0].actor_id == inbox.mine.member.user_id


async def test_a_rename_tells_every_conversation_of_the_contact_and_a_no_op_none(
    inbox: Inbox,
) -> None:
    """CD-T10: one `conversation.updated` naming `participant` per conversation."""
    shop = inbox.mine
    first = await _nour(inbox)
    async with inbox.database.session_factory() as session:
        session.add(
            Conversation(
                tenant_id=shop.tenant_id,
                contact_id=first.contact_id,
                account_id=shop.second_account_id,
            )
        )
        await session.commit()
    async with inbox.database.session_factory() as session:
        actor = await session.get(User, shop.member.user_id)
        assert actor is not None
        contacts = ContactService(session=session, tenant_id=shop.tenant_id)
        await contacts.rename(first.contact_id, name="Nour VIP", actor=actor)
        staged = list(session.info.get(events._PENDING, []))
        session.info.get(events._PENDING, []).clear()
        await contacts.rename(first.contact_id, name="Nour VIP", actor=actor)
        again = list(session.info.get(events._PENDING, []))
        await session.rollback()
    assert len(staged) == 2 and not again
    assert {hint.type for hint in staged} == {"conversation.updated"}
    assert all(hint.data["fields"] == ["participant"] for hint in staged)
    assert all(hint.tenant_id == shop.tenant_id for hint in staged)


async def test_the_webhook_keeps_the_channels_name_and_never_the_teams(
    db_session: AsyncSession,
) -> None:
    """CD-T7."""
    account = await _account(db_session, slug="named-contact")
    service = WhatsAppIngestionService(session=db_session)
    await service.ingest(_inbound(text="hello", message_id="wamid.name.1"))
    contact = await _contact(db_session, account)
    contact.name = "Our Nour"
    contact.display_name = "Old profile"
    await db_session.flush()

    await service.ingest(_inbound(text="hello again", message_id="wamid.name.2"))
    await db_session.flush()
    await db_session.refresh(contact)
    assert contact.name == "Our Nour"
    assert contact.display_name == "Nour", "the channel's name is kept current"


async def test_the_teams_name_is_the_one_a_conversation_shows(inbox: Inbox) -> None:
    """CD-T9: `participant.display_name` and `ContactRead.display_name` are one rule."""
    conversation = await _nour(inbox)
    headers = inbox.mine.member.headers
    before = (await inbox.client.get(CONVERSATIONS, headers=headers)).json()["items"][0]
    assert before["participant"]["display_name"] == "Nour Test"
    await _rename(inbox, conversation.contact_id, "Nour (VIP) Test")
    listed = (await inbox.client.get(CONVERSATIONS, headers=headers)).json()["items"][0]
    one = (await inbox.client.get(f"{CONVERSATIONS}/{conversation.id}", headers=headers)).json()
    picked = (await inbox.client.get(CONTACTS, headers=headers)).json()["items"][0]
    assert listed["participant"]["display_name"] == "Nour (VIP) Test"
    assert one["participant"]["display_name"] == "Nour (VIP) Test"
    assert picked["display_name"] == "Nour (VIP) Test" and picked["name"] == "Nour (VIP) Test"


async def test_both_searches_find_the_name_the_team_gave(inbox: Inbox) -> None:
    """CD-T17."""
    conversation = await _nour(inbox)
    await _rename(inbox, conversation.contact_id, "Zeytin Bakery")
    headers = inbox.mine.member.headers
    for path in (CONTACTS, CONVERSATIONS):
        for term in ("zeytin", "nour"):
            found = (await inbox.client.get(path, params={"q": term}, headers=headers)).json()
            assert len(found["items"]) == 1, (path, term)


# ----------------------------------------------------------- Turkish folding


TURKISH_TERMS = [
    ("ipek", IPEK),
    ("\u0130PEK", IPEK),
    ("IPEK", IPEK),
    ("i\u015f\u0131k", IPEK),
    ("isik", IPEK),
    ("ISIK", IPEK),
    ("istanbul", ISTANBUL),
    ("\u0130stanbul", ISTANBUL),
    ("\u0131stanbul", ISTANBUL),
    ("ilgin", ILGIN),
    ("ILGIN", ILGIN),
]


async def _turkish(desk: Inbox) -> None:
    shop = desk.mine
    for index, name in enumerate((IPEK, ILGIN, ISTANBUL)):
        conversation = await customer(
            desk.database,
            shop.tenant_id,
            shop.account_id,
            name=name,
            phone=f"20105550{index:04d}",
            last_message_at=NOW - timedelta(minutes=index),
        )
        async with desk.database.session_factory() as session:
            session.add(
                Lead(
                    tenant_id=shop.tenant_id,
                    contact_id=conversation.contact_id,
                    name=name,
                    status=LeadStatus.NEW,
                )
            )
            await session.commit()


async def test_every_search_folds_turkish_letters_on_both_sides(inbox: Inbox) -> None:
    """TF-T1: each term finds its row in the contacts, the leads and the inbox search."""
    await _turkish(inbox)
    headers = inbox.mine.member.headers
    missed = []
    for term, name in TURKISH_TERMS:
        contacts = await inbox.client.get(CONTACTS, params={"q": term}, headers=headers)
        leads = await inbox.client.get(LEADS, params={"search": term}, headers=headers)
        chats = await inbox.client.get(CONVERSATIONS, params={"q": term}, headers=headers)
        got = (
            [row["display_name"] for row in contacts.json()["items"]],
            [row["name"] for row in leads.json()["items"]],
            [row["participant"]["display_name"] for row in chats.json()["items"]],
        )
        if got != ([name], [name], [name]):
            missed.append((term, got))
    assert not missed


async def test_a_search_without_those_letters_matches_as_before(inbox: Inbox) -> None:
    """TF-T2, TF-T3: case-insensitive, `%` and `_` literal, the bounds unchanged."""
    shop = inbox.mine
    await customer(
        inbox.database, shop.tenant_id, shop.account_id, name="100% Omar_Said", phone="201055507004"
    )
    await customer(
        inbox.database, shop.tenant_id, shop.account_id, name="Mona Adel", phone="201055509999"
    )
    headers = shop.member.headers

    async def names(term: str) -> list[str]:
        answer = await inbox.client.get(CONVERSATIONS, params={"q": term}, headers=headers)
        assert answer.status_code == 200, answer.text
        return sorted(row["participant"]["display_name"] for row in answer.json()["items"])

    assert await names("OMAR") == ["100% Omar_Said"]
    assert await names("100%") == ["100% Omar_Said"]
    assert await names("r_s") == ["100% Omar_Said"]
    assert await names("%%") == []
    assert await names("__") == []
    assert await names("5550999") == ["Mona Adel"]
    short = await inbox.client.get(CONVERSATIONS, params={"q": "a"}, headers=headers)
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "search_term_invalid"


def test_the_folding_is_written_once() -> None:
    """TF-T5: one helper holds the expression; no search spells `translate` itself."""
    import pathlib

    root = pathlib.Path(repository_base.__file__).resolve().parents[1]
    users = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*.py")
        if "func.translate(" in path.read_text(encoding="utf-8")
    )
    assert users == ["repositories/base.py"]
    for module in ("contact_picker_repository", "conversation_repository", "lead_repository"):
        text = (root / "repositories" / f"{module}.py").read_text(encoding="utf-8")
        assert "contains_folded(" in text, module


# ---------------------------------------------------------- consent history


async def _events(desk: Inbox, contact_id: uuid.UUID, who: Any = None, **params: Any) -> Any:
    return await desk.client.get(
        f"{CONTACTS}/{contact_id}/consent-events",
        params=params,
        headers=(who or desk.mine.member).headers,
    )


async def test_recording_and_clearing_are_remembered_once_each_with_who_did_it(
    inbox: Inbox,
) -> None:
    """CD-T14, CD-T15."""
    shop = inbox.mine
    conversation = await _nour(inbox)
    path = f"{CONTACTS}/{conversation.contact_id}/opt-out"
    body = {"channel": "whatsapp"}

    assert (await _events(inbox, conversation.contact_id)).json() == {
        "items": [],
        "next_cursor": None,
    }
    for _ in range(2):
        recorded = await inbox.client.post(path, json=body, headers=shop.member.headers)
        assert recorded.status_code == 200, recorded.text
    refused = await inbox.client.delete(path, params=body, headers=shop.member.headers)
    assert refused.status_code == 403
    for _ in range(2):
        cleared = await inbox.client.delete(path, params=body, headers=shop.admin.headers)
        assert cleared.status_code == 200, cleared.text

    history = (await _events(inbox, conversation.contact_id)).json()
    assert history["next_cursor"] is None
    assert [
        (item["action"], item["source"], item["via"], item["by"]["user_id"])
        for item in history["items"]
    ] == [
        ("opt_out_cleared", "team", "team", str(shop.admin.user_id)),
        ("opted_out", "team", "team", str(shop.member.user_id)),
    ]
    assert all(
        item["channel"] == "whatsapp" and not item["before_history"] for item in history["items"]
    )
    read = await inbox.client.get(
        f"{CONTACTS}/{conversation.contact_id}", headers=shop.member.headers
    )
    assert read.json()["opted_out"] is False


async def test_a_customers_stop_word_is_remembered_as_the_customers(
    db_session: AsyncSession,
) -> None:
    """CD-T14: source customer, via message, no member."""
    account = await _account(db_session, slug="stop-history")
    service = WhatsAppIngestionService(session=db_session)
    await service.ingest(_inbound(text="STOP", message_id="wamid.h.1"))
    await service.ingest(_inbound(text="stop", message_id="wamid.h.2"))
    await db_session.flush()
    contact = await _contact(db_session, account)
    rows = (
        (
            await db_session.execute(
                select(ContactConsentEvent).where(ContactConsentEvent.contact_id == contact.id)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.action, row.source, row.via, row.actor_user_id) for row in rows] == [
        (ConsentAction.OPTED_OUT, OptOutSource.CUSTOMER, OptOutVia.MESSAGE, None)
    ]


async def test_the_history_pages_newest_first_and_keeps_a_departed_members_name(
    inbox: Inbox,
) -> None:
    """CD-T15."""
    shop = inbox.mine
    conversation = await _nour(inbox)
    path = f"{CONTACTS}/{conversation.contact_id}/opt-out"
    body = {"channel": "whatsapp"}
    async with inbox.database.session_factory() as session:
        admin = await session.get(User, shop.admin.user_id)
        assert admin is not None
        admin.full_name = "Mona Admin"
        await session.commit()
    for _ in range(2):
        await inbox.client.post(path, json=body, headers=shop.admin.headers)
        await inbox.client.delete(path, params=body, headers=shop.admin.headers)
    async with inbox.database.session_factory() as session:
        from app.db.models import Membership, MembershipStatus

        membership = (
            await session.execute(
                select(Membership).where(Membership.user_id == shop.admin.user_id)
            )
        ).scalar_one()
        membership.status = MembershipStatus.REVOKED
        await session.commit()

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(4):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = (await _events(inbox, conversation.contact_id, **params)).json()
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [item["action"] for item in seen] == [
        "opt_out_cleared",
        "opted_out",
        "opt_out_cleared",
        "opted_out",
    ]
    assert len({item["id"] for item in seen}) == 4
    assert [item["at"] for item in seen] == sorted((item["at"] for item in seen), reverse=True)
    assert {item["by"]["display_name"] for item in seen} == {"Mona Admin"}


async def test_an_opt_out_older_than_the_history_closes_the_list_and_is_never_stored(
    inbox: Inbox,
) -> None:
    """CD-T16."""
    shop = inbox.mine
    conversation = await _nour(inbox)
    async with inbox.database.session_factory() as session:
        session.add(
            ContactChannelConsent(
                tenant_id=shop.tenant_id,
                contact_id=conversation.contact_id,
                channel=Channel.WHATSAPP,
                marketing_opt_out_at=NOW - timedelta(days=30),
                opt_out_source=OptOutSource.CUSTOMER,
                opt_out_via=OptOutVia.MESSAGE,
            )
        )
        await session.commit()
    history = (await _events(inbox, conversation.contact_id)).json()
    assert history["items"] == [
        {
            "id": None,
            "channel": "whatsapp",
            "action": "opted_out",
            "source": None,
            "via": None,
            "by": None,
            "at": (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            "before_history": True,
        }
    ]
    assert not await _row(inbox, ContactConsentEvent)

    # Cleared now: the clear is remembered, and nothing stands to synthesize.
    await inbox.client.delete(
        f"{CONTACTS}/{conversation.contact_id}/opt-out",
        params={"channel": "whatsapp"},
        headers=shop.owner.headers,
    )
    after = (await _events(inbox, conversation.contact_id)).json()["items"]
    assert [(item["action"], item["before_history"]) for item in after] == [
        ("opt_out_cleared", False)
    ]


async def test_the_event_and_the_change_are_one_transaction(inbox: Inbox) -> None:
    """CD-T18: a failure after the change leaves neither."""
    shop = inbox.mine
    conversation = await _nour(inbox)
    async with inbox.database.session_factory() as session:
        changed = await opt_out.record_opt_out(
            session,
            tenant_id=shop.tenant_id,
            contact_id=conversation.contact_id,
            channel=Channel.WHATSAPP,
            source=OptOutSource.TEAM,
            via=OptOutVia.TEAM,
            at=NOW,
            actor_user_id=shop.member.user_id,
        )
        assert changed
        await session.flush()
        assert await session.scalar(select(func.count()).select_from(ContactConsentEvent)) == 1
        await session.rollback()
    assert not await _row(inbox, ContactConsentEvent)
    assert not await _row(inbox, ContactChannelConsent)


async def test_a_consent_event_cannot_name_another_workspaces_contact(inbox: Inbox) -> None:
    """CD-T20: the composite key."""
    from sqlalchemy.exc import IntegrityError

    conversation = await _nour(inbox)
    async with inbox.database.session_factory() as session:
        session.add(
            ContactConsentEvent(
                tenant_id=inbox.theirs.tenant_id,
                contact_id=conversation.contact_id,
                channel=Channel.WHATSAPP,
                action=ConsentAction.OPTED_OUT,
                source=OptOutSource.TEAM,
                via=OptOutVia.TEAM,
            )
        )
        with pytest.raises(IntegrityError, match="fk_contact_consent_events_tenant_contact"):
            await session.flush()
        await session.rollback()


# ------------------------------------------------- the contact's conversations


async def test_contact_id_lists_exactly_that_customers_conversations(inbox: Inbox) -> None:
    """CD-T11, CD-T13."""
    shop = inbox.mine
    headers = shop.member.headers
    nour = await _nour(inbox, assigned_to_id=shop.member.user_id)
    async with inbox.database.session_factory() as session:
        session.add(
            Conversation(
                tenant_id=shop.tenant_id,
                contact_id=nour.contact_id,
                account_id=shop.second_account_id,
                status=ConversationStatus.CLOSED,
                last_message_at=NOW - timedelta(minutes=5),
            )
        )
        await session.commit()
    await customer(
        inbox.database, shop.tenant_id, shop.account_id, name="Omar", phone="201055507002"
    )
    theirs = await customer(
        inbox.database,
        inbox.theirs.tenant_id,
        inbox.theirs.account_id,
        name="X",
        phone="201055507003",
    )

    async def ids(**params: Any) -> list[str]:
        answer = await inbox.client.get(
            CONVERSATIONS, params={"contact_id": str(nour.contact_id), **params}, headers=headers
        )
        assert answer.status_code == 200, answer.text
        return [row["id"] for row in answer.json()["items"]]

    assert await ids() == [str(nour.id)], "the default view still hides closed ones"
    assert len(await ids(status="all")) == 2
    assert await ids(status="all", assigned_to="me") == [str(nour.id)]
    assert await ids(status="closed", assigned_to="me") == []
    assert await ids(status="all", q="nour") and await ids(status="all", q="omar") == []

    for stranger in (theirs.contact_id, uuid.uuid4()):
        empty = await inbox.client.get(
            CONVERSATIONS, params={"contact_id": str(stranger), "status": "all"}, headers=headers
        )
        assert empty.status_code == 200
        assert empty.json()["items"] == [] and empty.json()["next_cursor"] is None
    malformed = await inbox.client.get(
        CONVERSATIONS, params={"contact_id": "nour"}, headers=headers
    )
    assert malformed.status_code == 422


async def test_a_cursor_of_one_contact_is_refused_on_another_and_the_default_is_untouched(
    inbox: Inbox,
) -> None:
    """CD-T12."""
    shop = inbox.mine
    headers = shop.member.headers
    nour = await _nour(inbox)
    async with inbox.database.session_factory() as session:
        for index in range(2):
            from tests.integration.test_inbox_contract import _second_account

            session.add(
                Conversation(
                    tenant_id=shop.tenant_id,
                    contact_id=nour.contact_id,
                    account_id=await _second_account(inbox.database, shop.tenant_id),
                    last_message_at=NOW - timedelta(minutes=index + 1),
                )
            )
        await session.commit()
    other = await customer(
        inbox.database, shop.tenant_id, shop.account_id, name="Omar", phone="201055507005"
    )

    first = (
        await inbox.client.get(
            CONVERSATIONS, params={"contact_id": str(nour.contact_id), "limit": 2}, headers=headers
        )
    ).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    rest = await inbox.client.get(
        CONVERSATIONS,
        params={"contact_id": str(nour.contact_id), "limit": 2, "cursor": first["next_cursor"]},
        headers=headers,
    )
    assert len(rest.json()["items"]) == 1 and rest.json()["next_cursor"] is None
    for params in (
        {"contact_id": str(other.contact_id)},
        {},
        {"contact_id": str(nour.contact_id), "status": "all"},
    ):
        refused = await inbox.client.get(
            CONVERSATIONS,
            params={**params, "limit": 2, "cursor": first["next_cursor"]},
            headers=headers,
        )
        assert refused.status_code == 422, params
        assert refused.json()["error"]["code"] == "cursor_view_mismatch"

    # The default view's cursor carries no tag, exactly as before.
    default = (await inbox.client.get(CONVERSATIONS, params={"limit": 2}, headers=headers)).json()
    assert default["next_cursor"] and "." not in default["next_cursor"]
