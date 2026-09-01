"""The whole money path, over a real socket, with a real login.

This is the sequence that was walked by hand against a live Paymob test account
- register, choose a plan, open a checkout, receive the callback, watch the
invoice settle and the entitlements follow. That walk is not repeatable in CI
and proves nothing about tomorrow's code, so this is the automated form of it.

**What is real here, and what is not.** The application is real and running on a
loopback socket under uvicorn. Authentication is real: a token minted by
`POST /auth/register` is the only thing these requests carry. Routing,
authorization, the committing-route boundary, the webhook's signature check and
every database write are real. The single thing replaced is the provider's
socket, because the alternative is a test that charges somebody.

**Why a real socket rather than the in-process ASGI transport**, which the rest
of the suite uses and which is much faster: that transport awaits the entire
application call, dependency teardown included, before handing back a response.
It cannot observe ordering. The money path has a genuine ordering hazard - the
checkout endpoint hands out a payment id that the provider will quote back, and
if the response were sent before that row were durable, a fast callback would
arrive for a payment this system had not yet heard of. `CommittingRoute` is what
prevents that, and only a real socket can show it working.

The writes here are real and are not rolled back, so the test tidies up after
itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.session import Database
from app.integrations.billing import paymob
from app.integrations.billing.paymob import hmac_signature
from app.main import create_app

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

PASSWORD = "correct horse battery staple"
REDIS_URL = "redis://localhost:6379/14"
HMAC_SECRET = "an-end-to-end-hmac-secret"
CLIENT_SECRET = "csk_test_endtoend"
INTENTION_ID = "pi_test_endtoend"
INTEGRATION_ID = 5885262
AMOUNT_CENTS = 2500


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fake_provider_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer Paymob without reaching Paymob.

    Patched onto the provider class rather than injected through a dependency
    override, because the point of this file is that the *real* dependency
    graph runs - and that graph builds its own provider from settings.
    """
    original = paymob.PaymobProvider.__init__

    def handler(request: httpx.Request) -> httpx.Response:
        if "intention" in str(request.url):
            return httpx.Response(
                201,
                json={"id": INTENTION_ID, "client_secret": CLIENT_SECRET},
            )
        return httpx.Response(200, json={"id": 900000001, "success": True, "pending": False})

    def patched(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        original(self, **kwargs)

    monkeypatch.setattr(paymob.PaymobProvider, "__init__", patched)


@contextlib.asynccontextmanager
async def _serving(database_url: str) -> AsyncIterator[str]:
    """The real application, configured to take payments, on a real socket."""
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        rate_limit_enabled=False,
        billing_provider="paymob",
        paymob_secret_key="sk_test_endtoend000000",
        paymob_public_key="pk_test_endtoend000000",
        paymob_hmac_secret=HMAC_SECRET,
        paymob_integration_ids=[INTEGRATION_ID],
        app_public_url="https://e2e.example.com",
    )
    app = create_app(settings)
    database = Database(settings)
    redis = RedisClient(settings)
    app.state.database = database
    app.state.redis = redis

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(600):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - the server failed to come up
            raise RuntimeError("the test server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=15)
        await database.dispose()
        await redis.close()


@pytest.fixture
def scratch_engine(prepared_database: str) -> AsyncEngine:
    """A pool the test reads and writes through, outside the server."""
    return create_async_engine(prepared_database, poolclass=NullPool)


async def _seed_plan(engine: AsyncEngine, code: str) -> None:
    """A plan priced in the integration's currency.

    EGP rather than the platform default, because a Paymob integration is
    issued per currency and an intention whose currency disagrees with it is
    refused - which is the mistake this catches before anybody meets it.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO plans (id, code, name, price, currency, interval, trial_days,"
                " limits, is_public, is_active, sort_order, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :code, 'E2E Pro', 25.00, 'EGP', 'monthly', 0,"
                " '{\"agents\": 7}'::jsonb, true, true, 1, now(), now())"
            ),
            {"code": code},
        )


async def _seed_default_plan(engine: AsyncEngine) -> None:
    """The free tier `DEFAULT_PLAN_CODE` names, as migration 0016 seeds it.

    Registration puts a new workspace on this plan, so without it these tests
    run against a workspace with no subscription at all - which is a real state
    (a deployment whose catalogue is missing) but not the one being tested. The
    scratch database is built from model metadata rather than by running
    migrations, so the seeded catalogue is not there.

    `ON CONFLICT DO NOTHING` because it is shared catalogue data: several tests
    register against the same database, and the first one to arrive seeds it.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO plans (id, code, name, price, currency, interval, trial_days,"
                " limits, is_public, is_active, sort_order, created_at, updated_at)"
                " VALUES (gen_random_uuid(), 'starter', 'Starter', 0.00, 'EGP', 'monthly', 0,"
                " '{\"agents\": 1}'::jsonb, true, true, 0, now(), now())"
                " ON CONFLICT (code) DO NOTHING"
            )
        )


async def _forget(engine: AsyncEngine, *, slug: str, email: str, plan_code: str) -> None:
    """Remove what the server committed.

    The rolled-back `db_session` fixture is no use here: the server writes on
    its own connections and those writes are real.

    Every statement is a literal with bound parameters. The values are all
    generated by this test and could not be hostile, but building SQL by
    concatenation is a habit rather than a decision, and this file is not the
    place to keep it.
    """
    by_tenant = "DELETE FROM {table} WHERE tenant_id IN (SELECT id FROM tenants WHERE slug = :slug)"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM payment_events WHERE payment_id IN ("
                " SELECT id FROM payments WHERE tenant_id IN ("
                " SELECT id FROM tenants WHERE slug = :slug))"
            ),
            {"slug": slug},
        )
        for table in ("payments", "payment_methods", "invoices", "subscriptions", "audit_logs"):
            await connection.execute(
                text(by_tenant.format(table=table)),
                {"slug": slug},
            )
        await connection.execute(
            text(
                "DELETE FROM memberships WHERE user_id IN"
                " (SELECT id FROM users WHERE email = :email)"
            ),
            {"email": email},
        )
        await connection.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})
        await connection.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})
        await connection.execute(text("DELETE FROM plans WHERE code = :code"), {"code": plan_code})


def _transaction(*, reference: str, transaction: int = 900000001, **overrides) -> dict:
    """A callback shaped exactly like the documented one."""
    body = {
        "id": transaction,
        "pending": False,
        "amount_cents": AMOUNT_CENTS,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": INTEGRATION_ID,
        "has_parent_transaction": False,
        "order": {"id": 1, "merchant_order_id": reference},
        "created_at": "2026-08-29T11:33:44.592345",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    body.update(overrides)
    return body


async def _deliver(client: httpx.AsyncClient, transaction: dict) -> httpx.Response:
    """Post a signed callback the way the provider posts one."""
    return await client.post(
        "/api/v1/webhooks/paymob",
        params={"hmac": hmac_signature(transaction, secret=HMAC_SECRET)},
        content=json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


async def test_paying_a_plan_settles_the_invoice_and_moves_the_entitlements(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complete money path, through real authentication and a real socket.

    Every step is a request nobody has privileged access to: the token comes
    from registration, the checkout is authorised by role, and the callback
    arrives unauthenticated and is believed only because it is signed.
    """
    _fake_provider_socket(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    slug, email = f"e2e-{suffix}", f"e2e-{suffix}@wasla-example.com"
    plan_code = f"e2e-pro-{suffix}"
    await _seed_default_plan(scratch_engine)
    await _seed_plan(scratch_engine, plan_code)

    try:
        async with (
            _serving(prepared_database) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=30) as client,
        ):
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": "E2E Co",
                    "workspace_slug": slug,
                },
            )
            assert registered.status_code == 201
            auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}

            # The plan cannot simply be asked for (ADR-059). A real
            # authorization decision happens first - this token is a workspace
            # owner because registration made its holder one - and the refusal
            # that follows is commercial rather than a permission problem.
            asked = await client.post(
                "/api/v1/billing/subscription/plan",
                json={"plan_code": plan_code},
                headers=auth,
            )
            assert asked.status_code == 402, asked.text
            assert asked.json()["error"]["code"] == "payment_required"

            before = await client.get("/api/v1/billing/subscription", headers=auth)
            was = {row["key"]: row["limit"] for row in before.json()["entitlements"]}
            assert was["agents"] != 7, "the paid plan's allowance must not apply yet"

            started = await client.post(
                "/api/v1/billing/checkout",
                json={"plan_code": plan_code},
                headers=auth,
            )
            assert started.status_code == 201, started.text
            checkout = started.json()
            assert checkout["amount"] == "25.00"
            assert checkout["currency"] == "EGP"

            # The durability property, and the reason this file needs a
            # real socket. The provider is about to quote this id back at
            # us; the row behind it must already be visible.
            polled = await client.get(
                f"/api/v1/billing/payments/{checkout['payment_id']}",
                headers=auth,
            )
            assert polled.status_code == 200
            assert polled.json()["status"] == "pending"

            # The callback carries no credential of ours and is believed
            # only because the signature checks out.
            delivered = await _deliver(client, _transaction(reference=checkout["payment_id"]))
            assert delivered.status_code == 200
            assert delivered.json() == {"status": "received"}

            settled = await client.get(
                f"/api/v1/billing/payments/{checkout['payment_id']}",
                headers=auth,
            )
            assert settled.json()["status"] == "succeeded"

            invoice = await client.get(
                f"/api/v1/invoices/{checkout['invoice_id']}",
                headers=auth,
            )
            assert invoice.status_code == 200
            assert invoice.json()["status"] == "paid"
            assert invoice.json()["amount_paid"] == "25.00"
            assert invoice.json()["outstanding"] == "0.00"

            # The commercial invariant, at the far end of the money path: the
            # plan a customer could not ask for is theirs now, and the only
            # thing that granted it was a signed callback saying the invoice
            # was paid.
            state = await client.get("/api/v1/billing/subscription", headers=auth)
            assert state.json()["subscription"]["plan"]["code"] == plan_code
            assert state.json()["subscription"]["status"] == "active"
            limits = {row["key"]: row["limit"] for row in state.json()["entitlements"]}
            assert limits["agents"] == 7, "the paid plan's allowance is what applies"
    finally:
        await _forget(scratch_engine, slug=slug, email=email, plan_code=plan_code)
        await scratch_engine.dispose()


async def test_a_declined_payment_leaves_the_workspace_exactly_as_it_was(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same path, ending in a decline, asserted through the API.

    The dangerous decline bug is not a crash - it is an invoice that settles
    anyway. Checked here through the endpoints a customer would read, so a
    regression shows up as the product being given away rather than as a
    failing internal assertion.
    """
    _fake_provider_socket(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    slug, email = f"e2e-{suffix}", f"e2e-{suffix}@wasla-example.com"
    plan_code = f"e2e-pro-{suffix}"
    await _seed_default_plan(scratch_engine)
    await _seed_plan(scratch_engine, plan_code)

    try:
        async with (
            _serving(prepared_database) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=30) as client,
        ):
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": "E2E Co",
                    "workspace_slug": slug,
                },
            )
            auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            # No plan selection: a priced plan is only ever granted by
            # settlement, so the checkout below is the whole of the request.
            checkout = (
                await client.post(
                    "/api/v1/billing/checkout",
                    json={"plan_code": plan_code},
                    headers=auth,
                )
            ).json()

            declined = await _deliver(
                client,
                _transaction(
                    reference=checkout["payment_id"],
                    success=False,
                    error_occured=True,
                    data={"message": "Insufficient funds"},
                ),
            )
            assert declined.status_code == 200

            payment = (
                await client.get(
                    f"/api/v1/billing/payments/{checkout['payment_id']}",
                    headers=auth,
                )
            ).json()
            assert payment["status"] == "failed"
            assert payment["failure_reason"] == "Insufficient funds"

            invoice = (
                await client.get(
                    f"/api/v1/invoices/{checkout['invoice_id']}",
                    headers=auth,
                )
            ).json()
            assert invoice["status"] == "open"
            assert invoice["amount_paid"] == "0.00"
            assert invoice["outstanding"] == "25.00"
    finally:
        await _forget(scratch_engine, slug=slug, email=email, plan_code=plan_code)
        await scratch_engine.dispose()


async def test_a_forged_callback_cannot_settle_anything_over_a_real_socket(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is public, so this is the attack it exists to survive.

    A correct payload naming a real payment, with a signature the attacker does
    not have. Asserted through the API afterwards rather than in the database,
    because "did the customer get the product" is the question that matters.
    """
    _fake_provider_socket(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    slug, email = f"e2e-{suffix}", f"e2e-{suffix}@wasla-example.com"
    plan_code = f"e2e-pro-{suffix}"
    await _seed_default_plan(scratch_engine)
    await _seed_plan(scratch_engine, plan_code)

    try:
        async with (
            _serving(prepared_database) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=30) as client,
        ):
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": "E2E Co",
                    "workspace_slug": slug,
                },
            )
            auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            # No plan selection: a priced plan is only ever granted by
            # settlement, so the checkout below is the whole of the request.
            checkout = (
                await client.post(
                    "/api/v1/billing/checkout",
                    json={"plan_code": plan_code},
                    headers=auth,
                )
            ).json()

            body = json.dumps(
                {"type": "TRANSACTION", "obj": _transaction(reference=checkout["payment_id"])}
            ).encode("utf-8")

            unsigned = await client.post(
                "/api/v1/webhooks/paymob",
                content=body,
                headers={"content-type": "application/json"},
            )
            forged = await client.post(
                "/api/v1/webhooks/paymob",
                params={"hmac": "a" * 128},
                content=body,
                headers={"content-type": "application/json"},
            )
            # The payload altered but signed with the *original* body's
            # digest: the signature no longer matches the bytes.
            relayed = await client.post(
                "/api/v1/webhooks/paymob",
                params={
                    "hmac": hmac_signature(
                        _transaction(reference=checkout["payment_id"]),
                        secret=HMAC_SECRET,
                    )
                },
                content=json.dumps(
                    {
                        "type": "TRANSACTION",
                        "obj": {
                            **_transaction(reference=checkout["payment_id"]),
                            "amount_cents": 1,
                        },
                    }
                ).encode("utf-8"),
                headers={"content-type": "application/json"},
            )

            # And the layer behind it: a callback signed correctly but
            # naming the wrong figure. That one *is* verified, so it
            # answers 200 like every other verified outcome - and settles
            # nothing, which is the assertion at the end.
            mismatched = await _deliver(
                client,
                {
                    **_transaction(reference=checkout["payment_id"]),
                    "amount_cents": 1,
                },
            )

            assert unsigned.status_code == 403
            assert forged.status_code == 403
            assert relayed.status_code == 403
            assert mismatched.status_code == 200

            invoice = (
                await client.get(
                    f"/api/v1/invoices/{checkout['invoice_id']}",
                    headers=auth,
                )
            ).json()
            assert invoice["status"] == "open"
            assert invoice["amount_paid"] == "0.00"
    finally:
        await _forget(scratch_engine, slug=slug, email=email, plan_code=plan_code)
        await scratch_engine.dispose()


async def test_a_retried_callback_settles_the_invoice_once(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider retries anything it did not get a 2xx for.

    Driven over HTTP so the whole stack participates, and asserted on the
    figure: settling twice would show 50.00 collected against 25.00 due.
    """
    _fake_provider_socket(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    slug, email = f"e2e-{suffix}", f"e2e-{suffix}@wasla-example.com"
    plan_code = f"e2e-pro-{suffix}"
    await _seed_default_plan(scratch_engine)
    await _seed_plan(scratch_engine, plan_code)

    try:
        async with (
            _serving(prepared_database) as base_url,
            httpx.AsyncClient(base_url=base_url, timeout=30) as client,
        ):
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": "E2E Co",
                    "workspace_slug": slug,
                },
            )
            auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            # No plan selection: a priced plan is only ever granted by
            # settlement, so the checkout below is the whole of the request.
            checkout = (
                await client.post(
                    "/api/v1/billing/checkout",
                    json={"plan_code": plan_code},
                    headers=auth,
                )
            ).json()
            callback = _transaction(reference=checkout["payment_id"])

            for _ in range(3):
                assert (await _deliver(client, callback)).status_code == 200

            invoice = (
                await client.get(
                    f"/api/v1/invoices/{checkout['invoice_id']}",
                    headers=auth,
                )
            ).json()
            assert invoice["status"] == "paid"
            assert Decimal(invoice["amount_paid"]) == Decimal("25.00")
    finally:
        await _forget(scratch_engine, slug=slug, email=email, plan_code=plan_code)
        await scratch_engine.dispose()
