"""Proving that a workspace controls the number it is claiming.

The problem this exists for
---------------------------

`phone_number_id` is unique platform-wide, and until this module existed that
uniqueness was the *only* thing standing between a workspace and somebody
else's number. A unique constraint answers "has anyone claimed this?" It does
not answer "may *you* claim it?", and those are different questions. The
identifiers involved are not secret - a `phone_number_id` appears in every
webhook payload, in Meta's dashboard, and in support tickets - so a workspace
that knew a competitor's number could claim it first and become the tenant that
every inbound message for that number resolves to. That is the whole product's
isolation boundary, defeated by typing a number somebody published.

What proof looks like
---------------------

The credential is the proof. A Meta access token that can read the phone number
node is a token issued to the business that owns the number; nothing else can
read it. So verification is:

1. Read `GET /{phone_number_id}` with the *caller's* token.
2. Require the node that comes back to be the node that was asked for.
3. Take the owning WhatsApp Business Account from Meta's answer, never from the
   request.

Three rules hold throughout:

**Nothing user-supplied is trusted as an identifier.** The WABA id, the display
number and the verified name all come back from Meta and overwrite whatever the
request said. A supplied WABA id is treated as an assertion to *check*, and a
mismatch is a refusal rather than a correction.

**The platform token is never a proof.** It can read every number the platform
is connected to, so accepting it would let any workspace claim any of them -
exactly the hole this closes. See ADR-037.

**Failures are indistinguishable.** A wrong id, a revoked token, a Meta outage
and a malformed reply all raise the same error with the same message. Meta's
own error text is logged, never returned: it echoes fragments of the request,
and the request contains a credential.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from app.core.exceptions import WaslaError
from app.core.logging import get_logger
from app.core.net import build_guarded_client

logger = get_logger(__name__)

GRAPH_BASE_URL: Final = "https://graph.facebook.com"
# Deliberately tighter than the send client's ten seconds. This runs inside a
# request a person is waiting on, and a claim that cannot be proven quickly is
# better refused than left hanging.
VERIFY_TIMEOUT_SECONDS: Final = 8.0
# Fields asked for by name rather than taking the default node shape, so a
# future Graph version cannot quietly stop returning the WABA edge and have
# that read as "no WABA" instead of "look again".
NUMBER_FIELDS: Final = "id,display_phone_number,verified_name,whatsapp_business_account"
# One page is plenty: this is the fallback path, used only when the phone
# number node omits its WABA edge, and a business account holding more numbers
# than this does not need us to walk all of them to answer one question.
WABA_NUMBER_PAGE_SIZE: Final = 200
MAX_LABEL_LENGTH: Final = 200


class NumberOwnershipError(WaslaError):
    """The claim could not be proven.

    422 rather than 403: nothing about the *caller's* authorization failed -
    they are an administrator of their own workspace - so this is a statement
    about the payload. One message for every cause, because distinguishing
    "that number does not exist" from "your token cannot read it" turns this
    endpoint into an oracle for mapping other businesses' numbers.
    """

    status_code = 422
    error_code = "whatsapp_ownership_unverified"
    message = (
        "This WhatsApp number could not be verified with the credential supplied. "
        "Check that the number belongs to your business and that the access token "
        "has permission to read it."
    )


@dataclass(frozen=True, slots=True)
class VerifiedNumber:
    """What Meta says about a number, once the credential has proven access.

    Every field here came from Meta. Nothing on it was copied from the request,
    which is the point: these are the values written to the account row.
    """

    phone_number_id: str
    waba_id: str
    display_phone_number: str
    verified_name: str | None


class OwnershipVerifier(Protocol):
    """The one operation the connect path needs.

    A protocol so the service depends on the question rather than on httpx, and
    so a test can answer it without a network.
    """

    async def verify(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        claimed_waba_id: str | None = None,
    ) -> VerifiedNumber:
        """Prove that `access_token` controls `phone_number_id`, or raise."""
        ...  # pragma: no cover - protocol declaration


class MetaOwnershipVerifier:
    """Asks Meta whether this credential can read this number.

    The HTTP client is injected so the failure modes - timeout, 4xx, garbage
    body - are testable without a network, and so a caller can share a pool.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_version: str = "v21.0",
        base_url: str = GRAPH_BASE_URL,
    ) -> None:
        self._http = http
        self._api_version = api_version
        self._base_url = base_url.rstrip("/")

    async def verify(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        claimed_waba_id: str | None = None,
    ) -> VerifiedNumber:
        token = access_token.strip()
        number_id = phone_number_id.strip()
        if not token or not number_id:
            raise NumberOwnershipError()

        if self._http is not None:
            return await self._verify_with(self._http, token, number_id, claimed_waba_id)
        # No shared client: open one for this call. The timeout is set here
        # rather than left to a default, because a verification that hangs
        # holds a worker and a person's browser tab open together. Guarded like
        # every other outbound client, so a Graph host that somehow resolved
        # inward could not be reached even with a credential attached.
        async with build_guarded_client(timeout=httpx.Timeout(VERIFY_TIMEOUT_SECONDS)) as http:
            return await self._verify_with(http, token, number_id, claimed_waba_id)

    async def _verify_with(
        self,
        http: httpx.AsyncClient,
        token: str,
        phone_number_id: str,
        claimed_waba_id: str | None,
    ) -> VerifiedNumber:
        node = await self._read(
            http,
            token,
            f"{self._base_url}/{self._api_version}/{phone_number_id}",
            params={"fields": NUMBER_FIELDS},
            event="whatsapp.ownership_number_unreadable",
        )

        # The identity check. Graph resolves some aliases, and a node that
        # answers to a *different* id is not the number being claimed even
        # though the request succeeded.
        returned = node.get("id")
        if not isinstance(returned, str) or returned != phone_number_id:
            logger.warning(
                "whatsapp.ownership_identity_mismatch",
                extra={
                    "event": "whatsapp.ownership_identity_mismatch",
                    "phone_number_id": phone_number_id,
                },
            )
            raise NumberOwnershipError()

        waba_id = await self._resolve_waba(
            http,
            token,
            node=node,
            phone_number_id=phone_number_id,
            claimed_waba_id=claimed_waba_id,
        )

        display = node.get("display_phone_number")
        if not isinstance(display, str) or not display.strip():
            # Every real phone number node carries one. Its absence means this
            # is not the object it claims to be, so the claim fails rather than
            # falling back to whatever the request said the number was.
            logger.warning(
                "whatsapp.ownership_display_missing",
                extra={
                    "event": "whatsapp.ownership_display_missing",
                    "phone_number_id": phone_number_id,
                },
            )
            raise NumberOwnershipError()

        verified_name = node.get("verified_name")
        logger.info(
            "whatsapp.ownership_verified",
            extra={
                "event": "whatsapp.ownership_verified",
                "phone_number_id": phone_number_id,
                "waba_id": waba_id,
            },
        )
        return VerifiedNumber(
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            display_phone_number=display.strip()[:MAX_LABEL_LENGTH],
            verified_name=(
                verified_name.strip()[:MAX_LABEL_LENGTH]
                if isinstance(verified_name, str) and verified_name.strip()
                else None
            ),
        )

    async def _resolve_waba(
        self,
        http: httpx.AsyncClient,
        token: str,
        *,
        node: dict[str, Any],
        phone_number_id: str,
        claimed_waba_id: str | None,
    ) -> str:
        """Which business account owns this number, according to Meta.

        Preferred source is the edge on the number itself. When a Graph version
        omits it, the fallback inverts the question: list the numbers on the
        *claimed* account and require this one to be among them. That still
        proves the relationship, because listing a WABA's numbers needs a token
        with access to that WABA.

        A claimed id that disagrees with Meta is a refusal. Silently preferring
        Meta's answer would let a request assert a business relationship it does
        not have and have the mistake corrected out of the audit trail.
        """
        edge = node.get("whatsapp_business_account")
        found = edge.get("id") if isinstance(edge, dict) else None
        if isinstance(found, str) and found:
            if claimed_waba_id and claimed_waba_id != found:
                logger.warning(
                    "whatsapp.ownership_waba_mismatch",
                    extra={
                        "event": "whatsapp.ownership_waba_mismatch",
                        "phone_number_id": phone_number_id,
                    },
                )
                raise NumberOwnershipError()
            return found

        if not claimed_waba_id:
            # Nothing to check against and nothing returned: the business
            # relationship is unproven, so the claim fails closed.
            logger.warning(
                "whatsapp.ownership_waba_unknown",
                extra={
                    "event": "whatsapp.ownership_waba_unknown",
                    "phone_number_id": phone_number_id,
                },
            )
            raise NumberOwnershipError()

        listing = await self._read(
            http,
            token,
            f"{self._base_url}/{self._api_version}/{claimed_waba_id}/phone_numbers",
            params={"fields": "id", "limit": str(WABA_NUMBER_PAGE_SIZE)},
            event="whatsapp.ownership_waba_unreadable",
        )
        entries = listing.get("data")
        if not isinstance(entries, list):
            raise NumberOwnershipError()
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == phone_number_id:
                return claimed_waba_id

        logger.warning(
            "whatsapp.ownership_number_not_on_waba",
            extra={
                "event": "whatsapp.ownership_number_not_on_waba",
                "phone_number_id": phone_number_id,
            },
        )
        raise NumberOwnershipError()

    async def _read(
        self,
        http: httpx.AsyncClient,
        token: str,
        url: str,
        *,
        params: dict[str, str],
        event: str,
    ) -> dict[str, Any]:
        """One Graph read, with every failure collapsed into one refusal.

        Not retried, and that is deliberate: this runs inside a person's
        request, a claim is a rare deliberate act, and retrying a rejected
        credential three times only slows down the refusal. Redirects are not
        followed - every URL here is built from `_base_url` and a Graph node
        that answers with a redirect is not something to chase.
        """
        try:
            response = await http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                timeout=httpx.Timeout(VERIFY_TIMEOUT_SECONDS),
            )
        except httpx.HTTPError as error:
            # Includes the timeout. A verification we could not complete is a
            # verification that did not succeed.
            logger.warning(
                event,
                extra={"event": event, "reason": type(error).__name__},
            )
            raise NumberOwnershipError() from error

        if response.status_code != httpx.codes.OK:
            self._log_rejection(event, response)
            raise NumberOwnershipError()

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            logger.warning(event, extra={"event": event, "reason": "unreadable_body"})
            raise NumberOwnershipError() from error
        if not isinstance(body, dict):
            logger.warning(event, extra={"event": event, "reason": "unexpected_body"})
            raise NumberOwnershipError()
        return body

    @staticmethod
    def _log_rejection(event: str, response: httpx.Response) -> None:
        """Record Meta's error code, never its text.

        Provider error strings quote the request back, and this request carries
        a live credential. The numeric code is enough to tell "token expired"
        from "no such object" while debugging, and carries nothing secret.
        """
        code: Any = None
        subcode: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
            subcode = body["error"].get("error_subcode")
        logger.warning(
            event,
            extra={
                "event": event,
                "status": response.status_code,
                "meta_code": code,
                "meta_subcode": subcode,
            },
        )
