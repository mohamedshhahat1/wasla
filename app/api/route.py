"""The route class that closes the request's unit of work.

A request commits **before** its response is emitted, and this is where that
happens.

The alternative — committing in the session dependency's teardown, which is what
this application did — is wrong in a way that is invisible in tests and obvious
in production. A ``yield`` dependency's teardown runs *after* the response has
reached the client. With a commit costing a network round trip and an fsync,
that gap is tens of milliseconds, and it was measured at 25-75 ms against a
containerised PostgreSQL: a token minted by ``POST /auth/register`` was refused
by ``GET /auth/me`` for the whole of it, because the user row was not yet
visible.

The timing is the symptom. The defect is that **the API answered `201 Created`
before the write was durable** — so a commit that failed afterwards would leave
the caller holding a success for something that never happened, with nothing in
the response to say so.

It is invisible in tests because the in-process ASGI transport awaits the entire
application call, teardown included, before handing back a response object.
There is no ordering to observe. Only a real socket can tell the difference,
which is why `tests/integration/test_commit_boundary.py` runs one.

Rollback stays with the session context manager in `app.db.session`. An
exception unwinds through it and the transaction is discarded — that belongs
outside the handler, because a handler that raised is in no position to decide
anything.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import SESSION_STATE_ATTRIBUTE


class CommittingRoute(APIRoute):
    """Commits the request's session once the handler has produced a response.

    Only a route that actually used a session has one parked on the request, so
    a handler that touched no database — the health endpoints, the webhook
    verification challenge — costs nothing here.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def commit_after(request: Request) -> Response:
            response = await handler(request)
            session = getattr(request.state, SESSION_STATE_ATTRIBUTE, None)
            if isinstance(session, AsyncSession) and session.in_transaction():
                # Nothing swallows a failure here. A commit that cannot complete
                # must reach the error handlers and become a 5xx, because the
                # only worse answer than a failed request is a successful one
                # describing a write that did not land.
                await session.commit()
            return response

        return commit_after
