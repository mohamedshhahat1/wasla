"""An S3-compatible implementation of `MediaStorage`.

Local disk was always a stated limitation rather than a design (ADR-023): it
requires the API and the worker to share a volume, which is one host, and one
host is a single point at which every attachment a workspace ever received
disappears. This is the implementation that removes that.

**One protocol, not one provider.** S3's object API is what AWS S3, MinIO,
Cloudflare R2, Wasabi, Backblaze B2 and Ceph all speak, so a single
implementation reaches any of them and this repository chooses none. The same
reasoning the backup upload already follows (ADR-075).

## Why the requests are signed here rather than by an SDK

`boto3` is the obvious answer and is the wrong one for this codebase. It is
synchronous, so every `put` and `get` would have to cross a thread boundary in
an application that is asynchronous end to end; it is a large addition to a
runtime image that is deliberately small; and it brings its own retry, timeout
and endpoint-resolution behaviour that would have to be pinned down to match
what every other client here already does.

The four operations this needs - PUT, GET, DELETE and HEAD on a single object -
are the simplest possible use of SigV4: one request, no multipart, no
pagination, no session tokens. That is a bounded amount of well-specified
signing code over the `httpx` client the rest of the system uses, and the drill
against a real MinIO is what proves it correct. A signing bug is not a subtle
failure here: the store answers 403 and nothing works.

## Why this client is not the guarded transport

Every *integration* client in this system is built by `build_guarded_client`,
which refuses to connect to a private address (`app.core.net`). That guard
exists because those clients fetch URLs that arrive in somebody else's
response - a WhatsApp media location, a redirect - and the worker sits inside
the deployment network.

An object store is not that. Its endpoint comes from configuration and from
nowhere else: no request, no provider response, no database row can influence
it, and the settings validator refuses an endpoint carrying a path. It is
infrastructure in the same class as `DATABASE_URL` and `REDIS_URL`, both of
which point at private addresses by design - and applying the guard here would
make `http://minio:9000` unreachable, which is the ordinary self-hosted case.
`tests/unit/test_outbound_pinning.py` states this as an assertion rather than
leaving it to this comment.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import uuid
from typing import Final, NoReturn
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import SAFE_KEY, StorageError, build_key

logger = get_logger(__name__)

SERVICE: Final = "s3"
ALGORITHM: Final = "AWS4-HMAC-SHA256"
# The payload hash for a request with no body. S3 requires the header on every
# signed request, including the ones that carry nothing.
EMPTY_PAYLOAD_HASH: Final = hashlib.sha256(b"").hexdigest()

# Statuses that mean the object is not there, as opposed to a store that is
# unwell. Both are refusals to a caller, but only the second is worth an alarm.
_NOT_FOUND: Final = frozenset({403, 404})
_OK: Final = frozenset({200, 201, 204})


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _encode_key(key: str) -> str:
    """Percent-encode an object key for a request path.

    `safe="/"` because the key's slashes are path separators in the store as
    well. Everything else is escaped, which for a key this module produced is a
    no-op - the point is that it stays a no-op if the key format ever grows a
    character that would otherwise change the request's shape.
    """
    return quote(key, safe="/")


class S3MediaStorage:
    """Objects in one private S3-compatible bucket.

    Satisfies `MediaStorage` structurally, like `LocalMediaStorage` does. Keys
    come from the same `build_key`, so the tenant-first layout, the generated
    identifier and the pattern check are identical across both backends and a
    deployment can move between them without rewriting a key.

    **The bucket is private, and nothing here asks for it not to be.** No ACL
    header is ever sent, no presigned URL is ever produced, and bytes reach a
    colleague only by being streamed back through the authenticated API. A
    public-read object would be a link that leaves the workspace and never comes
    back, which is the property the download route exists to avoid.
    """

    def __init__(
        self,
        *,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        path_style: bool = True,
        server_side_encryption: str | None = None,
        timeout_seconds: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region
        self._endpoint = (endpoint_url or f"https://{SERVICE}.{region}.amazonaws.com").rstrip("/")
        self._path_style = path_style
        self._encryption = server_side_encryption
        self._timeout = timeout_seconds
        # Injected in tests and in a process that wants one pool for many
        # objects; otherwise one is opened per request, which is right for the
        # handful of object calls a single request or job makes.
        self._http = http

    @classmethod
    def from_settings(cls, settings: Settings) -> S3MediaStorage:
        """Build from configuration, which the validator has already checked.

        The three `or ""` fallbacks are unreachable: `Settings` refuses to
        construct with `MEDIA_STORAGE_BACKEND=s3` and any of them missing. They
        are here so this constructor has no opinion about a state that cannot
        exist, rather than asserting one and giving a caller a different error
        than the one the validator already wrote.
        """
        return cls(
            bucket=settings.media_s3_bucket or "",
            access_key_id=settings.media_s3_access_key_id or "",
            secret_access_key=settings.media_s3_secret_access_key or "",
            region=settings.media_s3_region,
            endpoint_url=settings.media_s3_endpoint_url,
            path_style=settings.media_s3_path_style,
            server_side_encryption=settings.media_s3_server_side_encryption,
            timeout_seconds=settings.media_s3_timeout_seconds,
        )

    # ------------------------------------------------------------- the protocol

    async def put(
        self,
        *,
        tenant_id: uuid.UUID,
        data: bytes,
        mime_type: str | None = None,
    ) -> str:
        """Store `data` under a fresh key and return it."""
        key = build_key(tenant_id=tenant_id, mime_type=mime_type)
        headers = {"Content-Type": mime_type} if mime_type else {}
        if self._encryption:
            headers["x-amz-server-side-encryption"] = self._encryption

        response = await self._request("PUT", key, body=data, headers=headers)
        if response.status_code not in _OK:
            self._refuse("store", response.status_code, tenant_id=tenant_id)
        return key

    async def get(self, key: str) -> bytes:
        """Read back what `put` stored. Raises `StorageError` if it is gone."""
        response = await self._request("GET", key)
        if response.status_code not in _OK:
            self._refuse("read", response.status_code)
        return response.content

    async def delete(self, key: str) -> None:
        """Remove a stored object. Removing one already gone is not an error.

        S3 answers 204 for a key that was never there, which is exactly the
        semantics the retention sweep needs: a pass that is retried after a
        partial failure must be able to delete the same object twice without
        the second attempt looking like a problem.
        """
        response = await self._request("DELETE", key)
        # 404 is included because a store behind a gateway may report it rather
        # than S3's own 204, and "it is not there" is the outcome either way.
        if response.status_code not in _OK and response.status_code != 404:
            self._refuse("delete", response.status_code)

    async def exists(self, key: str) -> bool:
        """Whether an object is in the store.

        Used by the orphan sweep, which must never delete a row whose object it
        merely failed to reach - so a store that answers with anything other
        than a clear yes or no raises rather than returning False.
        """
        response = await self._request("HEAD", key)
        if response.status_code in _OK:
            return True
        if response.status_code in _NOT_FOUND:
            return False
        self._refuse("head", response.status_code)

    # ------------------------------------------------------------------ signing

    async def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One signed request against one object.

        The key is validated against the same pattern `LocalMediaStorage` uses,
        and for the same reason: a key read back from a database row is input,
        whatever wrote it. Path traversal does not mean the same thing in an
        object store as on a filesystem, but a key with a `..` or a `?` in it
        would still change which object a request addresses, and the cheapest
        place to refuse that is before the request is built.
        """
        if not SAFE_KEY.match(key):
            logger.warning("media.key_refused", extra={"event": "media.key_refused"})
            raise StorageError()

        url, canonical_uri = self._address(key)
        signed = self._signed_headers(
            method=method,
            canonical_uri=canonical_uri,
            body=body,
            headers=dict(headers or {}),
        )

        try:
            if self._http is not None:
                return await self._http.request(method, url, content=body, headers=signed)
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(method, url, content=body, headers=signed)
        except httpx.HTTPError as error:
            # Translated at the boundary, like every other integration here.
            # The exception text can carry the endpoint and, through a proxy
            # configuration, more than that; the caller gets the domain error
            # and the operator gets a log line with no interpolated detail.
            logger.warning(
                "media.object_store_unreachable",
                extra={"event": "media.object_store_unreachable", "operation": method},
            )
            raise StorageError() from error

    def _address(self, key: str) -> tuple[str, str]:
        """The request URL and the path that gets signed.

        The two must agree exactly, which is why they are produced together
        rather than rebuilt at the signing step: a mismatch between what is
        signed and what is sent is a 403 with nothing to point at.
        """
        encoded = _encode_key(key)
        if self._path_style:
            canonical_uri = f"/{self._bucket}/{encoded}"
            return f"{self._endpoint}{canonical_uri}", canonical_uri

        canonical_uri = f"/{encoded}"
        scheme, _, host = self._endpoint.partition("://")
        return f"{scheme}://{self._bucket}.{host}{canonical_uri}", canonical_uri

    def _signed_headers(
        self,
        *,
        method: str,
        canonical_uri: str,
        body: bytes,
        headers: dict[str, str],
    ) -> dict[str, str]:
        """SigV4 for a single-object request.

        Deliberately the narrow case: no query string, no session token, no
        chunked upload. The payload hash is computed over the whole body rather
        than declared `UNSIGNED-PAYLOAD`, so the store verifies that what
        arrived is what was signed - which is worth the hash of a file that is
        already in memory.
        """
        now = dt.datetime.now(dt.UTC)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_PAYLOAD_HASH

        host = self._host()
        headers["host"] = host
        headers["x-amz-content-sha256"] = payload_hash
        headers["x-amz-date"] = timestamp

        # Signed headers are lower-cased and ordered, and the canonical request
        # must list exactly the ones it signs. Building both from one sorted
        # pass is what keeps them from drifting apart.
        signable = sorted((name.lower(), value.strip()) for name, value in headers.items())
        canonical_headers = "".join(f"{name}:{value}\n" for name, value in signable)
        signed_names = ";".join(name for name, _ in signable)

        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_names, payload_hash]
        )
        scope = f"{date}/{self._region}/{SERVICE}/aws4_request"
        to_sign = "\n".join(
            [
                ALGORITHM,
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        signing_key = _sign(
            _sign(
                _sign(_sign(f"AWS4{self._secret_access_key}".encode(), date), self._region), SERVICE
            ),
            "aws4_request",
        )
        signature = hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"{ALGORITHM} Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        return headers

    def _host(self) -> str:
        _, _, host = self._endpoint.partition("://")
        return host if self._path_style else f"{self._bucket}.{host}"

    def _refuse(self, operation: str, status_code: int, **context: object) -> NoReturn:
        """Turn a store's refusal into this system's error, saying nothing extra.

        What deliberately does not appear: the bucket name, the endpoint, the
        object key, the access key id, and the store's own error body - which
        for a misconfigured gateway can echo request headers back. A caller
        gets `StorageError`, whose message names no infrastructure at all, and
        an operator gets the status code and the operation, which is what
        distinguishes "credentials are wrong" (403) from "the bucket is gone"
        (404) from "the store is unwell" (5xx).
        """
        logger.warning(
            "media.object_store_refused",
            extra={
                "event": "media.object_store_refused",
                "operation": operation,
                "status": status_code,
                **{name: str(value) for name, value in context.items()},
            },
        )
        raise StorageError()


__all__ = ["S3MediaStorage"]
