"""The team's read position on conversations, and what it implies (UR, ADR-198).

Read state is shared by a workspace's members: one member seeing a conversation
makes it read for all of them. Only the customer's (inbound) messages are ever
unread - a reply, the AI's or a colleague's, has been seen by whoever wrote it.

Every query here names the workspace as a constant beside the row's own tenant
column, the same discipline as the inbox's own queries.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, ScalarSelect, and_, func, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.conversation import (
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
)
from app.db.models.user import User

#: The most a conversation's unread count says (UR-03): the screen shows "99+".
UNREAD_CAP: Final = 100
#: The most the workspace's total says (UR-10): the screen shows "999+".
SUMMARY_CAP: Final = 1000


@dataclass(frozen=True, slots=True)
class LastMessage:
    """The newest message of a conversation, as the inbox row previews it (UR-04)."""

    id: uuid.UUID
    direction: MessageDirection
    kind: MessageKind
    body: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReadMarker:
    """Where the team's read position stands, and who moved it last."""

    message_id: uuid.UUID | None
    by_user_id: uuid.UUID | None
    by_name: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class ReadState:
    """What the list and the detail show beside a conversation (UR-05)."""

    unread_count: int
    last_message: LastMessage | None
    read: ReadMarker | None


EMPTY_READ_STATE: Final = ReadState(unread_count=0, last_message=None, read=None)


def marker_position(conversation: type[Conversation] = Conversation) -> ScalarSelect[int]:
    """The position of the conversation's read marker; NULL when there is none.

    A marker naming no message of its own conversation resolves to NULL too,
    which reads as "nothing seen".
    """
    marker = aliased(Message)
    return (
        select(marker.sequence)
        .where(
            marker.id == conversation.last_read_message_id,
            marker.conversation_id == conversation.id,
            marker.tenant_id == conversation.tenant_id,
        )
        .correlate(conversation)
        .scalar_subquery()
    )


def unread_messages(conversation: type[Conversation] = Conversation) -> ColumnElement[bool]:
    """The predicate of a customer message the team has not seen, for a correlated query."""
    return and_(
        Message.conversation_id == conversation.id,
        Message.tenant_id == conversation.tenant_id,
        Message.direction == MessageDirection.INBOUND,
        Message.sequence > func.coalesce(marker_position(conversation), 0),
    )


def has_unread(conversation: type[Conversation] = Conversation) -> ColumnElement[bool]:
    """Whether the conversation holds at least one unread customer message (UR-09)."""
    return select(Message.id).where(unread_messages(conversation)).correlate(conversation).exists()


class ReadStateRepository:
    """Read positions of one workspace's conversations."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def states(self, conversation_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, ReadState]:
        """Unread count, newest message and read marker of each conversation, in one query.

        The count is capped (UR-03) by reading at most `UNREAD_CAP` rows per
        conversation through the transcript's own `(conversation_id, sequence)`
        index; a page of 1 and a page of 50 cost the same number of statements.
        """
        if not conversation_ids:
            return {}
        unread_rows = (
            select(literal(1).label("one"))
            .where(unread_messages())
            .correlate(Conversation)
            .limit(UNREAD_CAP)
            .lateral("unread_rows")
        )
        unread = (
            select(func.count()).select_from(unread_rows).correlate(Conversation).scalar_subquery()
        )
        newest = (
            select(
                Message.id.label("id"),
                Message.direction.label("direction"),
                Message.kind.label("kind"),
                Message.body.label("body"),
                Message.created_at.label("created_at"),
            )
            .where(
                Message.conversation_id == Conversation.id,
                Message.tenant_id == Conversation.tenant_id,
            )
            .order_by(Message.sequence.desc())
            .limit(1)
            .correlate(Conversation)
            .lateral("newest")
        )
        statement = (
            select(
                Conversation.id,
                unread.label("unread"),
                newest.c.id,
                newest.c.direction,
                newest.c.kind,
                newest.c.body,
                newest.c.created_at,
                Conversation.last_read_message_id,
                Conversation.last_read_by_user_id,
                Conversation.last_read_at,
                User.full_name,
            )
            .select_from(Conversation)
            .outerjoin(newest, true())
            .outerjoin(User, User.id == Conversation.last_read_by_user_id)
            .where(
                Conversation.tenant_id == self._tenant_id,
                Conversation.id.in_(list(conversation_ids)),
            )
        )
        rows = (await self._session.execute(statement)).all()
        states: dict[uuid.UUID, ReadState] = {}
        for row in rows:
            last = (
                LastMessage(
                    id=row[2],
                    direction=row[3],
                    kind=row[4],
                    body=row[5],
                    created_at=row[6],
                )
                if row[2] is not None
                else None
            )
            read = (
                ReadMarker(message_id=row[7], by_user_id=row[8], by_name=row[10], at=row[9])
                if row[9] is not None
                else None
            )
            states[row[0]] = ReadState(unread_count=int(row[1] or 0), last_message=last, read=read)
        return states

    async def position(self, *, conversation_id: uuid.UUID, message_id: uuid.UUID) -> int | None:
        """The position of `message_id` in this conversation; None if it is not its own."""
        return (
            await self._session.execute(
                select(Message.sequence).where(
                    Message.id == message_id,
                    Message.conversation_id == conversation_id,
                    Message.tenant_id == self._tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def newest_before(
        self, *, conversation_id: uuid.UUID, sequence: int
    ) -> tuple[uuid.UUID, int] | None:
        """The message just before position `sequence`, by position, if there is one."""
        row = (
            await self._session.execute(
                select(Message.id, Message.sequence)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.tenant_id == self._tenant_id,
                    Message.sequence < sequence,
                )
                .order_by(Message.sequence.desc())
                .limit(1)
            )
        ).first()
        return None if row is None else (row[0], row[1])

    async def newest_inbound(self, conversation_id: uuid.UUID) -> int | None:
        """The position of the customer's newest message, or None if they never wrote."""
        return (
            await self._session.execute(
                select(Message.sequence)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.tenant_id == self._tenant_id,
                    Message.direction == MessageDirection.INBOUND,
                )
                .order_by(Message.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def summary(self) -> tuple[int, int]:
        """Unread customer messages, and conversations holding one, over what is not closed.

        Each message counted once and the total capped (UR-10), in one statement.
        """
        open_conversations = and_(
            Conversation.tenant_id == self._tenant_id,
            Conversation.status != ConversationStatus.CLOSED,
        )
        messages = (
            select(literal(1).label("one"))
            .select_from(Conversation)
            .join(Message, unread_messages())
            .where(open_conversations)
            .limit(SUMMARY_CAP)
            .subquery()
        )
        conversations = (
            select(Conversation.id).where(open_conversations, has_unread()).limit(SUMMARY_CAP)
        ).subquery()
        statement = select(
            select(func.count()).select_from(messages).scalar_subquery(),
            select(func.count()).select_from(conversations).scalar_subquery(),
        )
        row = (await self._session.execute(statement)).one()
        return int(row[0]), int(row[1])


__all__ = [
    "EMPTY_READ_STATE",
    "SUMMARY_CAP",
    "UNREAD_CAP",
    "LastMessage",
    "ReadMarker",
    "ReadState",
    "ReadStateRepository",
    "has_unread",
    "marker_position",
]
