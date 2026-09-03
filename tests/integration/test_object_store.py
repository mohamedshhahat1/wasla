"""`MediaStorage` has two implementations, and they must behave the same.

An interface with one implementation is a shape; an interface with two is a
contract, and the contract is only real if something checks it. Every test in
the first half runs against **both** backends from one body, so a divergence -
a local store that raises where the object store returns, a key one accepts and
the other refuses - fails here rather than in production on the day a deployment
switches.

The S3 half needs a real store and skips without one, following the same
convention the PostgreSQL tests use:

    TEST_S3_ENDPOINT_URL=http://localhost:9100 \\
    TEST_S3_BUCKET=wasla-media \\
    TEST_S3_ACCESS_KEY_ID=... TEST_S3_SECRET_ACCESS_KEY=... pytest

`docker run -p 9100:9000 minio/minio server /data` is enough to make them run,
and they are worth running: what they prove is that the SigV4 signing in
`object_store.py` is correct, and no amount of mocking a `boto` call proves
that. A signing bug is a 403 from a real store and a green test against a fake
one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.object_store import S3MediaStorage
from app.core.storage import (
    SAFE_KEY,
    LocalMediaStorage,
    MediaStorage,
    StorageError,
    build_media_storage,
)

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

ENDPOINT = "TEST_S3_ENDPOINT_URL"
BUCKET = "TEST_S3_BUCKET"
ACCESS_KEY = "TEST_S3_ACCESS_KEY_ID"
SECRET_KEY = "TEST_S3_SECRET_ACCESS_KEY"


def _s3_or_skip() -> S3MediaStorage:
    endpoint = os.environ.get(ENDPOINT)
    if not endpoint:
        pytest.skip(f"No object store configured; set {ENDPOINT} to run these tests.")
    return S3MediaStorage(
        bucket=os.environ.get(BUCKET, "wasla-media"),
        access_key_id=os.environ.get(ACCESS_KEY, ""),
        secret_access_key=os.environ.get(SECRET_KEY, ""),
        endpoint_url=endpoint,
        path_style=True,
    )


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[MediaStorage]:
    """One body, both backends. A divergence fails on the parameter that has it."""
    if request.param == "local":
        yield LocalMediaStorage(tmp_path)
        return
    yield _s3_or_skip()


# ============================================== the contract, on both backends


async def test_what_is_put_comes_back_byte_for_byte(storage: MediaStorage) -> None:
    key = await storage.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    assert await storage.get(key) == PNG


async def test_a_key_is_tenant_prefixed_and_matches_the_shared_pattern(
    storage: MediaStorage,
) -> None:
    """Both backends produce keys from the same `build_key`.

    That is what lets a deployment move between them: a key written by one is
    a key the other can read, so the migration is copying objects rather than
    rewriting every row that points at one.
    """
    tenant = uuid.uuid4()
    key = await storage.put(tenant_id=tenant, data=PNG, mime_type="image/png")

    assert key.startswith(f"{tenant}/")
    assert SAFE_KEY.match(key)


async def test_a_key_is_never_built_from_the_content_or_a_filename(storage: MediaStorage) -> None:
    """A customer's filename arrives from a stranger's phone. It builds nothing."""
    key = await storage.put(tenant_id=uuid.uuid4(), data=b"invoice.pdf %PDF-1.7", mime_type=None)

    assert "invoice" not in key
    assert "pdf" not in key.rsplit("/", 1)[-1].split(".")[0]


async def test_two_workspaces_never_share_a_prefix(storage: MediaStorage) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()

    one = await storage.put(tenant_id=first, data=PNG, mime_type="image/png")
    two = await storage.put(tenant_id=second, data=PDF, mime_type="application/pdf")

    assert not two.startswith(f"{first}/")
    assert not one.startswith(f"{second}/")


async def test_one_workspaces_key_never_returns_anothers_bytes(storage: MediaStorage) -> None:
    """Key separation is not authorization, and it is still worth asserting."""
    one = await storage.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")
    two = await storage.put(tenant_id=uuid.uuid4(), data=PDF, mime_type="application/pdf")

    assert await storage.get(one) == PNG
    assert await storage.get(two) == PDF


async def test_reading_a_key_that_was_never_stored_fails_cleanly(storage: MediaStorage) -> None:
    absent = f"{uuid.uuid4()}/2026/09/{uuid.uuid4()}.png"

    with pytest.raises(StorageError):
        await storage.get(absent)


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "not a key at all",
        "..%2f..%2fetc%2fpasswd",
        "/absolute/path",
        "tenant/2026/09/file.png\x00.txt",
    ],
)
async def test_a_key_that_is_not_one_we_produced_is_refused(
    storage: MediaStorage, key: str
) -> None:
    """A key read back from a database row is input, whatever wrote it."""
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_deleting_removes_the_object(storage: MediaStorage) -> None:
    key = await storage.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    await storage.delete(key)

    with pytest.raises(StorageError):
        await storage.get(key)


async def test_deleting_is_idempotent(storage: MediaStorage) -> None:
    """The retention sweep retries, and a second delete must be a no-op.

    Without this a partially-failed pass could never be safely re-run: the
    objects it did remove would raise on the retry and stop it reaching the
    ones it did not.
    """
    key = await storage.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    await storage.delete(key)
    await storage.delete(key)
    await storage.delete(f"{uuid.uuid4()}/2026/09/{uuid.uuid4()}.png")


async def test_a_key_that_escapes_cannot_be_deleted_either(storage: MediaStorage) -> None:
    with pytest.raises(StorageError):
        await storage.delete("../../etc/passwd")


async def test_an_empty_file_round_trips(storage: MediaStorage) -> None:
    """Nothing upstream stores one, and a store that could not would be surprising."""
    key = await storage.put(tenant_id=uuid.uuid4(), data=b"", mime_type="text/plain")

    assert await storage.get(key) == b""


async def test_a_large_file_round_trips(storage: MediaStorage) -> None:
    """A megabyte, which is an ordinary voice note and not an ordinary test string."""
    payload = bytes(range(256)) * 4096
    key = await storage.put(tenant_id=uuid.uuid4(), data=payload, mime_type="audio/ogg")

    assert await storage.get(key) == payload


# =========================================================== the object store


async def test_a_fresh_client_reads_what_another_wrote() -> None:
    """The property that makes host loss survivable.

    Nothing about the object lives in the instance that stored it. A different
    client, with its own connection, reads the same bytes - which is exactly
    what a replacement container does after the original host is gone.
    """
    writer = _s3_or_skip()
    key = await writer.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    reader = _s3_or_skip()
    assert await reader.get(key) == PNG

    await writer.delete(key)


async def test_exists_distinguishes_absent_from_unreachable() -> None:
    """The orphan sweep turns on this answer, so it must not guess.

    A store that could not be reached must raise rather than report False: an
    orphan sweep that read an outage as "the object is gone" would delete the
    rows pointing at every object in the bucket.
    """
    store = _s3_or_skip()
    key = await store.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    assert await store.exists(key) is True
    await store.delete(key)
    assert await store.exists(key) is False

    unreachable = S3MediaStorage(
        bucket="wasla-media",
        access_key_id="k",
        secret_access_key="s",
        # A port nothing listens on: a connection failure, not a 404.
        endpoint_url="http://127.0.0.1:9",
        path_style=True,
        timeout_seconds=2.0,
    )
    with pytest.raises(StorageError):
        await unreachable.exists(f"{uuid.uuid4()}/2026/09/{uuid.uuid4()}.png")


async def test_a_rejected_request_says_nothing_about_the_deployment() -> None:
    """A storage error reaches an API response. It must name no infrastructure.

    Bucket, endpoint, key and credential are all things an operator can find in
    a log line, and none of them belong in an error a caller is handed.
    """
    store = _s3_or_skip()
    wrong = S3MediaStorage(
        bucket=os.environ.get(BUCKET, "wasla-media"),
        access_key_id=os.environ.get(ACCESS_KEY, ""),
        secret_access_key="definitely-not-the-secret",
        endpoint_url=os.environ[ENDPOINT],
        path_style=True,
    )

    with pytest.raises(StorageError) as raised:
        await wrong.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")

    message = str(raised.value)
    for secret in (
        "definitely-not-the-secret",
        os.environ[ENDPOINT],
        os.environ.get(BUCKET, "wasla-media"),
        os.environ.get(ACCESS_KEY, "") or "unset-access-key",
    ):
        assert secret not in message

    # The working store still works: the failure above was the credential and
    # not something this test left behind.
    key = await store.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")
    await store.delete(key)


async def test_an_unreachable_store_is_a_storage_error_not_an_httpx_error() -> None:
    """Translated at the boundary, like every other integration here."""
    unreachable = S3MediaStorage(
        bucket="wasla-media",
        access_key_id="k",
        secret_access_key="s",
        endpoint_url="http://127.0.0.1:9",
        path_style=True,
        timeout_seconds=2.0,
    )

    with pytest.raises(StorageError):
        await unreachable.put(tenant_id=uuid.uuid4(), data=PNG, mime_type="image/png")


# ================================================================ the factory


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        **overrides,
    )


def test_the_default_backend_is_local_disk() -> None:
    """A developer who configures nothing gets a store that needs nothing."""
    assert isinstance(build_media_storage(_settings()), LocalMediaStorage)


def test_selecting_s3_builds_the_object_store() -> None:
    storage = build_media_storage(
        _settings(
            media_storage_backend="s3",
            media_s3_bucket="wasla-media",
            media_s3_access_key_id="key",
            media_s3_secret_access_key="secret",
        )
    )
    assert isinstance(storage, S3MediaStorage)


def test_selecting_s3_without_credentials_refuses_to_start() -> None:
    """Fail closed. A silent fall back to local disk would give the API and the
    worker each their own copy on their own container."""
    with pytest.raises(ValueError, match="MEDIA_S3_BUCKET"):
        _settings(media_storage_backend="s3")


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("media_s3_bucket", "MEDIA_S3_BUCKET"),
        ("media_s3_access_key_id", "MEDIA_S3_ACCESS_KEY_ID"),
        ("media_s3_secret_access_key", "MEDIA_S3_SECRET_ACCESS_KEY"),
    ],
)
def test_each_missing_object_store_setting_is_named(missing: str, expected: str) -> None:
    """Each one individually, so the error tells an operator which is absent."""
    complete = {
        "media_s3_bucket": "wasla-media",
        "media_s3_access_key_id": "key",
        "media_s3_secret_access_key": "secret",
    }
    del complete[missing]

    with pytest.raises(ValueError, match=expected):
        _settings(media_storage_backend="s3", **complete)


def test_a_local_deployment_is_asked_for_no_object_store_credential() -> None:
    """The other direction: local must not become half-mandatory by accident."""
    settings = _settings(media_storage_backend="local")

    assert settings.media_s3_bucket is None
    assert settings.media_s3_access_key_id is None
    assert settings.media_s3_secret_access_key is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "not-a-url",
        "ftp://store.example.com",
        "https://store.example.com/wasla",
        "https://store.example.com?bucket=wasla",
    ],
)
def test_an_endpoint_that_cannot_mean_what_it_says_is_refused(endpoint: str) -> None:
    """A path here looks like it scopes the bucket and is silently ignored."""
    with pytest.raises(ValueError, match="MEDIA_S3_ENDPOINT_URL"):
        _settings(
            media_storage_backend="s3",
            media_s3_bucket="wasla-media",
            media_s3_access_key_id="key",
            media_s3_secret_access_key="secret",
            media_s3_endpoint_url=endpoint,
        )


@pytest.mark.parametrize("endpoint", ["http://minio:9000", "https://s3.example.com"])
def test_a_private_endpoint_is_accepted(endpoint: str) -> None:
    """`http://minio:9000` is the correct value in a self-hosted stack.

    The object store is infrastructure on the deployment network, in the same
    class as the database URL - not an integration reached across the internet -
    so the outbound guard's rules deliberately do not apply to it.
    """
    settings = _settings(
        media_storage_backend="s3",
        media_s3_bucket="wasla-media",
        media_s3_access_key_id="key",
        media_s3_secret_access_key="secret",
        media_s3_endpoint_url=endpoint,
    )
    assert settings.media_s3_endpoint_url == endpoint


def _executable_source(module_file: str) -> str:
    """A module's code with its comments and string literals removed.

    Needed because the assertion below is about what the module *does*, and the
    module explains at length why it does not do it - so a plain substring
    search over the file finds the explanation and fails.
    """
    import io
    import tokenize

    with open(module_file, encoding="utf-8") as handle:
        tokens = tokenize.generate_tokens(io.StringIO(handle.read()).readline)
        return " ".join(
            token.string
            for token in tokens
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )


def test_no_public_acl_is_ever_sent() -> None:
    """A public-read object is a link that leaves the workspace and never returns.

    Asserted over the module's code rather than over one request, because the
    property is "this header is never sent on any path" and the way that stops
    being true is somebody adding it to a branch no test happens to exercise.
    """
    import app.core.object_store as module

    code = _executable_source(module.__file__).lower()

    assert "public-read" not in code
    assert "x-amz-acl" not in code
    assert "presign" not in code


def test_no_service_reaches_past_the_storage_adapter() -> None:
    """Every S3 call goes through `MediaStorage`, and nothing else knows S3 exists.

    The failure this prevents is the one SEC-08 was: an integration built by
    hand beside the abstraction that was supposed to own it. A service holding
    its own client would bypass the key validation, the error translation and
    the credential boundary all at once.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "object_store.py"
        and any(
            marker in _executable_source(str(path)).lower()
            for marker in ("aws4-hmac", "x-amz-", "boto3", "botocore")
        )
    ]

    assert offenders == [], f"S3 details leaked outside the storage adapter: {offenders}"
