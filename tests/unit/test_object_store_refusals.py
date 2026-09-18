"""The S3 store's answers when it will not answer (MEDIA-18: M29, M30).

M29: a 403 is not "missing". A store refusing a HEAD or a GET - a rotated
credential, a bucket policy - says nothing about whether the object is there,
and reading it as absence would let reconciliation abandon every upload in
flight during the outage (ADR-087). Only a 404 is absence.

M30: nothing the store is authenticated with reaches a log line, on a refusal
or on a success. The request is proven to have been signed with the sentinel
credential before its absence from the logs is asserted.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.core.logging import JsonFormatter
from app.core.object_store import S3MediaStorage
from app.core.storage import StorageError

ACCESS_KEY = "SENTINELACCESSKEY7Q2"
SECRET = "SENTINEL-S3-SECRET-8f41aa"
KEY = "3f2b8c1e-7d4a-4c9b-9e1f-2a6b5c8d0e7f/2026/09/0f8e7d6c-5b4a-4938-8271-605f4e3d2c1b.png"


def _store(handler: httpx.MockTransport) -> S3MediaStorage:
    return S3MediaStorage(
        bucket="wasla-media",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET,
        endpoint_url="http://minio.test:9000",
        http=httpx.AsyncClient(transport=handler),
    )


def _answering(status: int, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=b"" if request.method == "HEAD" else b"body")

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_a_refused_head_is_not_an_absent_object(status: int) -> None:
    with pytest.raises(StorageError):
        await _store(_answering(status)).exists(KEY)


async def test_only_a_404_head_means_the_object_is_absent() -> None:
    assert await _store(_answering(404)).exists(KEY) is False
    assert await _store(_answering(200)).exists(KEY) is True


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_a_refused_get_is_a_storage_error_not_empty_bytes(status: int) -> None:
    with pytest.raises(StorageError):
        await _store(_answering(status)).get(KEY)


async def test_a_refused_delete_is_an_error_and_a_missing_object_is_not() -> None:
    with pytest.raises(StorageError):
        await _store(_answering(403)).delete(KEY)
    await _store(_answering(404)).delete(KEY)
    await _store(_answering(204)).delete(KEY)


async def test_no_store_credential_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []

    await _store(_answering(200, seen)).put_at(key=KEY, data=b"x", mime_type="image/png")
    for status in (403, 500):
        with pytest.raises(StorageError):
            await _store(_answering(status, seen)).get(KEY)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(StorageError):
        await _store(httpx.MockTransport(unreachable)).exists(KEY)

    # The requests really were signed with the sentinel access key.
    assert seen
    assert all(ACCESS_KEY in request.headers.get("Authorization", "") for request in seen)
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert rendered
    assert SECRET not in rendered
    assert ACCESS_KEY not in rendered
    assert "Signature=" not in rendered
