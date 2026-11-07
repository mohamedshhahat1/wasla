"""Run the real Wasla api or worker with the e2e fakes installed in-process (E2E-01, E2E-03).

    python -m tests.e2e_stack.launcher api       # app.serve, the production entry point
    python -m tests.e2e_stack.launcher worker    # app.workers.runner

Nothing under ``app/`` changes for the e2e stack. What this process changes,
before the application is imported:

- the socket wall and the provider reroute (`tests.e2e_stack.guard`);
- the two provider URLs a *browser* is sent to, so they point at the fakes
  service's published port instead of Google and Paymob:
  `app.integrations.google.client.GOOGLE_AUTHORIZATION_URL` and the checkout
  host of `app.integrations.billing.paymob.REGIONS["egypt"]`;
- api only: WhatsApp ownership verification uses
  `tests.fake_ownership.FakeOwnershipVerifier`, which owns every phone number id
  starting ``PNID-E2E``;
- worker only: the email provider is `tests.e2e_stack.mailbox.CapturingEmailProvider`,
  so a message stays readable in the mailbox after it is sent.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from app.core.dependencies import SessionDep, SettingsDep
from app.services.credential_service import CredentialService
from app.services.messaging_limit_service import MessagingLimitService
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.e2e_stack.guard import install_reroute, install_socket_wall
from tests.fake_ownership import FakeOwnershipVerifier

FAKES_PUBLIC = os.environ.get("WASLA_E2E_FAKES_PUBLIC_URL", "http://localhost:8026").rstrip("/")
OWNED_PREFIX = "PNID-E2E"


class _OwnsTheE2ENumbers(dict[str, str]):
    """`FakeOwnershipVerifier`'s ownership table: every e2e phone number id, one WABA."""

    def get(self, key: Any, default: Any = None) -> Any:
        return "555000111" if str(key).startswith(OWNED_PREFIX) else default


def _browser_urls() -> None:
    from app.integrations.billing import paymob
    from app.integrations.google import client as google_client

    google_client.GOOGLE_AUTHORIZATION_URL = f"{FAKES_PUBLIC}/google/authorize"  # type: ignore[misc]
    api_base, _ = paymob.REGIONS["egypt"]
    paymob.REGIONS["egypt"] = (api_base, f"{FAKES_PUBLIC}/paymob")


_VERIFIER = FakeOwnershipVerifier(owned=_OwnsTheE2ENumbers())


def whatsapp_accounts(settings: SettingsDep, session: SessionDep) -> WhatsAppAccountService:
    """`get_whatsapp_account_service`, with the fake ownership verifier."""
    return WhatsAppAccountService(
        session=session,
        ownership=_VERIFIER,
        credentials=CredentialService(settings),
        default_plan_code=settings.default_plan_code,
        limits=MessagingLimitService(session, settings=settings),
    )


def _api() -> None:
    from app.api.dependencies import get_whatsapp_account_service
    from app.main import app as application
    from app.serve import main as serve

    application.dependency_overrides[get_whatsapp_account_service] = whatsapp_accounts
    serve(["--host", "0.0.0.0", "--port", os.environ.get("PORT", "8000")])  # noqa: S104


def _worker() -> None:
    from app.workers import email_worker, runner
    from tests.e2e_stack.mailbox import CapturingEmailProvider

    # Replaced where the email worker looks it up, as a test's monkeypatch would.
    vars(email_worker)["build_email_provider"] = lambda settings: CapturingEmailProvider()
    asyncio.run(runner.main())


def main(argv: list[str]) -> None:
    install_socket_wall()
    install_reroute()
    _browser_urls()
    mode = argv[0] if argv else "api"
    if mode == "api":
        _api()
    elif mode == "worker":
        _worker()
    else:
        raise SystemExit(f"unknown mode {mode!r}: api or worker")


if __name__ == "__main__":
    main(sys.argv[1:])
