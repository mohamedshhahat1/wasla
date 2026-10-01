"""The media row's own rules, before anything downloads or reads a file.

The distinctions asserted here are the ones the rest of the phase depends on:
which statuses still hold up a reply, and which of them are worth retrying.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import UniqueConstraint

from app.db.models.media import (
    MAX_ATTEMPTS,
    UNRESOLVED_MEDIA_STATUSES,
    MediaStatus,
    MessageMedia,
)
from tests.fakes import as_table


def _media(**overrides: Any) -> MessageMedia:
    fields = {
        "tenant_id": uuid.uuid4(),
        "message_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "wa_media_id": "media-1",
        "status": MediaStatus.PENDING,
        "byte_size": 0,
        "is_voice": False,
        "attempts": 0,
        **overrides,
    }
    return MessageMedia(**fields)


def test_an_unread_file_holds_up_the_reply() -> None:
    for status in (MediaStatus.PENDING, MediaStatus.DOWNLOADING, MediaStatus.STORED):
        assert _media(status=status).is_resolved is False


def test_a_read_file_no_longer_holds_up_the_reply() -> None:
    assert _media(status=MediaStatus.READY).is_resolved is True


def test_an_unreadable_file_does_not_hold_up_the_reply_forever() -> None:
    """The customer is still owed an answer.

    An agent that says it could not open the attachment is better than one that
    never speaks at all, so both give-up states count as resolved.
    """
    assert _media(status=MediaStatus.SKIPPED).is_resolved is True
    assert _media(status=MediaStatus.FAILED).is_resolved is True


def test_every_status_is_classified() -> None:
    """A status added later must be placed deliberately, not default to resolved."""
    resolved = {MediaStatus.READY, MediaStatus.SKIPPED, MediaStatus.FAILED}
    assert resolved | UNRESOLVED_MEDIA_STATUSES == set(MediaStatus)
    assert resolved & UNRESOLVED_MEDIA_STATUSES == set()


def test_attempts_are_bounded() -> None:
    assert _media(attempts=MAX_ATTEMPTS - 1).is_exhausted is False
    assert _media(attempts=MAX_ATTEMPTS).is_exhausted is True


def test_the_table_is_tenant_scoped() -> None:
    """Media is read by similarity to nothing, but it is still tenant data."""
    assert "tenant_id" in as_table(MessageMedia.__table__).columns


def test_one_file_per_position_in_a_message() -> None:
    """A replay cannot add a copy; a second attachment has a place of its own.

    This used to pin `UNIQUE(message_id)` - one file per message - which is
    exactly what lost the second photograph of a Messenger or Instagram message
    (OMNI-009). The replay protection is unchanged in substance: the position is
    the provider's order, so a replay names the same one and is refused.
    """
    table = as_table(MessageMedia.__table__)
    by_name = {
        constraint.name: constraint
        for constraint in table.constraints
        if constraint.name is not None
    }
    assert "uq_message_media_message_id" not in by_name
    position = by_name["uq_message_media_message_id_position"]
    assert isinstance(position, UniqueConstraint)
    assert [column.name for column in position.columns] == ["message_id", "position"]


def test_a_file_is_keyed_to_its_message_and_conversation_in_its_own_workspace() -> None:
    """OMNI-022: the audit inserted a file naming another workspace's message."""
    keys = {
        key.name: [element.parent.name for element in key.elements]
        for key in as_table(MessageMedia.__table__).foreign_key_constraints
    }
    assert keys["fk_message_media_tenant_message"] == ["tenant_id", "conversation_id", "message_id"]
    assert keys["fk_message_media_tenant_conversation"] == ["tenant_id", "conversation_id"]
    assert "fk_message_media_message_id_messages" not in keys
    assert "fk_message_media_conversation_id_conversations" not in keys
