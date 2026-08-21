"""Invitation issuing, against a recording session rather than a database.

The token is the whole point of an invitation: it is handed to the invitee once
and never stored, and only its hash goes in the row. This exercises that
contract without PostgreSQL, because the property it guards - that what the
caller receives is the token and what is persisted is its digest - is pure
service logic.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import PermissionDeniedError
from app.core.security import hash_invitation_token
from app.db.models import InvitationStatus, TenantRole, User
from app.services.invitation_service import InvitationService

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
INVITER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class RecordingSession:
    """Answers every read with nothing found, and records what was staged."""

    def __init__(self):
        self.added = []

    async def execute(self, statement):
        return _FakeResult([])

    def add(self, entity):
        self.added.append(entity)

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.fixture
def inviter() -> User:
    return User(id=INVITER_ID, email="owner@example.com", is_active=True)


async def _issue(session, inviter, *, role=TenantRole.MEMBER, inviter_role=TenantRole.TENANT_OWNER):
    return await InvitationService(session=session).issue(
        tenant_id=TENANT_ID,
        inviter=inviter,
        inviter_role=inviter_role,
        email="invited@example.com",
        role=role,
    )


async def test_the_returned_token_is_the_one_that_opens_the_invitation(inviter):
    session = RecordingSession()

    invitation, raw_token = await _issue(session, inviter)

    # The regression this exists for: the generator returns a (token, hash)
    # pair, and treating that pair as the token stored an unusable digest and
    # handed the invitee a tuple.
    assert isinstance(raw_token, str)
    assert invitation.token_hash == hash_invitation_token(raw_token)


async def test_the_raw_token_is_never_persisted(inviter):
    session = RecordingSession()

    invitation, raw_token = await _issue(session, inviter)

    assert invitation.token_hash != raw_token
    assert raw_token not in invitation.token_hash


async def test_the_invitation_is_staged_pending_and_scoped_to_the_workspace(inviter):
    session = RecordingSession()

    invitation, _ = await _issue(session, inviter)

    assert session.added == [invitation]
    assert invitation.tenant_id == TENANT_ID
    assert invitation.status is InvitationStatus.PENDING
    assert invitation.email == "invited@example.com"
    assert invitation.invited_by_id == INVITER_ID


async def test_two_invitations_never_share_a_token(inviter):
    _, first = await _issue(RecordingSession(), inviter)
    _, second = await _issue(RecordingSession(), inviter)

    assert first != second


async def test_an_admin_cannot_invite_an_owner(inviter):
    with pytest.raises(PermissionDeniedError):
        await _issue(
            RecordingSession(),
            inviter,
            role=TenantRole.TENANT_OWNER,
            inviter_role=TenantRole.TENANT_ADMIN,
        )
