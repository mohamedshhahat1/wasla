"""The media row's own rules, before anything downloads or reads a file.

The distinctions asserted here are the ones the rest of the phase depends on:
which statuses still hold up a reply, and which of them are worth retrying.
"""

from __future__ import annotations

import uuid
from typing import Any

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


def test_one_file_per_message() -> None:
    """A webhook replay must not be able to add a second row for one message."""
    constraints = {
        constraint.name
        for constraint in as_table(MessageMedia.__table__).constraints
        if constraint.name is not None
    }
    assert "uq_message_media_message_id" in constraints
