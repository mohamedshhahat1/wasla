"""What the lead screens read from the API beyond FE-4's contract (FE-5; LG, EC5).

- `next_statuses`: the moves the pipeline allows from where a lead stands, read
  from the one graph the service judges a move by - a screen offers these and
  never copies the graph;
- two refusals a screen must tell apart get a code of their own, and nothing
  else about them changes: a move the graph forbids, and an assignee who is not
  an active member of the workspace.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models.lead import Lead, LeadStatus
from tests.integration.test_inbox_contract import Inbox, inbox

pytestmark = pytest.mark.integration

__all__ = ["inbox"]

LEADS = "/api/v1/leads"


async def _row(desk: Inbox, model: Any, *where: Any) -> Any:
    async with desk.database.session_factory() as session:
        return (await session.execute(select(model).where(*where))).scalars().all()


async def _lead(desk: Inbox, status: LeadStatus = LeadStatus.NEW) -> uuid.UUID:
    async with desk.database.session_factory() as session:
        lead = Lead(tenant_id=desk.mine.tenant_id, name="Lead Test", status=status)
        session.add(lead)
        await session.commit()
        return lead.id


async def test_next_statuses_is_the_graphs_moves_in_pipeline_order(inbox: Inbox) -> None:
    """LG-T1 .. LG-T3: read from the graph itself, on every answer that returns a lead."""
    from app.db.models.lead import ALLOWED_TRANSITIONS

    headers = inbox.mine.owner.headers
    order = [status.value for status in LeadStatus]
    for status in LeadStatus:
        lead_id = await _lead(inbox, status)
        body = (await inbox.client.get(f"{LEADS}/{lead_id}", headers=headers)).json()
        expected = sorted((move.value for move in ALLOWED_TRANSITIONS[status]), key=order.index)
        assert body["next_statuses"] == expected, status
        assert status.value not in body["next_statuses"]
    assert (await inbox.client.get(LEADS, headers=headers)).json()["items"][0][
        "next_statuses"
    ] is not None

    lead_id = await _lead(inbox)
    patched = await inbox.client.patch(
        f"{LEADS}/{lead_id}", json={"interest": "kitchen"}, headers=headers
    )
    scored = await inbox.client.post(
        f"{LEADS}/{lead_id}/score", json={"score": 40}, headers=headers
    )
    assigned = await inbox.client.post(
        f"{LEADS}/{lead_id}/assignment",
        json={"assigned_to_id": str(inbox.mine.member.user_id), "expected_assigned_to_id": None},
        headers=headers,
    )
    for answer in (patched, scored, assigned):
        assert answer.status_code == 200, answer.text
        assert answer.json()["next_statuses"] == ["contacted", "qualified", "lost"]
    moved = await inbox.client.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": "proposal", "expected_status": "new"},
        headers=headers,
    )
    assert moved.status_code == 422
    moved = await inbox.client.post(
        f"{LEADS}/{lead_id}/status",
        json={"status": "qualified", "expected_status": "new"},
        headers=headers,
    )
    assert moved.json()["next_statuses"] == ["proposal", "won", "lost"]
    won = await inbox.client.post(
        f"{LEADS}/{lead_id}/status", json={"status": "won"}, headers=headers
    )
    assert won.json()["next_statuses"] == []


async def test_a_forbidden_move_and_an_assignee_who_is_not_a_member_have_their_own_codes(
    inbox: Inbox,
) -> None:
    """EC5-02, EC5-03: the code only - the status and the sentence are what they were."""
    headers = inbox.mine.owner.headers
    lead_id = await _lead(inbox, LeadStatus.QUALIFIED)
    forbidden = await inbox.client.post(
        f"{LEADS}/{lead_id}/status", json={"status": "new"}, headers=headers
    )
    assert forbidden.status_code == 422
    error = forbidden.json()["error"]
    assert error["code"] == "lead_status_transition_not_allowed"
    assert error["message"] == "A lead cannot move from qualified to new."

    missing = await inbox.client.get(f"{LEADS}/{uuid.uuid4()}", headers=headers)
    for stranger in (inbox.theirs.member.user_id, uuid.uuid4()):
        refused = await inbox.client.post(
            f"{LEADS}/{lead_id}/assignment",
            json={"assigned_to_id": str(stranger), "expected_assigned_to_id": None},
            headers=headers,
        )
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "assignee_not_active_member"
        assert refused.json()["error"]["message"] == missing.json()["error"]["message"]
        assert set(refused.json()["error"]) == set(missing.json()["error"])
    [lead] = await _row(inbox, Lead, Lead.id == lead_id)
    assert lead.assigned_to_id is None and lead.status is LeadStatus.QUALIFIED
