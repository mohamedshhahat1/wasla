"""Walk J1-J10 against a running e2e stack, from the API side (FE-0, E2E-07).

    python -m tests.e2e_stack.smoke [--api http://127.0.0.1:8000] [--mailbox ...] [--fakes ...]

Not a pytest module (no ``test_`` prefix): it needs the stack of
``docker-compose.e2e.yml`` running. One fresh set of accounts per run. Prints one
table row per journey - PASSED, EXPECTED UNTIL FE-2 (J6), or NOT WALKED with
the backend gap that blocks the step - then the signed-forward lines, and exits
non-zero if any journey FAILED.

Every authentication call is signed for a made-up browser address with the
stack's public ``SESSION_FORWARD_SECRET``, exactly as the frontend tier will
(FEB-01): all of this script's traffic arrives from one address, and without
the forward it would share one ten-a-minute bucket.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.client_ip_forward import (
    FORWARD_IP_HEADER,
    FORWARD_SIG_HEADER,
    FORWARD_TS_HEADER,
    sign_forward,
)
from app.core.config import Settings
from app.core.security import TokenType, create_access_token, decode_token
from tests.e2e_stack.mailbox import code_after, code_mails

FORWARD_SECRET = "e2e-not-a-secret-0123456789abcdef0123456789abcdef"
META_APP_SECRET = b"e2e-meta-app-secret-NOT-A-SECRET"
PASSWORD = "Correct-Horse-Battery-e2e-9"
STAFF = ("staff.e2e@example.com", "e2e-staff-password-NOT-A-SECRET")
PG_CONTAINER = "wasla-fe-e2e-pg"
# docker-compose.e2e.yml's public, made-up signing key: the smoke re-signs a real
# account's token for a workspace it never joined, to prove a stranger learns
# nothing (FEB-07, WG-03).
E2E_JWT_SECRET = "e2e-public-jwt-secret-NOT-A-SECRET-0123456789abcdef"
AI_REPLY = "Hello from the Wasla e2e agent."
SEARCH_REPLY = "Here is what I found."


def _e2e_settings() -> Settings:
    return Settings(_env_file=None, environment="test", jwt_secret=E2E_JWT_SECRET)


def workspace_of(token: str) -> str:
    """The workspace an access token names."""
    claims = decode_token(token, settings=_e2e_settings(), expected_type=TokenType.ACCESS)
    return str(claims.tenant_id)


def error_code(response: httpx.Response) -> str | None:
    """The error envelope's code, or None for a body without one."""
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


def token_for_another_workspace(token: str, tenant_id: str) -> str:
    """The same account and token version, signed for a workspace it never joined."""
    settings = _e2e_settings()
    claims = decode_token(token, settings=settings, expected_type=TokenType.ACCESS)
    forged, _ = create_access_token(
        settings=settings,
        subject=claims.subject,
        tenant_id=uuid.UUID(tenant_id),
        token_version=claims.token_version,
    )
    return forged


def stored_media_id(conversation: str) -> str:
    """The newest stored attachment of a conversation, read from the database.

    FEB-03: no message the API returns carries its media's id, so a client -
    this smoke included - cannot find it. The frontend will need FEB-03; the
    smoke reads the stack's own database through `docker exec` instead.
    """
    conversation_id = uuid.UUID(conversation)  # refuses anything that is not a UUID
    docker = shutil.which("docker") or "docker"
    query = (
        "select id from message_media "  # noqa: S608
        f"where conversation_id = '{conversation_id}' and storage_state = 'stored' "
        "order by created_at desc limit 1;"
    )
    return subprocess.run(  # noqa: S603 - fixed argv, the stack's own container
        [docker, "exec", "-i", PG_CONTAINER, "psql", "-U", "wasla", "-d", "wasla", "-tA"],
        input=query,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


class JourneyError(AssertionError):
    pass


def check(condition: bool, what: str) -> None:
    if not condition:
        raise JourneyError(what)


@dataclass
class Result:
    name: str
    status: str = "FAILED"
    note: str = ""


@dataclass
class Stream:
    """An event stream read in the background; every frame kept."""

    frames: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None

    def events(self, kind: str) -> list[str]:
        return [frame for frame in self.frames if f"event: {kind}" in frame]

    async def wait_for(self, kind: str, *, contains: str = "", within: float = 45) -> str:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            for frame in self.events(kind):
                if contains in frame:
                    return frame
            await asyncio.sleep(0.25)
        raise JourneyError(f"no {kind} {contains!r} frame within {within}s: {self.frames[-3:]}")

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()


class Smoke:
    def __init__(self, api: str, mailbox: str, fakes: str) -> None:
        self.base = api.rstrip("/") + "/api/v1"
        self.mailbox = mailbox.rstrip("/")
        self.fakes = fakes.rstrip("/")
        self.tag = uuid.uuid4().hex[:6]
        self.http = httpx.AsyncClient(timeout=30, trust_env=False)
        self.addresses = (f"198.18.{n // 250}.{n % 250 + 1}" for n in range(10_000))
        self.state: dict[str, Any] = {}

    # ------------------------------------------------------------- plumbing

    def signed(self, method: str, path: str, address: str | None = None) -> dict[str, str]:
        address = address or next(self.addresses)
        ts = str(int(time.time()))
        return {
            FORWARD_IP_HEADER: address,
            FORWARD_TS_HEADER: ts,
            FORWARD_SIG_HEADER: sign_forward(
                FORWARD_SECRET, ts=ts, client_ip=address, method=method, path=path
            ),
        }

    async def call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        *,
        sign: bool = False,
        address: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        headers.setdefault("Origin", "http://localhost:3000")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if sign:
            headers.update(self.signed(method, "/api/v1" + path.split("?")[0], address))
        return await self.http.request(method, self.base + path, headers=headers, **kwargs)

    async def mail(self, address: str, *, want: str = "codes", within: float = 60) -> str:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            messages = (
                await self.http.get(f"{self.mailbox}/messages", params={"to": address})
            ).json()
            for message in messages:
                if message[want]:
                    return str(message[want][0])
            await asyncio.sleep(0.5)
        raise JourneyError(f"no {want} for {address} in the mailbox")

    async def _messages(self, address: str) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.mailbox}/messages", params={"to": address})
        return list(response.json())

    async def code_mails_now(self, address: str, *, at_least: int, within: float = 60) -> int:
        """How many code mails `address` holds, once it holds `at_least` (FEB-32)."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            count = code_mails(await self._messages(address))
            if count >= at_least:
                return count
            await asyncio.sleep(0.5)
        raise JourneyError(f"fewer than {at_least} code mails for {address}")

    async def code_of_own_send(self, address: str, *, seen: int, within: float = 60) -> str:
        """The code of the mail this caller's send caused: newer than the `seen` before it."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            code = code_after(await self._messages(address), seen=seen)
            if code is not None:
                return code
            await asyncio.sleep(0.5)
        raise JourneyError(f"no new code for {address} in the mailbox")

    async def stream(self, token: str) -> Stream:
        ticket = await self.call("POST", "/events/ticket", token)
        check(ticket.status_code == 201, f"ticket {ticket.status_code}")
        reader = Stream()
        url = f"{self.base}/events/stream?ticket={ticket.json()['ticket']}"

        async def read() -> None:
            async with self.http.stream(
                "GET", url, headers={"Accept": "text/event-stream"}, timeout=None
            ) as response:
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        reader.frames.append(frame)

        reader.task = asyncio.create_task(read())
        await asyncio.sleep(0.5)
        return reader

    async def account(self, who: str) -> tuple[str, str, str]:
        """Register, verify and sign in a new owner. Returns (email, token, slug)."""
        email = f"{who}-{self.tag}@example.com"
        slug = f"{who}-{self.tag}"
        address = next(self.addresses)
        registered = await self.call(
            "POST",
            "/auth/register",
            sign=True,
            address=address,
            json={
                "email": email,
                "password": PASSWORD,
                "full_name": who.title(),
                "workspace_name": f"WS {who}",
                "workspace_slug": slug,
                "timezone": "Africa/Cairo",
            },
        )
        check(registered.status_code == 202, f"register {registered.status_code}")
        token = await self.login(email, address=address)
        # Registration mails a code of its own; it is counted before the send, so
        # the send's code is the one read even when registration's arrives late.
        seen = await self.code_mails_now(email, at_least=1)
        sent = await self.call("POST", "/auth/email/verification/send", token)
        check(sent.status_code in {200, 202, 204}, f"verification send {sent.status_code}")
        code = await self.code_of_own_send(email, seen=seen)
        verified = await self.call(
            "POST", "/auth/email/verification/verify", token, json={"code": code}
        )
        check(verified.status_code == 200, f"verify {verified.status_code} {verified.text[:120]}")
        return email, token, slug

    async def login(self, email: str, password: str = PASSWORD, **kwargs: Any) -> str:
        response = await self.call(
            "POST",
            "/auth/login",
            sign=True,
            address=kwargs.pop("address", None),
            json={"email": email, "password": password, **kwargs},
        )
        check(response.status_code == 200, f"login {response.status_code} {response.text[:120]}")
        token: str = response.json()["access_token"]
        return token

    async def webhook(self, phone_number_id: str, message: dict[str, Any], sender: str) -> None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "555000111",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "201000000000",
                                    "phone_number_id": phone_number_id,
                                },
                                "contacts": [{"profile": {"name": "Customer"}, "wa_id": sender}],
                                "messages": [
                                    {
                                        "from": sender,
                                        "id": f"wamid.IN{uuid.uuid4().hex[:12]}",
                                        "timestamp": str(int(time.time())),
                                        **message,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(META_APP_SECRET, raw, hashlib.sha256).hexdigest()
        response = await self.http.post(
            f"{self.base}/webhooks/whatsapp",
            content=raw,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        check(response.status_code == 200, f"webhook {response.status_code}")

    async def conversation_ids(self, token: str) -> set[str]:
        listed = (await self.call("GET", "/conversations?limit=100", token)).json()
        return {str(item["id"]) for item in listed.get("items", [])}

    async def new_conversation(self, token: str, known: set[str], within: float = 30) -> str:
        """The conversation an inbound message just opened (FEB-04: no handle to match on)."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            fresh = await self.conversation_ids(token) - known
            if fresh:
                return fresh.pop()
            await asyncio.sleep(0.5)
        raise JourneyError("no conversation appeared")

    async def outbound_text(
        self, token: str, conversation: str, text: str, within: float = 45
    ) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            listed = (
                await self.call("GET", f"/conversations/{conversation}/messages?limit=50", token)
            ).json()
            for item in listed.get("items", []):
                if item.get("direction") == "outbound" and text in str(item.get("body") or ""):
                    return
            await asyncio.sleep(0.5)
        raise JourneyError(f"no outbound {text!r} within {within}s")

    # ------------------------------------------------------------- journeys

    async def j1(self) -> str:
        email, token, slug = await self.account("owner")
        self.state.update(owner=email, token=token, slug=slug)
        me = await self.call("GET", "/auth/me", token)
        check(me.status_code == 200 and bool(me.json().get("email_verified_at")), "me")
        made = await self.call(
            "POST",
            "/workspaces",
            token,
            sign=True,
            json={"name": "Second", "slug": f"second-{self.tag}", "timezone": "Africa/Cairo"},
        )
        check(made.status_code == 201, f"create workspace {made.status_code} {made.text[:120]}")
        return "register 202, mailbox code, verify 200, me verified, workspace 201"

    async def _agent(self, token: str) -> None:
        agent = await self.call(
            "POST", "/agents", token, json={"name": "E2E agent", "system_prompt": "Be brief."}
        )
        check(agent.status_code == 201, f"agent {agent.status_code} {agent.text[:160]}")
        agent_id = agent.json()["id"]
        for tool in ("search_knowledge", "request_human_handoff"):
            granted = await self.call(
                "PUT", f"/agents/{agent_id}/tools", token, json={"name": tool}
            )
            check(granted.status_code in {200, 201}, f"grant {tool} {granted.status_code}")
        active = await self.call("PATCH", f"/agents/{agent_id}", token, json={"status": "active"})
        check(active.status_code == 200, f"activate {active.status_code} {active.text[:160]}")
        default = await self.call("POST", f"/agents/{agent_id}/default", token)
        check(default.status_code == 200, f"default {default.status_code}")

    async def j2(self) -> str:
        token = self.state["token"]
        pnid = f"PNID-E2E-{self.tag}"
        self.state["pnid"] = pnid
        connected = await self.call(
            "POST",
            "/whatsapp/accounts",
            token,
            json={"phone_number_id": pnid, "access_token": "e2e-fake-token", "display_name": "L1"},
        )
        check(
            connected.status_code == 201, f"connect {connected.status_code} {connected.text[:200]}"
        )
        await self._agent(token)
        stream = await self.stream(token)
        try:
            known = await self.conversation_ids(token)
            await self.webhook(
                pnid, {"type": "text", "text": {"body": "Hello there"}}, "201000000101"
            )
            await stream.wait_for("message.created", contains="inbound")
            conversation = await self.new_conversation(token, known)
            self.state["conversation"] = conversation
            await self.outbound_text(token, conversation, AI_REPLY)
            mode = await self.call(
                "POST",
                f"/conversations/{conversation}/mode",
                token,
                json={"mode": "human", "handoff_reason": "owner takes over"},
            )
            check(mode.status_code == 200, f"take over {mode.status_code}")
            await stream.wait_for("conversation.updated")
            key = str(uuid.uuid4())
            first = await self.call(
                "POST",
                f"/conversations/{conversation}/messages",
                token,
                json={"body": "Hi, this is the owner"},
                headers={"Idempotency-Key": key},
            )
            again = await self.call(
                "POST",
                f"/conversations/{conversation}/messages",
                token,
                json={"body": "Hi, this is the owner"},
                headers={"Idempotency-Key": key},
            )
            check(first.status_code == 201, f"reply {first.status_code} {first.text[:160]}")
            check(again.json().get("id") == first.json().get("id"), "retry gave another id")
        finally:
            stream.close()
        return (
            "connect 201, signed webhook, message.created, scripted AI reply, "
            "takeover, same id on retry"
        )

    async def j3(self) -> str:
        token = self.state["token"]
        mate = f"mate-{self.tag}@example.com"
        invited = await self.call(
            "POST", "/invitations", token, json={"email": mate, "role": "member"}
        )
        check(invited.status_code == 201, f"invite {invited.status_code}")
        link = await self.mail(mate, want="links")
        query = parse_qs(urlsplit(link).query)
        check("token" in query, "no token in the invitation link")
        accepted = await self.call(
            "POST",
            "/invitations/accept",
            sign=True,
            json={"token": query["token"][0], "password": PASSWORD, "full_name": "Mate"},
        )
        check(accepted.status_code == 200, f"accept {accepted.status_code} {accepted.text[:160]}")
        mate_token = await self.login(mate, workspace_slug=self.state["slug"])
        seen = await self.code_mails_now(mate, at_least=0)
        await self.call("POST", "/auth/email/verification/send", mate_token)
        code = await self.code_of_own_send(mate, seen=seen)
        verified = await self.call(
            "POST", "/auth/email/verification/verify", mate_token, json={"code": code}
        )
        check(verified.status_code == 200, f"mate verify {verified.status_code}")
        refused = await self.call(
            "POST", "/invitations", mate_token, json={"email": "x@example.com", "role": "member"}
        )
        billing = await self.call("GET", "/billing/summary", mate_token)
        allowed = await self.call("GET", "/conversations", mate_token)
        check(refused.status_code == 403 and billing.status_code == 403, "member limits")
        check(allowed.status_code == 200, f"member conversations {allowed.status_code}")
        stream = await self.stream(mate_token)
        try:
            me = (await self.call("GET", "/auth/me", mate_token)).json()
            revoked = await self.call("DELETE", f"/workspace/members/{me['id']}", token)
            check(revoked.status_code == 200, f"revoke {revoked.status_code}")
            await stream.wait_for("closed", contains="membership_revoked", within=60)
        finally:
            stream.close()
        after = await self.call("GET", "/conversations", mate_token)
        check(
            after.status_code == 403 and error_code(after) == "membership_revoked",
            f"after revoke {after.status_code} {after.text[:160]}",
        )
        gates = await self.call("GET", "/workspace/gates", mate_token)
        check(
            gates.status_code == 403 and error_code(gates) == "membership_revoked",
            f"gates after revoke {gates.status_code}",
        )
        # Somebody who never belonged: the staff account, its own token re-signed
        # for this workspace. The same 404 a random workspace id gets (WG-03).
        staff = await self.login(*STAFF)
        stranger = await self.call(
            "GET", "/conversations", token_for_another_workspace(staff, workspace_of(token))
        )
        nowhere = await self.call(
            "GET", "/conversations", token_for_another_workspace(staff, str(uuid.uuid4()))
        )
        check(
            stranger.status_code == nowhere.status_code == 404
            and error_code(stranger) == error_code(nowhere) == "not_found",
            f"stranger {stranger.status_code} / random {nowhere.status_code}",
        )
        return (
            "invite 201, mailbox link, accept 200, member 403/403/200, "
            "stream closed membership_revoked, then 403 membership_revoked (gates too); "
            "a stranger 404 not_found = a random id"
        )

    async def j4(self) -> str:
        token = self.state["token"]
        base = await self.call("POST", "/knowledge/bases", token, json={"name": "Facts"})
        check(base.status_code == 201, f"knowledge base {base.status_code} {base.text[:160]}")
        base_id = base.json()["id"]
        uploaded = await self.call(
            "POST",
            f"/knowledge/bases/{base_id}/documents/upload",
            token,
            data={"title": "Prices"},
            files={"file": ("prices.txt", b"The price of a 150 m2 finishing is 500000 EGP.\n")},
        )
        check(uploaded.status_code == 202, f"upload {uploaded.status_code} {uploaded.text[:160]}")
        document = uploaded.json()["document"]["id"]
        statuses = [uploaded.json()["document"].get("status")]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and statuses[-1] != "ready":
            await asyncio.sleep(1)
            listed = (
                await self.call("GET", f"/knowledge/documents?knowledge_base_id={base_id}", token)
            ).json()
            found = [item for item in listed.get("items", []) if item["id"] == document]
            if found and found[0]["status"] != statuses[-1]:
                statuses.append(found[0]["status"])
            check(statuses[-1] != "failed", "document failed")
        check(statuses[-1] == "ready", f"document statuses {statuses}")
        known = await self.conversation_ids(token)
        await self.webhook(
            self.state["pnid"],
            {"type": "text", "text": {"body": "What is the price?"}},
            "201000000104",
        )
        conversation = await self.new_conversation(token, known)
        await self.outbound_text(token, conversation, SEARCH_REPLY, within=60)
        return (
            f"base 201, upload 202, statuses {' -> '.join(statuses)}, AI turn used search_knowledge"
        )

    async def j5(self) -> str:
        token = self.state["token"]
        plans = (await self.call("GET", "/billing/plans", token)).json()
        check(any(plan.get("code") == "pro" for plan in plans), "no pro plan")
        started = await self.call(
            "POST",
            "/billing/checkout",
            token,
            json={"plan_code": "pro", "idempotency_key": str(uuid.uuid4())},
        )
        check(started.status_code == 201, f"checkout {started.status_code} {started.text[:200]}")
        payment = started.json()["payment_id"]
        redirect = started.json()["redirect_url"]
        check(redirect.startswith(self.fakes + "/paymob/"), f"redirect {redirect}")
        page = await self.http.get(redirect, follow_redirects=False)
        check(page.status_code == 302, f"fake pay page {page.status_code}")
        check("/billing/checkout/return" in page.headers["location"], "return URL")
        status = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and status != "succeeded":
            status = (await self.call("GET", f"/billing/payments/{payment}", token)).json()[
                "status"
            ]
            await asyncio.sleep(0.5)
        check(status in {"succeeded", "paid"}, f"payment status {status}")
        summary = (await self.call("GET", "/billing/summary", token)).json()
        plan = (summary.get("subscription") or {}).get("plan_code") or json.dumps(
            summary.get("subscription")
        )
        check("pro" in str(plan), f"subscription after payment: {plan}")
        return f"checkout 201, fake pay page 302 to return, payment {status}, subscription pro"

    async def j6(self) -> str:
        # Its own account: the race signs every session of the account out.
        racer, _, _ = await self.account("racer")
        address = next(self.addresses)
        response = await self.call(
            "POST",
            "/auth/login",
            sign=True,
            address=address,
            json={"email": racer, "password": PASSWORD},
        )
        refresh_token = response.json()["refresh_token"]
        both = await asyncio.gather(
            *(
                self.call(
                    "POST",
                    "/auth/refresh",
                    sign=True,
                    address=address,
                    json={"refresh_token": refresh_token},
                )
                for _ in range(2)
            )
        )
        statuses = sorted(r.status_code for r in both)
        winner = next((r for r in both if r.status_code == 200), None)
        check(statuses == [200, 401], f"two refreshes {statuses}")
        assert winner is not None
        after = await self.call("GET", "/auth/me", winner.json()["access_token"])
        check(after.status_code == 401, f"winner's token afterwards {after.status_code}")
        return "one 200, one 401, the winner's access token then 401 - the race FE-2 must avoid"

    async def j7(self) -> str:
        token = self.state["token"]
        switched = await self.call(
            "POST", "/auth/workspace", token, json={"workspace_slug": f"second-{self.tag}"}
        )
        check(switched.status_code == 200, f"switch {switched.status_code}")
        other = await self.call(
            "GET", f"/conversations/{self.state['conversation']}", switched.json()["access_token"]
        )
        check(other.status_code == 404, f"new token reads the first workspace: {other.status_code}")
        return "switch 200, the first workspace's conversation 404 with the new token"

    async def j8(self) -> str:
        staff = await self.login(*STAFF)
        token = await self.login(self.state["owner"], workspace_slug=self.state["slug"])
        listed = (
            await self.call("GET", f"/platform/tenants?search={self.state['slug']}", staff)
        ).json()
        tenant = next(
            item["tenant"]["id"]
            for item in listed["items"]
            if item["tenant"]["slug"] == self.state["slug"]
        )
        stream = await self.stream(token)
        try:
            suspended = await self.call(
                "POST", f"/platform/tenants/{tenant}/suspend", staff, json={"reason": "e2e smoke"}
            )
            check(suspended.status_code == 200, f"suspend {suspended.status_code}")
            await stream.wait_for("closed", contains="workspace_unavailable", within=60)
        finally:
            stream.close()
        refused = await self.call("GET", "/conversations", token)
        me = await self.call("GET", "/auth/me", token)
        gates = await self.call("GET", "/workspace/gates", token)
        restored = await self.call("POST", f"/platform/tenants/{tenant}/restore", staff)
        back = await self.call("GET", "/conversations", token)
        gates_back = await self.call("GET", "/workspace/gates", token)
        check(
            refused.status_code == 403 and error_code(refused) == "workspace_suspended",
            f"while suspended {refused.status_code} {refused.text[:160]}",
        )
        listed = {item["slug"]: item["status"] for item in me.json()["workspaces"]}
        check(listed.get(self.state["slug"]) == "suspended", f"/auth/me while suspended {listed}")
        check(
            gates.status_code == 200 and gates.json()["status"] == "suspended",
            f"gates while suspended {gates.status_code} {gates.text[:160]}",
        )
        check(restored.status_code == 200 and back.status_code == 200, "restore")
        check(gates_back.json()["status"] == "active", f"gates after restore {gates_back.text}")
        self.state["token"] = token
        return (
            "staff suspend 200, stream closed workspace_unavailable, 403 workspace_suspended, "
            "/auth/me status suspended, gates 200 suspended, restore 200, 200, gates active"
        )

    async def j9(self) -> str:
        started = await self.call("POST", "/auth/google/authorize", sign=True)
        check(started.status_code == 200, f"authorize {started.status_code} {started.text[:160]}")
        cookie = started.headers.get("set-cookie", "")
        binding = re.search(r"((?:__Host-)?wasla_oauth=[^;]+)", cookie)
        check(binding is not None, "no binding cookie")
        assert binding is not None
        email = f"google-{self.tag}@example.com"
        page = await self.http.get(
            started.json()["authorization_url"] + f"&email={email}", follow_redirects=False
        )
        check(page.status_code == 302, f"fake Google {page.status_code}")
        back = parse_qs(urlsplit(page.headers["location"]).query)
        session = await self.call(
            "POST",
            "/auth/google/callback",
            sign=True,
            json={"code": back["code"][0], "state": back["state"][0]},
            headers={"Cookie": binding.group(1)},
        )
        check(session.status_code == 200, f"callback {session.status_code} {session.text[:200]}")
        check("access_token" in session.json(), "no tokens")
        return "authorize 200 + binding cookie, fake Google 302, callback 200 with tokens"

    async def j10(self) -> str:
        token = self.state["token"]
        media_id = f"e2e-media-{self.tag}"
        known = await self.conversation_ids(token)
        await self.webhook(
            self.state["pnid"],
            {"type": "image", "image": {"id": media_id, "mime_type": "image/png"}},
            "201000000110",
        )
        conversation = await self.new_conversation(token, known)
        stored = ""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not stored:
            stored = await asyncio.to_thread(stored_media_id, conversation)
            if not stored:
                await asyncio.sleep(1)
        check(bool(stored), "the media never reached storage")
        served = await self.call("GET", f"/conversations/{conversation}/media/{stored}", token)
        check(served.status_code == 200, f"media {served.status_code}")
        disposition = served.headers.get("content-disposition", "")
        cache = served.headers.get("cache-control", "")
        check(disposition.startswith("attachment"), f"disposition {disposition}")
        check("private" in cache and "no-store" in cache, f"cache-control {cache}")
        return "media stored; id read from the database (FEB-03); 200 attachment, private, no-store"

    async def forwards(self) -> list[str]:
        """Two signed addresses do not share the bucket; an unsigned run uses the caller's."""
        one, two = "203.0.113.200", "203.0.113.201"
        body = {"password": "a-wrong-password-123"}
        first = [
            (
                await self.call(
                    "POST",
                    "/auth/login",
                    sign=True,
                    address=one,
                    json={"email": f"f{n}-{self.tag}@example.com", **body},
                )
            ).status_code
            for n in range(11)
        ]
        other = (
            await self.call(
                "POST",
                "/auth/login",
                sign=True,
                address=two,
                json={"email": f"g-{self.tag}@example.com", **body},
            )
        ).status_code
        unsigned = [
            (
                await self.call(
                    "POST",
                    "/auth/login",
                    headers={FORWARD_IP_HEADER: "203.0.113.250"},
                    json={"email": f"u{n}-{self.tag}@example.com", **body},
                )
            ).status_code
            for n in range(11)
        ]
        check(first[-1] == 429 and 429 not in first[:10], f"signed one {first}")
        check(other == 401, f"signed two {other}")
        check(429 in unsigned, f"unsigned {unsigned}")
        return [
            f"signed {one}: {first}",
            f"signed {two}: {other} (its own bucket)",
            f"unsigned claim: {unsigned} (counted under the caller's address)",
        ]


async def run(options: argparse.Namespace) -> int:
    smoke = Smoke(options.api, options.mailbox, options.fakes)
    journeys: list[tuple[str, Callable[[], Awaitable[str]], str]] = [
        ("J1 sign up, verify, workspace", smoke.j1, "PASSED"),
        ("J2 number, inbound, stream, AI, takeover", smoke.j2, "PASSED"),
        ("J3 invite, accept, member, revoke", smoke.j3, "PASSED"),
        ("J4 knowledge upload, AI search", smoke.j4, "PASSED"),
        ("J5 plan, checkout, callback, entitlements", smoke.j5, "PASSED"),
        ("J6 two refreshes at once", smoke.j6, "EXPECTED UNTIL FE-2"),
        ("J7 switch workspace", smoke.j7, "PASSED"),
        ("J8 staff suspends, restores", smoke.j8, "PASSED"),
        ("J9 Google sign-in", smoke.j9, "PASSED"),
        ("J10 private media", smoke.j10, "PASSED WITH FEB-03"),
    ]
    results = []
    try:
        for name, journey, success in journeys:
            result = Result(name)
            started = time.monotonic()
            try:
                result.note = await journey()
                result.status = success
            except Exception as error:
                result.note = f"{type(error).__name__}: {error}"[:300]
            result.note += f" ({time.monotonic() - started:.1f}s)"
            results.append(result)
            print(f"| {result.name} | {result.status} | {result.note} |", flush=True)  # noqa: T201
        lines = await smoke.forwards()
        for line in lines:
            print(f"forward: {line}", flush=True)  # noqa: T201
    finally:
        await smoke.http.aclose()
    return 1 if any(result.status == "FAILED" for result in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--mailbox", default="http://127.0.0.1:8025")
    parser.add_argument("--fakes", default="http://localhost:8026")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
