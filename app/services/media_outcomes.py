"""Why a file ended where it did, in a closed vocabulary.

Every terminal state a media row can reach carries one of these reasons, and the
reason decides two things: which of `SKIPPED` or `FAILED` the row becomes, and
the sentence written to `last_error` - which an agent is shown as
`[image, unreadable: ...]` and a colleague sees in the inbox.

**The sentences are Wasla's, always.** They used to be assembled from whatever
was to hand, including a provider-supplied MIME type interpolated without a
bound into a `String(500)` column: a descriptor with a long enough type string
overflowed the column, failed the write, and stranded the conversation
(MEDIA-04). Nothing from a provider, a parser or a customer reaches these texts
now, and every one of them is short.

The reason is also the label a metric is recorded under, so it is a closed set
of short tokens and nothing else - no tenant, no file, no URL (MEDIA-15).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from app.db.models.media import MediaStatus


class MediaReason(StrEnum):
    """Why one attempt at one file stopped."""

    # Decisions about the file: no retry changes them.
    NO_FILE = "no_file"
    OVERSIZE = "oversize"
    UNSUPPORTED_TYPE = "unsupported_type"
    TYPE_MISMATCH = "type_mismatch"
    CAPACITY = "capacity"
    UNAVAILABLE = "unavailable"
    NOTHING_STORED = "nothing_stored"
    UNREADABLE = "unreadable"
    # Decisions about the workspace, not the file (PD-MEDIA-05).
    WORKSPACE_SUSPENDED = "workspace_suspended"
    WORKSPACE_DELETED = "workspace_deleted"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    # Attempts that broke, after the provider client's own bounded retries
    # (PD-MEDIA-08). Terminal all the same: there is no queue-level retry of
    # these, because a second round of the same three attempts minutes later
    # mostly repeats the answer while the customer waits.
    CREDENTIAL_REFUSED = "credential_refused"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    HOST_REFUSED = "host_refused"
    MALFORMED_DESCRIPTOR = "malformed_descriptor"
    RATE_LIMITED = "rate_limited"
    DOWNLOAD_FAILED = "download_failed"
    TIMEOUT = "timeout"
    STORAGE_FAILED = "storage_failed"
    UPLOAD_CONFLICT = "upload_conflict"
    PROVIDER_FAILED = "provider_failed"
    READER_FAILED = "reader_failed"
    # The attempt never finished, and nothing will finish it: its job
    # dead-lettered, or its worker vanished more often than the attempt budget
    # allows (MEDIA-03).
    ABANDONED = "abandoned"


# Which terminal status each reason puts a row in.
SKIPPED_REASONS: Final = frozenset(
    {
        MediaReason.NO_FILE,
        MediaReason.OVERSIZE,
        MediaReason.UNSUPPORTED_TYPE,
        MediaReason.TYPE_MISMATCH,
        MediaReason.CAPACITY,
        MediaReason.UNAVAILABLE,
        MediaReason.NOTHING_STORED,
        MediaReason.UNREADABLE,
        MediaReason.WORKSPACE_SUSPENDED,
        MediaReason.WORKSPACE_DELETED,
        MediaReason.CHANNEL_UNAVAILABLE,
    }
)

# The sentence each reason is recorded with. `UNREADABLE` has none here: a
# reader decision - silence, a scan, a PDF past the parser's limits - carries
# its own fixed sentence from the exception type that expressed it.
REASON_TEXT: Final[dict[MediaReason, str]] = {
    MediaReason.NO_FILE: "This message carries no file to download.",
    MediaReason.OVERSIZE: "This file is larger than the size limit.",
    MediaReason.UNSUPPORTED_TYPE: "Files of this type cannot be read.",
    MediaReason.TYPE_MISMATCH: (
        "This file's contents do not match the type it arrived as, so it was not stored."
    ),
    MediaReason.CAPACITY: (
        "This workspace has used all the file storage its plan allows, so this "
        "attachment was not saved. Delete some attachments or upgrade the plan."
    ),
    MediaReason.UNAVAILABLE: "WhatsApp no longer has this file.",
    MediaReason.NOTHING_STORED: "There is nothing stored to read.",
    MediaReason.WORKSPACE_SUSPENDED: "This workspace is suspended, so the file was not processed.",
    MediaReason.WORKSPACE_DELETED: "This workspace is deleted, so the file was not processed.",
    MediaReason.CHANNEL_UNAVAILABLE: (
        "This number is no longer connected, so the file was not processed."
    ),
    MediaReason.CREDENTIAL_REFUSED: "WhatsApp refused this number's credentials for the file.",
    MediaReason.CREDENTIAL_UNAVAILABLE: "No WhatsApp credential is available to fetch this file.",
    MediaReason.HOST_REFUSED: "WhatsApp could not return this file.",
    MediaReason.MALFORMED_DESCRIPTOR: "WhatsApp returned an unusable description of this file.",
    MediaReason.RATE_LIMITED: "WhatsApp was rate limiting this account when the file was fetched.",
    MediaReason.DOWNLOAD_FAILED: "WhatsApp could not return this file.",
    MediaReason.TIMEOUT: "The file took too long to download.",
    MediaReason.STORAGE_FAILED: "The file store refused the write.",
    MediaReason.UPLOAD_CONFLICT: "This file no longer matches the upload already recorded for it.",
    MediaReason.PROVIDER_FAILED: "The service that reads this kind of file was unavailable.",
    MediaReason.READER_FAILED: "This file could not be read.",
    MediaReason.ABANDONED: "This file could not be processed.",
}

# The longest a recorded reason may be. Far inside `last_error`'s 500, so a
# sentence edited later cannot drift into the column's limit unnoticed: a test
# holds every entry above - and the reader decisions - to it.
MAX_REASON_LENGTH: Final = 200


def status_for(reason: MediaReason) -> MediaStatus:
    return MediaStatus.SKIPPED if reason in SKIPPED_REASONS else MediaStatus.FAILED


def text_for(reason: MediaReason) -> str:
    return REASON_TEXT.get(reason, REASON_TEXT[MediaReason.READER_FAILED])


__all__ = [
    "MAX_REASON_LENGTH",
    "REASON_TEXT",
    "SKIPPED_REASONS",
    "MediaReason",
    "status_for",
    "text_for",
]
