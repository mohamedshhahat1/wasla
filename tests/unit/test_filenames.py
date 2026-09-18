"""Filename normalisation for inbound names and validation for outbound ones
(MEDIA-05, MEDIA-06).

Control characters are built with `chr()` rather than written as escapes, so
this file stays plain text whatever tool touches it.
"""

from __future__ import annotations

import unicodedata

import pytest
from sqlalchemy import String

from app.core.filenames import (
    MAX_FILENAME_LENGTH,
    UnstorableFilenameError,
    display_filename,
    require_storable_filename,
)
from app.db.models.media import MessageMedia

NUL = chr(0)
BELL = chr(7)
ESCAPE = chr(27)
DELETE = chr(127)
LONE_SURROGATE = chr(0xD800)
ARABIC = "عقد"


def test_the_bound_is_the_column_bound() -> None:
    column_type = MessageMedia.__table__.c.filename.type
    assert isinstance(column_type, String)
    assert column_type.length == MAX_FILENAME_LENGTH


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("quote.pdf", "quote.pdf"),
        ("  quote.pdf  ", "quote.pdf"),
        (f"con{NUL}tract.pdf", "contract.pdf"),
        (f"a{BELL}b{ESCAPE}c{DELETE}.pdf", "abc.pdf"),
        (f"x{LONE_SURROGATE}.pdf", "x�.pdf"),
        (f"{ARABIC}.pdf", f"{ARABIC}.pdf"),
        ("", None),
        (f"{NUL}{BELL}", None),
    ],
)
def test_an_inbound_name_is_normalised_never_refused(raw: str, expected: str | None) -> None:
    assert display_filename(raw) == expected


def test_a_non_string_name_is_no_name() -> None:
    assert display_filename(12345) is None
    assert display_filename(None) is None


def test_decomposed_and_composed_spellings_become_one() -> None:
    decomposed = unicodedata.normalize("NFD", "café.pdf")
    assert display_filename(decomposed) == "café.pdf"


@pytest.mark.parametrize("length", [MAX_FILENAME_LENGTH + 1, 2_000, 10_000])
def test_a_long_inbound_name_keeps_its_extension_within_the_bound(length: int) -> None:
    name = "a" * (length - 4) + ".pdf"
    shortened = display_filename(name)
    assert shortened is not None
    assert len(shortened) == MAX_FILENAME_LENGTH
    assert shortened.endswith(".pdf")


def test_a_long_arabic_name_is_bounded_in_characters() -> None:
    name = ARABIC * 200 + ".pdf"
    shortened = display_filename(name)
    assert shortened is not None and len(shortened) <= MAX_FILENAME_LENGTH
    assert shortened.endswith(".pdf")
    assert shortened.startswith(ARABIC)


def test_a_name_at_the_bound_is_kept_exactly() -> None:
    name = "a" * (MAX_FILENAME_LENGTH - 4) + ".pdf"
    assert display_filename(name) == name


def test_a_long_name_with_no_short_extension_is_cut_plainly() -> None:
    name = "a" * 400 + "." + "b" * 40
    assert display_filename(name) == name[:MAX_FILENAME_LENGTH]


@pytest.mark.parametrize(
    "raw",
    [
        f"contract{NUL}.pdf",
        f"contract{BELL}.pdf",
        "a" * (MAX_FILENAME_LENGTH - 3) + ".pdf",
    ],
)
def test_an_outbound_name_that_cannot_be_stored_as_given_is_refused(raw: str) -> None:
    with pytest.raises(UnstorableFilenameError):
        require_storable_filename(raw)


def test_an_outbound_name_is_canonicalised_when_it_is_storable() -> None:
    name = "a" * (MAX_FILENAME_LENGTH - 4) + ".pdf"
    assert require_storable_filename(name) == name
    assert require_storable_filename(f"  {ARABIC}.pdf ") == f"{ARABIC}.pdf"
    assert require_storable_filename(None) is None
    assert require_storable_filename("   ") is None
