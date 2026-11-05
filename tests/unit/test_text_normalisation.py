"""Text normalisation for storage, matching and lexical search (RQ-01, RV-01, RV-23, P-R16)."""

from __future__ import annotations

import random
import unicodedata

import pytest

from app.services.text_normalisation import (
    for_lexical,
    for_matching,
    for_storage,
    normalise_whitespace,
)

# What the Chromium-rendered Arabic PDF's text layer held for "يمكن": the
# presentation forms of yeh, meem, kaf and noon (RAG_SYSTEM_REVIEW.md A.3).
GLYPHS = "\ufef3\ufee4\ufedc\ufee6"
LETTERS = "\u064a\u0645\u0643\u0646"
PHRASE = "\u064a\u0645\u0643\u0646 \u0644\u0644\u0639\u0645\u064a\u0644"  # "can the customer"
INVISIBLE = (
    "\u200b\u200c\u200d\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
ALPHABET = (
    "abc XYZ 0123456789 .,;:!?\n\t"
    + "\u0627\u0628\u062a\u062b\u062c\u062d\u062e\u062f\u0631\u0633\u064a\u0647\u0629\u0649"
    + "\u0623\u0625\u0622\u0640\u064b\u064e\u0650\u0670"
    + "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
    + "\u06f0\u06f4\u06f9\u06cc\u06a9\u06be"
    + GLYPHS
    + INVISIBLE
)


def _samples(count: int = 300) -> list[str]:
    generator = random.Random(20261029)  # noqa: S311 - reproducible samples
    return [
        "".join(generator.choice(ALPHABET) for _ in range(generator.randint(0, 80)))
        for _ in range(count)
    ]


def test_presentation_forms_are_stored_as_the_letters_a_customer_types() -> None:
    assert for_storage(GLYPHS) == LETTERS
    assert not any(
        0xFB50 <= ord(c) <= 0xFDFF or 0xFE70 <= ord(c) <= 0xFEFF for c in for_storage(GLYPHS)
    )


def test_nfkc_alone_does_not_unify_digits_and_matching_does() -> None:
    # The review's verification: NFKC leaves Arabic-Indic digits alone.
    assert unicodedata.normalize("NFKC", "\u0664\u0665\u0660\u0660") != "4500"
    assert for_storage("\u0664\u0665\u0660\u0660") == "\u0664\u0665\u0660\u0660"
    assert for_matching("\u0664\u0665\u0660\u0660") == "4500"
    assert for_matching("\u06f4\u06f5\u06f0\u06f0") == "4500"


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        # Tatweel, diacritics and Arabic-Indic digits in what the customer types.
        ("\u0633\u0640\u0640\u0639\u0631 \u0664\u0665\u0660\u0660", "\u0633\u0639\u0631 4500"),
        ("\u0633\u064e\u0639\u0652\u0631 4500", "\u0633\u0639\u0631 \u0664\u0665\u0660\u0660"),
        # Pasted with invisible bidi controls.
        ("\u202b\u0633\u0639\u0631\u202c 4500\u200f", "\u0633\u0639\u0631 4500"),
        # A PDF font's Farsi yeh and heh for the Arabic letters (RR-13).
        (
            "\u064a\u0645\u0643\u0646 \u062c\u0646\u064a\u0647",
            "\ufef3\ufee4\ufedc\ufee6 \u062c\u0646\ufbfe\ufbab",
        ),
    ],
)
def test_a_typed_query_and_the_stored_passage_match(typed: str, stored: str) -> None:
    assert for_matching(typed) == for_matching(for_storage(stored))


def test_the_review_phrase_is_found_after_matching_normalisation() -> None:
    stored = for_storage("\ufef3\ufee4\ufedc\ufee6 \ufedf\ufee0\ufecc\ufee4\ufef4\ufede")
    assert for_matching(PHRASE) in for_matching(stored)


@pytest.mark.parametrize("function", [for_storage, for_matching, for_lexical, normalise_whitespace])
def test_every_form_is_idempotent(function: object) -> None:
    for sample in _samples():
        once = function(sample)  # type: ignore[operator]
        assert function(once) == once  # type: ignore[operator]


def test_storage_never_changes_ascii_text_except_whitespace() -> None:
    generator = random.Random(7)  # noqa: S311 - reproducible samples
    printable = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:!?-#()\n"
    for _ in range(300):
        sample = "".join(generator.choice(printable) for _ in range(generator.randint(0, 120)))
        assert for_storage(sample) == normalise_whitespace(sample)


def test_storage_removes_every_invisible_control_and_nul() -> None:
    for character in INVISIBLE + "\x00":
        assert for_storage(f"a{character}b") == "ab"


def test_the_digit_map_round_trips_every_digit_in_both_scripts() -> None:
    for value in range(10):
        assert for_matching(chr(0x0660 + value)) == str(value)
        assert for_matching(chr(0x06F0 + value)) == str(value)
    assert for_matching("0123456789") == "0123456789"


def test_matching_keeps_line_structure_for_the_chunker() -> None:
    assert for_matching("a\u0640b\n\nc") == "ab\n\nc"


def test_lexical_folding_is_heavier_and_never_the_matching_form() -> None:
    assert for_lexical(
        "\u0623\u062d\u0645\u062f \u0645\u0643\u062a\u0628\u0629 \u0645\u0633\u062a\u0634\u0641\u0649"  # noqa: E501
    ) == (
        "\u0627\u062d\u0645\u062f \u0645\u0643\u062a\u0628\u0647 \u0645\u0633\u062a\u0634\u0641\u064a"  # noqa: E501
    )
    assert for_matching("\u0623\u062d\u0645\u062f") == "\u0623\u062d\u0645\u062f"
    assert for_lexical("Premium FINISHING") == "premium finishing"
