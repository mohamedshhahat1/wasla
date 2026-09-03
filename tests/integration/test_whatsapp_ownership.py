"""Claiming a number, against a real database.

The attack this file exists for: `phone_number_id` is not a secret. It appears
in every webhook payload, in Meta's dashboard, and in support threads. Before
ownership proof, a workspace that knew a competitor's number could claim it and
become the tenant every inbound message for that number resolved to. The
uniqueness index does not help - it decides who was *first*, not who is
entitled.

What is asserted here that a unit test cannot be: the uniqueness index really
holds under concurrent claims, a released number really becomes claimable, and
the credential really is ciphertext in the column.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.crypto import generate_key
from app.core.exceptions import ConflictError, DependencyUnavailableError
from app.db.models import Tenant, TenantStatus, WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp.ownership import NumberOwnershipError
from app.repositories.whatsapp_repository import WhatsAppAccountDirectory
from app.services.credential_service import CredentialService
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.fake_ownership import FakeOwnershipVerifier

pytestmark = pytest.mark.integration

NUMBER = "109876543210"
TOKEN = "EAAG-the-workspaces-own-credential"


async def _tenant(session: AsyncSession, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    return tenant


def _service(
    session: AsyncSession,
    verifier: FakeOwnershipVerifier | None = None,
    *,
    credentials: CredentialService | None = None,
) -> WhatsAppAccountService:
    return WhatsAppAccountService(
        session=session,
        ownership=verifier if verifier is not None else FakeOwnershipVerifier().owns(NUMBER),
        credentials=credentials,
    )


async def test_a_workspace_that_proves_control_gets_the_number(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "acme")

    account = await _service(db_session).connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    assert account.phone_number_id == NUMBER
    assert account.ownership_verified_at is not None
    # Meta's answer, not the request's.
    assert account.waba_id == "555000111"


async def test_a_workspace_cannot_claim_a_number_it_cannot_prove(db_session: AsyncSession) -> None:
    """The hijack, refused. The attacker knows the number - that was never the
    hard part - and has a perfectly valid credential of their own. What they do
    not have is a credential that can read *this* number."""
    attacker = await _tenant(db_session, "attacker")
    verifier = FakeOwnershipVerifier().owns("a-number-the-attacker-really-owns")

    with pytest.raises(NumberOwnershipError):
        await _service(db_session, verifier).connect(
            tenant_id=attacker.id,
            phone_number_id=NUMBER,
            access_token=TOKEN,
        )

    # And nothing was written, so the failed claim cannot squat the number
    # either: the true owner can still take it.
    rows = await db_session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == NUMBER)
    )
    assert rows.scalars().all() == []


async def test_a_deployment_without_a_verifier_refuses_to_connect(db_session: AsyncSession) -> None:
    """Fail closed. A service built without verification does not fall back to
    trusting the caller."""
    tenant = await _tenant(db_session, "acme")
    service = WhatsAppAccountService(session=db_session, ownership=None)

    with pytest.raises(DependencyUnavailableError):
        await service.connect(
            tenant_id=tenant.id,
            phone_number_id=NUMBER,
            access_token=TOKEN,
        )


async def test_the_platform_token_is_not_a_route_to_a_claim(db_session: AsyncSession) -> None:
    """The bypass that had to be removed. A platform credential can read every
    number the platform is connected to, so proving a claim with it would prove
    nothing about the workspace making it.

    Asserted structurally: the service takes no settings and no platform token,
    so there is no value it could reach for even if a future edit wanted to.
    """
    service = _service(db_session)

    assert not hasattr(service, "_settings")
    assert "meta_access_token" not in repr(service.__dict__)


async def test_two_workspaces_cannot_hold_the_same_number(db_session: AsyncSession) -> None:
    first = await _tenant(db_session, "first")
    second = await _tenant(db_session, "second")

    await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    # The second workspace proves control too - the number may genuinely have
    # moved at Meta - and is still refused, because somebody holds the claim
    # and giving it up is their decision to make.
    with pytest.raises(ConflictError):
        await _service(db_session).connect(
            tenant_id=second.id,
            phone_number_id=NUMBER,
            access_token=TOKEN,
        )


async def test_the_conflict_does_not_name_the_workspace_holding_the_number(
    db_session: AsyncSession,
) -> None:
    first = await _tenant(db_session, "first")
    second = await _tenant(db_session, "second")
    await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    with pytest.raises(ConflictError) as raised:
        await _service(db_session).connect(
            tenant_id=second.id,
            phone_number_id=NUMBER,
            access_token=TOKEN,
        )

    message = str(raised.value).lower()
    assert "first" not in message
    assert str(first.id) not in message


async def test_concurrent_claims_produce_one_winner_and_one_conflict(
    engine: AsyncEngine, prepared_database: str
) -> None:
    """Two claims arriving together, in separate transactions.

    This is the case the read-then-insert check cannot cover: both callers see
    the number free. Without the integrity handler the loser gets a 500 for a
    situation that is neither internal nor an error; with it, the two racing
    callers get the same pair of answers whichever order they land in.

    Runs outside the shared transaction fixture on purpose - a race needs two
    real connections, and a savepoint-joined session would serialise it away.
    """
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    number = f"race-{uuid.uuid4().hex[:12]}"
    tenant_ids: list[uuid.UUID] = []

    async with sessions() as setup:
        for slug in ("race-a", "race-b"):
            tenant = Tenant(
                name=slug,
                slug=f"{slug}-{uuid.uuid4().hex[:8]}",
                status=TenantStatus.ACTIVE,
            )
            setup.add(tenant)
        await setup.flush()
        result = await setup.execute(select(Tenant).where(Tenant.slug.like("race-%")))
        tenant_ids = [tenant.id for tenant in result.scalars().all()][-2:]
        await setup.commit()

    async def claim(tenant_id: uuid.UUID) -> str:
        async with sessions() as session:
            try:
                await WhatsAppAccountService(
                    session=session,
                    ownership=FakeOwnershipVerifier().owns(number),
                ).connect(
                    tenant_id=tenant_id,
                    phone_number_id=number,
                    access_token=TOKEN,
                )
                await session.commit()
                return "won"
            except ConflictError:
                await session.rollback()
                return "conflict"

    try:
        outcomes = await asyncio.gather(*(claim(tenant_id) for tenant_id in tenant_ids))

        assert outcomes.count("won") == 1
        assert outcomes.count("conflict") == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                text("DELETE FROM whatsapp_accounts WHERE phone_number_id = :number"),
                {"number": number},
            )
            await cleanup.execute(
                text("DELETE FROM tenants WHERE id = ANY(:ids)"),
                {"ids": tenant_ids},
            )
            await cleanup.commit()


async def test_releasing_a_number_frees_it_for_another_workspace(db_session: AsyncSession) -> None:
    first = await _tenant(db_session, "first")
    second = await _tenant(db_session, "second")
    account = await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    await _service(db_session).release(tenant_id=first.id, account_id=account.id)

    taken = await _service(db_session).connect(
        tenant_id=second.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )
    assert taken.tenant_id == second.id
    # And the first workspace's history is still there.
    assert account.released_at is not None
    assert account.status is WhatsAppAccountStatus.RELEASED


async def test_a_released_number_no_longer_resolves_inbound_traffic(
    db_session: AsyncSession,
) -> None:
    """The half that would be easy to miss. A released row still exists - it
    holds the conversations - and must never resolve a webhook, or a number
    handed to a new workspace would keep delivering to the old one."""
    first = await _tenant(db_session, "first")
    account = await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    assert await WhatsAppAccountDirectory(db_session).get_by_phone_number_id(NUMBER) is not None

    await _service(db_session).release(tenant_id=first.id, account_id=account.id)

    assert await WhatsAppAccountDirectory(db_session).get_by_phone_number_id(NUMBER) is None


async def test_releasing_drops_the_stored_credential(db_session: AsyncSession) -> None:
    """A credential for a number this workspace no longer holds is a live
    sending capability retained past any authority to use it."""
    settings = Settings(
        _env_file=None,
        environment="test",
        credential_encryption_keys=[
            generate_key(),
        ],
    )
    tenant = await _tenant(db_session, "acme")
    service = _service(db_session, credentials=CredentialService(settings))
    account = await service.connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )
    assert account.access_token_encrypted is not None

    await service.release(tenant_id=tenant.id, account_id=account.id)

    assert account.access_token_encrypted is None


async def test_a_released_account_cannot_be_re_enabled(db_session: AsyncSession) -> None:
    """Enabling it would mean sending on a number somebody else may hold."""
    tenant = await _tenant(db_session, "acme")
    service = _service(db_session)
    account = await service.connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )
    await service.release(tenant_id=tenant.id, account_id=account.id)

    from app.core.exceptions import TenantIsolationError

    with pytest.raises(TenantIsolationError):
        await service.set_status(
            tenant_id=tenant.id,
            account_id=account.id,
            status=WhatsAppAccountStatus.ACTIVE,
        )


async def test_another_workspace_cannot_release_a_number_it_does_not_hold(
    db_session: AsyncSession,
) -> None:
    """The scoped lookup, on the operation where a gap would be worst: releasing
    somebody else's number frees it for the caller to claim a moment later."""
    owner = await _tenant(db_session, "owner")
    attacker = await _tenant(db_session, "attacker")
    account = await _service(db_session).connect(
        tenant_id=owner.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    from app.core.exceptions import TenantIsolationError

    with pytest.raises(TenantIsolationError):
        await _service(db_session).release(tenant_id=attacker.id, account_id=account.id)


async def test_the_stored_credential_is_ciphertext_in_the_column(db_session: AsyncSession) -> None:
    """Read back through SQL rather than the ORM, because what is being checked
    is what a database dump would contain."""
    settings = Settings(
        _env_file=None,
        environment="test",
        credential_encryption_keys=[generate_key()],
    )
    tenant = await _tenant(db_session, "acme")
    account = await _service(db_session, credentials=CredentialService(settings)).connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )
    await db_session.flush()

    stored = await db_session.execute(
        text("SELECT access_token_encrypted FROM whatsapp_accounts WHERE id = :id"),
        {"id": account.id},
    )
    value = stored.scalar_one()

    assert value is not None
    assert TOKEN not in value
    assert value.startswith("v1.")


async def test_a_deployment_without_a_key_still_connects_but_keeps_nothing(
    db_session: AsyncSession,
) -> None:
    """Proof and storage are separate questions. Refusing to store a secret we
    do not need to keep is not a reason to refuse the claim."""
    settings = Settings(_env_file=None, environment="test", credential_encryption_keys=[])
    tenant = await _tenant(db_session, "acme")

    account = await _service(db_session, credentials=CredentialService(settings)).connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    assert account.ownership_verified_at is not None
    assert account.access_token_encrypted is None


async def test_the_credential_is_what_gets_verified(db_session: AsyncSession) -> None:
    """Not a hash of it, not a truncation - the value the caller supplied is
    what is presented to Meta, or the proof is of something else."""
    tenant = await _tenant(db_session, "acme")
    verifier = FakeOwnershipVerifier().owns(NUMBER)

    await _service(db_session, verifier).connect(
        tenant_id=tenant.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    assert verifier.calls[0]["token"] == TOKEN
    assert verifier.calls[0]["phone_number_id"] == NUMBER


async def test_surrounding_whitespace_does_not_create_a_second_claim(
    db_session: AsyncSession,
) -> None:
    """Identifiers are copied by hand from a dashboard. A trailing space that
    survived would break webhook resolution for every inbound message, and
    would let the same number be claimed twice."""
    first = await _tenant(db_session, "first")
    second = await _tenant(db_session, "second")
    await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )

    with pytest.raises(ConflictError):
        await _service(db_session).connect(
            tenant_id=second.id,
            phone_number_id=f"  {NUMBER}  ",
            access_token=TOKEN,
        )


async def test_a_claim_that_slips_past_the_read_check_still_conflicts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race window, forced open.

    `asyncio.gather` above proves the pair of outcomes but cannot guarantee the
    two claims interleave inside the window - the scheduler may serialise them,
    in which case the read check catches the second and the integrity handler is
    never reached. Here the read is stubbed to miss, which is exactly what it
    does when two inserts are in flight, so the index is the only thing left.

    Without the handler this raises `IntegrityError` and the caller answers 500.
    """
    first = await _tenant(db_session, "first")
    second = await _tenant(db_session, "second")
    await _service(db_session).connect(
        tenant_id=first.id,
        phone_number_id=NUMBER,
        access_token=TOKEN,
    )
    await db_session.flush()

    async def blind(self: object, phone_number_id: str) -> None:
        return None

    monkeypatch.setattr(WhatsAppAccountDirectory, "get_by_phone_number_id", blind)

    with pytest.raises(ConflictError):
        await _service(db_session).connect(
            tenant_id=second.id,
            phone_number_id=NUMBER,
            access_token=TOKEN,
        )
