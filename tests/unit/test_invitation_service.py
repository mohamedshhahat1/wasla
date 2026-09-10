"""Invitation issuing, against a recording session rather than a database.

The token is the whole point of an invitation: it is handed to the invitee once
and never stored, and only its hash goes in the row. This exercises that
contract without PostgreSQL, because the property it guards - that what the
caller receives is the token and what is persisted is its digest - is pure
service logic.

**Every test here builds its own `Settings` and passes them in.**
`InvitationService` defaults that argument to `get_settings()`, which reads
`.env` from the working directory, so until this change the module's outcome
depended on what the developer running it happened to have in a file (AUTH-03).
With `EMAIL_ENABLED=true` in a local `.env`, four of the five tests failed -
the outbox write becomes reachable and a recording session is not a database -
and the same four passed on a machine without one. A unit test that answers
differently depending on the host is not a test of the code.

The fix is explicit settings rather than a suite-wide pin, and the difference
matters: pinning `EMAIL_ENABLED=false` across the suite would have made the
email path unreachable everywhere, and the security-relevant claim about
invitations is precisely that the token's only destination is the invited
mailbox. So the default here is email *off* - which is what the token-hashing
tests are about - and `test_the_token_is_queued_only_to_the_invited_address`
turns it on deliberately and checks where the token goes.

`test_ambient_email_configuration_cannot_change_the_outcome` is the regression
for the finding itself: it sets `EMAIL_ENABLED` in the environment to each of
four values and asserts this module answers the same way regardless.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable
from sqlalchemy.sql.expression import Insert

from app.core.config import Settings
from app.core.exceptions import PermissionDeniedError
from app.core.security import hash_invitation_token
from app.db.models import InvitationStatus, TenantRole, User
from app.services.email_service import SEALED_CONTEXT_KEY, open_email_context
from app.services.email_templates import EmailTemplate
from app.services.invitation_service import InvitationService
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY, as_session

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
INVITER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _FakeScalars:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        # What the outbox insert reads off its RETURNING. Present so the email
        # path can be exercised on purpose, rather than failing on a missing
        # method the moment an ambient setting makes it reachable by accident.
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None

    def all(self) -> Sequence[Any]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class RecordingSession:
    """Answers every read with nothing found, and records what was staged.

    The outbox inserts through `execute` rather than `add`, so writes issued as
    statements are kept as well: without them, "the token went to the invited
    mailbox and nowhere else" would have nothing to be asserted against. Reads
    are ignored - the service issues several, none of them a claim about where
    anything went.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.inserted: list[Insert] = []

    async def execute(self, statement: Executable) -> _FakeResult:
        if isinstance(statement, Insert):
            self.inserted.append(statement)
        return _FakeResult([])

    def add(self, entity: object) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _settings(*, email: bool = False) -> Settings:
    """The settings this module runs under, built rather than discovered.

    `_env_file=None` is the load-bearing argument. Without it pydantic-settings
    reads `.env` from the working directory and every value below becomes a
    suggestion.
    """
    return Settings(
        _env_file=None,
        environment="test",
        email_enabled=email,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        credential_encryption_keys=[TEST_CREDENTIAL_ENCRYPTION_KEY],
    )


@pytest.fixture
def inviter() -> User:
    return User(id=INVITER_ID, email="owner@example.com", is_active=True)


async def _issue(
    session: AsyncSession,
    inviter: User,
    *,
    role: TenantRole = TenantRole.MEMBER,
    inviter_role: TenantRole = TenantRole.TENANT_OWNER,
    email: bool = False,
) -> tuple[Any, ...]:
    return await InvitationService(session=session, settings=_settings(email=email)).issue(
        tenant_id=TENANT_ID,
        inviter=inviter,
        inviter_role=inviter_role,
        email="invited@example.com",
        role=role,
    )


async def test_the_returned_token_is_the_one_that_opens_the_invitation(inviter: User) -> None:
    session = RecordingSession()

    invitation, raw_token = await _issue(as_session(session), inviter)

    # The regression this exists for: the generator returns a (token, hash)
    # pair, and treating that pair as the token stored an unusable digest and
    # handed the invitee a tuple.
    assert isinstance(raw_token, str)
    assert invitation.token_hash == hash_invitation_token(raw_token)


async def test_the_raw_token_is_never_persisted(inviter: User) -> None:
    session = RecordingSession()

    invitation, raw_token = await _issue(as_session(session), inviter)

    assert invitation.token_hash != raw_token
    assert raw_token not in invitation.token_hash


async def test_the_invitation_is_staged_pending_and_scoped_to_the_workspace(inviter: User) -> None:
    session = RecordingSession()

    invitation, _ = await _issue(as_session(session), inviter)

    # The invitation and its audit entry are staged together, in that order:
    # letting somebody into a workspace is a recorded act (phase 14).
    assert session.added[0] is invitation
    assert [type(row).__name__ for row in session.added] == ["TenantInvitation", "AuditLog"]
    assert invitation.tenant_id == TENANT_ID
    assert invitation.status is InvitationStatus.PENDING
    assert invitation.email == "invited@example.com"
    assert invitation.invited_by_id == INVITER_ID


async def test_two_invitations_never_share_a_token(inviter: User) -> None:
    _, first = await _issue(as_session(RecordingSession()), inviter)
    _, second = await _issue(as_session(RecordingSession()), inviter)

    assert first != second


async def test_an_admin_cannot_invite_an_owner(inviter: User) -> None:
    with pytest.raises(PermissionDeniedError):
        await _issue(
            as_session(RecordingSession()),
            inviter,
            role=TenantRole.TENANT_OWNER,
            inviter_role=TenantRole.TENANT_ADMIN,
        )


async def test_the_token_is_queued_only_to_the_invited_address(inviter: User) -> None:
    """With email on, the raw token's one destination is the invited mailbox.

    This is the claim the whole flow rests on - the token is proof of control
    of an address, so it must reach that address and nowhere else - and it was
    unreachable from this module while email was off, which is the half of
    AUTH-03 that is not merely hygiene.

    The context is sealed, so this opens it with the key the worker would use
    rather than reading the column. A plaintext token in
    `email_messages.context` would be a live credential at rest, in a table the
    worker, the retry sweep and any operator with read access can all see.
    """
    session = RecordingSession()

    invitation, raw_token = await _issue(as_session(session), inviter, email=True)

    assert len(session.inserted) == 1
    parameters = session.inserted[0].compile().params
    assert parameters["recipient"] == "invited@example.com"
    assert parameters["template"] == EmailTemplate.WORKSPACE_INVITATION.value
    assert parameters["idempotency_key"] == f"invitation:{invitation.id}"

    context = parameters["context"]
    assert set(context) == {SEALED_CONTEXT_KEY}
    assert raw_token not in str(context)
    # Nothing else on the row carries it either.
    assert raw_token not in str({k: v for k, v in parameters.items() if k != "context"})

    stored = SimpleNamespace(
        template=EmailTemplate.WORKSPACE_INVITATION.value,
        context=context,
        idempotency_key=parameters["idempotency_key"],
    )
    opened = open_email_context(stored, _settings(email=True))  # type: ignore[arg-type]
    assert opened["token"] == raw_token


@pytest.mark.parametrize("ambient", ["true", "false", "1", "0"])
async def test_ambient_email_configuration_cannot_change_the_outcome(
    inviter: User,
    monkeypatch: pytest.MonkeyPatch,
    ambient: str,
) -> None:
    """The regression for AUTH-03 itself.

    `InvitationService` still defaults `settings` to `get_settings()`, and that
    default is right for production - the request-scoped provider passes the
    real settings in. What must not happen again is this module depending on
    it, so whatever the environment says, these tests run under the settings
    they build.

    Set through the environment rather than through a `.env` file, because
    pydantic-settings gives the environment precedence over the file: an
    explicit `Settings` that survives this survives a `.env` too, and this is
    the stricter of the two checks.
    """
    monkeypatch.setenv("EMAIL_ENABLED", ambient)

    session = RecordingSession()
    invitation, raw_token = await _issue(as_session(session), inviter)

    assert invitation.token_hash == hash_invitation_token(raw_token)
    assert [type(row).__name__ for row in session.added] == ["TenantInvitation", "AuditLog"]
    # Email is off in `_settings()`, so nothing is queued - and the ambient
    # value, whichever of the four it is, did not reach the service.
    assert session.inserted == []
