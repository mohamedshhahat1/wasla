"""Expected database conflicts are answered as such, never as a 500 (DB-017).

The audit saw a deadlock and a unique violation both surface as the generic
`internal_error`: an operator retrying a manual payment was told the platform
had broken, and a provider retrying a callback was told nothing it could use.
Contention now answers 503 with `Retry-After`; a duplicate answers 409. Neither
body names a SQLSTATE, a constraint or a statement - those describe the schema.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import register_exception_handlers
from app.db.errors import RETRYABLE_STATES, is_retryable, is_unique_violation


class _DriverError(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(f'duplicate key value violates unique constraint "uq_secret" ({state})')
        self.sqlstate = state


def _error(state: str) -> DBAPIError:
    return DBAPIError("UPDATE invoices SET amount_paid = $1", {}, _DriverError(state))


def _app(state: str) -> FastAPI:
    app = FastAPI()
    # A deployed environment: the generic handler shows exception details only
    # to a developer, and these tests assert what everybody else sees.
    app.state.settings = SimpleNamespace(is_developer_environment=False)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise _error(state)

    return app


@pytest.mark.parametrize("state", sorted(RETRYABLE_STATES))
async def test_contention_answers_503_with_a_retry_hint(state: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(state)), base_url="http://t") as http:
        response = await http.get("/boom")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    body = response.json()
    assert body["error"]["code"] == "retryable_conflict"
    assert state not in response.text
    assert "invoices" not in response.text and "uq_secret" not in response.text


async def test_a_unique_violation_answers_409_without_the_constraint() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app("23505")), base_url="http://t") as http:
        response = await http.get("/boom")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "uq_secret" not in response.text and "23505" not in response.text


async def test_any_other_refusal_is_still_an_internal_error() -> None:
    """A trigger refusing money (23000) is a defect to see, not a conflict to retry."""
    async with AsyncClient(transport=ASGITransport(app=_app("23000")), base_url="http://t") as http:
        response = await http.get("/boom")
    assert response.status_code == 500
    assert "uq_secret" not in response.text


def test_the_classification_is_by_sqlstate_only() -> None:
    assert is_retryable(_error("40P01"))
    assert not is_retryable(_error("23505"))
    assert is_unique_violation(_error("23505"))
    assert not is_unique_violation(ValueError("23505"))
