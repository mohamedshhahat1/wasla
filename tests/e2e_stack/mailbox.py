"""The e2e mailbox: every email the worker sends, kept after it is sent (FE-0, E2E-03).

The repository's fake email provider keeps messages in the worker's memory, and
sending clears the sealed context the outbox row held - so the frontend plan
could read a verification code or an invitation link only with the worker
stopped (its deviation 2). Here the worker's provider is wrapped: each message
is *also* appended, at send time, to a JSON-lines file on a volume shared with
the mailbox service, which serves it read-only:

    GET    /messages?to=<address>   newest first
    DELETE /messages                empty the mailbox
    GET    /health

Each line: ``{to, subject, text, html, links, codes, sent_at}``. ``codes`` are
the six-digit numbers in the text (verification codes); ``links`` every URL.

Run as a service: ``python -m tests.e2e_stack.mailbox``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.integrations.email.base import EmailMessage, EmailSendResult
from app.integrations.email.fake import FakeEmailProvider
from app.services.email_templates import EmailTemplate, subject_for

MAILBOX_FILE = Path(os.environ.get("WASLA_E2E_MAILBOX_FILE", "/mailbox/messages.jsonl"))
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_LINK = re.compile(r"https?://[^\s\"'<>]+")


def record(message: EmailMessage, path: Path = MAILBOX_FILE) -> dict[str, Any]:
    """Append one message to the mailbox file and return what was written."""
    entry: dict[str, Any] = {
        "to": list(message.to),
        "subject": message.subject,
        "text": message.text,
        "html": message.html,
        "links": sorted(set(_LINK.findall(message.text))),
        "codes": _CODE.findall(message.text),
        "sent_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


class CapturingEmailProvider(FakeEmailProvider):
    """The repository's fake provider, which also writes each message to the mailbox."""

    async def send(
        self,
        message: EmailMessage,
        *,
        idempotency_key: str | None = None,
    ) -> EmailSendResult:
        record(message)
        return await super().send(message, idempotency_key=idempotency_key)


def read(path: Path = MAILBOX_FILE, *, to: str | None = None) -> list[dict[str, Any]]:
    """The mailbox, newest first, optionally only one recipient's."""
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    if to is not None:
        wanted = to.strip().lower()
        entries = [entry for entry in entries if wanted in (a.lower() for a in entry["to"])]
    return list(reversed(entries))


#: The verification mail's subject, from the app's own template table. Codes are read from
#: these mails only: `codes` lists every six-digit run of any mail, and an invitation's random
#: token or slug can hold one (FEB-32).
VERIFICATION_SUBJECT = subject_for(EmailTemplate.EMAIL_VERIFICATION)


def _verification(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m["subject"] == VERIFICATION_SUBJECT and m["codes"]]


def code_mails(messages: list[dict[str, Any]]) -> int:
    """How many of one recipient's messages (newest first) are verification mails with a code."""
    return len(_verification(messages))


def code_after(messages: list[dict[str, Any]], *, seen: int) -> str | None:
    """The code of the mail a request caused: the newest, once more than `seen` carry one.

    A send supersedes every earlier code, and the mail of an earlier request -
    registration's own - can be delivered late. A reader that counted the code
    mails before its request, and waits for one more, never takes that one for
    its own (FEB-32). None while the new mail has not arrived.
    """
    if code_mails(messages) <= seen:
        return None
    newest = _verification(messages)[0]
    return str(newest["codes"][0])


def service() -> Any:
    """The read-only mailbox HTTP service."""
    from fastapi import FastAPI, Response

    api = FastAPI(title="wasla e2e mailbox", docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/messages")
    async def messages(to: str | None = None) -> list[dict[str, Any]]:
        return read(to=to)

    @api.delete("/messages", status_code=204, response_class=Response, response_model=None)
    async def clear() -> Response:
        if MAILBOX_FILE.exists():
            MAILBOX_FILE.write_text("", encoding="utf-8")
        return Response(status_code=204)

    return api


if __name__ == "__main__":
    import uvicorn

    from tests.e2e_stack.guard import install_socket_wall

    install_socket_wall()
    uvicorn.run(service(), host="0.0.0.0", port=int(os.environ.get("PORT", "8025")))  # noqa: S104
