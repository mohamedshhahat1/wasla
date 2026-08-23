"""Rescuing a number claimed before ownership proof existed.

Migration 0022 leaves every pre-ADR-037 row with `ownership_verified_at IS
NULL`. Those rows were deliberately not back-dated - a manufactured timestamp
would erase exactly the list an operator needs - and deliberately not refused at
send time, because breaking every existing deployment's traffic to close a
claim-time hole is the worse outage.

That left one real gap, and it is the reason this file exists. `connect` refuses
a number that is already claimed, so an administrator holding a legacy row had
no way to attach proof to it. The only route was to **release the number and
claim it again** - which frees it to the entire platform in between, and hands
anybody watching a race worth running. The safe-looking action was the dangerous
one.

`reverify` closes that: control of a number the workspace already holds, proven
in place. The number is read from the row rather than taken as a parameter, so
this cannot move a claim - which is what keeps it from becoming a second, softer
way in (ADR-041).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.exceptions import DependencyUnavailableError, TenantIsolationError
from app.db.models import Tenant, TenantStatus, WhatsAppAccount, WhatsAppAccountStatus
from app.db.models.audit import AuditAction, AuditLog
from app.integrations.whatsapp.ownership import NumberOwnershipError
from app.repositories.whatsapp_repository import WhatsAppAccountDirectory
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.fake_ownership import FakeOwnershipVerifier

pytestmark = pytest.mark.integration

NUMBER = "109876543210"
TOKEN = "EAAG-the-workspaces-own-credential"


async def _tenant(session, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    return tenant


async def _legacy_account(session, tenant: Tenant, number: str = NUMBER) -> WhatsAppAccount:
    """A row exactly as migration 0022 leaves a pre-ADR-037 claim.

    Written directly rather than through the service, because the service can no
    longer produce one - which is the point.
    """
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=number,
        # Typed in by hand back when nothing checked it.
        waba_id="waba-nobody-verified",
        display_phone_number="+20 100 000 0000",
        status=WhatsAppAccountStatus.ACTIVE,
        ownership_verified_at=None,
    )
    session.add(account)
    await session.flush()
    return account


def _service(session, verifier: FakeOwnershipVerifier | None = None) -> WhatsAppAccountService:
    return WhatsAppAccountService(
        session=session,
        ownership=verifier if verifier is not None else FakeOwnershipVerifier().owns(NUMBER),
    )


# ------------------------------------------------------- the state is visible


async def test_a_legacy_row_reports_itself_unverified(db_session):
    """The security state of a number is not something to deduce from a null."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)

    assert account.ownership_verified is False
    assert account.ownership_verified_at is None


async def test_a_freshly_connected_number_reports_itself_verified(db_session):
    """The control. A flag that read False for everything would tell an
    operator nothing."""
    tenant = await _tenant(db_session, "fresh")

    account = await _service(db_session).connect(
        tenant_id=tenant.id, phone_number_id=NUMBER, access_token=TOKEN
    )

    assert account.ownership_verified is True


# ------------------------------------------------------ what a legacy row can do


async def test_a_legacy_row_still_carries_traffic(db_session):
    """Deliberate, and worth pinning so nobody "fixes" it into an outage.

    Refusing unverified numbers would break every number connected before
    ADR-037 the moment the release lands. The migration path is re-verification,
    not amputation.
    """
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)

    assert account.is_active is True
    resolved = await WhatsAppAccountDirectory(db_session).get_by_phone_number_id(NUMBER)
    assert resolved is not None
    assert resolved.id == account.id


async def test_nobody_else_can_claim_a_legacy_number(db_session):
    """Unverified does not mean unclaimed. The row still holds the number."""
    from app.core.exceptions import ConflictError

    owner = await _tenant(db_session, "owner")
    attacker = await _tenant(db_session, "attacker")
    await _legacy_account(db_session, owner)

    with pytest.raises(ConflictError):
        await _service(db_session).connect(
            tenant_id=attacker.id, phone_number_id=NUMBER, access_token=TOKEN
        )


# --------------------------------------------------------------- the rescue


async def test_re_verifying_stamps_the_row_without_giving_the_number_up(db_session):
    """The whole point: proof in place, with no window in which somebody else
    could take the number."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)

    verified = await _service(db_session).reverify(
        tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
    )

    assert verified.ownership_verified is True
    assert verified.is_released is False
    # And the number never stopped resolving inbound traffic.
    assert await WhatsAppAccountDirectory(db_session).get_by_phone_number_id(NUMBER) is not None


async def test_meta_overwrites_the_identifiers_nobody_ever_checked(db_session):
    """A legacy row's business account was typed in. Meta's answer is the first
    trustworthy value it has ever had."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    verifier = FakeOwnershipVerifier().owns(NUMBER, waba_id="the-real-business-account")
    verifier.verified_names[NUMBER] = "Acme Ltd"

    verified = await _service(db_session, verifier).reverify(
        tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
    )

    assert verified.waba_id == "the-real-business-account"
    assert verified.verified_name == "Acme Ltd"


async def test_a_claim_that_cannot_be_proven_leaves_the_row_untouched(db_session):
    """An administrator who cannot prove control does not get a stamp - and does
    not lose the number either."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    verifier = FakeOwnershipVerifier().owns("a-different-number")

    with pytest.raises(NumberOwnershipError):
        await _service(db_session, verifier).reverify(
            tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
        )

    assert account.ownership_verified is False
    assert account.is_active is True


async def test_the_number_is_read_from_the_row_and_not_from_the_caller(db_session):
    """The property that stops this being a second way to claim a number.

    Whatever an administrator supplies, what gets proven is the number they
    already hold - so `verify` can never move a claim the way `connect` grants
    one.
    """
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    verifier = FakeOwnershipVerifier().owns(NUMBER)

    await _service(db_session, verifier).reverify(
        tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
    )

    assert verifier.calls[0]["phone_number_id"] == NUMBER
    # And nothing was asserted about the business account: the stored one was
    # never verified, so checking against it would refuse the rows this exists
    # to rescue.
    assert verifier.calls[0]["claimed_waba_id"] is None


async def test_another_workspace_cannot_verify_a_number_it_does_not_hold(db_session):
    """The scoped lookup. Stamping somebody else's row would be a claim about
    their traffic."""
    owner = await _tenant(db_session, "owner")
    attacker = await _tenant(db_session, "attacker")
    account = await _legacy_account(db_session, owner)

    with pytest.raises(TenantIsolationError):
        await _service(db_session).reverify(
            tenant_id=attacker.id, account_id=account.id, access_token=TOKEN
        )


async def test_a_released_number_cannot_be_verified(db_session):
    """Proof on a row that no longer entitles the workspace to anything is
    meaningless, and if somebody else now holds the number it is worse."""
    tenant = await _tenant(db_session, "legacy")
    service = _service(db_session)
    account = await service.connect(tenant_id=tenant.id, phone_number_id=NUMBER, access_token=TOKEN)
    await service.release(tenant_id=tenant.id, account_id=account.id)

    with pytest.raises(TenantIsolationError):
        await service.reverify(tenant_id=tenant.id, account_id=account.id, access_token=TOKEN)


async def test_a_deployment_without_a_verifier_refuses_to_stamp(db_session):
    """Fail closed, exactly as `connect` does."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    service = WhatsAppAccountService(session=db_session, ownership=None)

    with pytest.raises(DependencyUnavailableError):
        await service.reverify(tenant_id=tenant.id, account_id=account.id, access_token=TOKEN)


async def test_re_verification_is_audited_with_what_changed(db_session):
    """A business account that changed is worth seeing: on a legacy row it means
    the typed-in value was wrong, and on a proven one it means the number
    moved."""
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    verifier = FakeOwnershipVerifier().owns(NUMBER, waba_id="the-real-business-account")

    await _service(db_session, verifier).reverify(
        tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
    )
    await db_session.flush()

    entries = (
        (await db_session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    entry = next(e for e in entries if e.action is AuditAction.WHATSAPP_ACCOUNT_VERIFIED)
    assert entry.meta["waba_id"] == "the-real-business-account"
    assert entry.meta["previous_waba_id"] == "waba-nobody-verified"
    assert entry.meta["waba_changed"] is True
    # The credential is named nowhere.
    assert TOKEN not in f"{entry.meta} {entry.target_label}"


async def test_a_verified_number_can_be_verified_again(db_session):
    """Not only a migration tool. Re-proving is how an operator establishes that
    a number they still hold is still theirs at Meta."""
    tenant = await _tenant(db_session, "fresh")
    service = _service(db_session)
    account = await service.connect(tenant_id=tenant.id, phone_number_id=NUMBER, access_token=TOKEN)
    first = account.ownership_verified_at

    again = await service.reverify(tenant_id=tenant.id, account_id=account.id, access_token=TOKEN)

    assert again.ownership_verified_at is not None
    assert again.ownership_verified_at >= first


async def test_re_verification_stores_the_credential_when_a_key_exists(db_session):
    """A legacy row had no way to acquire one: there is no update-credential
    endpoint, and `connect` refuses an already-claimed number. This is the only
    path by which such a number starts sending as itself."""
    from app.core.config import Settings
    from app.core.crypto import generate_key
    from app.services.credential_service import CredentialService

    settings = Settings(
        _env_file=None, environment="test", credential_encryption_keys=[generate_key()]
    )
    tenant = await _tenant(db_session, "legacy")
    account = await _legacy_account(db_session, tenant)
    assert account.has_own_credential is False

    service = WhatsAppAccountService(
        session=db_session,
        ownership=FakeOwnershipVerifier().owns(NUMBER),
        credentials=CredentialService(settings),
    )
    verified = await service.reverify(
        tenant_id=tenant.id, account_id=account.id, access_token=TOKEN
    )

    assert verified.has_own_credential is True
    assert verified.access_token_encrypted is not None
    assert TOKEN not in verified.access_token_encrypted


async def test_an_unknown_account_id_is_not_found(db_session):
    tenant = await _tenant(db_session, "legacy")

    with pytest.raises(TenantIsolationError):
        await _service(db_session).reverify(
            tenant_id=tenant.id, account_id=uuid.uuid4(), access_token=TOKEN
        )
