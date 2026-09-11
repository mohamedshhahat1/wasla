"""PostgreSQL refuses a child whose parent belongs to another workspace.

Every tenant-owned table carries `tenant_id`, and until migration 0053 every
foreign key between two of them referenced the parent's `id` alone. The audit
proved the consequence with a direct `INSERT`: a conversation in workspace A
holding workspace B's contact was accepted by the database (AUTHZ-04).

No API path builds such a row, and the audit's sweep of ten relational
invariants found none except the one it inserted by hand - so this was never a
reachable defect. It was the difference between "no code path does this" and
"this cannot be": the first has to be re-established by every reviewer of every
future writer, the second re-establishes itself.

**Every statement here is raw SQL, and that is the whole design.** Going through
a repository would test the application's tenant scoping, which is already
proven exhaustively elsewhere and is exactly the layer this is meant to do
without. The question is what the *database* accepts when the application is not
involved, so the only honest way to ask it is to bypass the application.

Each refusal is paired with a same-tenant insert of the same shape. A constraint
that rejected everything would pass the negative half alone and break ingestion.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class _Workspace:
    """Ids for one workspace's rows, seeded straight into the tables."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.tenant_id = uuid.uuid4()
        self.contact_id = uuid.uuid4()
        self.account_id = uuid.uuid4()
        # A second contact and number in the same workspace. `conversations`
        # carries `UNIQUE (tenant_id, contact_id, account_id)`, so a probe that
        # reused the seeded pair would be refused by *that* - and a test whose
        # negative case fails on the wrong constraint proves nothing.
        self.spare_contact_id = uuid.uuid4()
        self.spare_account_id = uuid.uuid4()
        self.conversation_id = uuid.uuid4()
        self.knowledge_base_id = uuid.uuid4()
        self.document_id = uuid.uuid4()


async def _seed(session: AsyncSession, workspace: _Workspace) -> None:
    """One complete, internally consistent workspace, written by hand."""
    await session.execute(
        text(
            "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
            "VALUES (:id, :name, :slug, 'active', now(), now())"
        ),
        {"id": workspace.tenant_id, "name": workspace.label, "slug": workspace.label},
    )
    await session.execute(
        text(
            "INSERT INTO contacts (id, tenant_id, wa_id, created_at, updated_at) "
            "VALUES (:id, :tenant_id, :wa_id, now(), now())"
        ),
        {
            "id": workspace.contact_id,
            "tenant_id": workspace.tenant_id,
            "wa_id": f"2010000{workspace.label}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO whatsapp_accounts "
            "(id, tenant_id, phone_number_id, waba_id, display_phone_number, status, "
            " created_at, updated_at) "
            "VALUES (:id, :tenant_id, :phone, :waba, :display, 'active', now(), now())"
        ),
        {
            "id": workspace.account_id,
            "tenant_id": workspace.tenant_id,
            "phone": f"phone-{workspace.label}",
            "waba": f"waba-{workspace.label}",
            "display": f"+2010000{workspace.label}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO contacts (id, tenant_id, wa_id, created_at, updated_at) "
            "VALUES (:id, :tenant_id, :wa_id, now(), now())"
        ),
        {
            "id": workspace.spare_contact_id,
            "tenant_id": workspace.tenant_id,
            "wa_id": f"2019999{workspace.label}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO whatsapp_accounts "
            "(id, tenant_id, phone_number_id, waba_id, display_phone_number, status, "
            " created_at, updated_at) "
            "VALUES (:id, :tenant_id, :phone, :waba, :display, 'active', now(), now())"
        ),
        {
            "id": workspace.spare_account_id,
            "tenant_id": workspace.tenant_id,
            "phone": f"spare-phone-{workspace.label}",
            "waba": f"spare-waba-{workspace.label}",
            "display": f"+2019999{workspace.label}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO conversations "
            "(id, tenant_id, contact_id, account_id, status, mode, priority, "
            " created_at, updated_at) "
            "VALUES (:id, :tenant_id, :contact_id, :account_id, 'open', 'ai', 'normal', "
            " now(), now())"
        ),
        {
            "id": workspace.conversation_id,
            "tenant_id": workspace.tenant_id,
            "contact_id": workspace.contact_id,
            "account_id": workspace.account_id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO knowledge_bases (id, tenant_id, name, created_at, updated_at) "
            "VALUES (:id, :tenant_id, :name, now(), now())"
        ),
        {
            "id": workspace.knowledge_base_id,
            "tenant_id": workspace.tenant_id,
            "name": f"kb-{workspace.label}",
        },
    )
    await session.execute(
        text(
            "INSERT INTO documents "
            "(id, tenant_id, knowledge_base_id, title, source, status, content_hash, "
            " byte_size, chunk_count, created_at, updated_at) "
            "VALUES (:id, :tenant_id, :kb_id, :title, 'text', 'pending', :digest, "
            " 0, 0, now(), now())"
        ),
        {
            "id": workspace.document_id,
            "tenant_id": workspace.tenant_id,
            "kb_id": workspace.knowledge_base_id,
            "title": f"doc-{workspace.label}",
            "digest": uuid.uuid4().hex,
        },
    )
    await session.flush()


@pytest_asyncio.fixture
async def workspaces(db_session: AsyncSession) -> AsyncIterator[tuple[_Workspace, _Workspace]]:
    """Two workspaces, each internally consistent, sharing nothing."""
    alpha, beta = _Workspace("alpha"), _Workspace("beta")
    await _seed(db_session, alpha)
    await _seed(db_session, beta)
    yield alpha, beta


# The statement to run, and which field of `_Workspace` supplies the parent id
# it is testing. The child row always belongs to alpha; the two tests below
# differ only in whether that parent id is taken from alpha or from beta.
RELATIONS = {
    "conversations -> contacts": (
        "INSERT INTO conversations "
        "(id, tenant_id, contact_id, account_id, status, mode, priority, "
        " created_at, updated_at) "
        "VALUES (:id, :tenant_id, :parent_id, :spare_account_id, 'open', 'ai', 'normal', "
        " now(), now())",
        "contact_id",
    ),
    "conversations -> whatsapp_accounts": (
        "INSERT INTO conversations "
        "(id, tenant_id, contact_id, account_id, status, mode, priority, "
        " created_at, updated_at) "
        "VALUES (:id, :tenant_id, :spare_contact_id, :parent_id, 'open', 'ai', 'normal', "
        " now(), now())",
        "account_id",
    ),
    "messages -> conversations": (
        "INSERT INTO messages "
        "(id, tenant_id, conversation_id, direction, kind, status, created_at, updated_at) "
        "VALUES (:id, :tenant_id, :parent_id, 'inbound', 'text', 'pending', now(), now())",
        "conversation_id",
    ),
    "documents -> knowledge_bases": (
        "INSERT INTO documents "
        "(id, tenant_id, knowledge_base_id, title, source, status, content_hash, "
        " byte_size, chunk_count, created_at, updated_at) "
        "VALUES (:id, :tenant_id, :parent_id, 'probe', 'text', 'pending', :digest, "
        " 0, 0, now(), now())",
        "knowledge_base_id",
    ),
    "document_chunks -> documents": (
        "INSERT INTO document_chunks "
        "(id, tenant_id, document_id, knowledge_base_id, ordinal, content, "
        " token_estimate, created_at, updated_at) "
        "VALUES (:id, :tenant_id, :parent_id, :knowledge_base_id, 0, 'probe', 1, "
        " now(), now())",
        "document_id",
    ),
    "document_chunks -> knowledge_bases": (
        "INSERT INTO document_chunks "
        "(id, tenant_id, document_id, knowledge_base_id, ordinal, content, "
        " token_estimate, created_at, updated_at) "
        "VALUES (:id, :tenant_id, :document_id, :parent_id, 1, 'probe', 1, "
        " now(), now())",
        "knowledge_base_id",
    ),
}


def _arguments(child: _Workspace, parent: _Workspace, parent_field: str) -> dict[str, object]:
    """Every id the statements might bind, with the parent under test switched."""
    return {
        "id": uuid.uuid4(),
        "tenant_id": child.tenant_id,
        "parent_id": getattr(parent, parent_field),
        "contact_id": child.contact_id,
        "account_id": child.account_id,
        "spare_contact_id": child.spare_contact_id,
        "spare_account_id": child.spare_account_id,
        "conversation_id": child.conversation_id,
        "knowledge_base_id": child.knowledge_base_id,
        "document_id": child.document_id,
        "digest": uuid.uuid4().hex,
    }


@pytest.mark.parametrize("relation", sorted(RELATIONS))
async def test_the_database_refuses_a_parent_from_another_workspace(
    db_session: AsyncSession,
    workspaces: tuple[_Workspace, _Workspace],
    relation: str,
) -> None:
    """The negative control: alpha's child, beta's parent, refused by PostgreSQL.

    A savepoint because the failure aborts the surrounding transaction and the
    fixture's rollback would otherwise be the only thing left to do with it.
    """
    alpha, beta = workspaces
    statement, parent_field = RELATIONS[relation]

    with pytest.raises(IntegrityError) as refusal:
        async with db_session.begin_nested():
            await db_session.execute(text(statement), _arguments(alpha, beta, parent_field))

    # Named so a *different* constraint failing - a not-null, a unique - cannot
    # be mistaken for this one holding.
    assert "foreign key" in str(refusal.value).lower(), relation


@pytest.mark.parametrize("relation", sorted(RELATIONS))
async def test_the_database_accepts_a_parent_from_the_same_workspace(
    db_session: AsyncSession,
    workspaces: tuple[_Workspace, _Workspace],
    relation: str,
) -> None:
    """The positive control, and the reason it is not optional.

    A constraint that refused every insert would satisfy every assertion above
    while making ingestion impossible. This is the same statement with the same
    shape, differing only in whose parent it names.
    """
    alpha, _ = workspaces
    statement, parent_field = RELATIONS[relation]

    async with db_session.begin_nested():
        await db_session.execute(text(statement), _arguments(alpha, alpha, parent_field))


async def test_no_cross_tenant_rows_exist_in_the_seeded_schema(
    db_session: AsyncSession,
    workspaces: tuple[_Workspace, _Workspace],
) -> None:
    """The sweep the migration runs, run again as a test.

    Migration 0053 refuses to apply while any of these counts is non-zero, and
    that check is the kind of code nobody executes until the night it matters.
    This runs the same six queries against a populated schema so the query text
    itself is known to work.
    """
    pairs = [
        ("conversations", "contact_id", "contacts"),
        ("conversations", "account_id", "whatsapp_accounts"),
        ("messages", "conversation_id", "conversations"),
        ("documents", "knowledge_base_id", "knowledge_bases"),
        ("document_chunks", "document_id", "documents"),
        ("document_chunks", "knowledge_base_id", "knowledge_bases"),
    ]
    for child, column, parent in pairs:
        crossed = await db_session.scalar(
            text(
                f"SELECT count(*) FROM {child} c "  # noqa: S608 - fixed table names
                f"JOIN {parent} p ON p.id = c.{column} "
                f"WHERE c.tenant_id <> p.tenant_id"
            )
        )
        assert crossed == 0, f"{child}.{column} -> {parent}"
