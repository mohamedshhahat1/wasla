"""Keyset cursors for large collections.

Offset pagination is wrong for an inbox. Pages are read while the collection is
being written to, and an offset shifts under every insert: a conversation
arriving between page one and page two pushes a row across the boundary, so the
reader sees it twice or not at all. A keyset cursor names the last row seen and
asks for what follows it, which is stable under concurrent writes.

The cursor is opaque by construction rather than by obfuscation. It carries no
secret - everything in it is already visible in the page it came from - and it
is only ever applied inside a tenant-scoped query, so a forged one can reach no
further than a page of the caller's own data. Encoding it keeps clients from
building cursors by hand and depending on a sort key that is ours to change.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from app.core.exceptions import ValidationError

# Cursors are compared against a timestamp and a UUID, so this bounds what is
# worth attempting to decode at all.
MAX_CURSOR_LENGTH = 128
SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class Cursor:
    """The last row of a page: its sort value and its id.

    The id is the tiebreaker, and it is what makes the ordering total. Two
    conversations can share a `last_message_at` to the microsecond, and without
    a second key the page boundary between them is arbitrary - which is another
    way of saying a row can be skipped.

    `sort_value` is None for a row whose sort column is null. Those rows order
    last, so a null cursor means the reader has reached that final block.
    """

    sort_value: datetime | None
    id: uuid.UUID

    def encode(self) -> str:
        """Render as a URL-safe string, without padding."""
        moment = self.sort_value.isoformat() if self.sort_value is not None else ""
        raw = f"{moment}{SEPARATOR}{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, raw: str) -> Self:
        """Parse a cursor, rejecting anything malformed.

        Every failure is one `ValidationError` - a 422 - because a cursor the
        caller did not get from us is a bad request however it is broken. It
        must never become a 500: cursors arrive in query strings, which means
        they arrive mangled by proxies, truncated by clients, and fuzzed.
        """
        if not raw or len(raw) > MAX_CURSOR_LENGTH:
            raise ValidationError("That pagination cursor is not valid.")

        # Padding was stripped on the way out, so it is restored here rather
        # than requiring callers to preserve it through a URL.
        padded = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as error:
            raise ValidationError("That pagination cursor is not valid.") from error

        moment, separator, identifier = decoded.partition(SEPARATOR)
        if not separator:
            raise ValidationError("That pagination cursor is not valid.")

        try:
            row_id = uuid.UUID(identifier)
            sort_value = datetime.fromisoformat(moment) if moment else None
        except ValueError as error:
            raise ValidationError("That pagination cursor is not valid.") from error

        if sort_value is not None and sort_value.tzinfo is None:
            # Every timestamp column is timezone-aware. A naive value would
            # compare against them incorrectly rather than fail, so it is pinned
            # to UTC instead of trusted.
            sort_value = sort_value.replace(tzinfo=UTC)
        return cls(sort_value=sort_value, id=row_id)


@dataclass(frozen=True, slots=True)
class Page[RowT]:
    """One page of rows and the cursor that follows it."""

    items: list[RowT]
    next_cursor: str | None


def paginate[RowT](
    rows: list[RowT],
    *,
    limit: int,
    key: Callable[[RowT], Cursor],
) -> Page[RowT]:
    """Wrap rows in a page, issuing a cursor only when more may follow.

    A short page ends the collection and gets no cursor: handing one back would
    invite a request guaranteed to return nothing. A full page gets one even
    when it happens to be the last, because the only way to know otherwise is
    to have read further than the caller asked for.
    """
    if not rows or len(rows) < limit:
        return Page(items=rows, next_cursor=None)
    return Page(items=rows, next_cursor=key(rows[-1]).encode())
