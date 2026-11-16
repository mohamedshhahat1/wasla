"""Repository base classes.

Repositories are the only place database queries are written. Services and
routes never build them, which is what makes it possible to guarantee a filter
is always applied: it is applied in exactly one place.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TenantIsolationError
from app.core.logging import get_logger
from app.db.base import Base

logger = get_logger(__name__)


# PEP 695 type parameters: the bound keeps a repository pinned to a mapped
# class, so `model` and every query helper stay tied to the same table.
class BaseRepository[ModelT: Base]:
    """Data access for one model within one session."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    def _select(self) -> Select[tuple[ModelT]]:
        """Starting point for every read in this repository."""
        return select(self.model)

    async def _first(self, statement: Select[tuple[ModelT]]) -> ModelT | None:
        result = await self._session.execute(statement.limit(1))
        return result.scalars().first()

    async def _all(self, statement: Select[tuple[ModelT]]) -> list[ModelT]:
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    def add(self, entity: ModelT) -> ModelT:
        """Stage a row for insertion.

        Repositories never commit. A request is one unit of work, so the
        transaction boundary belongs to the caller.
        """
        self._session.add(entity)
        return entity


class TenantScopedRepository[ModelT: Base](BaseRepository[ModelT], ABC):
    """Data access confined to a single tenant.

    The tenant id is supplied once, from the authenticated context, and is
    fixed for the lifetime of the repository. It is never read from request
    input. Reads all start from :meth:`_select`, which carries the tenant
    predicate, and writes take their tenant from :attr:`tenant_id`, so no query
    made through this class can reach another tenant's rows.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        super().__init__(session)
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    @abstractmethod
    def _tenant_filter(self) -> ColumnElement[bool]:
        """Predicate restricting this model to :attr:`tenant_id`.

        Declaring it is mandatory: a subclass that omits it cannot be
        instantiated at all.
        """

    def _select(self) -> Select[tuple[ModelT]]:
        return select(self.model).where(self._tenant_filter())

    async def _require(self, statement: Select[tuple[ModelT]]) -> ModelT:
        """Fetch a row that must exist inside this tenant.

        A miss raises :class:`TenantIsolationError`, which answers 404 with the
        generic not-found message. Whether the row exists in some other tenant
        is deliberately not checked: that answer is itself information.
        """
        entity = await self._first(statement)
        if entity is None:
            logger.warning(
                "repository.scoped_row_missing",
                extra={
                    "event": "repository.scoped_row_missing",
                    "model": self.model.__name__,
                    "tenant_id": str(self._tenant_id),
                },
            )
            raise TenantIsolationError()
        return entity


def escape_like(value: str) -> str:
    """`value` matched literally inside a LIKE pattern escaped with a backslash."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: The letters a Turkish keyboard writes that a search treats as their plain
#: neighbour (TF-02): the four i-letters are one letter, and s-cedilla, soft g,
#: c-cedilla and the two umlauts match the letter under the mark. Folded before
#: `lower()`, because PostgreSQL lower-cases a dotted capital I to an i plus a
#: combining dot under some collations, which then matches nothing.
_FOLD_FROM: Final = "\u0130I\u0131\u015e\u015f\u011e\u011f\u00c7\u00e7\u00d6\u00f6\u00dc\u00fc"
_FOLD_TO: Final = "iiissggccoouu"


def folded(expression: Any) -> ColumnElement[str]:
    """`expression` as every name search compares it: Turkish letters folded, then lower-cased."""
    return func.lower(func.translate(expression, _FOLD_FROM, _FOLD_TO))


def contains_folded(column: Any, term: str) -> ColumnElement[bool]:
    """Whether `column` contains `term`, case-insensitively and with Turkish letters folded.

    The one predicate behind every name search - contacts, leads and the inbox
    (TF-02) - applied to both sides, so "ipek" finds a dotted capital I and
    "ISIK" a dotless one, in every workspace. `term` is matched literally:
    `%` and `_` are ordinary text.
    """
    return folded(column).like(folded(f"%{escape_like(term)}%"), escape="\\")
