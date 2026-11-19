"""The languages a Wasla account and its e-mail can be in (FEB-09, ADR-201).

One list, read by the database constraint, the request schemas and the e-mail
renderer, so a language cannot be accepted by one of them and refused by
another. English is the default everywhere a language is not chosen: an empty
preference renders exactly the text the product sent before languages existed.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

Locale = Literal["en", "ar", "fr", "tr"]

SUPPORTED_LOCALES: Final[tuple[str, ...]] = get_args(Locale)
DEFAULT_LOCALE: Final = "en"
MAX_LOCALE_LENGTH: Final = 8


def locale_check_sql(column: str) -> str:
    """The CHECK body for a nullable language column: empty, or one of the list."""
    allowed = ", ".join(f"'{code}'" for code in SUPPORTED_LOCALES)
    return f"{column} IS NULL OR {column} IN ({allowed})"
