"""Work that may run only once the transaction that called for it has committed (RGM-16).

A job published inside a transaction can be claimed before the rows it names
are visible - a worker then chases a document that does not exist yet - and a
transaction that rolls back after the publish leaves a job for a document that
never existed. So a caller *defers* the publish instead: `defer` parks it on the
session, the session's own `after_commit` event moves it to "committed", a
rollback drops it, and the code that committed runs it with `run_committed`.

Two places commit a unit of work and so call `run_committed`: `CommittingRoute`
(a request) and `Database.session` (a worker, a command, a test harness). The
action runs once, in the order it was deferred, after the commit has returned
- never before, never after a rollback.

An action that fails is logged and the rest still run: the transaction has
already committed, so there is nothing left to undo, and a request must not
answer 5xx for work that landed. The caller's own recovery covers it - for
indexing jobs, `IngestionRecoveryWorker` re-publishes what was committed and
never queued.

Only the outermost commit counts, and only the outermost rollback drops: a
savepoint's release is not a commit, and its rollback undoes only what it held.
An action deferred *inside* a savepoint that is later rolled back is therefore
still run if the outer transaction commits - defer only after the rows the
action names are written in the outer transaction, as the knowledge service
does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging import get_logger

logger = get_logger(__name__)

Action = Callable[[], Awaitable[object]]

_PENDING: Final = "wasla.after_commit.pending"
_COMMITTED: Final = "wasla.after_commit.committed"


def defer(session: AsyncSession, action: Action) -> None:
    """Run `action` once this session's current transaction has committed."""
    session.sync_session.info.setdefault(_PENDING, []).append(action)


async def run_committed(session: AsyncSession) -> int:
    """Run what the last commit released, in order. Returns how many ran."""
    actions: list[Action] = session.sync_session.info.pop(_COMMITTED, [])
    for action in actions:
        try:
            await action()
        except Exception:
            logger.exception("after_commit.action_failed")
    return len(actions)


def _committed(session: Session) -> None:
    # SQLAlchemy dispatches this for a savepoint's release too; only the
    # outermost transaction's commit makes anything visible to another session.
    if session.in_nested_transaction():
        return
    pending: list[Action] = session.info.pop(_PENDING, [])
    if pending:
        session.info.setdefault(_COMMITTED, []).extend(pending)


def _rolled_back(session: Session) -> None:
    # And for a savepoint's rollback, which undoes only what the savepoint held:
    # an action deferred before it began still belongs to the outer transaction.
    if session.in_nested_transaction():
        return
    session.info.pop(_PENDING, None)


event.listen(Session, "after_commit", _committed)
event.listen(Session, "after_rollback", _rolled_back)


__all__ = ["defer", "run_committed"]
