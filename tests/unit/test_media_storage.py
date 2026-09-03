"""The file store, and the paths it must refuse.

Most of this file is about keys. A key is the only thing standing between a
store and the rest of the host's filesystem, and the values worth testing are
the ones an attacker would send rather than the ones a caller would.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.storage import EXTENSIONS, LocalMediaStorage, StorageError, build_key
from tests.fakes import store_object

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def storage(tmp_path: Path) -> LocalMediaStorage:
    return LocalMediaStorage(tmp_path)


def test_a_key_starts_with_the_workspace() -> None:
    """So everything one workspace owns is under one prefix.

    That is what makes deleting a workspace, or moving one to its own bucket, a
    single operation rather than a scan of everything stored.
    """
    assert build_key(tenant_id=TENANT).startswith(f"{TENANT}/")


def test_a_key_is_generated_not_derived() -> None:
    """Two files never collide, and no input influences where one lands."""
    first = build_key(tenant_id=TENANT, mime_type="image/jpeg")
    second = build_key(tenant_id=TENANT, mime_type="image/jpeg")
    assert first != second


def test_a_known_type_gets_its_extension() -> None:
    assert build_key(tenant_id=TENANT, mime_type="application/pdf").endswith(".pdf")
    assert build_key(tenant_id=TENANT, mime_type="AUDIO/OGG").endswith(".ogg")


def test_an_unknown_type_gets_no_extension_rather_than_a_guess() -> None:
    key = build_key(tenant_id=TENANT, mime_type="application/x-invented")
    assert key.rsplit("/", 1)[-1].count(".") == 0


def test_every_declared_extension_is_lowercase_and_dotted() -> None:
    """The table is matched case-insensitively, so its keys must be lowercase."""
    for mime_type, extension in EXTENSIONS.items():
        assert mime_type == mime_type.lower()
        assert extension.startswith(".")


async def test_what_is_put_can_be_read_back(storage: LocalMediaStorage) -> None:
    key = await store_object(storage, tenant_id=TENANT, data=b"hello", mime_type="text/plain")
    assert await storage.get(key) == b"hello"


async def test_a_file_lands_under_the_root(storage: LocalMediaStorage) -> None:
    key = await store_object(storage, tenant_id=TENANT, data=b"hello")
    assert (storage.root / key).is_file()


async def test_no_partial_file_is_left_behind(storage: LocalMediaStorage) -> None:
    """Writes are staged and renamed, so a reader never sees a half-written file."""
    await store_object(storage, tenant_id=TENANT, data=b"hello", mime_type="image/png")
    leftovers = [path for path in storage.root.rglob("*") if path.name.startswith(".")]
    assert leftovers == []


async def test_deleting_is_idempotent(storage: LocalMediaStorage) -> None:
    key = await store_object(storage, tenant_id=TENANT, data=b"hello")
    await storage.delete(key)
    # Deleting again is not an error: a caller cleaning up after a failure
    # should not have to know how far the failure got.
    await storage.delete(key)
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_reading_a_key_that_was_never_stored_fails_cleanly(
    storage: LocalMediaStorage,
) -> None:
    missing = build_key(tenant_id=TENANT, mime_type="image/png")
    with pytest.raises(StorageError):
        await storage.get(missing)


@pytest.mark.parametrize(
    "key",
    [
        "../../../etc/passwd",
        f"{TENANT}/2026/08/../../../../etc/passwd",
        "/etc/passwd",
        f"{TENANT}/2026/08/../../../secrets.txt",
        "..",
        "",
        f"{TENANT}/2026/08/file.txt\x00.png",
    ],
)
async def test_a_key_that_escapes_the_root_is_refused(
    storage: LocalMediaStorage,
    key: str,
) -> None:
    """The whole point of the store owning its keys.

    A key read back from a database row is still input - whatever wrote it - so
    it is checked on the way out, not only on the way in.
    """
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_an_escaping_key_cannot_be_deleted_either(storage: LocalMediaStorage) -> None:
    with pytest.raises(StorageError):
        await storage.delete("../../../etc/passwd")


async def test_two_workspaces_files_do_not_share_a_prefix(storage: LocalMediaStorage) -> None:
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    mine = await store_object(storage, tenant_id=TENANT, data=b"mine")
    theirs = await store_object(storage, tenant_id=other, data=b"theirs")

    assert mine.split("/")[0] != theirs.split("/")[0]
    assert await storage.get(mine) == b"mine"
    assert await storage.get(theirs) == b"theirs"
