"""The knowledge base endpoints.

The workspace dependency is overridden, but the real role guard runs: a member
being refused an upload is asserted against the actual wiring, not a mock of it.

Reading is open to any member because the people staffing an inbox need to see
what their agents can answer from. Writing is administrators only, because a
document added here is something the AI will state to customers as fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_knowledge_service,
)
from app.core.exceptions import TenantIsolationError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.knowledge import Document, DocumentSource, DocumentStatus, KnowledgeBase

pytestmark = pytest.mark.integration

PATH = "/api/v1/knowledge"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
BASE_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
DOCUMENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
MOMENT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

DOCUMENT_BODY = {
    "title": "Finishing prices",
    "content": "Economy finishing costs 4500 EGP per square metre.",
}


def _base() -> KnowledgeBase:
    return KnowledgeBase(
        id=BASE_ID,
        tenant_id=TENANT_ID,
        name="General",
        description="Company documents.",
        created_at=MOMENT,
        updated_at=MOMENT,
    )


def _document(**overrides: Any) -> Document:
    values = {
        "id": DOCUMENT_ID,
        "tenant_id": TENANT_ID,
        "knowledge_base_id": BASE_ID,
        "title": "Finishing prices",
        "source": DocumentSource.TEXT,
        "status": DocumentStatus.PENDING,
        "content_hash": "a" * 64,
        "filename": None,
        "media_type": None,
        "byte_size": 51,
        "content": "Economy finishing costs 4500 EGP per square metre.",
        "chunk_count": 0,
        "error": None,
        "ingested_at": None,
        "created_at": MOMENT,
        "updated_at": MOMENT,
    }
    values.update(overrides)
    return Document(**values)


class StubKnowledge:
    """Records what it was asked to do and returns canned rows."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.reingested: list[uuid.UUID] = []
        self.deleted: list[uuid.UUID] = []
        self.created_bases: list[dict[str, Any]] = []
        self.missing = False
        self.document = _document()
        self.created = True

    async def list_knowledge_bases(self, *, limit: int = 50) -> list[Any]:
        return [_base()]

    async def get_knowledge_base(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase:
        if self.missing:
            raise TenantIsolationError()
        return _base()

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
    ) -> KnowledgeBase:
        self.created_bases.append({"name": name, "description": description})
        return _base()

    async def list_documents(
        self,
        *,
        knowledge_base_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[Any]:
        if self.missing:
            raise TenantIsolationError()
        return [self.document]

    async def get_document(self, document_id: uuid.UUID) -> Document:
        if self.missing:
            raise TenantIsolationError()
        return self.document

    async def submit(self, **kwargs: Any) -> tuple[Any, ...]:
        self.submitted.append(kwargs)
        return self.document, self.created

    async def reingest(self, document_id: uuid.UUID) -> Document:
        self.reingested.append(document_id)
        return _document(status=DocumentStatus.PENDING)

    async def delete_document(self, document_id: uuid.UUID) -> None:
        if self.missing:
            raise TenantIsolationError()
        self.deleted.append(document_id)


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
        ),
        tenant=Tenant(
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


@pytest.fixture
def knowledge(app: FastAPI) -> StubKnowledge:
    stub = StubKnowledge()
    app.dependency_overrides[get_knowledge_service] = lambda: stub
    return stub


def _as(app: FastAPI, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


async def test_a_member_can_list_knowledge_bases(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/bases")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "General"


async def test_an_admin_can_create_a_knowledge_base(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/bases", json={"name": "Products"})

    assert response.status_code == 201
    assert knowledge.created_bases[0]["name"] == "Products"


async def test_a_member_cannot_create_a_knowledge_base(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/bases", json={"name": "Products"})

    assert response.status_code == 403
    assert knowledge.created_bases == []


async def test_an_admin_can_submit_a_document(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/bases/{BASE_ID}/documents", json=DOCUMENT_BODY)

    # 202, not 201: the document exists but is not yet retrievable.
    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    assert body["document"]["status"] == "pending"
    assert knowledge.submitted[0]["title"] == "Finishing prices"


async def test_a_member_cannot_submit_a_document(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    """A document here is something the AI will state to customers as fact."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/bases/{BASE_ID}/documents", json=DOCUMENT_BODY)

    assert response.status_code == 403
    assert knowledge.submitted == []


async def test_a_repeat_submission_reports_created_false(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    """Not an error: the upload was recognised, not duplicated."""
    _as(app, TenantRole.TENANT_ADMIN)
    knowledge.created = False

    response = await client.post(f"{PATH}/bases/{BASE_ID}/documents", json=DOCUMENT_BODY)

    assert response.status_code == 202
    assert response.json()["created"] is False


async def test_an_unknown_field_is_rejected_rather_than_ignored(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        f"{PATH}/bases/{BASE_ID}/documents",
        json={**DOCUMENT_BODY, "embedding_model": "mine"},
    )

    assert response.status_code == 422
    assert knowledge.submitted == []


async def test_an_empty_document_is_refused(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        f"{PATH}/bases/{BASE_ID}/documents",
        json={"title": "Empty", "content": ""},
    )

    assert response.status_code == 422


async def test_a_member_can_read_a_documents_ingestion_state(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)
    knowledge.document = _document(
        status=DocumentStatus.FAILED,
        error="The AI provider is unavailable.",
    )

    body = (await client.get(f"{PATH}/documents/{DOCUMENT_ID}")).json()

    assert body["status"] == "failed"
    assert body["error"] == "The AI provider is unavailable."


async def test_a_ready_document_reports_its_chunk_count(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)
    knowledge.document = _document(
        status=DocumentStatus.READY,
        chunk_count=7,
        ingested_at=MOMENT,
    )

    body = (await client.get(f"{PATH}/documents/{DOCUMENT_ID}")).json()

    assert body["status"] == "ready"
    assert body["chunk_count"] == 7
    assert body["ingested_at"] is not None


async def test_the_document_read_does_not_return_the_extracted_text(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    """The API reports what was ingested; it is not a document store."""
    _as(app, TenantRole.MEMBER)

    body = (await client.get(f"{PATH}/documents/{DOCUMENT_ID}")).json()

    assert "content" not in body


async def test_an_admin_can_queue_a_reingest(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/documents/{DOCUMENT_ID}/ingest")

    assert response.status_code == 202
    assert knowledge.reingested == [DOCUMENT_ID]


async def test_a_member_cannot_queue_a_reingest(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/documents/{DOCUMENT_ID}/ingest")

    assert response.status_code == 403
    assert knowledge.reingested == []


async def test_an_admin_can_delete_a_document(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.delete(f"{PATH}/documents/{DOCUMENT_ID}")

    assert response.status_code == 204
    assert knowledge.deleted == [DOCUMENT_ID]


async def test_a_member_cannot_delete_a_document(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.delete(f"{PATH}/documents/{DOCUMENT_ID}")

    assert response.status_code == 403
    assert knowledge.deleted == []


async def test_another_workspaces_document_is_not_found(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)
    knowledge.missing = True

    response = await client.get(f"{PATH}/documents/{DOCUMENT_ID}")

    assert response.status_code == 404
    assert "not found" in response.json()["error"]["message"].lower()


async def test_a_malformed_document_id_is_rejected(
    client: AsyncClient, app: FastAPI, knowledge: StubKnowledge
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/documents/not-a-uuid")

    assert response.status_code == 422


async def test_knowledge_routes_require_authentication(client: AsyncClient) -> None:
    """No workspace override here: the real dependency chain runs."""
    response = await client.get(f"{PATH}/bases")

    assert response.status_code == 401
