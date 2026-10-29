"""How knowledge text is normalised: for storage, for matching, for lexical search.

Three forms, because they serve three readers (RQ-01, FX-1.1, IMP-03):

`for_storage` is what a document *is* once it is in Wasla - the text the model
is shown, the text `content_hash` is computed over. NFKC, because a PDF's text
layer can hold Arabic as presentation-form glyphs (U+FB50..U+FDFF,
U+FE70..U+FEFF) that look right and share no code point with what a customer
types (RV-01); and with the invisible controls removed - NUL, zero-width
characters, bidi embeddings and isolates - which carry no meaning a reader sees
and make two equal-looking strings unequal.

`for_matching` is what is compared: the embedding input of every chunk and of
every query (RV-23). On top of `for_storage` it removes tatweel and Arabic
diacritics, maps Arabic-Indic and Persian digits to ASCII - NFKC does *not*
unify `٤٥٠٠` with `4500` - and folds the Persian letter forms that some PDF
fonts emit for Arabic yeh, kaf and heh (RR-13: Chromium's text layer here came
back as presentation forms whose NFKC targets were U+06CC, U+06A9 and U+06BE,
which left five of nine query words unmatched after NFKC alone).

`for_lexical` folds further - alef forms, alef maqsura, teh marbuta, case - for
an exact-word channel only. It is never stored and never embedded: folding that
aggressively merges distinct words, which a lexical match tolerates and a
display text must not.

Pure functions, no I/O; each is idempotent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# Invisible characters that change equality without changing what is read:
# NUL; zero-width space, non-joiner, joiner; BOM / zero-width no-break space;
# left-to-right and right-to-left marks; bidi embeddings and overrides; bidi
# isolates.
_INVISIBLE: Final = re.compile("[\x00\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_WHITESPACE_RUN: Final = re.compile(r"[ \t\f\v\u00a0]+")
_BLANK_RUN: Final = re.compile(r"\n{3,}")
_TATWEEL_AND_MARKS: Final = re.compile("[\u0640\u064b-\u065f\u0670]")

_DIGITS_AND_LETTERS: Final = str.maketrans(
    {
        **{chr(0x0660 + n): str(n) for n in range(10)},  # Arabic-Indic
        **{chr(0x06F0 + n): str(n) for n in range(10)},  # Persian (extended)
        "\u06cc": "\u064a",  # Farsi yeh -> Arabic yeh
        "\u06a9": "\u0643",  # keheh -> kaf
        "\u06be": "\u0647",  # heh doachashmee -> heh
    }
)
_LEXICAL_FOLD: Final = str.maketrans(
    {
        "\u0623": "\u0627",  # alef with hamza above
        "\u0625": "\u0627",  # alef with hamza below
        "\u0622": "\u0627",  # alef with madda
        "\u0671": "\u0627",  # alef wasla
        "\u0649": "\u064a",  # alef maqsura -> yeh
        "\u0629": "\u0647",  # teh marbuta -> heh
    }
)


def normalise_whitespace(text: str) -> str:
    """Tidy whitespace without destroying structure.

    Runs of spaces and tabs collapse and trailing space goes, but line breaks
    survive and blank lines survive: they are the boundaries the chunker
    depends on. Three or more newlines carry no more meaning than two.
    """
    lines = [
        _WHITESPACE_RUN.sub(" ", line).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def for_storage(text: str) -> str:
    """The text a document is stored, hashed and shown as."""
    # Invisible controls go first: removed after NFKC, one standing between two
    # combining marks leaves them adjacent in an order NFKC would have changed,
    # and the result is no longer its own normal form.
    return normalise_whitespace(unicodedata.normalize("NFKC", _INVISIBLE.sub("", text)))


def for_matching(text: str) -> str:
    """The text a passage or a query is embedded as."""
    folded = _TATWEEL_AND_MARKS.sub("", for_storage(text)).translate(_DIGITS_AND_LETTERS)
    # Removing a mark can leave a doubled space where it stood between two.
    return normalise_whitespace(folded)


def for_lexical(text: str) -> str:
    """The text an exact-word channel indexes. Never stored as display text."""
    return for_matching(text).translate(_LEXICAL_FOLD).casefold()


__all__ = ["for_lexical", "for_matching", "for_storage", "normalise_whitespace"]
