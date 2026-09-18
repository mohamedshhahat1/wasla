"""A customer's or a colleague's filename, made safe to store and to show.

A filename is display metadata and nothing else: it never builds a path, a key,
a header or a subprocess argument (`build_key` takes none). What it can still do
is fail a database write, and on the webhook that failure was a whole Meta
delivery answered 500 on every retry for seven days - the customer's file and
every sibling message in the delivery lost with it (MEDIA-05). On the outbound
path the same name failed *after* Meta had delivered the file (MEDIA-06).

So both paths bring a name to one canonical form before it goes anywhere:

- NFC, so the same Arabic name typed on two phones is one string;
- no control characters - NUL above all, which PostgreSQL will not store in
  `text`, and the rest of C0/C1 with it, none of which belongs in a name;
- lone surrogates replaced, since they cannot be encoded as UTF-8 at all;
- surrounding whitespace removed;
- at most `MAX_FILENAME_LENGTH` characters - the column's own bound, counted as
  PostgreSQL counts `varchar`, in characters rather than bytes.

The two paths differ in what they do with a name that needs changing, and the
difference is who sent it. A customer's name is **normalised**: the message is
theirs and must be kept, and a shortened display name is a small price for it.
A colleague's upload is **refused before anything is sent** when its name cannot
be stored as given, because the person is there to choose a better one - and
silently renaming what somebody else chose to send is worse than asking.

Arabic, emoji, combining marks and bidirectional characters are left as they
are. A download is served with a bare `Content-Disposition: attachment`, so no
name reaches a header to be spoofed in.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Final

from app.core.exceptions import ValidationError

# `message_media.filename`. Defined here and imported by the model, so the
# normaliser and the column cannot disagree about the bound.
MAX_FILENAME_LENGTH: Final = 300

# The longest extension kept intact when a long name is shortened. A name cut
# to its first 300 characters loses the one part a reader uses to know what it
# is; keeping a short suffix costs nothing.
MAX_KEPT_EXTENSION: Final = 16

REPLACEMENT: Final = chr(0xFFFD)
SURROGATES_START: Final = 0xD800
SURROGATES_END: Final = 0xDFFF


class UnstorableFilenameError(ValidationError):
    """A colleague's filename that cannot be stored as it was given."""

    message = (
        f"File names must be at most {MAX_FILENAME_LENGTH} characters and contain "
        "no control characters."
    )


def _is_control(character: str) -> bool:
    return unicodedata.category(character) == "Cc"


def _canonical(value: str) -> str:
    # One replacement character per lone surrogate: they cannot be encoded as
    # UTF-8 at all, and a name is still worth keeping around one.
    encodable = "".join(
        REPLACEMENT if SURROGATES_START <= ord(character) <= SURROGATES_END else character
        for character in value
    )
    return unicodedata.normalize("NFC", encodable).strip()


def display_filename(value: object) -> str | None:
    """A name from a stranger's phone, in a form the column always accepts.

    Never raises. Returns None for anything that is not a string or that is
    empty once cleaned, which is what "no name" already means.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in _canonical(value) if not _is_control(character))
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) <= MAX_FILENAME_LENGTH:
        return cleaned
    stem, extension = os.path.splitext(cleaned)
    if 0 < len(extension) <= MAX_KEPT_EXTENSION:
        return stem[: MAX_FILENAME_LENGTH - len(extension)].rstrip() + extension
    return cleaned[:MAX_FILENAME_LENGTH].rstrip()


def require_storable_filename(value: str | None) -> str | None:
    """A colleague's name in canonical form, or a refusal before any side effect.

    Raises `UnstorableFilenameError` for a control character anywhere, or for a
    name longer than the column once normalised. None and blank mean no name.
    """
    if value is None:
        return None
    canonical = _canonical(value)
    if any(_is_control(character) for character in canonical):
        raise UnstorableFilenameError()
    if len(canonical) > MAX_FILENAME_LENGTH:
        raise UnstorableFilenameError()
    return canonical or None


__all__ = [
    "MAX_FILENAME_LENGTH",
    "MAX_KEPT_EXTENSION",
    "UnstorableFilenameError",
    "display_filename",
    "require_storable_filename",
]
