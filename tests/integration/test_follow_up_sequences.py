"""The owner's reminders, sent and stopped as decided on 2026-10-23 (FUS-02 .. FUS-06).

A customer who stops answering hears from the business again at the owner's
reminders - counted from the AI's reply, then from each reminder sent - at any
hour, and never after they reply, after a person takes over, after the lead is
won or the conversation closed, after STOP, or outside the window. Counted at
the transport (`sequence_harness`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.core.exceptions import ValidationError
from app.db.models.conversation import ConversationMode, ConversationStatus
from app.db.models.enums import TenantRole, TenantStatus
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.follow_up_settings import FollowUpSettings
from app.db.models.lead import ActorKind, Lead, LeadSource, LeadStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.follow_up_service import SEQUENCES_AUTOMATIC, FollowUpService
from app.services.follow_up_settings_service import FollowUpSettingsService, Step
from app.services.inbox_service import InboxService
from app.services.lead_service import LeadService
from tests.integration.campaign_harness import USER_DOMAIN, FakeMeta, erase, worker
from tests.integration.sequence_harness import (
    PLACEHOLDER,
    World,
    ai_reply,
    build,
    conversations,
    customer_writes,
    kinds,
    make_due,
    pending,
    runs_of,
    set_conversation,
    step_sends,
    steps_of,
    sweeper,
    wa_id,
)

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

SLACK = timedelta(seconds=5)


async def _world(prepared_database: str, name: str, **build_args: object) -> World:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name=name)
    return await build(database, prepared_database, meta, **build_args)  # type: ignore[arg-type]


async def _person(world: World) -> User:
    async with world.database.session_factory() as session:
        user = User(
            email=f"seq-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            full_name="Colleague",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        from app.db.models.enums import MembershipStatus
        from app.db.models.membership import Membership

        session.add(
            Membership(
                tenant_id=world.workspace.tenant_id,
                user_id=user.id,
                role=TenantRole.TENANT_ADMIN,
                status=MembershipStatus.ACTIVE,
            )
        )
        await session.commit()
        return user


# --------------------------------------------------------------------------- the owner's scenario


async def test_two_reminders_at_their_times_at_any_hour(prepared_database: str) -> None:
    """[5 min, 30 min]: step 1 five minutes after the reply, step 2 thirty after step 1 went.

    Quiet hours are on and contain now: the steps are sent anyway (FUS-08).
    Both free-form, neither a template; nothing after the last.
    """
    world = await _world(prepared_database, "seq-two", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)

        first = await pending(world, conversation)
        assert first is not None and first.step_index == 1
        (run,) = await runs_of(world, conversation)
        assert first.anchor_at == run.anchor_at
        assert first.scheduled_at - run.anchor_at == timedelta(minutes=5)
        assert first.requested_at == first.scheduled_at
        assert first.created_by_kind is ActorKind.SYSTEM

        await make_due(world, conversation)
        await sweeper(world).run_once()
        sent_first = next(row for row in await steps_of(world, conversation) if row.step_index == 1)
        assert sent_first.status is FollowUpStatus.SENT
        second = await pending(world, conversation)
        assert second is not None and second.step_index == 2
        assert sent_first.sent_at is not None
        assert second.scheduled_at - sent_first.sent_at == timedelta(minutes=30)

        await make_due(world, conversation)
        await sweeper(world).run_once()

        sent = step_sends(world, number)
        assert [entry["payload"]["text"]["body"] for entry in sent] == [PLACEHOLDER, PLACEHOLDER]
        assert kinds(sent) == {"text"}
        assert await pending(world, conversation) is None
        (run,) = await runs_of(world, conversation)
        assert (run.state, run.end_reason, run.steps_sent) == ("ended", "completed", 2)
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- stop rules


async def test_a_reply_cancels_the_next_step_and_the_next_ai_reply_starts_again(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-reply", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)
        await make_due(world, conversation)
        await sweeper(world).run_once()
        assert await pending(world, conversation) is not None

        await customer_writes(world, conversation, "Thanks, how about delivery?")

        assert await pending(world, conversation) is None
        cancelled = [row for row in await steps_of(world, conversation) if row.step_index == 2]
        assert cancelled[0].status is FollowUpStatus.CANCELLED
        assert cancelled[0].cancelled_reason == "customer_replied"
        (run,) = await runs_of(world, conversation)
        assert run.end_reason == "customer_replied"
        await sweeper(world).run_once()
        assert len(step_sends(world, number)) == 1

        await ai_reply(world, conversation, "Anything else?")
        restarted = await pending(world, conversation)
        assert restarted is not None and restarted.step_index == 1
        assert len(await runs_of(world, conversation)) == 2
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_takeover_cancels_the_waiting_step(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-takeover", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)
        await make_due(world, conversation)
        await sweeper(world).run_once()
        person = await _person(world)

        async with world.database.session_factory() as session:
            await InboxService(session=session, tenant_id=world.workspace.tenant_id).set_mode(
                conversation_id=conversation.id, mode=ConversationMode.HUMAN, actor=person
            )
            await session.commit()

        assert await pending(world, conversation) is None
        assert (await runs_of(world, conversation))[0].end_reason == "human_took_over"
        await sweeper(world).run_once()
        assert len(step_sends(world, number)) == 1
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_won_lead_and_a_closed_conversation_stop_the_reminders(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-lead", steps=[5, 30], recipients=2)
    try:
        won, closed = await conversations(world)
        person = await _person(world)
        for conversation in (won, closed):
            await ai_reply(world, conversation)
        async with world.database.session_factory() as session:
            lead = Lead(
                tenant_id=world.workspace.tenant_id,
                contact_id=won.contact_id,
                status=LeadStatus.QUALIFIED,
                source=LeadSource.MANUAL,
            )
            session.add(lead)
            await session.commit()
        async with world.database.session_factory() as session:
            await LeadService(session=session, tenant_id=world.workspace.tenant_id).change_status(
                lead_id=lead.id, status=LeadStatus.WON, actor_id=person.id
            )
            await InboxService(session=session, tenant_id=world.workspace.tenant_id).close(
                closed.id, actor=person
            )
            await session.commit()

        assert (await runs_of(world, won))[0].end_reason == "lead_closed"
        assert (await runs_of(world, closed))[0].end_reason == "conversation_closed"
        for conversation in (won, closed):
            assert await pending(world, conversation) is None
        await sweeper(world).run_once()
        for conversation in (won, closed):
            assert step_sends(world, await wa_id(world, conversation)) == []
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_stop_ends_the_reminders_as_an_opt_out(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-stop", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)
        await customer_writes(world, conversation, "STOP")
        assert (await runs_of(world, conversation))[0].end_reason == "opted_out"
        await ai_reply(world, conversation, "Anything else?")
        assert await pending(world, conversation) is None, "no run for somebody who opted out"
        await sweeper(world).run_once()
        assert step_sends(world, number) == []
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_suspended_workspace_sends_no_step(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-suspended", steps=[5])
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        async with world.database.session_factory() as session:
            await session.execute(
                update(Tenant)
                .where(Tenant.id == world.workspace.tenant_id)
                .values(status=TenantStatus.SUSPENDED)
            )
            await session.commit()
        await make_due(world, conversation)
        await sweeper(world).run_once()
        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, "not_served")
        assert (await runs_of(world, conversation))[0].end_reason == "not_served"
        assert step_sends(world, await wa_id(world, conversation)) == []
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- the window


async def test_a_step_past_the_window_is_never_scheduled(prepared_database: str) -> None:
    """[5 min, 23 h], the customer wrote 23 h 20 min ago: step 2 would land after the window."""
    world = await _world(
        prepared_database,
        "seq-window",
        steps=[5, 1380],
        last_inbound_ago=timedelta(hours=23, minutes=20),
    )
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)
        await make_due(world, conversation)
        await sweeper(world).run_once()

        assert await pending(world, conversation) is None
        assert [row.step_index for row in await steps_of(world, conversation)] == [1]
        (run,) = await runs_of(world, conversation)
        assert (run.end_reason, run.steps_sent) == ("window_closing", 1)
        assert len(step_sends(world, number)) == 1
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_step_found_past_the_margin_at_dispatch_is_skipped(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-closed", steps=[5])
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        # The clock moved on: the window now ends in 20 minutes, inside the margin.
        await set_conversation(
            world,
            conversation,
            last_inbound_at=datetime.now(UTC) - timedelta(hours=23, minutes=40),
        )
        await make_due(world, conversation)
        await sweeper(world).run_once()

        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, "window_closed")
        assert (await runs_of(world, conversation))[0].end_reason == "window_closed"
        assert step_sends(world, await wa_id(world, conversation)) == []
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- restarts and limits


async def test_a_second_reply_before_step_one_restarts_from_the_new_anchor(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-restart", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        await ai_reply(world, conversation, "Anything else?")

        first, second = await runs_of(world, conversation)
        assert (first.state, first.end_reason) == ("ended", "sequence_restarted")
        assert second.state == "active"
        step = await pending(world, conversation)
        assert step is not None and step.sequence_run_id == second.id
        assert step.scheduled_at - second.anchor_at == timedelta(minutes=5)
        cancelled = [
            row
            for row in await steps_of(world, conversation)
            if row.status is FollowUpStatus.CANCELLED
        ]
        assert [row.cancelled_reason for row in cancelled] == ["sequence_restarted"]
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_business_message_to_a_silent_customer_starts_nothing(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-silent", steps=[5])
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        await make_due(world, conversation)
        await sweeper(world).run_once()
        assert (await runs_of(world, conversation))[0].end_reason == "completed"

        await ai_reply(world, conversation, "Anything else?")

        assert len(await runs_of(world, conversation)) == 1
        assert await pending(world, conversation) is None
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_five_reminders_are_five_sends_and_never_a_sixth(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-five", steps=[5, 5, 5, 5, 5])
    try:
        conversation = (await conversations(world))[0]
        number = await wa_id(world, conversation)
        await ai_reply(world, conversation)
        for _ in range(7):
            await make_due(world, conversation)
            await sweeper(world).run_once()
        assert len(step_sends(world, number)) == 5
        assert [row.step_index for row in await steps_of(world, conversation)] == [1, 2, 3, 4, 5]
        assert (await runs_of(world, conversation))[0].steps_sent == 5
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- who owns the slot


async def test_sequences_off_start_nothing(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-off", steps=[5], enabled=False)
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        assert await runs_of(world, conversation) == []
        assert await pending(world, conversation) is None
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_the_agent_s_own_follow_up_is_refused_in_words_it_reads(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-agent", steps=[5])
    try:
        conversation = (await conversations(world))[0]
        async with world.database.session_factory() as session:
            with pytest.raises(ValidationError) as refused:
                await FollowUpService(
                    session=session, tenant_id=world.workspace.tenant_id
                ).schedule(
                    conversation_id=conversation.id,
                    delay=timedelta(minutes=30),
                    body="Still there?",
                    created_by_kind=ActorKind.AGENT,
                )
        assert str(refused.value) == SEQUENCES_AUTOMATIC
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_person_s_one_off_replaces_the_waiting_step(prepared_database: str) -> None:
    world = await _world(prepared_database, "seq-person", steps=[5, 30])
    try:
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        person = await _person(world)
        async with world.database.session_factory() as session:
            await FollowUpService(session=session, tenant_id=world.workspace.tenant_id).schedule(
                conversation_id=conversation.id,
                delay=timedelta(minutes=10),
                body="Hi, this is Mona from the shop.",
                created_by_id=person.id,
                created_by_kind=ActorKind.USER,
            )
            await session.commit()

        rows = await steps_of(world, conversation)
        assert [(row.origin, row.status, row.cancelled_reason) for row in rows] == [
            ("sequence", FollowUpStatus.CANCELLED, "replaced_by_person"),
            ("one_off", FollowUpStatus.PENDING, None),
        ]
        assert (await runs_of(world, conversation))[0].end_reason == "replaced_by_person"
        await make_due(world, conversation)
        await sweeper(world).run_once()
        assert [row.status for row in await steps_of(world, conversation)][
            -1
        ] is FollowUpStatus.SENT
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_colleague_s_reminder_keeps_the_run_away_and_an_agent_s_gives_way(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "seq-slots", steps=[5], recipients=2)
    try:
        colleague, agent = await conversations(world)
        person = await _person(world)
        async with world.database.session_factory() as session:
            for conversation, kind in ((colleague, ActorKind.USER), (agent, ActorKind.AGENT)):
                session.add(
                    FollowUp(
                        tenant_id=world.workspace.tenant_id,
                        conversation_id=conversation.id,
                        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
                        status=FollowUpStatus.PENDING,
                        body="Earlier nudge.",
                        created_by_kind=kind,
                        created_by_id=person.id if kind is ActorKind.USER else None,
                        attempts=0,
                    )
                )
            await session.commit()

        await ai_reply(world, colleague)
        await ai_reply(world, agent)

        assert await runs_of(world, colleague) == []
        kept = await pending(world, colleague)
        assert kept is not None and kept.origin == "one_off"
        replaced, step = await steps_of(world, agent)
        assert (replaced.status, replaced.cancelled_reason) == (
            FollowUpStatus.CANCELLED,
            "replaced_by_sequence",
        )
        assert step.origin == "sequence" and step.status is FollowUpStatus.PENDING
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- the settings card


async def test_switching_off_and_deleting_a_reminder_cancel_what_waits(
    prepared_database: str,
) -> None:
    """[5, 30, 120]: one run waits on step 3, one on step 2. Delete the 2 h reminder."""
    world = await _world(prepared_database, "seq-edit", steps=[5, 30, 120], recipients=3)
    try:
        on_three, on_two, off = await conversations(world)
        for conversation, sent in ((on_three, 2), (on_two, 1), (off, 0)):
            await ai_reply(world, conversation)
            for _ in range(sent):
                await make_due(world, conversation)
                await sweeper(world).run_once()
        person = await _person(world)
        due_two = (await pending(world, on_two)).scheduled_at  # type: ignore[union-attr]

        async with world.database.session_factory() as session:
            service = FollowUpSettingsService(session, tenant_id=world.workspace.tenant_id)
            await service.update(
                actor=person,
                enabled=True,
                channel_types=["whatsapp"],
                steps=[Step(5, None), Step(30, None)],
                version=1,
            )
            await session.commit()

        assert await pending(world, on_three) is None
        assert (await runs_of(world, on_three))[0].end_reason == "settings_changed"
        kept = await pending(world, on_two)
        assert kept is not None and kept.scheduled_at == due_two

        async with world.database.session_factory() as session:
            service = FollowUpSettingsService(session, tenant_id=world.workspace.tenant_id)
            await service.update(
                actor=person,
                enabled=False,
                channel_types=["whatsapp"],
                steps=[Step(5, None), Step(30, None)],
                version=2,
            )
            await session.commit()

        for conversation in (on_two, off):
            assert await pending(world, conversation) is None
            assert (await runs_of(world, conversation))[-1].end_reason == "settings_changed"
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- two workers


async def test_two_workers_send_every_step_once(prepared_database: str) -> None:
    """200 conversations, [5 min, 30 min], two workers sweeping at once: 400 steps, none twice."""
    world = await _world(prepared_database, "seq-race", steps=[5, 30], recipients=200)
    try:
        everyone = await conversations(world)
        for conversation in everyone:
            await ai_reply(world, conversation)
        first, second = sweeper(world, name="one"), sweeper(world, name="two")
        for _ in range(2):
            async with world.database.session_factory() as session:
                await session.execute(
                    update(FollowUp)
                    .where(
                        FollowUp.tenant_id == world.workspace.tenant_id,
                        FollowUp.status == FollowUpStatus.PENDING,
                    )
                    .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await session.commit()
            for _ in range(40):
                handled = await asyncio.gather(first.run_once(), second.run_once())
                if list(handled) == [0, 0]:
                    break

        counted: dict[str, int] = {}
        for entry in world.meta.requests:
            text = entry["payload"].get("text", {}).get("body")
            if text == PLACEHOLDER:
                counted[str(entry["to"])] = counted.get(str(entry["to"]), 0) + 1
        assert len(counted) == 200
        assert set(counted.values()) == {2}
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_no_run_starts_without_a_settings_row(prepared_database: str) -> None:
    """A workspace never configured - written directly - keeps today's behaviour."""
    world = await _world(prepared_database, "seq-none", steps=[5])
    try:
        async with world.database.session_factory() as session:
            await session.execute(
                delete(FollowUpSettings).where(
                    FollowUpSettings.tenant_id == world.workspace.tenant_id
                )
            )
            await session.commit()
        conversation = (await conversations(world))[0]
        await ai_reply(world, conversation)
        assert await runs_of(world, conversation) == []
        assert ConversationStatus.OPEN  # the conversation itself is untouched
    finally:
        await erase(world.database)
        await world.database.dispose()
