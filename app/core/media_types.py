"""What a file actually is, decided from its bytes rather than from a claim.

Every media type in this system used to arrive as a string somebody else wrote:
`file.content_type` from a browser, `mime_type` from Meta's media descriptor.
Both were believed. A PDF uploaded as `image/jpeg` was sent to a customer as an
image, stored as an image and served back as one, and a claimed `image/svg+xml`
was admitted by an `image/*` wildcard (SEC-09).

The rule this module exists to enforce: **the claim is a hint, the bytes are the
answer.** A claim is used for exactly one thing - choosing between the members
of a container class that genuinely carries more than one type, such as an
ISO-BMFF file that may be audio or video - and it is never used to widen what
was detected.

## What detection does and does not prove

It answers "do these bytes begin as a supported container of a known format?".

It does **not** prove the file is harmless. A valid JPEG can carry an exploit
for somebody's decoder, a valid PDF can carry JavaScript, and a file can be a
polyglot that is a legitimate JPEG and a legitimate ZIP at once. Nothing here
scans for malware, and nothing here should be described as if it did. What it
removes is the class where a file is *processed and served as a type it is
not* - which is the class that turns storage into a delivery mechanism.

The rest of the defence is unchanged and still doing its job: files are served
with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`,
never inline, never from a public URL, and always from the canonical type this
module returned rather than from anything a caller said.

## Why a table rather than libmagic

`python-magic` needs libmagic present in the image, which is a system package
on a distroless-leaning runtime and a second thing to patch. The set of formats
this product actually supports is small and fixed - it is bounded by what the
media reader can read and what WhatsApp will carry - so a table of signatures
for exactly those is smaller than the dependency, has no deployment story, and
can be tested exhaustively, which is what the tests beside this module do.

The cost is honest: an unusual-but-valid file of a supported type whose
signature this table does not recognise is refused rather than admitted. That
is the direction to fail in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.exceptions import ValidationError

# How much of a file is read to identify it. Enough for every signature below,
# including an ISO-BMFF `ftyp` box with a long brand list and a ZIP whose first
# local file header is preceded by padding, and small enough that identifying a
# file never means holding it. Callers that stream may hand over only this much.
SNIFF_BYTES: Final = 4096

# Types a caller may send that mean "I do not know", as opposed to a claim.
# Treated as absence rather than as a conflict: a browser that omits the header
# and one that fills it in with the generic value are saying the same thing.
UNDECLARED_TYPES: Final = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/unknown"}
)


class MediaClass(StrEnum):
    """What kind of thing a file is, once its bytes have been read."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class MediaTypeError(ValidationError):
    """The bytes are not a supported type, or are not the type that was claimed.

    A `ValidationError`, so it becomes a 400 through the existing handler rather
    than a new status. The message says what was refused and never what was
    seen: echoing a header or a byte prefix back to whoever supplied it turns an
    error response into a probe of the detector.
    """

    message = "This file is not a type Wasla can accept."


@dataclass(frozen=True, slots=True)
class DetectedMedia:
    """One file's identity, as read from the file.

    `mime_type` is canonical: it is the spelling this system stores, sends to
    Meta and serves back, whatever the caller wrote.
    """

    mime_type: str
    kind: MediaClass


# The canonical type of every format this product supports, and the class it
# belongs to. Nothing may be stored, sent or served as a type absent from here.
#
# The set is derived from what the system can actually do rather than from what
# a format list contains: images and audio are what `MediaReader` can read,
# video and the office formats are what a colleague may send outward, and
# `application/pdf` and `text/plain` are both.
CANONICAL_TYPES: Final[dict[str, MediaClass]] = {
    "image/jpeg": MediaClass.IMAGE,
    "image/png": MediaClass.IMAGE,
    "image/gif": MediaClass.IMAGE,
    "image/webp": MediaClass.IMAGE,
    "audio/ogg": MediaClass.AUDIO,
    "audio/mpeg": MediaClass.AUDIO,
    "audio/mp4": MediaClass.AUDIO,
    "audio/amr": MediaClass.AUDIO,
    "audio/aac": MediaClass.AUDIO,
    "audio/wav": MediaClass.AUDIO,
    "audio/webm": MediaClass.AUDIO,
    "video/mp4": MediaClass.VIDEO,
    "video/3gpp": MediaClass.VIDEO,
    "video/webm": MediaClass.VIDEO,
    "application/pdf": MediaClass.DOCUMENT,
    "text/plain": MediaClass.DOCUMENT,
    "text/csv": MediaClass.DOCUMENT,
    "application/msword": MediaClass.DOCUMENT,
    "application/vnd.ms-excel": MediaClass.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        MediaClass.DOCUMENT
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": MediaClass.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        MediaClass.DOCUMENT
    ),
}

# ISO base media file format brands, from the `ftyp` box. The brand is what
# separates an MP4 that is a film from one that is a voice note, and a phone
# sends both. An unlisted brand is refused rather than guessed at as video.
AUDIO_BRANDS: Final = frozenset({b"M4A ", b"M4B ", b"M4P ", b"mp41", b"F4A ", b"F4B "})
VIDEO_BRANDS: Final = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp42", b"avc1", b"mmp4", b"MSNV", b"dash"}
)
THREE_GPP_PREFIXES: Final = (b"3gp", b"3g2")

# The part names OOXML puts in the first entries of its ZIP. `[Content_Types]`
# is in every one of them and identifies the family; the directory prefix is
# what separates a document from a spreadsheet from a presentation. Read from a
# bounded prefix, so a crafted archive cannot make identification expensive.
OOXML_MARKERS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (b"ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)

# A legacy Office file is an OLE2 compound document, and Word and Excel share
# that container byte for byte at the front. Telling them apart needs the
# compound-file directory, which is not at a fixed offset - so this returns the
# pair and lets the claim choose between them. The security question is
# "are these bytes a legacy Office document rather than a PDF, a script or an
# executable?", and that is answered; which Office application opens it is not
# a question about safety.
OLE2_TYPES: Final = frozenset({"application/msword", "application/vnd.ms-excel"})

# Matroska carries audio and video in one container with one signature, so the
# same rule applies: detection narrows to the pair and the claim picks.
WEBM_TYPES: Final = frozenset({"audio/webm", "video/webm"})

# Types that have no signature at all, because their content is their format.
# Reached only when nothing above matched and the bytes decode as text.
TEXT_TYPES: Final = frozenset({"text/plain", "text/csv"})

# Bytes that never appear in a plain text file this system should accept. NUL
# rules out every binary format that got this far, and the C0 controls rule out
# the escape sequences that make a "text" file a terminal payload. Tab, newline
# and carriage return are text.
_TEXT_FORBIDDEN: Final = (
    frozenset(range(0x00, 0x09)) | frozenset({0x0B, 0x0C}) | frozenset(range(0x0E, 0x20))
)


def _matches_mp3(prefix: bytes) -> bool:
    """An MP3, by ID3 tag or by a bare frame sync.

    Both are needed. A file from a phone usually carries an ID3 header, and a
    file cut from a stream usually does not - it starts at a frame boundary,
    which is eleven set bits followed by a version and layer that are not the
    reserved values. Checking only for `ID3` would refuse half of what arrives.
    """
    if prefix.startswith(b"ID3"):
        return True
    if len(prefix) < 2 or prefix[0] != 0xFF:
        return False
    second = prefix[1]
    if second & 0xE0 != 0xE0:
        return False
    # Version 01 and layer 00 are both reserved; a file using either is not an
    # MPEG audio frame and matching it would admit arbitrary bytes that happen
    # to begin 0xFF 0xE0.
    return (second >> 3) & 0b11 != 0b01 and (second >> 1) & 0b11 != 0b00


def _matches_aac(prefix: bytes) -> bool:
    """AAC as ADTS frames, or the rarer ADIF header."""
    if prefix.startswith(b"ADIF"):
        return True
    if len(prefix) < 2 or prefix[0] != 0xFF:
        return False
    # ADTS sync is twelve set bits, then a layer field that must be zero. That
    # last requirement is what keeps this from also matching an MP3 frame.
    return prefix[1] & 0xF6 in (0xF0, 0xF8)


def _iso_bmff_type(prefix: bytes) -> frozenset[str] | None:
    """The canonical types of an ISO base media file, read from its brands.

    The major brand sits at offset 8 and the compatible brands follow it, and a
    file that is really a voice note frequently declares a video major brand
    with an audio one alongside. Both are read, audio first, because calling a
    voice note a video would send it down the wrong reader.
    """
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        return None

    # The box length, so the brand list is read from this box and not from
    # whatever follows it. Bounded by what was sniffed either way.
    size = int.from_bytes(prefix[0:4], "big")
    end = min(len(prefix), size if 8 < size <= len(prefix) else len(prefix))
    brands = [prefix[offset : offset + 4] for offset in range(8, max(8, end - 3), 4)]
    if not brands:
        return None

    if any(brand in AUDIO_BRANDS for brand in brands):
        return frozenset({"audio/mp4"})
    if any(brand.startswith(THREE_GPP_PREFIXES) for brand in brands):
        return frozenset({"video/3gpp"})
    if any(brand in VIDEO_BRANDS for brand in brands):
        return frozenset({"video/mp4"})
    return None


def _zip_type(prefix: bytes) -> frozenset[str] | None:
    """Which OOXML document a ZIP is, or nothing if it is a plain archive.

    OOXML writers put `[Content_Types].xml` first and the application's own
    directory immediately after, so the family is identifiable from a bounded
    prefix without inflating anything. A ZIP that is not OOXML is not a
    supported type: this product has no reason to carry an archive, and
    admitting one would be admitting whatever is inside it.
    """
    if b"[Content_Types].xml" not in prefix:
        return None
    for marker, canonical in OOXML_MARKERS:
        if marker in prefix:
            return frozenset({canonical})
    return None


def _looks_like_text(data: bytes) -> bool:
    """Whether these bytes are plausibly a text file.

    Deliberately strict. Anything with a NUL or a C0 control character is not
    text that this product should carry, and anything that does not decode as
    UTF-8 is not text this product can index - the extraction path tries other
    encodings for files it already knows are text, but a *type decision* made
    on a lenient decoder is a decision that accepts every byte sequence.
    """
    if not data:
        return False
    if any(byte in _TEXT_FORBIDDEN for byte in data):
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        # A prefix can end mid-character, which is not evidence of anything.
        # Trimming the last three bytes covers every truncated UTF-8 sequence.
        try:
            data[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def detect(prefix: bytes) -> frozenset[str] | None:
    """The canonical types these bytes could legitimately be, or None.

    A set rather than a single value because two of the supported containers
    genuinely carry more than one type - Matroska is audio or video, and an
    OLE2 compound document is Word or Excel - and pretending otherwise would
    mean either refusing valid files or picking one at random. Everything else
    returns a set of one.

    `prefix` may be the whole file or the first `SNIFF_BYTES` of it.
    """
    if prefix.startswith(b"\xff\xd8\xff"):
        return frozenset({"image/jpeg"})
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return frozenset({"image/png"})
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return frozenset({"image/gif"})
    if prefix.startswith(b"RIFF") and len(prefix) >= 12:
        # RIFF is a wrapper, and the form type at offset 8 is what it wraps.
        # Checking only `RIFF` would call a WAV a WEBP.
        if prefix[8:12] == b"WEBP":
            return frozenset({"image/webp"})
        if prefix[8:12] == b"WAVE":
            return frozenset({"audio/wav"})
        return None
    if prefix.startswith(b"OggS"):
        return frozenset({"audio/ogg"})
    if prefix.startswith(b"#!AMR"):
        return frozenset({"audio/amr"})
    if prefix.startswith(b"%PDF-"):
        return frozenset({"application/pdf"})
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return WEBM_TYPES
    if prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return OLE2_TYPES
    if prefix.startswith(b"PK\x03\x04"):
        return _zip_type(prefix)

    iso = _iso_bmff_type(prefix)
    if iso is not None:
        return iso
    # Both are checked after every signature above, because an MP3 frame sync
    # and an ADTS sync are two bytes and would otherwise shadow longer, more
    # specific signatures that happen to start with 0xFF.
    if _matches_mp3(prefix):
        return frozenset({"audio/mpeg"})
    if _matches_aac(prefix):
        return frozenset({"audio/aac"})
    if _looks_like_text(prefix):
        return TEXT_TYPES
    return None


def resolve(*, claimed: str | None, prefix: bytes) -> DetectedMedia:
    """The canonical type of these bytes, refusing anything unsupported.

    The whole policy, in one place, so no caller has to remember it:

    - Bytes of no supported format are refused, whatever was claimed.
    - A claim that agrees with the bytes is accepted, and the *canonical*
      spelling is returned rather than the caller's.
    - A claim that contradicts the bytes is refused. It is not silently
      corrected: a file whose declared type and content disagree is either a
      broken client or an attempt, and neither is something to quietly relabel
      and carry on with.
    - A claim that is absent or generic is not a conflict. The detected type is
      used, unless the bytes are one of the two ambiguous containers, where
      there is no honest way to choose and the file is refused.
    """
    candidates = detect(prefix)
    if candidates is None:
        raise MediaTypeError("This file is not a type Wasla can accept.")

    normalised = (claimed or "").split(";", 1)[0].strip().lower()
    if normalised in UNDECLARED_TYPES:
        if len(candidates) != 1:
            raise MediaTypeError(
                "This file does not say what type it is, and its contents could "
                "be more than one. Send it with a content type."
            )
        canonical = next(iter(candidates))
        return DetectedMedia(mime_type=canonical, kind=CANONICAL_TYPES[canonical])

    if normalised not in candidates:
        # The message names neither the claim nor what was found. It is read by
        # whoever sent the file, and the useful half - which file, and what the
        # detector concluded - belongs in a log line, not in a response that
        # would otherwise let a caller enumerate the table above.
        raise MediaTypeError("This file's contents do not match the type it was sent as.")

    return DetectedMedia(mime_type=normalised, kind=CANONICAL_TYPES[normalised])


__all__ = [
    "CANONICAL_TYPES",
    "SNIFF_BYTES",
    "DetectedMedia",
    "MediaClass",
    "MediaTypeError",
    "detect",
    "resolve",
]
