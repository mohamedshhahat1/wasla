"""Seed the e2e stack (E2E-05). Idempotent: a second run changes nothing.

    python -m tests.e2e_stack.seed            the platform staff account (J8)
    python -m tests.e2e_stack.seed --demo     also a ready workspace, through the API

The plans the frontend's journeys need (``pro`` at 99 EGP for J5) are seeded by
the migrations themselves, so nothing else is created by default.

The staff account is written straight to the database - a platform role is
never granted through the API - with a **public test password**, printed here
and in README.md. It is a test value for a stack that is never deployed.

``--demo`` (off by default, never used by the smoke) builds what FE-3 and FE-4
develop against: a verified owner, a workspace, a WhatsApp number through the
fake ownership verifier, a default agent on the scripted model, and a knowledge
base with one small document. It goes through the running API and the mailbox,
exactly as a person would.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

STAFF_EMAIL = "staff.e2e@example.com"
STAFF_PASSWORD = "e2e-staff-password-NOT-A-SECRET"
DEMO_EMAIL = "demo.owner.e2e@example.com"
DEMO_PASSWORD = "e2e-demo-password-NOT-A-SECRET"
DEMO_SLUG = "e2e-demo"
API = os.environ.get("WASLA_E2E_API_INTERNAL", "http://api:8000") + "/api/v1"
MAILBOX = os.environ.get("WASLA_E2E_MAILBOX_INTERNAL", "http://mailbox:8025")


async def seed_staff() -> bool:
    """Create the platform owner if absent. True when something was written."""
    from app.core.config import get_settings
    from app.core.security import hash_password
    from app.db.models import PlatformRole, User
    from app.db.session import Database

    database = Database(get_settings())
    try:
        async with database.session() as session:
            found = await session.scalar(select(User).where(User.email == STAFF_EMAIL))
            if found is not None:
                return False
            session.add(
                User(
                    email=STAFF_EMAIL,
                    full_name="E2E Staff",
                    hashed_password=hash_password(STAFF_PASSWORD),
                    is_active=True,
                    email_verified_at=datetime.now(UTC),
                    platform_role=PlatformRole.PLATFORM_OWNER,
                )
            )
            await session.commit()
            return True
    finally:
        await database.dispose()


def _code(client: httpx.Client, address: str) -> str:
    for _ in range(60):
        for message in client.get(f"{MAILBOX}/messages", params={"to": address}).json():
            if message["codes"]:
                return str(message["codes"][0])
        time.sleep(1)
    raise RuntimeError(f"no code reached the mailbox for {address}")


def seed_demo() -> bool:
    """Build the demo workspace through the API. True when something was created."""
    with httpx.Client(timeout=30, trust_env=False) as client:
        login = client.post(
            f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        if login.status_code == 200:
            return False
        registered = client.post(
            f"{API}/auth/register",
            json={
                "email": DEMO_EMAIL,
                "password": DEMO_PASSWORD,
                "full_name": "Demo Owner",
                "workspace_name": "E2E Demo",
                "workspace_slug": DEMO_SLUG,
                "timezone": "Africa/Cairo",
            },
        )
        registered.raise_for_status()
        session = client.post(
            f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        ).json()
        bearer = {"Authorization": f"Bearer {session['access_token']}"}
        client.post(f"{API}/auth/email/verification/send", headers=bearer).raise_for_status()
        client.post(
            f"{API}/auth/email/verification/verify",
            headers=bearer,
            json={"code": _code(client, DEMO_EMAIL)},
        ).raise_for_status()
        client.post(
            f"{API}/whatsapp/accounts",
            headers=bearer,
            json={
                "phone_number_id": "PNID-E2E-DEMO",
                "access_token": "e2e-fake-system-user-token",
                "display_name": "Demo line",
            },
        ).raise_for_status()
        agent = client.post(
            f"{API}/agents",
            headers=bearer,
            json={"name": "Demo agent", "system_prompt": "You answer customers of E2E Demo."},
        )
        agent.raise_for_status()
        agent_id = agent.json()["id"]
        for tool in ("search_knowledge", "request_human_handoff"):
            client.put(
                f"{API}/agents/{agent_id}/tools", headers=bearer, json={"name": tool}
            ).raise_for_status()
        client.patch(
            f"{API}/agents/{agent_id}", headers=bearer, json={"status": "active"}
        ).raise_for_status()
        client.post(f"{API}/agents/{agent_id}/default", headers=bearer).raise_for_status()
        base = client.post(f"{API}/knowledge/bases", headers=bearer, json={"name": "Demo facts"})
        base.raise_for_status()
        client.post(
            f"{API}/knowledge/bases/{base.json()['id']}/documents/upload",
            headers=bearer,
            data={"title": "Prices"},
            files={"file": ("prices.txt", b"The price of a 150 m2 finishing is 500000 EGP.\n")},
        ).raise_for_status()
        return True


def main(argv: list[str]) -> None:
    created = asyncio.run(seed_staff())
    print(  # noqa: T201 - this script's output is its interface
        f"staff {'created' if created else 'already present'}: "
        f"{STAFF_EMAIL} / {STAFF_PASSWORD} (public test values)"
    )
    if "--demo" in argv:
        made = seed_demo()
        print(  # noqa: T201
            f"demo {'created' if made else 'already present'}: "
            f"{DEMO_EMAIL} / {DEMO_PASSWORD}, workspace {DEMO_SLUG}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
