"""Customer-sent text files decode to what was written (RV-20, FX-1.7, P-R07)."""

from __future__ import annotations

import pytest

from app.services.extraction import extract_text


@pytest.mark.parametrize(
    ("text", "encoding"),
    [
        ("Our refund policy\u2019s simple: 14 days.", "cp1252"),
        ("Livraison gratuite \u00e0 partir de 500 \u20ac, p\u00e9riode d'essai.", "cp1252"),
        ("Caf\u00e9 cr\u00e8me \u2013 \u00bd price", "cp1252"),
        (
            "\u0633\u0639\u0631 \u0627\u0644\u062a\u0634\u0637\u064a\u0628 4500 \u062c\u0646\u064a\u0647",  # noqa: E501
            "cp1256",
        ),
        (
            "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643\u0645 \u0641\u064a \u0645\u062a\u062c\u0631\u0646\u0627\u060c \u0627\u0644\u0634\u062d\u0646 \u0645\u062c\u0627\u0646\u064a",  # noqa: E501
            "cp1256",
        ),
        ("Plain ASCII stays plain.", "ascii"),
        ("UTF-8 \u0633\u0639\u0631 \u2019", "utf-8"),
    ],
)
def test_text_decodes_to_what_was_written(text: str, encoding: str) -> None:
    assert extract_text(text.encode(encoding)) == text


def test_utf16_is_read_only_behind_its_byte_order_mark() -> None:
    text = "\u0633\u0639\u0631 4500 EGP"
    assert extract_text(text.encode("utf-16")) == text  # carries a BOM
    assert extract_text(b"\xef\xbb\xbf" + text.encode("utf-8")) == text


def test_windows_1252_english_is_never_read_as_utf16() -> None:
    decoded = extract_text("Our refund policy\u2019s simple: 14 days.".encode("cp1252"))
    assert not any("\u3000" <= character <= "\u9fff" for character in decoded)
