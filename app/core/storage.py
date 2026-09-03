"""Where uploaded and downloaded files live.

An interface with one implementation, which is normally a smell and is not one
here. The choice of store is a deployment decision that changes per environment
and will change again - local disk on one machine today, object storage the
moment there are two - and the alternative to naming the boundary now is
scattering `open()` calls through services that would then all have to be
rewritten.

Deliberately not in PostgreSQL (claude.md §26). A voice note is a megabyte and a
video is fifteen; putting them in rows makes every backup carry them, and the
database is the one component that cannot be scaled by adding disks.

Keys are opaque and are produced here, never by a caller and never from
anything a customer supplied. `MediaStorage` is a `Protocol` rather than an
abstract base class so a test double satisfies it by shape, without importing
anything from this module.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from app.core.config import Settings
from app.core.exceptions import WaslaError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Mime types mapped to the extension a key ends with. The extension is a
# convenience for anyone looking at the store by hand - nothing reads it back -
# so an unknown type simply gets none rather than a guess.
EXTENSIONS: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/amr": ".amr",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}

# A key is built from a UUID, a date and an extension from the table above, so
# this pattern is a restatement of what this module produces rather than a
# filter over anything a caller passes in. It is checked on the way out anyway:
# a key read back from a database row is input, whatever wrote it.
SAFE_KEY = re.compile(r"^[0-9a-f-]+/\d{4}/\d{2}/[0-9a-f-]+(\.[a-z0-9]{1,8})?$")


class StorageError(WaslaError):
    """A file could not be written, read or removed."""

    status_code = 500
    error_code = "storage_error"
    message = "The file store is unavailable."


class MediaStorage(Protocol):
    """Somewhere bytes can be put and fetched back by key.

    There is deliberately no method that allocates a key and writes in one
    step. A store that minted its own key would be deciding, inside a network
    call, the name of an object PostgreSQL has not yet heard of - and an object
    whose key was never committed cannot be found again after a crash without
    listing the bucket, which is the thing this system will not do (ADR-087).
    The caller allocates with `build_key`, commits that, and then writes here.
    """

    async def put_at(
        self,
        *,
        key: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> None:
        """Store `data` at exactly `key`, which the caller already owns."""
        ...

    async def get(self, key: str) -> bytes:
        """Read back what `put_at` stored. Raises `StorageError` if it is gone."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a stored file. Removing one that is already gone is not an error."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether an object is there.

        Three answers, not two. True and False are what the store said; a store
        that could not be reached, or that refused the question, raises
        `StorageError` instead of answering False. Reconciliation turns on this
        distinction: an outage read as "the object is gone" would abandon every
        upload in flight during it (ADR-087).
        """
        ...


def build_key(*, tenant_id: uuid.UUID, mime_type: str | None = None) -> str:
    """A fresh key for one file: `{tenant}/{year}/{month}/{uuid}{ext}`.

    Two properties matter and neither is aesthetic. The tenant comes first, so
    everything one workspace owns is under one prefix - which is what makes
    deleting a workspace, or moving one to its own bucket, a single operation
    instead of a scan. And the identifier is generated, so a key can never be
    influenced by a filename, a caption or anything else that arrived from a
    stranger's phone.

    The date is there so a directory listing stays a usable size on a filesystem
    that cares.
    """
    now = datetime.now(UTC)
    extension = EXTENSIONS.get((mime_type or "").lower(), "")
    return f"{tenant_id}/{now:%Y}/{now:%m}/{uuid.uuid4()}{extension}"


class LocalMediaStorage:
    """Files on the local filesystem, under a configured root.

    For development and for a single-host deployment. It requires the API and
    the worker to share a volume, because one writes what the other reads, and
    that arrangement stops working the moment there are two hosts - which is the
    point at which the object-store implementation behind this interface is
    written (ADR-023).

    Writes go to a temporary name and are then renamed into place. A rename
    within a directory is atomic, so a reader can never open a file that is
    still being written; without it a worker killed mid-download would leave a
    truncated file that looks perfectly valid.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    async def put_at(
        self,
        *,
        key: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> None:
        destination = self._path(key)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(f".{destination.name}.partial")
            staging.write_bytes(data)
            staging.replace(destination)
        except OSError as error:
            logger.exception("media.store_failed")
            raise StorageError() from error

    async def exists(self, key: str) -> bool:
        """Whether the file is there, distinguishing absent from unreadable.

        `stat` rather than `Path.exists`, which answers False for a permission
        error and for a mount that has gone away - both of which are a store
        this process cannot see rather than a file that is not there, and
        reconciliation acts on them completely differently.
        """
        path = self._path(key)
        try:
            path.stat()
        except FileNotFoundError:
            return False
        except NotADirectoryError:
            # A parent component is a file, so nothing can be at this key.
            return False
        except OSError as error:
            logger.warning("media.head_failed")
            raise StorageError() from error
        return True

    async def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as error:
            # Includes the file simply not being there. A caller holding a key
            # for a file that has been swept has the same problem either way.
            logger.warning("media.read_failed")
            raise StorageError() from error

    async def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            logger.warning("media.delete_failed")
            raise StorageError() from error

    def _path(self, key: str) -> Path:
        """Resolve a key to a path, refusing anything that leaves the root.

        Two checks rather than one. The pattern rejects a key that does not look
        like something `build_key` produced, and the containment check catches
        anything that satisfies the pattern and still escapes - a symlinked
        directory, say. Neither is expensive, and the failure they prevent is
        reading or overwriting a file elsewhere on the host.
        """
        if not SAFE_KEY.match(key):
            raise StorageError()

        resolved = (self._root / key).resolve()
        root = self._root.resolve()
        if not resolved.is_relative_to(root):
            raise StorageError()
        return resolved


def build_media_storage(settings: Settings) -> MediaStorage:
    """The store this deployment is configured for.

    One function, so "which backend is running?" has a single answer that the
    API dependency and the worker both reach for rather than each deciding.
    They used to construct `LocalMediaStorage` separately, which was harmless
    while there was one implementation and is exactly how two processes end up
    writing to different places once there are two.

    The import is deferred because `object_store` imports `Settings` and this
    module is imported by nearly everything; a module-level import here would
    make every consumer of a storage key pull in an HTTP client it does not use.
    """
    if settings.media_storage_backend == "s3":
        from app.core.object_store import S3MediaStorage

        return S3MediaStorage.from_settings(settings)
    return LocalMediaStorage(settings.media_storage_path)
