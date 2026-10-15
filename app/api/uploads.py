"""Reading a multipart upload into memory, refused as soon as it passes the cap.

Shared by every route that accepts a file - an attachment sent in a
conversation, a campaign's header file - so the cap and the way it is enforced
are one rule rather than one per route.
"""

from __future__ import annotations

from typing import Final

from fastapi import UploadFile

from app.channels.policy import REQUEST_UPLOAD_CEILING_BYTES
from app.core.exceptions import ValidationError

# Bounded here as well as in the media settings, because this limit protects the
# API process rather than the store: the whole upload is held in memory to be
# handed to the provider, and an unbounded one is a way to exhaust it. The
# provider's own per-type limits are applied before anything is staged.
MAX_UPLOAD_BYTES: Final = REQUEST_UPLOAD_CEILING_BYTES

# How much is read from an upload at a time. Small enough that the check below
# stops an oversized file within a chunk of the limit rather than after it.
UPLOAD_CHUNK_BYTES: Final = 64 * 1024


async def read_capped(file: UploadFile) -> bytes:
    """Read an upload, refusing it as soon as it passes the cap.

    Chunked rather than `await file.read()`, which returns the whole body and
    so learns the size only once the process is already holding it. The request
    middleware caps the body too, but it is one deployment-wide number for every
    route; this is the rule that understands attachments, and the two together
    mean the oversized case is refused twice rather than trusted once.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise ValidationError("This file is too large to send.")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["MAX_UPLOAD_BYTES", "UPLOAD_CHUNK_BYTES", "read_capped"]
