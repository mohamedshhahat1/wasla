"""SEC-09: the bytes decide the type, and a contradicting claim is refused.

The defect these tests close: `file.content_type` from a browser and `mime_type`
from Meta's media descriptor were both believed without ever opening the file,
and `image/*` was a wildcard that admitted anything beginning with those seven
characters - `image/svg+xml` included. A PDF uploaded as `image/jpeg` was sent
to a customer as an image, stored as one and served back as one.

Samples are **built rather than committed**. A repository holding a real JPEG
and a real MP4 to prove a security control is a repository whose test data is
somebody's photograph, and the constructed files here are exact about the part
that matters - the signature - which is the part under test.

The one thing these tests must not do is assert the table back to itself. Each
case names the format in its own terms (the eight bytes a PNG starts with, the
`ftyp` box an MP4 carries) rather than importing a constant and comparing it to
itself, so a wrong entry in the table fails here instead of agreeing with itself.
"""

from __future__ import annotations

import io
import zipfile
import zlib

import pytest

from app.core.media_types import (
    CANONICAL_TYPES,
    SNIFF_BYTES,
    MediaClass,
    MediaTypeError,
    detect,
    resolve,
)

# --------------------------------------------------------------- real samples


def png() -> bytes:
    """A one-pixel PNG, assembled to the specification rather than pasted."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


def jpeg() -> bytes:
    """A JPEG: SOI, a JFIF APP0 segment, and EOI."""
    return (
        b"\xff\xd8\xff\xe0"
        + (16).to_bytes(2, "big")
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xd9"
    )


def gif() -> bytes:
    return b"GIF89a" + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + b"\x00\x00\x00;"


def webp() -> bytes:
    body = b"VP8 " + (10).to_bytes(4, "little") + b"\x00" * 10
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def wav() -> bytes:
    """A WAV, which is RIFF like WEBP is - and must not be confused with it."""
    fmt = b"fmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00" + b"\x00" * 12
    data = b"data" + (0).to_bytes(4, "little")
    body = fmt + data
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body


def ogg() -> bytes:
    return b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead"


def mp3_with_tag() -> bytes:
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 100


def mp3_bare_frame() -> bytes:
    """A stream cut at a frame boundary, which carries no ID3 tag at all."""
    return b"\xff\xfb\x90\x00" + b"\x00" * 200


def aac_adts() -> bytes:
    return b"\xff\xf1\x50\x80\x00\x1f\xfc" + b"\x00" * 64


def amr() -> bytes:
    return b"#!AMR\n" + b"\x3c" + b"\x00" * 31


def iso_bmff(major: bytes, *compatible: bytes) -> bytes:
    brands = major + b"\x00\x00\x02\x00" + b"".join(compatible)
    box = b"ftyp" + brands
    return (len(box) + 4).to_bytes(4, "big") + box + b"\x00\x00\x00\x08free"


def webm() -> bytes:
    return b"\x1aE\xdf\xa3" + b"\x01\x00\x00\x00\x00\x00\x00\x1f" + b"webm" + b"\x00" * 32


def pdf() -> bytes:
    return (
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def ole2() -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def ooxml(directory: str) -> bytes:
    """A real ZIP laid out the way an Office writer lays one out."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{directory}/document.xml", "<w/>")
    return buffer.getvalue()


def plain_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hello")
    return buffer.getvalue()


HTML = b"<html><head><title>x</title></head><body><script>alert(1)</script></body></html>"
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><script>alert(1)</script></svg>'
)
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 56
DOS_EXECUTABLE = b"MZ\x90\x00\x03" + b"\x00" * 60
RTF = b"{\\rtf1\\ansi\\deff0 hello}"


# -------------------------------------------------- each format is identified

SUPPORTED = [
    pytest.param(jpeg(), "image/jpeg", id="jpeg"),
    pytest.param(png(), "image/png", id="png"),
    pytest.param(gif(), "image/gif", id="gif"),
    pytest.param(webp(), "image/webp", id="webp"),
    pytest.param(wav(), "audio/wav", id="wav"),
    pytest.param(ogg(), "audio/ogg", id="ogg"),
    pytest.param(mp3_with_tag(), "audio/mpeg", id="mp3-id3"),
    pytest.param(mp3_bare_frame(), "audio/mpeg", id="mp3-bare-frame"),
    pytest.param(aac_adts(), "audio/aac", id="aac-adts"),
    pytest.param(amr(), "audio/amr", id="amr"),
    pytest.param(iso_bmff(b"M4A ", b"isom"), "audio/mp4", id="m4a"),
    pytest.param(iso_bmff(b"isom", b"mp42"), "video/mp4", id="mp4"),
    pytest.param(iso_bmff(b"3gp4"), "video/3gpp", id="3gp"),
    pytest.param(pdf(), "application/pdf", id="pdf"),
    pytest.param(b"a plain note\n", "text/plain", id="text"),
]


@pytest.mark.parametrize(("data", "expected"), SUPPORTED)
def test_each_supported_format_is_identified_from_its_bytes(data: bytes, expected: str) -> None:
    candidates = detect(data)
    assert candidates is not None
    assert expected in candidates


@pytest.mark.parametrize(("data", "expected"), SUPPORTED)
def test_an_honest_claim_resolves_to_the_canonical_type(data: bytes, expected: str) -> None:
    assert resolve(claimed=expected, prefix=data).mime_type == expected


def test_every_canonical_type_is_reachable_from_some_byte_sequence() -> None:
    """A type nothing can produce is a type nothing validates.

    Without this the table could grow an entry the detector never returns, and
    the entry would look like support while refusing every real file.
    """
    reachable = {expected for _, expected in [(p.values[0], p.values[1]) for p in SUPPORTED]}
    reachable |= set(detect(webm()) or ())
    reachable |= set(detect(ole2()) or ())
    for directory, kind in (
        ("word", "wordprocessingml.document"),
        ("xl", "spreadsheetml.sheet"),
        ("ppt", "presentationml.presentation"),
    ):
        found = detect(ooxml(directory))
        assert found is not None, directory
        assert any(kind in name for name in found)
        reachable |= found
    reachable.add("text/csv")

    assert set(CANONICAL_TYPES) == reachable


# ------------------------------------------------------------- RIFF ambiguity


def test_a_wav_is_not_mistaken_for_a_webp() -> None:
    """Both are RIFF. Reading only the first four bytes calls one the other."""
    assert detect(wav()) == frozenset({"audio/wav"})
    assert detect(webp()) == frozenset({"image/webp"})


def test_a_riff_container_of_an_unsupported_form_is_refused() -> None:
    assert detect(b"RIFF" + (4).to_bytes(4, "little") + b"AVI ") is None


# ------------------------------------------------------- ISO-BMFF brand rules


def test_a_voice_note_is_audio_even_when_its_major_brand_is_video() -> None:
    """Phones write `isom` on a file whose compatible brands say `M4A `."""
    assert detect(iso_bmff(b"isom", b"M4A ")) == frozenset({"audio/mp4"})


def test_an_iso_container_with_no_recognised_brand_is_refused() -> None:
    assert detect(iso_bmff(b"qt  ")) is None


def test_the_minor_version_field_is_not_read_as_a_brand() -> None:
    """Offset 12 is a version number, and treating it as a brand only widens.

    A file whose major brand is unsupported and whose minor version happens to
    equal the four bytes of a supported one must still be refused: the check
    exists to narrow, and anything that makes it accept more is a bug in the
    direction that matters.
    """
    box = b"ftyp" + b"qt  " + b"isom"  # major `qt  `, minor version spelling `isom`
    crafted = (len(box) + 4).to_bytes(4, "big") + box

    assert detect(crafted) is None
    with pytest.raises(MediaTypeError):
        resolve(claimed="video/mp4", prefix=crafted)


def test_a_truncated_ftyp_box_is_refused() -> None:
    """Not enough bytes to hold a major brand is not enough to identify one."""
    assert detect((16).to_bytes(4, "big") + b"ftyp") is None
    assert detect((16).to_bytes(4, "big") + b"ftypM4A") is None


def test_bytes_that_merely_contain_ftyp_are_not_a_video() -> None:
    """The box is at a fixed offset. Finding the word anywhere proves nothing.

    These particular bytes are printable, so they are text - which is the
    correct answer and emphatically not `video/mp4`.
    """
    assert detect(b"not-a-box" + b"ftyp" + b"isom") == frozenset({"text/plain", "text/csv"})
    with pytest.raises(MediaTypeError):
        resolve(claimed="video/mp4", prefix=b"not-a-box" + b"ftyp" + b"isom")
    assert detect(b"\x00\x00\x00\x18nope" + b"ftypisom") is None


# ------------------------------------------------------------ container pairs


def test_matroska_narrows_to_the_pair_it_genuinely_could_be() -> None:
    assert detect(webm()) == frozenset({"audio/webm", "video/webm"})


def test_a_legacy_office_file_narrows_to_word_or_excel() -> None:
    assert detect(ole2()) == frozenset({"application/msword", "application/vnd.ms-excel"})


def test_the_claim_picks_within_an_ambiguous_container() -> None:
    assert resolve(claimed="audio/webm", prefix=webm()).kind is MediaClass.AUDIO
    assert resolve(claimed="video/webm", prefix=webm()).kind is MediaClass.VIDEO


def test_an_ambiguous_container_with_no_claim_is_refused_rather_than_guessed() -> None:
    with pytest.raises(MediaTypeError):
        resolve(claimed=None, prefix=webm())


def test_an_ambiguous_container_cannot_be_claimed_as_something_outside_the_pair() -> None:
    with pytest.raises(MediaTypeError):
        resolve(claimed="video/mp4", prefix=webm())


# ------------------------------------------------------------- OOXML vs a ZIP


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xl", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("ppt", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_an_office_document_is_identified_by_the_part_it_carries(
    directory: str, expected: str
) -> None:
    assert detect(ooxml(directory)) == frozenset({expected})


def test_a_plain_zip_is_not_a_supported_type() -> None:
    """An archive is whatever is inside it, and this product carries none."""
    assert detect(plain_zip()) is None


def test_a_zip_claimed_as_a_word_document_is_refused() -> None:
    with pytest.raises(MediaTypeError):
        resolve(
            claimed=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            prefix=plain_zip(),
        )


# ------------------------------------------------------- the spoofing classes


@pytest.mark.parametrize(
    ("claimed", "data", "label"),
    [
        ("image/jpeg", pdf(), "pdf-as-jpeg"),
        ("image/png", b"just some words, nothing more", "text-as-png"),
        ("image/png", HTML, "html-as-png"),
        ("image/jpeg", ELF, "elf-as-jpeg"),
        ("image/jpeg", DOS_EXECUTABLE, "exe-as-jpeg"),
        ("image/webp", plain_zip(), "zip-as-webp"),
        ("audio/mpeg", pdf(), "pdf-as-mp3"),
        ("audio/ogg", jpeg(), "jpeg-as-ogg"),
        ("video/mp4", pdf(), "pdf-as-mp4"),
        ("application/pdf", png(), "png-as-pdf"),
        ("application/pdf", RTF, "rtf-as-pdf"),
        ("text/plain", png(), "png-as-text"),
        ("image/gif", webp(), "webp-as-gif"),
        ("audio/wav", webp(), "webp-as-wav"),
    ],
)
def test_bytes_contradicting_the_claim_are_refused(claimed: str, data: bytes, label: str) -> None:
    with pytest.raises(MediaTypeError):
        resolve(claimed=claimed, prefix=data)


def test_the_hard_gate_a_pdf_sent_as_a_photograph() -> None:
    """The exact class SEC-09 is closed against, stated once and by itself.

    `Content-Type: image/jpeg`, `filename: photo.jpg`, body: a valid PDF.
    """
    with pytest.raises(MediaTypeError):
        resolve(claimed="image/jpeg", prefix=pdf())


# --------------------------------------------------------- wildcards are gone


@pytest.mark.parametrize(
    "claimed",
    [
        "image/svg+xml",
        "image/x-icon",
        "image/x-anything-at-all",
        "image/*",
        "audio/x-invented",
        "video/x-invented",
        "text/html",
        "application/javascript",
        "application/x-msdownload",
        "application/zip",
    ],
)
def test_an_unsupported_type_is_refused_whatever_the_bytes_are(claimed: str) -> None:
    """No family prefix authorises anything. Only the exact table does.

    Parametrised over the bytes as well: a claim outside the table must be
    refused when the file is genuinely of some other supported type *and* when
    it is not a supported type at all, because the two used to differ.
    """
    for data in (jpeg(), SVG, HTML, plain_zip()):
        with pytest.raises(MediaTypeError):
            resolve(claimed=claimed, prefix=data)


def test_an_svg_is_no_longer_an_image() -> None:
    """It is text, and admitting it as an image is what the wildcard did."""
    assert detect(SVG) == frozenset({"text/plain", "text/csv"})
    with pytest.raises(MediaTypeError):
        resolve(claimed="image/svg+xml", prefix=SVG)


# ---------------------------------------------------------- a missing header


def test_a_missing_content_type_falls_back_to_what_the_bytes_say() -> None:
    assert resolve(claimed=None, prefix=png()).mime_type == "image/png"


@pytest.mark.parametrize("generic", ["", "application/octet-stream", "binary/octet-stream"])
def test_a_generic_content_type_is_an_absence_rather_than_a_conflict(generic: str) -> None:
    assert resolve(claimed=generic, prefix=pdf()).mime_type == "application/pdf"


def test_a_missing_content_type_does_not_admit_unsupported_bytes() -> None:
    for data in (ELF, DOS_EXECUTABLE, plain_zip()):
        with pytest.raises(MediaTypeError):
            resolve(claimed=None, prefix=data)


# ------------------------------------------------------- the claim is a hint


def test_a_claim_with_parameters_and_odd_case_is_still_matched() -> None:
    assert resolve(claimed="IMAGE/JPEG; charset=binary", prefix=jpeg()).mime_type == "image/jpeg"


def test_the_canonical_spelling_is_returned_not_the_callers() -> None:
    """What gets stored and served is the table's spelling, never the input's."""
    assert resolve(claimed="  Image/Png  ", prefix=png()).mime_type == "image/png"


# ------------------------------------------------------------------ text rules


def test_text_with_a_nul_byte_is_not_text() -> None:
    assert detect(b"looks like text\x00but is not") is None


def test_text_with_terminal_escapes_is_not_text() -> None:
    assert detect(b"\x1b[2J\x1b[3Jwipe the screen") is None


def test_empty_bytes_are_not_any_type() -> None:
    assert detect(b"") is None


def test_arabic_text_is_text() -> None:
    """This product's customers send it, and a latin-only rule would refuse it."""
    assert detect("مرحبا، كيف حالك؟\n".encode()) == frozenset({"text/plain", "text/csv"})


def test_a_prefix_cut_mid_character_is_still_text() -> None:
    """Sniffing reads a bounded prefix, which lands wherever it lands."""
    body = "مرحبا " * 2000
    assert detect(body.encode()[:SNIFF_BYTES]) is not None


def test_a_csv_can_be_claimed_as_one() -> None:
    assert resolve(claimed="text/csv", prefix=b"name,price\nsofa,4500\n").mime_type == "text/csv"


# -------------------------------------------------------------- sniff bounds


def test_identification_needs_only_the_sniff_prefix() -> None:
    """A caller that streams hands over `SNIFF_BYTES` and gets the same answer."""
    for data, expected in [(p.values[0], p.values[1]) for p in SUPPORTED]:
        padded = data + b"\x00" * (SNIFF_BYTES * 4)
        if expected == "text/plain":
            continue  # padding with NULs stops it being text, which is correct
        assert expected in (detect(padded[:SNIFF_BYTES]) or frozenset()), expected


def test_a_signature_hidden_past_the_sniff_window_does_not_count() -> None:
    """Otherwise a file could carry its real signature out of reach."""
    buried = b"\x00" * (SNIFF_BYTES * 2) + png()
    assert detect(buried[:SNIFF_BYTES]) is None


# ---------------------------------------------------------------- error shape


def test_a_refusal_never_echoes_the_bytes_or_the_claim() -> None:
    """An error that quoted either would be a probe of the detector."""
    secret = b"%PDF-1.7 secret-marker-9f2a"
    try:
        resolve(claimed="image/jpeg", prefix=secret)
    except MediaTypeError as error:
        assert "secret-marker" not in str(error)
        assert "image/jpeg" not in str(error)
        assert "%PDF" not in str(error)
    else:  # pragma: no cover - the call above raises
        pytest.fail("a spoofed file was accepted")
