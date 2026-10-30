"""The multipart knowledge routes (RQ-22): the same pipeline, limits and answers as JSON.

The real application on the test's transaction (the tenant-isolation suite's
wiring): real routes, real `KnowledgeService`, real extraction - only the plan
entitlement is allowed. Indexing is not run here; what these prove is what a
request is answered with and what is recorded.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Document, DocumentSource
from app.services.document_formats import MAX_TEXT_UPLOAD_BYTES
from app.services.knowledge_limits import MAX_BINARY_DOCUMENT_BYTES, MAX_TABLE_ROWS
from tests.integration.test_tenant_isolation import (  # noqa: F401 - fixtures
    Workspace,
    attacker,
    http,
    isolation_app,
    victim,
)
from tests.knowledge_files import PRICE_ROWS, csv_text, docx_bytes, xlsx_bytes

pytestmark = pytest.mark.integration

API = "/api/v1/knowledge"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _documents(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(func.count(Document.id)).where(Document.tenant_id == tenant_id))
        or 0
    )


@pytest.mark.parametrize(
    ("filename", "data", "source"),
    [
        ("prices.csv", csv_text(PRICE_ROWS).encode(), DocumentSource.CSV),
        ("prices.xlsx", xlsx_bytes(), DocumentSource.XLSX),
        ("policy.docx", docx_bytes(paragraphs=["Delivery takes one day."]), DocumentSource.DOCX),
        ("policy.html", b"<h1>Policy</h1><p>Delivery takes one day.</p>", DocumentSource.HTML),
        ("notes.md", b"# Notes\n\nDelivery takes one day.", DocumentSource.MARKDOWN),
    ],
    ids=["csv", "xlsx", "docx", "html", "markdown"],
)
async def test_a_file_uploaded_as_multipart_is_recorded_like_the_json_route(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
    filename: str,
    data: bytes,
    source: DocumentSource,
) -> None:
    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": (filename, data, "application/octet-stream")},
        data={"title": f"{filename} upload"},
        headers=victim.headers,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True
    assert body["document"]["source"] == str(source)
    assert body["document"]["status"] == "pending"
    stored = await db_session.get(Document, uuid.UUID(body["document"]["id"]))
    assert stored is not None and stored.filename == filename
    assert stored.source is source and stored.content


async def test_the_json_route_still_takes_base64_and_answers_the_same(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
) -> None:
    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents",
        json={
            "title": "Prices",
            "source": "xlsx",
            "content": base64.b64encode(xlsx_bytes()).decode(),
            "filename": "prices.xlsx",
        },
        headers=victim.headers,
    )
    assert response.status_code == 202, response.text
    assert response.json()["document"]["source"] == "xlsx"


async def test_an_oversized_upload_is_refused_with_413_and_nothing_recorded(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    before = await _documents(db_session, victim.tenant.id)
    for filename, size in (
        ("big.xlsx", MAX_BINARY_DOCUMENT_BYTES + 1),
        ("big.csv", MAX_TEXT_UPLOAD_BYTES + 1),
    ):
        response = await http.post(
            f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
            files={"file": (filename, b"PK\x03\x04" + b"a" * size, XLSX)},
            data={"title": "Too big"},
            headers=victim.headers,
        )
        assert response.status_code == 413, (filename, response.text)
        assert response.json()["error"]["code"] == "document_too_large"
    assert await _documents(db_session, victim.tenant.id) == before


async def test_a_file_past_a_row_limit_is_refused_with_its_stable_error(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
) -> None:
    rows = [(f"SKU-{n}", f"Item {n}", f"{n} EGP") for n in range(MAX_TABLE_ROWS + 1)]
    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("long.csv", csv_text(rows).encode(), "text/csv")},
        data={"title": "Long"},
        headers=victim.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_limit_exceeded"
    assert str(MAX_TABLE_ROWS) in response.json()["error"]["message"]


async def test_a_file_that_is_not_its_declared_format_is_refused(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
) -> None:
    response = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("prices.xlsx", docx_bytes(paragraphs=["x"]), XLSX)},
        data={"title": "Not a workbook", "source": "xlsx"},
        headers=victim.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_format_mismatch"


async def test_a_multipart_replacement_starts_a_new_version_of_the_same_document(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    first = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("prices.csv", csv_text(PRICE_ROWS).encode(), "text/csv")},
        data={"title": "Prices"},
        headers=victim.headers,
    )
    document_id = first.json()["document"]["id"]
    newer = [(s, p, "4,650 EGP" if s == "BK-35" else c) for s, p, c in PRICE_ROWS]
    response = await http.put(
        f"{API}/documents/{document_id}/content/upload",
        files={"file": ("prices.csv", csv_text(newer).encode(), "text/csv")},
        headers=victim.headers,
    )
    assert response.status_code == 202, response.text
    assert response.json()["outcome"] == "replacement_started"
    assert response.json()["document"]["id"] == document_id
    assert await _documents(db_session, victim.tenant.id) == 2  # the seeded one + this one


async def test_the_multipart_routes_answer_not_found_for_another_workspace(
    http: AsyncClient,  # noqa: F811
    victim: Workspace,  # noqa: F811
    attacker: Workspace,  # noqa: F811
    db_session: AsyncSession,
) -> None:
    before = await _documents(db_session, victim.tenant.id)
    upload = await http.post(
        f"{API}/bases/{victim.knowledge_base.id}/documents/upload",
        files={"file": ("prices.csv", csv_text(PRICE_ROWS).encode(), "text/csv")},
        data={"title": "Planted"},
        headers=attacker.headers,
    )
    replace = await http.put(
        f"{API}/documents/{victim.document.id}/content/upload",
        files={"file": ("prices.csv", csv_text(PRICE_ROWS).encode(), "text/csv")},
        headers=attacker.headers,
    )
    assert (upload.status_code, replace.status_code) == (404, 404)
    assert "SECRET-VICTIM" not in upload.text + replace.text
    assert await _documents(db_session, victim.tenant.id) == before
