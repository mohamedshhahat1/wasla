"""What each environment is allowed to be lax about, and what it is not.

Two carried-over findings with one cause: a guard written as `is_production`
when the property it protects is "can somebody on the internet reach this".
`staging` is a first-class tier here and **must** be internet-reachable,
because Meta delivers webhooks to it - so a check that names production
exactly leaves the tier most likely to hold real customer data behind the
weakest door.

- **F-5.** With no `META_APP_SECRET`, staging accepted unsigned WhatsApp
  deliveries. The payload names `phone_number_id`, so an attacker chose which
  workspace to write into: contacts created, messages injected, agent jobs
  enqueued - and injected text is read by the agent as customer input, which
  makes it a prompt-injection channel with outbound sends as the effect.
- **F-6.** A 500 outside production carried the exception class and message.

Both are now decided by `Settings.is_developer_environment`, a *closed* list of
two rather than a test for one - so a tier added later is on the safe side by
default. That is the same correction `_validate_hardening` already applied to
the signing key.

The table at the bottom is the finding's own matrix, executed rather than
described.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.v1.webhooks import _require_signature, get_ingestion_service
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.exceptions import DependencyUnavailableError
from app.main import create_app
from app.services.whatsapp_service import IngestionOutcome
from tests.conftest import FakeDependency

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "an-app-secret-for-tests"
VERIFY_TOKEN = "a-verify-token"
# Long enough to satisfy the signing-key rule in every environment but `test`,
# which is what lets a `staging` Settings be constructed here at all.
JWT_SECRET = "a-staging-signing-key-that-is-long-enough-to-be-accepted-here"

DELIVERY: dict[str, Any] = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"phone_number_id": "109876543210"},
                        "messages": [{"from": "2012", "id": "wamid.one", "type": "text"}],
                    }
                }
            ]
        }
    ],
}

# Every environment the application declares, split by what it is allowed to
# do. Written as one list so a fifth environment added later has to be placed
# here deliberately - the two tests at the bottom read it.
DEVELOPER = ("local", "test")
INTERNET_REACHABLE = ("staging", "production")


class StubIngestion:
    """Counts what got through. A call here means the guard let it past."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def ingest(self, payload: dict[str, Any]) -> IngestionOutcome:
        self.calls.append(payload)
        return IngestionOutcome(stored=1)


def _settings(environment: str, *, app_secret: str | None = APP_SECRET) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        jwt_secret=JWT_SECRET,
        meta_app_secret=app_secret,
        meta_verify_token=VERIFY_TOKEN,
        cors_origins=["https://app.example.com"],
        docs_enabled=False,
        debug=False,
        rate_limit_enabled=False,
    )


def _body() -> bytes:
    return json.dumps(DELIVERY).encode("utf-8")


def _signature(body: bytes, *, secret: str = APP_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
def ingestion(app: FastAPI) -> StubIngestion:
    stub = StubIngestion()
    app.dependency_overrides[get_ingestion_service] = lambda: stub
    return stub


async def _post(
    app: FastAPI,
    environment: str,
    *,
    app_secret: str | None = APP_SECRET,
    headers: dict[str, str] | None = None,
) -> int:
    app.dependency_overrides[get_settings_from_state] = lambda: _settings(
        environment, app_secret=app_secret
    )
    body = _body()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            PATH,
            content=body,
            headers=headers or {"Content-Type": "application/json"},
        )
    return response.status_code


# ------------------------------------------------- F-5, one case at a time


async def test_staging_refuses_an_unsigned_delivery(app: FastAPI, ingestion: StubIngestion) -> None:
    """The finding itself. Staging answered 200 and stored the payload."""
    status = await _post(app, "staging")

    assert status == 403
    assert ingestion.calls == []


async def test_staging_refuses_a_delivery_signed_with_the_wrong_key(
    app: FastAPI, ingestion: StubIngestion
) -> None:
    """A signature is only a check if a wrong one is refused."""
    status = await _post(app, "staging", headers=_signature(_body(), secret="not-the-app-secret"))

    assert status == 403
    assert ingestion.calls == []


async def test_staging_with_no_configured_secret_fails_closed(
    app: FastAPI, ingestion: StubIngestion
) -> None:
    """503 rather than 200, and rather than 403.

    A deployment that cannot authenticate a delivery must refuse every one -
    but the refusal has to be the kind Meta retries, because a briefly
    misconfigured staging tier should not silently drop real traffic. That is
    the same answer the Paymob and Resend endpoints already give, and this is
    the branch that used to log a warning and continue.
    """
    status = await _post(app, "staging", app_secret=None)

    assert status == 503
    assert ingestion.calls == []


def test_production_refuses_to_start_without_the_app_secret() -> None:
    """Production never reached the runtime branch, and this is why.

    `_validate_hardening` refuses the configuration outright, which is stronger
    than answering 503 - the process does not serve at all. Asserted here
    because it is what makes the runtime `is_production` check unreachable in
    production, and therefore what makes staging the only tier the fail-open
    branch could ever have run in.

    Deliberately *not* extended to staging: a staging tier that has not
    connected WhatsApp yet should still boot, and the endpoint answering 503 is
    the right answer for it rather than a refusal to start the whole process.
    """
    with pytest.raises(ValidationError, match="META_APP_SECRET"):
        _settings("production", app_secret=None)


async def test_production_would_also_refuse_the_delivery_at_runtime(
    app: FastAPI, ingestion: StubIngestion
) -> None:
    """Defence in depth, reached by asking the guard directly.

    The configuration gate above means this can only be provoked by calling
    `_require_signature` with settings the validator would not have produced -
    which is the point: the runtime guard must not be relying on the startup
    one, because the startup one is production-only.
    """
    with pytest.raises(DependencyUnavailableError):
        _require_signature(
            body=_body(),
            header=None,
            settings=_settings("test", app_secret=None).model_copy(
                update={"environment": "production"}
            ),
        )
    assert ingestion.calls == []


async def test_a_correctly_signed_staging_delivery_is_accepted(
    app: FastAPI, ingestion: StubIngestion
) -> None:
    """The positive control, and the reason the refusals above mean anything.

    A guard that refused everything would pass all four tests before this one.
    """
    status = await _post(app, "staging", headers=_signature(_body()))

    assert status == 200
    assert len(ingestion.calls) == 1


async def test_local_may_still_run_without_meta_credentials(
    app: FastAPI, ingestion: StubIngestion
) -> None:
    """The developer shortcut survives, because it is what it was for.

    Exercising the inbound flow on a laptop with no Meta account is the whole
    reason the fail-open branch exists. What was wrong was its reach, not its
    existence.
    """
    status = await _post(app, "local", app_secret=None)

    assert status == 200
    assert len(ingestion.calls) == 1


async def test_the_test_environment_may_too(app: FastAPI, ingestion: StubIngestion) -> None:
    """The suite's own fixtures depend on this, so it is pinned rather than assumed."""
    status = await _post(app, "test", app_secret=None)

    assert status == 200
    assert len(ingestion.calls) == 1


@pytest.mark.parametrize("environment", INTERNET_REACHABLE)
async def test_no_internet_reachable_environment_accepts_an_unsigned_delivery(
    app: FastAPI, ingestion: StubIngestion, environment: str
) -> None:
    """The matrix, driven from the list rather than written out twice.

    A fifth environment added to `INTERNET_REACHABLE` gets these cases for
    free, which is the point: the finding was a policy expressed once, in one
    `if`, about one environment.

    The missing-secret column is asserted through the guard rather than the
    endpoint, because a production `Settings` with no secret cannot be built -
    see `test_production_refuses_to_start_without_the_app_secret`.
    """
    unsigned = await _post(app, environment)
    assert unsigned == 403

    with pytest.raises(DependencyUnavailableError):
        _require_signature(
            body=_body(),
            header=None,
            settings=_settings("test", app_secret=None).model_copy(
                update={"environment": environment}
            ),
        )
    assert ingestion.calls == []


@pytest.mark.parametrize("environment", DEVELOPER)
def test_only_local_and_test_are_developer_environments(environment: str) -> None:
    """The property both fixes read, asserted in both directions.

    Closed rather than open: `is_developer_environment` names the two that may
    be lax, so an environment nobody thought about is refused rather than
    trusted. `is_production` was the open shape, and F-5 and F-6 are what it
    cost.
    """
    assert _settings(environment).is_developer_environment is True
    for reachable in INTERNET_REACHABLE:
        assert _settings(reachable).is_developer_environment is False


def test_the_environment_list_is_exhaustive() -> None:
    """Non-vacuity for the two parametrised tests above.

    If `Environment` gains a value that appears in neither tuple, the matrix
    silently stops covering it. This is what fails instead.
    """
    from typing import get_args

    from app.core.config import Environment

    assert set(get_args(Environment)) == set(DEVELOPER) | set(INTERNET_REACHABLE)
    assert len(DEVELOPER) == 2
    assert len(INTERNET_REACHABLE) == 2


# ------------------------------------------------------------------- F-6


class LeakyError(Exception):
    """Carries something that must not reach a response body."""


async def _error_body(environment: str) -> dict[str, Any]:
    """One 500 from an application built for this environment.

    Built rather than overridden: `register_exception_handlers` reads
    `app.state.settings` once, when the handler is registered, so a dependency
    override cannot reach it - and a test that thought it could would report
    whatever the fixture's environment happened to be.
    """
    application = create_app(_settings(environment))
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = FakeDependency(name="redis")

    @application.get("/__boom__")
    async def boom() -> None:  # pragma: no cover - raises by design
        raise LeakyError("connection to postgres://wasla:hunter2@db.internal:5432 refused")

    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/__boom__")
    assert response.status_code == 500
    return dict(response.json())


async def test_a_staging_server_error_says_nothing_about_the_exception() -> None:
    """F-6. The body carried the class name and the message.

    The message here is the shape that makes it a finding rather than an
    untidiness: an exception raised deep in a driver quotes the thing it could
    not reach, and that is a hostname and sometimes a credential.
    """
    body = await _error_body("staging")

    assert body["error"]["code"] == "internal_error"
    assert body["error"].get("details") is None
    rendered = json.dumps(body)
    assert "LeakyError" not in rendered
    assert "postgres://" not in rendered
    assert "hunter2" not in rendered
    assert "db.internal" not in rendered


async def test_a_production_server_error_is_unchanged() -> None:
    """Production always did this. Asserted so the fix is a widening."""
    body = await _error_body("production")

    assert body["error"].get("details") is None
    assert "LeakyError" not in json.dumps(body)


async def test_a_local_server_error_still_says_what_broke() -> None:
    """The positive control, and the reason the two above are not vacuous.

    A handler that had simply stopped emitting details would pass every
    assertion above. Somebody debugging on a laptop keeps the exception.
    """
    body = await _error_body("local")

    details = body["error"]["details"]
    assert details["exception"] == "LeakyError"
    assert "refused" in details["message"]


async def test_every_server_error_still_carries_a_request_id() -> None:
    """What replaces the detail for somebody reading a staging incident.

    The exception is still logged; what the caller gets is the identifier that
    finds it. Suppressing the body without that would trade a disclosure for an
    unanswerable support ticket.
    """
    for environment in INTERNET_REACHABLE:
        body = await _error_body(environment)
        assert body["error"]["request_id"]
