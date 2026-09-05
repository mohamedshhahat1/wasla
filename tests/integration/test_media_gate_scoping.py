"""The one conversation this codebase addressed by id alone.

F-10, carried over as M-04. `ConversationMediaGate.lock()` took a row lock on a
conversation with no tenant predicate. It selects one column, returns nothing,
and its only reachable argument is a `media.conversation_id` read moments
earlier through a scoped repository - so it disclosed nothing and it is not the
finding a leak would be.

It is worth closing anyway, and the reason is structural rather than about this
query. The repository layer's tenancy rule is that the predicate lives in one
place and `_tenant_filter()` is abstract, so a repository that forgets it cannot
be instantiated. One class opted out of that by extending `BaseRepository`, and
an opt-out is a thing a reader has to know about. Now it does not exist.

These tests prove the predicate is real - a lock taken across the boundary finds
nothing to lock - and, more importantly, that the *serialisation the gate exists
for still works*, because a tenant predicate added to a lock is exactly the
change that could quietly stop two workers queueing behind each other.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models.conversation import Contact, Conversation, ConversationStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.media_repository import ConversationMediaGate

pytestmark = pytest.mark.integration


async def _workspace(session: AsyncSession, slug: str) -> tuple[Tenant, Conversation]:
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:10]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2010{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def _blocks(
    engine: AsyncEngine,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    seconds: float = 1.0,
) -> bool:
    """Whether a second connection's `lock()` waits, on a row already held.

    **This is the only way to observe what the gate did.** It returns nothing
    by design, so an assertion about which rows its predicate admits has to be
    an assertion about the lock - and running the same `WHERE` in the test
    would prove the test's predicate, not the gate's. That version of this file
    passed with the tenant predicate deleted.

    `FOR UPDATE` on a row somebody else holds blocks; on an empty set it
    returns at once. So "did it lock anything" is a stopwatch.
    """
    acquired = asyncio.Event()

    async def contend() -> None:
        async with AsyncSession(engine, expire_on_commit=False) as second:
            await ConversationMediaGate(second, tenant_id=tenant_id).lock(conversation_id)
            acquired.set()
            await second.rollback()

    waiter = asyncio.create_task(contend())
    try:
        await asyncio.wait_for(acquired.wait(), timeout=seconds)
        return False
    except TimeoutError:
        return True
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_the_gate_locks_its_own_workspaces_conversation(
    engine: AsyncEngine,
    committed: _Workspaces,
) -> None:
    """The positive control, and the reason the refusal below means anything.

    A predicate that admitted nothing would satisfy every cross-tenant
    assertion here and would also break the serialisation the gate exists for -
    silently, because locking no rows is not an error.
    """
    async with AsyncSession(engine, expire_on_commit=False) as holder:
        await ConversationMediaGate(holder, tenant_id=committed.tenant_id).lock(
            committed.conversation_id
        )

        assert await _blocks(
            engine,
            tenant_id=committed.tenant_id,
            conversation_id=committed.conversation_id,
        )

        await holder.rollback()


async def test_the_gate_cannot_lock_another_workspaces_conversation(
    engine: AsyncEngine,
    committed: _Workspaces,
) -> None:
    """The predicate, proven by the lock rather than by re-running the `WHERE`.

    One workspace holds its conversation. The other names that same real id -
    the realistic case, because ids travel in URLs and support tickets - and
    gets through immediately, because the predicate leaves it nothing to lock.
    Before the fix it would have waited: the unscoped statement found the row.

    Nothing is raised. A lock over an empty set is a no-op, which is exactly
    what happens when the id names nothing at all - the same generic miss the
    rest of the repository layer answers with, because distinguishing "not
    yours" from "does not exist" confirms the identifier names a real row.
    """
    async with AsyncSession(engine, expire_on_commit=False) as holder:
        await ConversationMediaGate(holder, tenant_id=committed.tenant_id).lock(
            committed.conversation_id
        )

        blocked = await _blocks(
            engine,
            tenant_id=committed.other_tenant_id,
            conversation_id=committed.conversation_id,
        )

        await holder.rollback()

    assert not blocked, "the gate locked a row belonging to another workspace"


async def test_a_conversation_id_that_names_nothing_behaves_the_same(
    engine: AsyncEngine,
    committed: _Workspaces,
) -> None:
    """Cross-tenant and non-existent are one answer, not two.

    Measured the same way, so the claim is about the lock rather than about the
    absence of an exception: an id nobody owns waits for nothing, exactly as
    another workspace's id does.
    """
    async with AsyncSession(engine, expire_on_commit=False) as holder:
        await ConversationMediaGate(holder, tenant_id=committed.tenant_id).lock(
            committed.conversation_id
        )

        blocked = await _blocks(
            engine,
            tenant_id=committed.tenant_id,
            conversation_id=uuid.uuid4(),
        )

        await holder.rollback()

    assert not blocked


def test_the_gate_cannot_be_built_without_a_tenant() -> None:
    """The structural half, and the actual point of the change.

    `_tenant_filter` is abstract on `TenantScopedRepository`, so the predicate
    is not something this class remembered to add - it is something it cannot
    be constructed without. The signature is checked rather than the behaviour
    because that is what stops the *next* repository opting out the way this
    one had.
    """
    import inspect

    from app.repositories.base import TenantScopedRepository

    assert issubclass(ConversationMediaGate, TenantScopedRepository)
    parameters = inspect.signature(ConversationMediaGate.__init__).parameters
    assert "tenant_id" in parameters
    assert parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY

    with pytest.raises(TypeError):
        ConversationMediaGate(None)  # type: ignore[call-arg, arg-type]


# ------------------------------------------- the property the gate exists for


@dataclass(frozen=True, slots=True)
class _Workspaces:
    """Two committed workspaces, and one conversation belonging to the first."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    other_tenant_id: uuid.UUID
    other_conversation_id: uuid.UUID


@pytest_asyncio.fixture
async def committed(engine: AsyncEngine) -> AsyncIterator[_Workspaces]:
    """Rows every session in this file can see, on their own connections.

    The savepoint-per-test fixture cannot serve these tests: two sessions have
    to see the same row and block on each other, which means the rows must be
    committed and the sessions must be genuinely separate connections. So this
    commits, and cleans up afterwards.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        first_tenant, first_conversation = await _workspace(session, "lockrace")
        second_tenant, second_conversation = await _workspace(session, "lockrival")
        await session.commit()
        workspaces = _Workspaces(
            tenant_id=first_tenant.id,
            conversation_id=first_conversation.id,
            other_tenant_id=second_tenant.id,
            other_conversation_id=second_conversation.id,
        )
    try:
        yield workspaces
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            for conversation_id, tenant_id in (
                (workspaces.conversation_id, workspaces.tenant_id),
                (workspaces.other_conversation_id, workspaces.other_tenant_id),
            ):
                row = await cleanup.get(Conversation, conversation_id)
                if row is None:
                    continue
                contact_id, account_id = row.contact_id, row.account_id
                await cleanup.delete(row)
                await cleanup.flush()
                for model, identifier in (
                    (Contact, contact_id),
                    (WhatsAppAccount, account_id),
                    (Tenant, tenant_id),
                ):
                    found = await cleanup.get(model, identifier)
                    if found is not None:
                        await cleanup.delete(found)
            await cleanup.commit()


async def test_the_lock_still_serialises_two_workers(
    engine: AsyncEngine,
    committed: _Workspaces,
) -> None:
    """The regression that matters, on two real connections.

    A tenant predicate on a `SELECT ... FOR UPDATE` is exactly the change that
    could stop it locking anything while every other test stays green - and
    what that would cost is the thing the gate is for: one webhook carrying two
    photographs becomes two media jobs, both finish, both see nothing
    outstanding, and the customer gets two answers to one question.

    So this holds the lock in one session and times how long the second waits.
    The second must not get in until the first commits.
    """
    tenant_id, conversation_id = committed.tenant_id, committed.conversation_id
    second_acquired = asyncio.Event()
    order: list[str] = []

    async with AsyncSession(engine, expire_on_commit=False) as first:
        await ConversationMediaGate(first, tenant_id=tenant_id).lock(conversation_id)
        order.append("first-locked")

        async def contend() -> None:
            async with AsyncSession(engine, expire_on_commit=False) as second:
                await ConversationMediaGate(second, tenant_id=tenant_id).lock(conversation_id)
                order.append("second-locked")
                second_acquired.set()
                await second.rollback()

        waiter = asyncio.create_task(contend())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_acquired.wait(), timeout=1.0)
        order.append("first-committing")
        await first.commit()

        await asyncio.wait_for(waiter, timeout=10)

    assert order == ["first-locked", "first-committing", "second-locked"]


async def test_two_workspaces_do_not_queue_behind_each_other(
    engine: AsyncEngine,
    committed: _Workspaces,
) -> None:
    """The control for the test above: the lock is per conversation, not global.

    A gate that serialised every workspace's media through one row would also
    pass the serialisation test, and would turn the media worker into a single
    queue for the whole platform.
    """
    async with AsyncSession(engine, expire_on_commit=False) as holder:
        await ConversationMediaGate(holder, tenant_id=committed.tenant_id).lock(
            committed.conversation_id
        )

        blocked = await _blocks(
            engine,
            tenant_id=committed.other_tenant_id,
            conversation_id=committed.other_conversation_id,
        )

        await holder.rollback()

    assert not blocked
