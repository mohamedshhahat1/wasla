"""Fetching an inbound attachment from wherever its provider says it is (OMNI-009).

The media pipeline - claim, bounds, hashing, byte-sniffed types, storage,
reading, retention - is shared by every channel (ADR-110). What differs per
provider is only *where the file is*: WhatsApp names a handle to resolve through
the Graph API; Messenger and Instagram put a signed CDN URL in the payload. This
module holds the outcome types every fetcher raises, the capped read they all
use, and the URL fetcher a URL-locating adapter shares - so a second channel
gets the same SSRF guard, host allow-list and byte cap rather than a copy of
them.

**A URL from a payload is a request to fetch something a stranger chose.** It
is fetched only through `app.core.net`'s guard, which resolves once, refuses any
non-public address and pins the connection to what it judged; every redirect is
judged again; and the host must be inside the provider's own allow-list - a
public host that is not the provider's is refused before any request leaves
(MEDIA-01). A credential, where a provider needs one, is only ever sent to an
allow-listed host.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import httpx

from app.channels.adapter import FetchedFile, FileProbe
from app.channels.inbound import AttachmentLocator
from app.core.exceptions import ExternalServiceError
from app.core.hostnames import HostNotAuthorizedError, credential_host, normalize_roots, within
from app.core.logging import get_logger
from app.core.net import MAX_REDIRECTS, Resolver, UnsafeUrlError, validate_outbound_url
from app.db.models.media import MediaLocatorKind

logger = get_logger(__name__)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_UNAUTHORIZED = frozenset({401, 403})
_SERVER_ERROR_FLOOR = 500
_CLIENT_ERROR_FLOOR = 400


class MediaHostRefusedError(ExternalServiceError):
    """A fetch named a host outside the provider's own (MEDIA-01).

    Refused outright rather than fetched anonymously: a location outside the
    provider's hosts is not a file the provider is serving, and reading it
    would still be downloading whatever that host chose into a customer's
    conversation.
    """

    message = "The provider could not return this file."


class MediaUnavailableError(ExternalServiceError):
    """The provider answered, permanently, that this file cannot be had (MEDIA-04).

    An expired handle or link, a deleted file, one belonging to another
    account. No retry changes that, so a caller records a decision rather than
    an attempt.
    """

    message = "The provider no longer has this file."


class MediaCredentialRefusedError(ExternalServiceError):
    """The provider refused the credential a fetch was made with.

    Kept apart from an unavailable file because the remedy is different -
    reconnecting, not asking the customer again.
    """

    message = "The provider refused this connection's credentials for the file."


class MalformedMediaDescriptorError(ExternalServiceError):
    """The provider's answer about a file could not be used: no location, not JSON, too big."""

    message = "The provider returned an unusable description of this file."


class MediaTooLargeError(ExternalServiceError):
    """A download passed the byte cap while it was being read.

    Distinct from a fetch that broke: the caller records this as a decision not
    to process the file, which no retry changes - the same split `MediaStatus`
    draws between skipped and failed.
    """

    message = "This file is larger than the limit."


async def read_capped(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Collect a streamed body, abandoning it the moment it passes `max_bytes`.

    The worker's exposure is the cap, not the sender's intent: a body that does
    not stop, or that is bigger than anything declared about it, costs at most
    one chunk past the limit before the read is given up.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            logger.info(
                "media.fetch_over_cap",
                extra={"event": "media.fetch_over_cap", "limit_bytes": max_bytes},
            )
            raise MediaTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


def locator_expired(locator: AttachmentLocator, *, now: datetime) -> bool:
    """Whether a locator's own expiry has passed. One without an expiry never has."""
    return locator.expires_at is not None and now >= locator.expires_at


class UrlMediaFetcher:
    """Fetches `url` locators for one provider, inside that provider's hosts.

    Neutral: nothing here is any provider's. A URL-locating adapter builds one
    with its own host roots and, if its CDN needs one, its connection's token -
    which is sent only to a host inside those roots, on every hop.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        host_roots: Iterable[str],
        token: str | None = None,
        resolver: Resolver | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._roots = normalize_roots(host_roots)
        if not self._roots:
            # An empty allow-list would refuse everything, which is safe - and
            # which is also a misconfiguration nobody should have to debug.
            raise ValueError("a URL media fetcher needs at least one host root")
        self._token = token
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    async def probe(self, locator: AttachmentLocator) -> FileProbe:
        """What the payload declared: URL providers publish no separate descriptor.

        The size is unknown until the body arrives, so the byte cap is enforced
        on the read itself rather than trusted from a declaration.
        """
        return FileProbe(mime_type=locator.mime_type, byte_size=None)

    async def fetch(self, locator: AttachmentLocator, *, max_bytes: int) -> FetchedFile:
        if locator.locator_kind is not MediaLocatorKind.URL:
            raise MalformedMediaDescriptorError()
        if locator_expired(locator, now=self._now()):
            raise MediaUnavailableError()

        url = locator.locator
        for _ in range(MAX_REDIRECTS + 1):
            headers = self._headers(url)
            try:
                async with self._http.stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as response:
                    status = response.status_code
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise ExternalServiceError("The provider could not return this file.")
                        url = str(response.url.join(location))
                        continue
                    if status in _UNAUTHORIZED:
                        raise MediaCredentialRefusedError()
                    if _CLIENT_ERROR_FLOOR <= status < _SERVER_ERROR_FLOOR:
                        raise MediaUnavailableError()
                    if status >= _SERVER_ERROR_FLOOR:
                        raise ExternalServiceError("The provider could not return this file.")
                    content = await read_capped(response, max_bytes=max_bytes)
                    declared = response.headers.get("Content-Length")
                    mime = response.headers.get("Content-Type")
                    return FetchedFile(
                        content=content,
                        mime_type=mime.split(";", 1)[0].strip() if mime else locator.mime_type,
                        declared_size=int(declared) if declared and declared.isdigit() else None,
                    )
            except httpx.HTTPError as error:
                raise ExternalServiceError("The provider could not be reached.") from error
        raise ExternalServiceError("The provider could not return this file.")

    def _headers(self, url: str) -> dict[str, str]:
        """Judge a hop before it exists: a safe address, and one of the provider's hosts."""
        try:
            validate_outbound_url(url, resolver=self._resolver)
        except UnsafeUrlError as error:
            logger.warning("media.url_refused", extra={"event": "media.url_refused"})
            raise ExternalServiceError("The provider could not return this file.") from error
        try:
            host = credential_host(url)
        except HostNotAuthorizedError as error:
            raise MediaHostRefusedError() from error
        if not any(within(host, root) for root in self._roots):
            logger.warning(
                "media.host_refused", extra={"event": "media.host_refused", "host": host}
            )
            raise MediaHostRefusedError()
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}


__all__ = [
    "MalformedMediaDescriptorError",
    "MediaCredentialRefusedError",
    "MediaHostRefusedError",
    "MediaTooLargeError",
    "MediaUnavailableError",
    "UrlMediaFetcher",
    "locator_expired",
    "read_capped",
]
