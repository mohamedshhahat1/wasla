"""Permanent checks for the audit's Graph and pagination survivors."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.dependencies import get_lead_service
from app.api.v1.leads import router as leads_router
from app.core.pagination import Page
from app.integrations.whatsapp.ownership import MetaOwnershipVerifier, NumberOwnershipError


async def test_graph_redirect_never_receives_the_bearer_at_a_second_host() -> None:
    """S11: the client defaults to following redirects, so the request must opt out."""
    sent: list[httpx.Request] = []
    sentinel = "synthetic-graph-bearer-sentinel"

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(307, headers={"location": "https://attacker.example/steal"})
        return httpx.Response(200, json={"id": "123"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), follow_redirects=True
    ) as client:
        verifier = MetaOwnershipVerifier(http=client)
        with pytest.raises(NumberOwnershipError):
            await verifier.verify(access_token=sentinel, phone_number_id="123")

    assert len(sent) == 1
    assert sent[0].url.host == "graph.facebook.com"
    assert sent[0].headers["Authorization"] == f"Bearer {sentinel}"


class _EmptyLeads:
    def __init__(self) -> None:
        self.limits: list[int] = []

    async def list_leads(self, *, limit: int, **_: Any) -> Page[Any]:
        self.limits.append(limit)
        return Page(items=[], next_cursor=None)


@pytest.mark.parametrize(("limit", "expected"), [(100, 200), (101, 422), (1000000, 422)])
async def test_lead_page_limit_is_enforced_at_the_http_boundary(limit: int, expected: int) -> None:
    """S19: one over the declared maximum never reaches the service."""
    app = FastAPI()
    app.include_router(leads_router, prefix="/api/v1")
    leads = _EmptyLeads()
    app.dependency_overrides[get_lead_service] = lambda: leads

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wasla.test"
    ) as client:
        response = await client.get("/api/v1/leads", params={"limit": limit})

    assert response.status_code == expected
    assert leads.limits == ([limit] if expected == 200 else [])
