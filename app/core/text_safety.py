"""Whether a piece of text can be stored, indexed and shown to a model.

Three inputs reached the database as 500s or as junk before this (RAG-11):

- **NUL** (`U+0000`) is valid in a JSON string and in a Python `str`, and
  PostgreSQL's `text` cannot hold it - the flush fails with
  `CharacterNotInRepertoireError` and the request with it.
- **A lone surrogate** (`"\\ud800"`) is valid JSON escape syntax and not a
  character at all: encoding it for the content hash raises
  `UnicodeEncodeError`.
- **Text with nothing visible in it** - zero-width spaces, direction marks,
  whitespace - passed the "not blank" check, was chunked, embedded and served
  as a passage of three `U+200B`.

The policy, stated because it is a choice rather than a sanitiser's accident:

- NUL and unencodable text are **refused**, never silently stripped from what a
  person submitted - they are a client bug or a probe, and the uploader should
  hear about it.
- Text must contain at least one character that is not a control, format,
  separator, unassigned, private-use or bare combining character.
- Every other control character stays legal. Tabs and newlines are structure;
  an escape sequence copied from a terminal is ugly and harmless. None of it can
  corrupt what it passes through: logs never carry document text, the API and
  the model receive it JSON-encoded, and a retrieved passage reaches the model
  only as structured tool output (RAG-12).
"""

from __future__ import annotations

import unicodedata
from typing import Final

# Categories that carry no searchable content on their own: controls (Cc),
# formats such as zero-width and direction marks (Cf), surrogates (Cs),
# private use (Co), unassigned (Cn), separators (Zs, Zl, Zp) and combining marks
# with nothing to combine with (Mn, Me).
INVISIBLE_CATEGORIES: Final = frozenset(
    {"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp", "Mn", "Me"}
)


def storable_problem(value: str) -> str | None:
    """Why `value` cannot be stored as text, or None if it can."""
    if "\x00" in value:
        return "must not contain NUL characters"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "must be valid Unicode text"
    return None


def has_visible_content(value: str) -> bool:
    """Whether any character in `value` is something a reader could see."""
    return any(unicodedata.category(character) not in INVISIBLE_CATEGORIES for character in value)


__all__ = ["INVISIBLE_CATEGORIES", "has_visible_content", "storable_problem"]
