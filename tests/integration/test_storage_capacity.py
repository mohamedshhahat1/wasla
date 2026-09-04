"""A workspace cannot fill the object store without limit.

`STORAGE_USED` was metered from the day media shipped and no `LimitKey` capped
it, so the one denial-of-wallet surface the audit could not close by argument
was the one where an authenticated member uploads until somebody notices the
bill.

Two properties are asserted here, and they are different questions.

**Where the number comes from.** `usage_events` is authoritative for what a
workspace has *consumed* and is append-only by design, so `STORAGE_USED`
records bytes when they are written and never subtracts when retention deletes
them. A capacity limit needs "how much is held", not "how much was ever
written", and the only durable record of that is the media rows themselves.
The tests below pin the difference: a workspace that uploads a megabyte and
purges it has consumed a megabyte and is holding nothing.

**That two uploads cannot both have the last megabyte.** The claim is the
committed intent - the row that names an object before the object exists
(ADR-087) - taken under the workspace's advisory lock. So the concurrency test
runs two real transactions on two real connections against real PostgreSQL,
because a fake cannot tell a lock that works from one that does not.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.exceptions import PlanLimitExceededError
from app.core.storage import LocalMediaStorage, MediaStorage, build_key
from app.db.models.billing import (
    RESOURCE_LIMITS,
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import (
    OCCUPYING_STORAGE_STATES,
    MediaStatus,
    MediaStorageState,
    MessageMedia,
)
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.billing_repository import SubscriptionRepository
from app.services.entitlement_service import EntitlementService
from app.services.media_service import content_hash
from app.services.media_upload_service import MediaUploadReconciler
from app.services.usage_service import UsageRecorder

pytestmark = pytest.mark.integration

MEGABYTE = 1024 * 1024
NOW = datetime.now(UTC)
# A PNG signature, so the synthetic bytes below are a plausible file rather
# than a string that happens to be stored.
PNG_HEADER = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


# ------------------------------------------------------------------ fixtures


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, over an engine of this file's own.

    The concurrency test needs two transactions that can actually see each
    other, which the suite's rolled-back `db_session` cannot provide: a
    savepoint release is invisible to another connection, so a lock test
    against it would pass whatever the lock did.
    """
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _plan(session: AsyncSession, limits: Mapping[LimitKey, int]) -> Plan:
    plan = Plan(
        code=f"cap-{uuid.uuid4().hex[:8]}",
        name="Capacity",
        price=Decimal("10.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={key.value: value for key, value in limits.items()},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscribe(session: AsyncSession, tenant: Tenant, plan: Plan) -> Subscription:
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=NOW - timedelta(days=5),
        current_period_end=NOW + timedelta(days=25),
    )
    await session.flush()
    return subscription


async def _workspace(
    session: AsyncSession, *, limit_bytes: int | None, slug: str | None = None
) -> Tenant:
    tenant = Tenant(name="Capacity", slug=slug or f"cap-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    limits = {} if limit_bytes is None else {LimitKey.STORAGE_BYTES: limit_bytes}
    plan = await _plan(session, limits)
    await _subscribe(session, tenant, plan)
    return tenant


async def _conversation(session: AsyncSession, tenant: Tenant) -> Conversation:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()
    conversation = Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()
    return conversation


async def _attachment(
    session: AsyncSession,
    *,
    tenant: Tenant,
    conversation: Conversation,
    byte_size: int,
    state: MediaStorageState,
) -> MessageMedia:
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    # `ck_message_media_storage_state` ties each state to the columns that must
    # accompany it, so a fixture cannot invent a row the lifecycle forbids.
    keyless = {MediaStorageState.ABSENT, MediaStorageState.PURGED}
    purging = {MediaStorageState.PURGING, MediaStorageState.PURGED}
    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id=f"wamid-{uuid.uuid4().hex[:10]}",
        mime_type="image/png",
        status=MediaStatus.STORED,
        storage_state=state,
        storage_key=(
            None
            if state in keyless
            # `build_key` rather than any string: `LocalMediaStorage` refuses a
            # key that does not look like one it produced, so a hand-written
            # one would make every reconciliation below answer UNREACHABLE.
            else build_key(tenant_id=tenant.id, mime_type="image/png")
        ),
        upload_started_at=NOW if state is MediaStorageState.PENDING else None,
        purge_started_at=NOW if state in purging else None,
        byte_size=byte_size,
    )
    session.add(media)
    await session.flush()
    return media


async def _forget(committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    """Remove what a committing test wrote.

    The plan goes with the workspace, and that is not tidiness. These tests
    commit for real - a rolled-back session cannot show one transaction another
    transaction's lock - and a `Plan` row left behind is visible to every test
    in the suite that follows, including the ones asserting the catalogue is
    empty. That is exactly the cascade the audit recorded as WSL-11, arriving
    from the other direction.
    """
    async with committing() as cleanup:
        await cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await cleanup.execute(delete(Plan).where(Plan.code.startswith("cap-")))
        await cleanup.commit()


def _service(session: AsyncSession, tenant: Tenant) -> EntitlementService:
    return EntitlementService(session, tenant_id=tenant.id)


# --------------------------------------------------------- what is counted


async def test_storage_is_a_resource_limit_not_a_period_one() -> None:
    """It measures what is held now, so it never resets with a billing period."""
    assert LimitKey.STORAGE_BYTES in RESOURCE_LIMITS


async def test_a_workspace_holding_nothing_has_used_nothing(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)

    entitlement = await _service(db_session, tenant).check(LimitKey.STORAGE_BYTES, additional=0)

    assert entitlement.used == 0
    assert entitlement.remaining == 10 * MEGABYTE


@pytest.mark.parametrize("state", sorted(OCCUPYING_STORAGE_STATES))
async def test_a_row_that_names_an_object_occupies_capacity(
    db_session: AsyncSession, state: MediaStorageState
) -> None:
    """Every state but `ABSENT` and `PURGED` still holds bytes.

    `PENDING` is the one that makes the intent a reservation, `MISMATCHED` is
    an object deliberately never deleted, and `PURGING` is a delete that has
    not finished - handing out its space early would let a workspace exceed the
    cap whenever a retention sweep is running.
    """
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, tenant)
    await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=3 * MEGABYTE,
        state=state,
    )

    entitlement = await _service(db_session, tenant).check(LimitKey.STORAGE_BYTES, additional=0)

    assert entitlement.used == 3 * MEGABYTE


@pytest.mark.parametrize("state", [MediaStorageState.ABSENT, MediaStorageState.PURGED])
async def test_a_row_with_no_object_behind_it_occupies_nothing(
    db_session: AsyncSession, state: MediaStorageState
) -> None:
    """Retention gives the space back, which is what makes the cap survivable."""
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, tenant)
    await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=3 * MEGABYTE,
        state=state,
    )

    entitlement = await _service(db_session, tenant).check(LimitKey.STORAGE_BYTES, additional=0)

    assert entitlement.used == 0


async def test_purging_a_file_gives_its_capacity_back(db_session: AsyncSession) -> None:
    """The whole sequence, because a cap that never frees is a cap that fills."""
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, tenant)
    media = await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=9 * MEGABYTE,
        state=MediaStorageState.STORED,
    )

    service = _service(db_session, tenant)
    assert not (await service.check(LimitKey.STORAGE_BYTES, additional=5 * MEGABYTE)).allowed

    media.storage_state = MediaStorageState.PURGED
    media.storage_key = None
    media.purge_started_at = NOW
    await db_session.flush()

    assert (await service.check(LimitKey.STORAGE_BYTES, additional=5 * MEGABYTE)).allowed


async def test_lifetime_usage_is_not_current_capacity(db_session: AsyncSession) -> None:
    """The reason this reads rows rather than `usage_events`.

    `STORAGE_USED` is append-only: a workspace that uploaded a gigabyte and
    purged it has consumed a gigabyte for ever, and is holding nothing. Reading
    the meter as a capacity would refuse them for space they gave back.
    """
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    UsageRecorder(db_session, tenant_id=tenant.id).record(
        UsageEventType.STORAGE_USED, quantity=100 * MEGABYTE
    )
    await db_session.flush()

    entitlement = await _service(db_session, tenant).check(LimitKey.STORAGE_BYTES, additional=0)

    assert entitlement.used == 0
    assert entitlement.allowed


# ------------------------------------------------------------- enforcement


async def test_an_upload_with_capacity_is_allowed(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, tenant)
    await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=4 * MEGABYTE,
        state=MediaStorageState.STORED,
    )

    entitlement = await _service(db_session, tenant).reserve(
        LimitKey.STORAGE_BYTES, additional=5 * MEGABYTE
    )

    assert entitlement.allowed


async def test_an_upload_without_capacity_is_refused(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, tenant)
    await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=9 * MEGABYTE,
        state=MediaStorageState.STORED,
    )

    entitlement = await _service(db_session, tenant).reserve(
        LimitKey.STORAGE_BYTES, additional=5 * MEGABYTE
    )

    assert not entitlement.allowed
    with pytest.raises(PlanLimitExceededError):
        await _service(db_session, tenant).require(LimitKey.STORAGE_BYTES, additional=5 * MEGABYTE)


async def test_a_plan_with_no_storage_limit_is_unlimited(db_session: AsyncSession) -> None:
    """The encoding the whole catalogue rests on: an absent key is no ceiling.

    Enterprise carries no limits, and a deployment that has not run the seeding
    migration carries none either. Both must keep working.
    """
    tenant = await _workspace(db_session, limit_bytes=None)
    conversation = await _conversation(db_session, tenant)
    await _attachment(
        db_session,
        tenant=tenant,
        conversation=conversation,
        byte_size=500 * MEGABYTE,
        state=MediaStorageState.STORED,
    )

    entitlement = await _service(db_session, tenant).check(
        LimitKey.STORAGE_BYTES, additional=MEGABYTE
    )

    assert entitlement.is_unlimited
    assert entitlement.allowed


async def test_reserving_a_period_limit_is_refused_as_a_programming_error() -> None:
    """`consume` records what it reserves; this cannot, so it must not be used."""
    service = EntitlementService(None, tenant_id=uuid.uuid4())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="use consume"):
        await service.reserve(LimitKey.PERIOD_MESSAGES, additional=1)


# ------------------------------------------------------------- isolation


async def test_another_workspaces_files_do_not_fill_this_ones_quota(
    db_session: AsyncSession,
) -> None:
    acme = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    globex = await _workspace(db_session, limit_bytes=10 * MEGABYTE)
    conversation = await _conversation(db_session, globex)
    await _attachment(
        db_session,
        tenant=globex,
        conversation=conversation,
        byte_size=9 * MEGABYTE,
        state=MediaStorageState.STORED,
    )

    theirs = await _service(db_session, globex).check(LimitKey.STORAGE_BYTES, additional=0)
    ours = await _service(db_session, acme).check(LimitKey.STORAGE_BYTES, additional=0)

    assert theirs.used == 9 * MEGABYTE
    assert ours.used == 0


# ------------------------------------------------------------ concurrency


async def test_two_concurrent_uploads_cannot_exceed_the_quota(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """The property a lock exists for, driven on two real connections.

    Ten megabytes of room, two eight-megabyte uploads. Without the advisory
    lock both read "two megabytes used" and both are told yes, and the
    workspace ends up holding sixteen. With it, the second waits for the first
    to commit its intent, counts those bytes, and is refused.

    Each side commits its own claim, which is what makes the next one see it -
    the reservation is the media row, not a counter.
    """
    async with committing() as setup:
        tenant = await _workspace(setup, limit_bytes=10 * MEGABYTE)
        conversation = await _conversation(setup, tenant)
        await setup.commit()
        tenant_id, conversation_id = tenant.id, conversation.id

    started = asyncio.Event()
    accepted: list[int] = []
    refused: list[int] = []

    async def upload(size: int, *, first: bool) -> None:
        async with committing() as session:
            reloaded = await session.get(Tenant, tenant_id)
            assert reloaded is not None
            entitlement = await EntitlementService(session, tenant_id=tenant_id).reserve(
                LimitKey.STORAGE_BYTES, additional=size
            )
            if not entitlement.allowed:
                refused.append(size)
                await session.rollback()
                return

            # The claim: an intent row committed while the lock is still held.
            loaded = await session.get(Conversation, conversation_id)
            assert loaded is not None
            await _attachment(
                session,
                tenant=reloaded,
                conversation=loaded,
                byte_size=size,
                state=MediaStorageState.PENDING,
            )
            accepted.append(size)
            if first:
                # Hold the lock until the other side is definitely waiting on
                # it, so the race is arranged rather than hoped for.
                started.set()
                await asyncio.sleep(0.2)
            await session.commit()

    async def second() -> None:
        await started.wait()
        await upload(8 * MEGABYTE, first=False)

    await asyncio.gather(upload(8 * MEGABYTE, first=True), second())

    assert accepted == [8 * MEGABYTE]
    assert refused == [8 * MEGABYTE]

    async with committing() as check:
        held = await check.scalar(
            select(MessageMedia.byte_size).where(MessageMedia.tenant_id == tenant_id)
        )
        assert held == 8 * MEGABYTE
    await _forget(committing, tenant_id)


# ------------------------------------------------- failure and crash recovery


async def _reconcile(committing: async_sessionmaker[AsyncSession], storage: MediaStorage) -> None:
    """One real reconciliation pass, with every intent already stale."""
    async with committing() as session:
        await MediaUploadReconciler(session=session, storage=storage).run(
            now=datetime.now(UTC) + timedelta(hours=1),
            grace_seconds=1.0,
            limit=50,
        )


async def test_a_failed_upload_does_not_permanently_consume_capacity(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """The object write never landed, so the space it claimed comes back.

    The intent is the reservation, which is what makes an upload that dies
    before its PUT hold capacity at all - and reconciliation is what gives it
    back. Without that, every failed write would cost a workspace space for
    ever and the cap would fill with files that do not exist.
    """
    storage = LocalMediaStorage(Path(tempfile.mkdtemp()))
    async with committing() as setup:
        tenant = await _workspace(setup, limit_bytes=10 * MEGABYTE)
        conversation = await _conversation(setup, tenant)
        # An intent whose object was never written: the crash between TX1 and
        # the PUT, which is the window ADR-087 exists for.
        await _attachment(
            setup,
            tenant=tenant,
            conversation=conversation,
            byte_size=9 * MEGABYTE,
            state=MediaStorageState.PENDING,
        )
        await setup.commit()
        tenant_id = tenant.id

    try:
        async with committing() as before:
            held = await EntitlementService(before, tenant_id=tenant_id).check(
                LimitKey.STORAGE_BYTES, additional=0
            )
            assert held.used == 9 * MEGABYTE, "an intent must claim its space"

        await _reconcile(committing, storage)

        async with committing() as after:
            freed = await EntitlementService(after, tenant_id=tenant_id).check(
                LimitKey.STORAGE_BYTES, additional=0
            )
            assert freed.used == 0
            assert (
                await EntitlementService(after, tenant_id=tenant_id).check(
                    LimitKey.STORAGE_BYTES, additional=9 * MEGABYTE
                )
            ).allowed
    finally:
        await _forget(committing, tenant_id)


async def test_a_crash_after_the_object_landed_keeps_the_space_accounted(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """The other half: the bytes are real, so they keep costing.

    A worker that died between the PUT and its finalisation leaves an object
    nobody has confirmed. Reconciliation finds it, settles the row as STORED -
    and the capacity it was already claiming as an intent stays claimed, which
    is the answer that matches what the store is actually holding.
    """
    directory = Path(tempfile.mkdtemp())
    storage = LocalMediaStorage(directory)
    payload = PNG_HEADER + b"a synthetic photograph" * 64

    async with committing() as setup:
        tenant = await _workspace(setup, limit_bytes=10 * MEGABYTE)
        conversation = await _conversation(setup, tenant)
        media = await _attachment(
            setup,
            tenant=tenant,
            conversation=conversation,
            byte_size=len(payload),
            state=MediaStorageState.PENDING,
        )
        media.content_hash = content_hash(payload)
        key = media.storage_key
        assert key is not None
        await setup.commit()
        tenant_id, media_id = tenant.id, media.id

    try:
        # The PUT that landed, and the finalisation that never ran.
        await storage.put_at(key=key, data=payload, mime_type="image/png")

        await _reconcile(committing, storage)

        async with committing() as after:
            settled = await after.get(MessageMedia, media_id, populate_existing=True)
            assert settled is not None
            assert settled.storage_state is MediaStorageState.STORED
            held = await EntitlementService(after, tenant_id=tenant_id).check(
                LimitKey.STORAGE_BYTES, additional=0
            )
            assert held.used == len(payload)
    finally:
        await _forget(committing, tenant_id)
        shutil.rmtree(directory, ignore_errors=True)
