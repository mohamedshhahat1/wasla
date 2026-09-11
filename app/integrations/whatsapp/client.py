"""Outbound WhatsApp Cloud API client.

Retry policy, and why it is narrow: the send endpoint takes no idempotency key,
so a retry can duplicate a customer-visible message. Only failures that
definitely did not send are retried.

| Failure | Retried | Raises | Reason |
| --- | --- | --- | --- |
| 429 | yes | `RateLimitedError` | Rejected outright; nothing was sent |
| connection error | yes | `SendNotAttemptedError` | No connection, so no request arrived |
| 401 / 403 / code 190 | no | `ProviderAuthError` | The credential is refused, not this message |
| other 4xx | no | `SendNotAttemptedError` | Meta read it and declined; nothing delivered |
| 5xx | no | `UncertainDeliveryError` | May have been accepted |
| read timeout | no | `UncertainDeliveryError` | Same: the request may have landed |
| transport failure | no | `UncertainDeliveryError` | The request left; no answer came back |
| 2xx, no usable id | no | `UncertainDeliveryError` | Meta said it accepted the message |

The last three rows were wrong, and two of them wrong in the direction that
costs a customer a second copy of a message.

A **2xx with no readable message id** was recorded as a definite failure, which
is the one classification that licenses a new send - and a campaign or
follow-up acting on it put the message on the customer's phone twice. A 2xx is
Meta saying it accepted the message. That it then failed to name it is a fact
about the response, not about the delivery (MSG-07).

A **reset connection** escaped as a raw `httpx` exception, past `_attempt` and
out of `MessagingService._dispatch` entirely, because the catch listed
`ConnectError` and `TimeoutException` and not the parent both belong to. The
row happened to be left in the right state; the caller got an unclassified
error and a campaign batch stopped where it stood (MSG-08).

An **invalid credential** was indistinguishable from a bad parameter, so a
workspace whose token had been revoked burned every recipient's attempt budget
one at a time and ended with an audience of individually-failed recipients and
no single explanation (MSG-18).

**The exception type is the answer to a question a caller has to ask.** Every
one of these used to be `ExternalServiceError`, which left "Meta declined this"
and "nobody knows whether Meta took it" indistinguishable - so the follow-up
and campaign sweeps retried both, and a read timeout could put a second copy of
a message on somebody's phone. The two types above are what a caller reads
instead of the message text (ADR-093).

Reads are the opposite and have their own path (`_get`). Fetching a file twice
costs a request and changes nothing anyone can see, so everything transient is
retried there, timeouts and 5xx included.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.net import MAX_REDIRECTS, UnsafeUrlError, build_guarded_client, validate_outbound_url
from app.core.telemetry import CallOutcome, Provider, ProviderCall

logger = get_logger(__name__)

# Followed by hand so each hop can be validated; see `_get`.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Meta's error codes that name the *template* rather than the request, mapped
# onto what the local registry should then record. Used to write a rejection
# back so every later send does not rediscover the same withdrawal one customer
# at a time (MSG-24).
#
# Deliberately a small, explicit table. A code that is not here leaves the
# registry alone, because marking a template invalid on an ambiguous error
# would take a working template away from a workspace on the strength of a
# guess - and getting it back needs a manual sync.
#
# 132000-132015 are Meta's template errors; only the ones whose meaning is
# unambiguously "this template may not be sent" are listed.
TEMPLATE_WITHDRAWN_CODES: Final[frozenset[int]] = frozenset(
    {
        # The template does not exist, or was deleted.
        132001,
        # Paused because of quality feedback.
        132015,
        # Disabled after repeated pausing.
        132016,
    }
)

# The two operations this client is counted under. Short constants, never
# anything derived from a request - a metric label domain has to be fixed at
# the point it is written, not at the point somebody sends a message.
SEND: Final = "send_message"
FETCH_MEDIA: Final = "fetch_media"

GRAPH_BASE_URL: Final = "https://graph.facebook.com"
MESSAGING_PRODUCT: Final = "whatsapp"
REQUEST_TIMEOUT_SECONDS: Final = 10.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 0.5
# How much random extra wait each backoff may add, as a fraction of the base.
# Additive only: a jittered retry may arrive later than the floor and never
# earlier. Without it, several replicas throttled at the same instant retry in
# lockstep and arrive together, which is what turns a brief throttle into a
# sustained one.
RETRY_JITTER: Final = 0.5
# The longest this client will wait because a provider header asked it to. The
# send path holds no database connection, but it does hold a worker, and a
# header is not something to let park one indefinitely.
MAX_RETRY_AFTER_SECONDS: Final = 30.0
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400
UNAUTHORIZED: Final = 401
FORBIDDEN: Final = 403
# Meta's own code for a credential that is expired, revoked, or was never
# valid. Checked alongside the status because Meta answers 400 with this code
# as readily as 401, and because a bare 403 can mean something else entirely -
# a permission on one number rather than a dead token - so neither the status
# nor the code is trusted on its own.
META_AUTH_CODE: Final = 190
MAX_REPLY_BUTTONS: Final = 3
# How many templates one page of the registry sync asks for, and how many
# pages it will follow. Meta caps the page size; the page *count* is ours,
# and it is bounded so a workspace with a pathological template list cannot
# turn one sync into an unbounded walk of a third party's pagination.
TEMPLATE_PAGE_SIZE: Final = 100
MAX_TEMPLATE_PAGES: Final = 20

MediaKind = Literal["image", "document", "audio", "video"]


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    """What Meta says about a file, without the file."""

    mime_type: str | None
    byte_size: int | None


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """A file that actually arrived.

    `byte_size` is what was received; `declared_size` is what Meta said it would
    be. They are kept apart rather than reconciled, because a mismatch is worth
    seeing and silently preferring one would hide it.
    """

    content: bytes
    mime_type: str | None
    byte_size: int
    declared_size: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class _Hop:
    """One fetched hop: either a body, or somewhere else to look for one.

    A pair rather than two methods, because the redirect loop has to tell the
    two apart on every hop and a `None` body with a `None` destination is the
    third case - a redirect with no `Location`, which is a dead end.
    """

    body: bytes | None
    redirect_to: str | None


def _retry_after(response: httpx.Response) -> float | None:
    """How long Meta asked this client to wait, if it said.

    Only the delta-seconds form is read. The HTTP-date form is permitted by the
    specification and is not what Meta sends here, and a date parser on this
    path would be more code than the case is worth - an absent or unreadable
    header simply falls back to the client's own backoff, which is the
    behaviour this had before.

    A negative or absurd value is refused rather than clamped to zero, because
    "wait no time at all" is not something a rate limiter would mean.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class SendNotAttemptedError(ExternalServiceError):
    """The request provably never reached Meta.

    Raised only where nothing can have been delivered: a connection that was
    never established, or a rejection issued before the message was read. The
    caller may record the send as undelivered and, if it wants to, make a new
    one - which is a decision it cannot safely take after any other failure
    (ADR-093).
    """


class UncertainDeliveryError(ExternalServiceError):
    """Meta may or may not have accepted the request, and nobody can tell.

    A read timeout, a reset connection, a 5xx. The request left this process
    and no answer came back, and Meta publishes no way to ask what became of
    it - there is no idempotency key on the send endpoint and no lookup keyed
    on anything this system generated. So this is terminal by construction: the
    one thing that must not follow it is another send.
    """


class TemplateWithdrawnError(SendNotAttemptedError):
    """Meta refused this template, not this message.

    Nothing was delivered, so the row is honestly `UNDELIVERED` - but the fact
    is about the template and outlives the send. The caller records it against
    the registry, so the next follow-up or campaign using the same template is
    refused locally instead of discovering the same withdrawal one customer at
    a time (MSG-24).

    Carries Meta's own code so the caller can record *why* without this client
    knowing anything about the registry.
    """

    message = "WhatsApp has withdrawn this template."

    def __init__(self, *, code: int) -> None:
        super().__init__(self.message)
        self.code = code


class ProviderAuthError(SendNotAttemptedError):
    """Meta refused the credential, so nothing was sent and nothing will be.

    A subclass of `SendNotAttemptedError` because that is the truth about this
    message - Meta declined before reading it, so nothing reached a customer
    and the row is honestly `UNDELIVERED`. It is a *type* of its own because
    the truth about the *workspace* is different: every other recipient will
    fail identically until somebody reconnects the number.

    Without the distinction a sweep works through its audience discovering the
    same dead token ten thousand times, spending one attempt budget per person
    and ending with no single thing to tell the operator (MSG-18). Campaigns
    and follow-ups let this one out rather than filing it against a recipient,
    which is the same shape `DependencyUnavailableError` already had for a
    credential that is missing rather than refused.

    Carries no credential material, here or in its message.
    """

    message = "WhatsApp refused this number's credentials."


class MediaTooLargeError(ExternalServiceError):
    """A download passed the byte cap while it was being read.

    Distinct from a fetch that broke. The caller records this as a decision not
    to process the file, which no retry changes, rather than as an attempt worth
    repeating - the same split `MediaStatus` draws between skipped and failed.
    """

    message = "This file is larger than the limit."


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Meta's acknowledgement of one accepted message."""

    message_id: str
    recipient: str
    raw: dict[str, Any]


def build_http_client() -> httpx.AsyncClient:
    """An HTTP client with a bounded timeout, aimed only at public addresses.

    Two properties, neither of which a caller should have to remember.

    A client without a timeout will eventually hang a worker on a provider
    stall, so the timeout is set here rather than left to callers.

    The transport resolves each request's host once and connects to a validated
    address rather than to a name (`app.core.net`). That matters most on this
    client, which is the one that fetches a URL it did not build - the media
    location arrives in a provider response - but it is applied to sends too:
    "which of our clients can be aimed at the deployment network?" should have
    the answer "none" rather than a list somebody has to keep current.
    """
    return build_guarded_client(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS))


class WhatsAppClient:
    """Sends messages through the WhatsApp Cloud API.

    The HTTP client, sleep function and attempt budget are injected so the retry
    behaviour can be tested without a network or a real wait.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        access_token: str,
        api_version: str = "v21.0",
        max_attempts: int = MAX_ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        if not access_token:
            # An absent platform credential is our misconfiguration, not the
            # caller's mistake, so this is a 503 rather than a 422.
            raise DependencyUnavailableError("The WhatsApp access token is not configured.")
        self._http = http
        self._access_token = access_token
        self._api_version = api_version
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        # Injected the same way `sleep` is, and for the same reason: the retry
        # wait is a formula a test should be able to pin exactly, rather than
        # one it has to patch `random` to observe.
        self._jitter = jitter

    async def send_text(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        preview_url: bool = False,
    ) -> SentMessage:
        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "text", "text": {"body": body, "preview_url": preview_url}},
        )

    async def send_media(
        self,
        *,
        phone_number_id: str,
        to: str,
        kind: MediaKind,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> SentMessage:
        """Send media by hosted link or by uploaded media id, never both."""
        if bool(link) == bool(media_id):
            raise ValidationError("Provide exactly one of a media link or a media id.")

        media: dict[str, Any] = {"link": link} if link else {"id": media_id}
        if caption and kind != "audio":
            # Meta rejects captions on audio; sending one fails the whole message.
            media["caption"] = caption
        if filename and kind == "document":
            media["filename"] = filename

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": kind, kind: media},
        )

    async def send_location(
        self,
        *,
        phone_number_id: str,
        to: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
    ) -> SentMessage:
        location: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
        if name:
            location["name"] = name
        if address:
            location["address"] = address

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "location", "location": location},
        )

    async def send_buttons(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
    ) -> SentMessage:
        """Reply buttons, as `(id, title)` pairs. Meta allows at most three."""
        if not buttons or len(buttons) > MAX_REPLY_BUTTONS:
            raise ValidationError("Provide between one and three reply buttons.")

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": button_id, "title": title}}
                            for button_id, title in buttons
                        ]
                    },
                },
            },
        )

    async def send_list(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> SentMessage:
        if not sections:
            raise ValidationError("A list message needs at least one section.")

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {"button": button_text, "sections": sections},
                },
            },
        )

    async def send_template(
        self,
        *,
        phone_number_id: str,
        to: str,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SentMessage:
        """Templates are the only way to open a conversation outside the 24-hour window."""
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "template", "template": template},
        )

    async def list_templates(
        self,
        *,
        waba_id: str,
        page_size: int = TEMPLATE_PAGE_SIZE,
        max_pages: int = MAX_TEMPLATE_PAGES,
    ) -> list[dict[str, Any]]:
        """Every message template on one WhatsApp Business account.

        A read, so it takes the retrying `_get` path rather than the send path's
        narrow one: asking twice costs a request and changes nothing.

        Pagination is followed by Meta's own `paging.next` URL, which already
        carries the cursor and the page size. It is bounded by `max_pages` so a
        malformed or cyclic `next` cannot turn a sync into a loop.
        """
        url = (
            f"{GRAPH_BASE_URL}/{self._api_version}/{waba_id}/message_templates"
            f"?limit={page_size}"
        )
        templates: list[dict[str, Any]] = []

        for _ in range(max(1, max_pages)):
            body = await self._get_json(url)
            page = body.get("data")
            if not isinstance(page, list):
                raise ExternalServiceError("WhatsApp returned an unreadable template list.")
            templates.extend(item for item in page if isinstance(item, dict))

            paging = body.get("paging")
            following = paging.get("next") if isinstance(paging, dict) else None
            if not isinstance(following, str) or not following:
                return templates
            url = following

        logger.warning("whatsapp.template_pages_exhausted", extra={"pages": max_pages})
        return templates

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        """Fetch an inbound file in the two steps Meta requires.

        The webhook carries a handle, not a file. Resolving the handle returns a
        short-lived URL on Meta's CDN, and that URL must still be requested with
        the access token - it is not public, despite looking like it. Both halves
        are done here so no caller ever holds a media URL.

        Retries are safe on this path, unlike a send: fetching a file twice
        costs a request and changes nothing a customer can see, which is why
        this uses the wider read policy rather than the send path's narrower one.

        **`max_bytes` is a required argument, not a default.** The body is read
        in chunks and abandoned the moment it passes the cap, so a descriptor
        that under-declares a file - or a CDN response that simply does not stop
        - costs a bounded amount of memory in the worker rather than whatever
        the far end felt like sending. A caller that had to remember to pass a
        limit would be a caller that eventually forgot.
        """
        descriptor = await self._get_json(f"{GRAPH_BASE_URL}/{self._api_version}/{media_id}")

        url = descriptor.get("url")
        if not isinstance(url, str) or not url:
            raise ExternalServiceError("WhatsApp did not return a location for this file.")

        # The trust boundary. Every other URL this client fetches is built here
        # from `GRAPH_BASE_URL`; this one arrives in a provider response, so it
        # is the one a compromised or spoofed reply could aim at the deployment
        # network. Validated before the first request rather than only on
        # redirects, because the first hop is a hop like any other.
        try:
            validate_outbound_url(url)
        except UnsafeUrlError as error:
            logger.warning(
                "whatsapp.media_url_refused",
                extra={"event": "whatsapp.media_url_refused"},
            )
            raise ExternalServiceError("WhatsApp could not return this file.") from error

        mime_type = descriptor.get("mime_type")
        declared = descriptor.get("file_size")

        content = await self._stream_capped(url, max_bytes=max_bytes)
        return DownloadedMedia(
            content=content,
            mime_type=mime_type.split(";", 1)[0].strip() if isinstance(mime_type, str) else None,
            byte_size=len(content),
            declared_size=declared if isinstance(declared, int) else None,
            sha256=descriptor.get("sha256") if isinstance(descriptor.get("sha256"), str) else None,
        )

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        """Ask how big a file is without downloading it.

        Worth a round trip: the alternative to asking is streaming a file that
        turns out to be ninety megabytes, and the point of the size cap is not
        to pay for that.
        """
        descriptor = await self._get_json(f"{GRAPH_BASE_URL}/{self._api_version}/{media_id}")
        mime_type = descriptor.get("mime_type")
        size = descriptor.get("file_size")
        return MediaDescriptor(
            mime_type=mime_type.split(";", 1)[0].strip() if isinstance(mime_type, str) else None,
            byte_size=size if isinstance(size, int) else None,
        )

    async def upload_media(
        self,
        *,
        phone_number_id: str,
        content: bytes,
        mime_type: str,
        filename: str,
    ) -> str:
        """Upload a file to Meta and return the id it can be sent with.

        Sending by hosted link is the other option and is not used: it would
        require every attachment to sit behind a public URL, which is a wider
        exposure than uploading the bytes for one send.
        """
        url = f"{GRAPH_BASE_URL}/{self._api_version}/{phone_number_id}/media"
        files = {"file": (filename, content, mime_type)}
        data = {"messaging_product": MESSAGING_PRODUCT, "type": mime_type}

        try:
            response = await self._http.post(
                url,
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        except httpx.HTTPError as error:
            raise ExternalServiceError("WhatsApp could not be reached.") from error

        if response.status_code >= CLIENT_ERROR_FLOOR:
            self._log_failure(response)
            raise ExternalServiceError("WhatsApp rejected the upload.")

        body = self._decode(response)
        uploaded = body.get("id")
        if not isinstance(uploaded, str) or not uploaded:
            raise ExternalServiceError("WhatsApp accepted the upload without an identifier.")
        return uploaded

    async def _stream_capped(self, url: str, *, max_bytes: int) -> bytes:
        """A media body, refused the moment it grows past `max_bytes`.

        The same hop-by-hop validation as `_get`, and for the same reason - a
        media URL arrives in a provider response, so every `Location` is a fresh
        destination that has to be judged before it is fetched.

        What is different is the read. `_get` returns a buffered response, which
        means the process is already holding whatever arrived by the time anyone
        can look at its length; a file that is bigger than the descriptor said
        would be entirely in memory before the cap that exists to stop it got a
        chance to run. This reads the body in chunks and gives up inside one
        chunk of the limit, so the worker's exposure is the cap and not the
        sender's intent.
        """
        for _ in range(MAX_REDIRECTS + 1):
            hop = await self._stream_hop(url, max_bytes=max_bytes)
            if hop.body is not None:
                return hop.body
            if hop.redirect_to is None:
                raise ExternalServiceError("WhatsApp could not return this file.")
            url = hop.redirect_to
            try:
                validate_outbound_url(url)
            except UnsafeUrlError as error:
                logger.warning(
                    "whatsapp.media_redirect_refused",
                    extra={"event": "whatsapp.media_redirect_refused"},
                )
                raise ExternalServiceError("WhatsApp could not return this file.") from error

        raise ExternalServiceError("WhatsApp could not return this file.")

    async def _stream_hop(self, url: str, *, max_bytes: int) -> _Hop:
        """One streamed hop, under the retry policy `_get_once` uses.

        The backoff happens after the response context has closed - `continue`
        leaves the `async with` first - so a retry never holds the connection it
        is retrying.

        Observed per hop rather than per file, which is what the counter beside
        it has always done: a redirect chain is several requests to Meta and an
        operator reading a media-fetch failure rate wants each of them.
        """
        call = ProviderCall(provider=Provider.WHATSAPP, operation=FETCH_MEDIA)
        attempt = 1
        while True:
            try:
                async with self._http.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        return _Hop(
                            body=None,
                            redirect_to=(str(response.url.join(location)) if location else None),
                        )

                    retryable = (
                        response.status_code == TOO_MANY_REQUESTS
                        or response.status_code >= SERVER_ERROR_FLOOR
                    )
                    if not (retryable and attempt < self._max_attempts):
                        if response.status_code == TOO_MANY_REQUESTS:
                            await call.record(CallOutcome.RATE_LIMITED)
                            raise RateLimitedError("WhatsApp is rate limiting this account.")
                        if response.status_code >= CLIENT_ERROR_FLOOR:
                            # Read before logging: the failure log wants the
                            # provider's error body, and a streamed response has
                            # not fetched one yet. Bounded by the same cap, so a
                            # hostile error body is no larger a problem than a
                            # hostile file.
                            await response.aread()
                            self._log_failure(response)
                            await call.record(CallOutcome.FAILURE)
                            raise ExternalServiceError("WhatsApp could not return this file.")

                        body = await self._read_capped(response, max_bytes=max_bytes)
                        await call.record(CallOutcome.SUCCESS)
                        return _Hop(body=body, redirect_to=None)
            except httpx.HTTPError as error:
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.media_unreachable", extra={"attempts": attempt})
                    await call.record(CallOutcome.UNAVAILABLE)
                    raise ExternalServiceError("WhatsApp could not be reached.") from error

            await self._backoff(attempt)
            attempt += 1

    async def _read_capped(self, response: httpx.Response, *, max_bytes: int) -> bytes:
        """Collect a streamed body, abandoning it if it passes the cap.

        `MediaTooLargeError` rather than a generic failure, because the caller
        draws a distinction that matters: a file that is too big is a decision
        never to process it, and a file that could not be fetched is an attempt
        worth repeating. Retrying an oversized download would spend the same
        bandwidth to reach the same conclusion.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                logger.info(
                    "whatsapp.media_over_cap",
                    extra={"event": "whatsapp.media_over_cap", "limit_bytes": max_bytes},
                )
                raise MediaTooLargeError()
            chunks.append(chunk)
        return b"".join(chunks)

    async def _get_json(self, url: str) -> dict[str, Any]:
        response = await self._get(url)
        return self._decode(response)

    async def _get(self, url: str) -> httpx.Response:
        """A retrying GET carrying the access token.

        Wider than the send path deliberately. A repeated read has no
        customer-visible effect, so every transient failure is worth another
        attempt - including the timeouts and 5xx that a send must never retry.

        **Redirects are followed by hand, and every hop is validated.** The URL
        of a media file is not one this application builds - it arrives in a
        provider response - and `follow_redirects=True` would take the worker
        wherever that response, or anything it redirects to, points. The worker
        sits inside the deployment network, so "wherever" includes the cloud
        metadata endpoint, the Redis holding the token denylist, and PostgreSQL;
        the body it fetches is stored as media and can be read back through the
        API. Checking only the first URL would check the one hop that is least
        likely to be hostile, so the loop below re-validates each `Location`.
        """
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._get_once(url)
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            url = str(response.url.join(location))
            try:
                validate_outbound_url(url)
            except UnsafeUrlError as error:
                logger.warning(
                    "whatsapp.media_redirect_refused",
                    extra={"event": "whatsapp.media_redirect_refused"},
                )
                raise ExternalServiceError("WhatsApp could not return this file.") from error

        raise ExternalServiceError("WhatsApp could not return this file.")

    async def _get_once(self, url: str) -> httpx.Response:
        """One hop, with the retry policy above and no redirect following."""
        call = ProviderCall(provider=Provider.WHATSAPP, operation=FETCH_MEDIA)
        attempt = 1
        while True:
            try:
                response = await self._http.get(
                    url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    follow_redirects=False,
                )
            except httpx.HTTPError as error:
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.media_unreachable", extra={"attempts": attempt})
                    await call.record(CallOutcome.UNAVAILABLE)
                    raise ExternalServiceError("WhatsApp could not be reached.") from error
                await self._backoff(attempt)
                attempt += 1
                continue

            retryable = (
                response.status_code == TOO_MANY_REQUESTS
                or response.status_code >= SERVER_ERROR_FLOOR
            )
            if retryable and attempt < self._max_attempts:
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code == TOO_MANY_REQUESTS:
                await call.record(CallOutcome.RATE_LIMITED)
                raise RateLimitedError("WhatsApp is rate limiting this account.")
            if response.status_code >= CLIENT_ERROR_FLOOR:
                self._log_failure(response)
                await call.record(CallOutcome.FAILURE)
                raise ExternalServiceError("WhatsApp could not return this file.")
            await call.record(CallOutcome.SUCCESS)
            return response

    async def mark_read(self, *, phone_number_id: str, message_id: str) -> None:
        """Show the customer a read receipt."""
        await self._post(
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": MESSAGING_PRODUCT,
                "status": "read",
                "message_id": message_id,
            },
        )

    async def _send(
        self,
        *,
        phone_number_id: str,
        to: str,
        content: dict[str, Any],
    ) -> SentMessage:
        payload: dict[str, Any] = {
            "messaging_product": MESSAGING_PRODUCT,
            "recipient_type": "individual",
            "to": to,
            **content,
        }
        response = await self._post(phone_number_id=phone_number_id, payload=payload)
        return SentMessage(
            message_id=self._message_id(response),
            recipient=self._recipient(response) or to,
            raw=response,
        )

    async def _post(self, *, phone_number_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one message, counting how it went.

        The outcome is recorded on every exit, including the ones that raise,
        because the ratio an operator alerts on is failures over attempts and a
        failure that left no trace makes that ratio a lie. Counting is
        best-effort - `ProviderCall.record` swallows - so it can never turn a
        successful send into a failed one.

        The same call carries the duration, and its clock starts before the
        retry loop: a send that took three attempts took the customer three
        attempts' worth of time, whatever the last one cost.
        """
        call = ProviderCall(provider=Provider.WHATSAPP, operation=SEND)
        url = f"{GRAPH_BASE_URL}/{self._api_version}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        attempt = 1
        while True:
            try:
                response = await self._http.post(url, json=payload, headers=headers)
            except httpx.ConnectError as error:
                # Nothing reached Meta, so a retry cannot duplicate anything.
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.send_unreachable", extra={"attempts": attempt})
                    await call.record(CallOutcome.UNAVAILABLE)
                    raise SendNotAttemptedError("WhatsApp could not be reached.") from error
                await self._backoff(attempt)
                attempt += 1
                continue
            except httpx.TimeoutException as error:
                # The request may have landed. Retrying risks a second message,
                # and so does anything upstream treating this as a failure it
                # can try again - which is why the exception says so by type
                # rather than leaving a caller to read the message (ADR-093).
                logger.warning("whatsapp.send_timed_out", extra={"attempts": attempt})
                await call.record(CallOutcome.UNAVAILABLE)
                raise UncertainDeliveryError("WhatsApp did not respond in time.") from error
            except httpx.TransportError as error:
                # Everything else that can go wrong on the wire, caught by the
                # parent the two branches above belong to: a reset connection,
                # a half-written request, a protocol violation. Routine behind
                # a load balancer, and previously not caught at all - it left
                # this client as a raw `httpx` exception, past `_attempt`,
                # which catches only this package's own types (MSG-08).
                #
                # Placed after `ConnectError`, deliberately. That one provably
                # never reached Meta and is the one case worth retrying; these
                # left the process with no answer coming back, which is the
                # same epistemic position as a read timeout and gets the same
                # answer.
                logger.warning(
                    "whatsapp.send_transport_failed",
                    extra={"attempts": attempt, "error_type": type(error).__name__},
                )
                await call.record(CallOutcome.UNAVAILABLE)
                raise UncertainDeliveryError("WhatsApp did not complete the request.") from error

            if response.status_code == TOO_MANY_REQUESTS:
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.send_rate_limited", extra={"attempts": attempt})
                    await call.record(CallOutcome.RATE_LIMITED)
                    raise RateLimitedError("WhatsApp is rate limiting this account.")
                await self._backoff(attempt, retry_after=_retry_after(response))
                attempt += 1
                continue

            if response.status_code >= SERVER_ERROR_FLOOR:
                # 5xx is not retried, and it is not a refusal either: Meta may
                # have accepted the message and failed somewhere after. Kept
                # apart from the 4xx below because the two lead to opposite
                # decisions upstream.
                self._log_failure(response)
                await call.record(CallOutcome.FAILURE)
                raise UncertainDeliveryError("WhatsApp did not complete the request.")

            if response.status_code >= CLIENT_ERROR_FLOOR:
                # Meta read the request and declined it. Nothing was delivered,
                # and that is known rather than assumed.
                error_code = self._log_failure(response)
                await call.record(CallOutcome.FAILURE)
                if self._is_credential_failure(response.status_code, error_code):
                    # Not this message's problem, and not fixable by trying the
                    # next recipient. Raised as its own type so a sweep can
                    # stop rather than discover it once per person (MSG-18).
                    raise ProviderAuthError
                if error_code in TEMPLATE_WITHDRAWN_CODES:
                    # About the template rather than this message, and worth
                    # writing down: the caller marks the registry so the next
                    # send of it is refused locally (MSG-24).
                    raise TemplateWithdrawnError(code=error_code)
                raise SendNotAttemptedError("WhatsApp rejected the message.")

            await call.record(CallOutcome.SUCCESS)
            # Decoded here rather than by the caller, so a 2xx whose body will
            # not yield a message id is classified while the status code is
            # still in hand. That is the whole of MSG-07: a 2xx is Meta saying
            # it accepted the message, and a body that fails to name it changes
            # nothing about the customer's phone.
            return self._decode(response, accepted=True)

    @staticmethod
    def _is_credential_failure(status_code: int, error_code: int | None) -> bool:
        """Whether Meta is refusing the credential rather than the message.

        Both halves matter. A 401 is unambiguous; a 403 is not, because Meta
        uses it for permission problems that are about the number rather than
        the token, and treating every 403 as a dead credential would stop a
        campaign that should have failed one recipient. Meta's own `code 190`
        is the authoritative signal and arrives on a 400 as readily as a 401,
        so either the unambiguous status or the explicit code is enough, and a
        bare 403 with no code is not.
        """
        if status_code == UNAUTHORIZED:
            return True
        return error_code == META_AUTH_CODE

    async def _backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        """Wait before the next attempt, honouring Meta if it said how long.

        Two changes from the linear wait this used to be, both of which matter
        more with several replicas than with one (MSG-17).

        **`Retry-After` is read when Meta sends it.** Retrying in half a second
        against an account Meta has just told to wait thirty is unlikely to
        help and may deepen the throttle. It is clamped rather than trusted:
        the send path holds no database connection but it does hold a worker,
        and a provider header is not something to let park one indefinitely.

        **The wait is jittered.** Three replicas rate-limited together retried
        in lockstep and arrived together, which is the shape that turns a brief
        throttle into a sustained one. The jitter is additive and never
        subtractive, so it can only ever make the wait longer than the floor -
        a retry that arrives *earlier* than intended is the one thing backoff
        must not do.
        """
        base = self._backoff_seconds * attempt
        if retry_after is not None:
            base = max(base, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        # The fraction is drawn here and the arithmetic is separate, matching
        # `RetryPolicy.delay_for`: a test pins the formula by supplying the
        # fraction rather than by patching `random` or watching a clock.
        draw = self._jitter or random.random
        fraction = draw()
        await self._sleep(base + base * RETRY_JITTER * fraction)

    def _log_failure(self, response: httpx.Response) -> int | None:
        """Log Meta's own error code, but never hand its text to the caller.

        Provider error text can echo fragments of the request, and this client
        holds a live platform credential.

        Returns the numeric code so the classification above can read it. The
        code is a fixed vocabulary Meta publishes, not free text, which is why
        it is safe to act on when the message beside it is not safe to repeat.
        """
        error: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        code = error.get("code")
        logger.warning(
            "whatsapp.send_failed",
            extra={
                "status": response.status_code,
                "meta_code": code,
                "meta_type": error.get("type"),
                "meta_subcode": error.get("error_subcode"),
            },
        )
        return code if isinstance(code, int) else None

    def _decode(self, response: httpx.Response, *, accepted: bool = False) -> dict[str, Any]:
        """Meta's body, or the right kind of failure for not having one.

        `accepted` says the status code was a success, and it changes which
        failure an unreadable body is. A 2xx means Meta took the message; a
        body this client cannot read afterwards says nothing about whether the
        customer got it, so the answer is "nobody knows" rather than "it
        failed" - and "it failed" is the one answer that licenses sending it
        again (MSG-07, ADR-093).

        Outside a 2xx - a media upload response, say - an unreadable body stays
        an ordinary external-service failure, because there the status has
        already said the request did not succeed.
        """
        try:
            body = response.json()
        except ValueError as error:
            if accepted:
                raise UncertainDeliveryError(
                    "WhatsApp accepted the message but its answer could not be read."
                ) from error
            raise ExternalServiceError("WhatsApp returned an unreadable response.") from error
        if not isinstance(body, dict):
            if accepted:
                raise UncertainDeliveryError(
                    "WhatsApp accepted the message but its answer could not be read."
                )
            raise ExternalServiceError("WhatsApp returned an unexpected response.")
        return body

    def _message_id(self, body: dict[str, Any]) -> str:
        messages = body.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = messages[0].get("id")
            if isinstance(message_id, str) and message_id:
                return message_id
        # Accepted but unidentifiable. Not usable - statuses arrive keyed on
        # the id - but not a failure either: Meta answered 2xx, which is Meta
        # saying it took the message. Recorded as unknown, never as failed, so
        # nothing upstream reads it as permission to send a second copy.
        raise UncertainDeliveryError("WhatsApp accepted the message without an identifier.")

    def _recipient(self, body: dict[str, Any]) -> str | None:
        contacts = body.get("contacts")
        if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
            recipient = contacts[0].get("wa_id")
            if isinstance(recipient, str):
                return recipient
        return None
