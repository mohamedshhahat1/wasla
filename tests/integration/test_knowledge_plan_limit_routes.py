"""A full plan refuses a new knowledge document at the routes, and never a replacement.

Since RQ-09 the document limit is no longer a route dependency: the upload
routes hand `KnowledgeService.submit` a `reserve_slot` that asks the entitlement
service for `LimitKey.KNOWLEDGE_DOCUMENTS`, and only once a new document would
really be created. `test_document_replacement.py` proves the service half with
an injected slot; these drive both halves through the real routes, the real
`KnowledgeService` and the real database, with only the entitlement service
replaced by one that refuses (RGM-01).

Both sides of the rule are pinned: a new document is refused with 402 on the
JSON and the multipart route, and a replacement of an existing document's
content (`PUT .../content` and `PUT .../content/upload`) is accepted on the same
full plan without asking for a slot (ADR-161, M-R15).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.db.models.billing import LimitKey
from app.db.models.knowledge import Document
from tests.integration.test_plan_enforcement import ExhaustedEntitlements
from tests.integration.test_tenant_isolation import (  # noqa: F401 - fixtures
    Workspace,
    http,
    isolation_app,
    victim,
)
from tests.knowledge_files import PRICE_ROWS, csv_text

pytestmark = pytest.mark.integration

API = "/api/v1/knowledge"


async def _documents(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(func.count(Document.id)).where(Document.tenant_id == tenant_id))
        or 0
    )


def _exhaust(application: FastAPI) -> ExhaustedEntitlements:
    stub = ExhaustedEntitlements()
    application.dependency_overrides[get_entitlement_service] = lambda: stub
    return stub


async def test_a_full_plan_refuses_a_new_document_on_the_json_route(
    http: AsyncClient,  # noqa: F811
    isolation_app: FastAPI,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    exhausted = _exhaust(isolation_app)
    before = await _documents(db_session, victim.tenant.id)

    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents",
        json={"title": "Prices", "content": "Everything costs money."},
        headers=victim.headers,
    )

    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "plan_limit_exceeded"
    assert exhausted.asked == [LimitKey.KNOWLEDGE_DOCUMENTS]
    assert await _documents(db_session, victim.tenant.id) == before


async def test_a_full_plan_refuses_a_new_document_on_the_multipart_route(
    http: AsyncClient,  # noqa: F811
    isolation_app: FastAPI,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    exhausted = _exhaust(isolation_app)
    before = await _documents(db_session, victim.tenant.id)

    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("prices.csv", csv_text(PRICE_ROWS).encode(), "text/csv")},
        data={"title": "Prices"},
        headers=victim.headers,
    )

    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "plan_limit_exceeded"
    assert exhausted.asked == [LimitKey.KNOWLEDGE_DOCUMENTS]
    assert await _documents(db_session, victim.tenant.id) == before


async def test_a_full_plan_still_accepts_a_replacement_on_both_routes(
    http: AsyncClient,  # noqa: F811
    isolation_app: FastAPI,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    # Created while the plan still had room (the fixture's allowing service).
    first = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("prices.csv", csv_text(PRICE_ROWS).encode(), "text/csv")},
        data={"title": "Prices"},
        headers=victim.headers,
    )
    assert first.status_code == 202, first.text
    document_id = first.json()["document"]["id"]
    exhausted = _exhaust(isolation_app)
    before = await _documents(db_session, victim.tenant.id)

    newer = [(s, p, "4,650 EGP" if s == "BK-35" else c) for s, p, c in PRICE_ROWS]
    multipart = await http.put(
        f"{API}/documents/{document_id}/content/upload",
        files={"file": ("prices.csv", csv_text(newer).encode(), "text/csv")},
        headers=victim.headers,
    )
    json_route = await http.put(
        f"{API}/documents/{document_id}/content",
        json={"content": "Premium finishing costs 7900 EGP per square metre."},
        headers=victim.headers,
    )

    assert multipart.status_code == 202, multipart.text
    assert json_route.status_code == 202, json_route.text
    for response in (multipart, json_route):
        assert response.json()["outcome"] == "replacement_started"
        assert response.json()["document"]["id"] == document_id
    assert exhausted.asked == []
    assert await _documents(db_session, victim.tenant.id) == before
