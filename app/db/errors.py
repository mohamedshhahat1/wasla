"""Telling a database's refusal of *content* from its refusal to *work*.

The webhook needs this distinction to be exact. PostgreSQL refusing a value -
too long for its column, a NUL in `text`, a number out of range - is a fact
about one message, permanent, and retrying the delivery will fail identically
for as long as Meta keeps retrying (MSG-03). PostgreSQL being down, a dropped
connection, a deadlock or a serialisation failure is the opposite: transient,
and exactly what Meta's retry exists for.

The route used to draw the line with `except DataError`, and under asyncpg no
data exception is ever a `DataError`: value-too-long, NUL and overflow all
surface as the generic `DBAPIError` (MEDIA-05). The handler was dead code and
the retry storm it was written to prevent was back. What does identify the
class reliably is the SQLSTATE PostgreSQL attaches - class `22`, "data
exception" - which both drivers expose, and which asyncpg's client-side checks
report as `22000` too. `DBAPIError` is the gate, and the SQLSTATE is the
decision; nothing outside class 22 is ever treated as permanent content.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.exc import DBAPIError

# PostgreSQL's SQLSTATE class for a value the server will not accept as data.
DATA_EXCEPTION_CLASS: Final = "22"


def sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE behind a SQLAlchemy database error, if it carries one."""
    original = getattr(error, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if isinstance(value, str) and value:
                return value
    return None


def is_data_exception(error: BaseException) -> bool:
    """Whether PostgreSQL refused a value rather than the request.

    True only for a `DBAPIError` whose SQLSTATE is in class 22. A connection
    failure, a deadlock, a serialisation failure, an integrity violation and a
    programming error all answer False, so they keep their own semantics.
    """
    if not isinstance(error, DBAPIError):
        return False
    state = sqlstate(error)
    return state is not None and state.startswith(DATA_EXCEPTION_CLASS)


__all__ = ["DATA_EXCEPTION_CLASS", "is_data_exception", "sqlstate"]
